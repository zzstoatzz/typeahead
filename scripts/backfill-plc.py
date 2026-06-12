#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
backfill actors from the PLC directory export for a time window where the
jetstream ingester was down (e.g. the 2026-05-29 → 2026-06-12 outage).

streams https://plc.directory/export and forwards every did:plc op in the
window through the worker's real ingest paths:
  - active ops      → /admin/ingest as {did, handle} upserts (COALESCE-safe;
                      maintains actor deltas, actors_total, enrich enqueue)
  - plc_tombstone   → /admin/delete
ops are posted in chronological order, so an actor's latest handle wins.
checkpoints the export cursor after every flush; --resume re-walks from the
checkpoint (overlap is idempotent). known gap: did:web actors never appear
in PLC and need manual indexing.

new accounts land with empty profiles; run scripts/bulk-enrich.py afterwards
to hydrate display names / avatars via getProfiles.

usage:
    source .env  # ADMIN_SECRET
    ./scripts/backfill-plc.py --after 2026-05-29T00:00:00Z --until 2026-06-12T21:30:00Z
    ./scripts/backfill-plc.py ... --dry-run        # walk + count, no writes
    ./scripts/backfill-plc.py ... --resume         # continue from state file
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PLC_EXPORT = "https://plc.directory/export"
PAGE_SIZE = 1000
WORKER_URL = os.environ.get("TYPEAHEAD_URL", "https://typeahead.waow.tech")
INGEST_BATCH = 100  # events per /admin/ingest POST (full upserts — keep Turso locks short)
INGEST_PAUSE = 0.25  # seconds between ingest POSTs
EXPORT_PAUSE = 0.25  # seconds between export pages (politeness + rate limits)
STATE_FILE = "scripts/.backfill-plc-state.json"

DIM = "\033[2m"
RESET = "\033[0m"


def _urlopen_with_retry(req, *, timeout=30, attempts=6):
    delay = 1.0
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = float(e.headers.get("Retry-After") or delay)
                time.sleep(max(retry_after, delay))
            elif e.code >= 500:
                time.sleep(delay)
            else:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError(f"giving up after {attempts} attempts: {req.full_url}")


def export_page(after: str) -> list[dict]:
    qs = urllib.parse.urlencode({"count": PAGE_SIZE, "after": after})
    req = urllib.request.Request(f"{PLC_EXPORT}?{qs}", headers={"Accept": "application/jsonl"})
    raw = _urlopen_with_retry(req).decode()
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def handle_from_op(op: dict) -> str | None:
    operation = op.get("operation") or {}
    if operation.get("type") == "create":  # legacy genesis format
        h = operation.get("handle")
        return h if h else None
    for entry in operation.get("alsoKnownAs") or []:
        if entry.startswith("at://"):
            return entry[len("at://"):]
    return None


def post_admin(path: str, payload: dict, secret: str) -> dict:
    req = urllib.request.Request(
        f"{WORKER_URL}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    return json.loads(_urlopen_with_retry(req, timeout=60))


class Sink:
    """buffers events/deletes and flushes through the worker admin API.
    no cursor field is ever sent — the jetstream KV cursor stays untouched."""

    def __init__(self, secret: str, dry_run: bool):
        self.secret = secret
        self.dry_run = dry_run
        self.events: list[dict] = []
        self.deletes: list[str] = []
        self.sent = self.tombstoned = 0

    def add(self, did: str, handle: str | None) -> None:
        self.events.append({"did": did, **({"handle": handle} if handle else {})})
        if len(self.events) >= INGEST_BATCH:
            self.flush_events()

    def add_delete(self, did: str) -> None:
        self.deletes.append(did)
        if len(self.deletes) >= INGEST_BATCH:
            self.flush_deletes()

    def flush_events(self) -> None:
        if not self.events:
            return
        if not self.dry_run:
            resp = post_admin("/admin/ingest", {"events": self.events}, self.secret)
            if not resp.get("ok"):
                raise RuntimeError(f"ingest rejected: {resp}")
            time.sleep(INGEST_PAUSE)
        self.sent += len(self.events)
        self.events.clear()

    def flush_deletes(self) -> None:
        if not self.deletes:
            return
        if not self.dry_run:
            post_admin("/admin/delete", {"dids": self.deletes}, self.secret)
            time.sleep(INGEST_PAUSE)
        self.tombstoned += len(self.deletes)
        self.deletes.clear()

    def flush(self) -> None:
        self.flush_events()
        self.flush_deletes()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", required=True, help="window start, ISO 8601")
    ap.add_argument("--until", required=True, help="window end, ISO 8601")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true", help=f"continue from {STATE_FILE}")
    args = ap.parse_args()

    secret = os.environ.get("ADMIN_SECRET", "")
    if not secret and not args.dry_run:
        print("error: ADMIN_SECRET not set (source .env)", file=sys.stderr)
        sys.exit(1)

    cursor = args.after
    if args.resume and os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            cursor = json.load(f)["cursor"]
        print(f"resuming from {cursor}")

    sink = Sink(secret, args.dry_run)
    pages = ops_seen = 0
    done = False
    while not done:
        page = export_page(cursor)
        if not page:
            break
        pages += 1
        for op in page:
            created = op.get("createdAt", "")
            if created > args.until:
                done = True
                break
            cursor = created
            did = op.get("did", "")
            if not did.startswith("did:plc:"):
                continue
            ops_seen += 1
            operation = op.get("operation") or {}
            if operation.get("type") == "plc_tombstone":
                sink.add_delete(did)
            else:
                sink.add(did, handle_from_op(op))
        sink.flush()
        with open(STATE_FILE, "w") as f:
            json.dump({"cursor": cursor}, f)
        if pages % 25 == 0:
            print(f"{DIM}{pages} pages | {ops_seen} ops | {sink.sent} ingested | "
                  f"{sink.tombstoned} tombstoned | at {cursor}{RESET}")
        time.sleep(EXPORT_PAUSE)

    sink.flush()
    print(f"done: {ops_seen} ops across {pages} pages → "
          f"{sink.sent} ingested, {sink.tombstoned} tombstoned")
    if args.dry_run:
        print("(dry run — nothing written)")
    else:
        print("next: scripts/bulk-enrich.py to hydrate profiles "
              "(avatar_url='' targets the new rows)")


if __name__ == "__main__":
    main()
