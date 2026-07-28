#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Resolve DID -> (handle, pds) in bulk from the PLC log.

WHY THIS EXISTS
---------------
The enrichment cron used to resolve identity one HTTP request per DID. On
2026-07-28 that left 1,311,817 rows without a handle and 9,315,512 without a
pds, draining at 500/run — 109 years and 212 years respectively. Those are not
backlogs, they are permanent conditions. Batching phase 1 through getProfiles
(25/call) fixes ~96% of the handle case, but getProfiles only knows actors the
appview indexed; the residual is exactly the self-hosted-PDS accounts we care
most about, and it does not return pds at all.

The PLC log is the authoritative source for both fields, and fig's Allegedly
(https://tangled.org/@microcosm.blue/Allegedly, the tool behind plc.wtf) makes
it bulk-readable: weekly gzipped bundles instead of paginated HTTP.

    allegedly backfill --dir ./weekly | plc-identity-sync.py --apply

BUNDLE SOURCES
--------------
Allegedly's default remote is https://plc.t3.storage.dev/plc.directory/, which
serves `<WEEK>.jsonl.gz` files (WEEK = unix ts floor'd to a 604800 multiple).
Measured 2026-07-28: 149 of 193 weeks present, 27.96 GB gzipped, complete from
2022-11-17 but ENDING 2025-09-18. The missing tail is not a gap in history —
it is simply not published yet. Fill it locally before backfilling:

    allegedly bundle --dest ./weekly --after 2025-09-18T00:00:00Z

`bundle` skips weeks already on disk (without --clobber), so seeding ./weekly
from the published host first means only the tail is scraped from
plc.directory. `bundle` also deliberately stops ~73h short of now, since PLC ops
can still be invalidated inside that window; the firehose #identity events the
ingester consumes cover the remainder.

ORDERING
--------
`allegedly backfill` output is explicitly UNORDERED (its own docs pipe it to
"ops-unordered.jsonl"). Current state therefore cannot be taken from the last
line seen for a DID — this keeps the op with the greatest `createdAt` per DID.
Getting that wrong silently resurrects stale handles, which is worse than
leaving the field empty.
"""

import argparse
import gzip
import io
import json
import os
import sys
import time
import urllib.request

TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
BUNDLE_HOST = "https://plc.t3.storage.dev/plc.directory/"
WEEK = 604800


def turso(sql: str, args: list | None = None, tries: int = 4) -> list[list]:
    """Read with backoff. Turso is a shared single-writer instance and these
    scans compete with live ingest; a transient read timeout is normal and must
    not abort a multi-hour job."""
    for attempt in range(tries):
        try:
            return _turso_once(sql, args)
        except Exception as e:
            if attempt == tries - 1:
                raise
            delay = 2 ** attempt * 5
            print(f"  turso read failed ({type(e).__name__}), retry in {delay}s", file=sys.stderr)
            time.sleep(delay)
    return []


def _turso_once(sql: str, args: list | None = None) -> list[list]:
    if not TURSO_URL:
        sys.exit("TURSO_URL not set (source .env)")
    url = TURSO_URL.replace("libsql://", "https://").rstrip("/")
    stmt: dict = {"sql": sql}
    if args:
        stmt["args"] = [
            {"type": "integer", "value": str(a)} if isinstance(a, int)
            else {"type": "text", "value": str(a)}
            for a in args
        ]
    body = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}
    req = urllib.request.Request(
        url + "/v2/pipeline",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        payload = json.load(r)
    res = payload["results"][0]
    if res.get("type") == "error":
        raise RuntimeError(res.get("error"))
    return [[c.get("value") for c in row] for row in res["response"]["result"]["rows"]]


def turso_batch(statements: list[tuple[str, list]]) -> None:
    """Many statements, ONE round trip, NO explicit transaction.

    Turso gives a transaction a 5-second window to complete; wrapping a thousand
    UPDATEs in BEGIN/COMMIT reliably exceeds it and the whole batch rolls back.
    Letting each statement autocommit keeps the single round trip, and because
    every UPDATE here is idempotent, a batch that dies halfway leaves resumable
    progress rather than nothing."""
    url = TURSO_URL.replace("libsql://", "https://").rstrip("/")
    reqs = []
    for sql, args in statements:
        reqs.append({
            "type": "execute",
            "stmt": {"sql": sql, "args": [{"type": "text", "value": str(a)} for a in args]},
        })
    reqs.append({"type": "close"})
    req = urllib.request.Request(
        url + "/v2/pipeline",
        data=json.dumps({"requests": reqs}).encode(),
        headers={"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        payload = json.load(r)
    for res in payload.get("results", []):
        if res.get("type") == "error":
            raise RuntimeError(res.get("error"))


def fetch_bundles(dest: str) -> None:
    """Seed `dest` from the published bundle host. Only downloads weeks that are
    absent locally, so this is resumable and safe to re-run."""
    os.makedirs(dest, exist_ok=True)
    start = 1668643200 // WEEK * WEEK  # 2022-11-17, PLC genesis week
    now = int(time.time()) // WEEK * WEEK
    got = skipped = missing = 0
    total_bytes = 0
    for w in range(start, now + WEEK, WEEK):
        path = os.path.join(dest, f"{w}.jsonl.gz")
        if os.path.exists(path):
            skipped += 1
            continue
        try:
            req = urllib.request.Request(
                f"{BUNDLE_HOST}{w}.jsonl.gz", headers={"User-Agent": "typeahead-plc-sync"}
            )
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.rename(tmp, path)  # never leave a torn bundle looking complete
            got += 1
            total_bytes += len(data)
            print(f"  + {w} ({len(data)/1e6:.1f} MB)", file=sys.stderr)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                missing += 1
                continue
            raise
    print(
        f"bundles: {got} downloaded ({total_bytes/1e9:.2f} GB), {skipped} already present, "
        f"{missing} not published",
        file=sys.stderr,
    )
    if missing:
        print(
            "  NOTE: unpublished weeks are the recent tail. Fill them with:\n"
            f"    allegedly bundle --dest {dest}",
            file=sys.stderr,
        )


def load_need(which: str) -> dict[str, tuple]:
    """Which DIDs still need handle/pds, read from Turso's `GET /dump`.

    Paginating this over /v2/pipeline does not work at this size: 100k-row pages
    time out against the live instance, and smaller pages mean hundreds of
    round trips competing with the ingester for the same single writer. /dump is
    ONE streaming request for the whole database (~6.7 MB/s measured), so the
    cost is linear, restartable, and does not hold a read transaction open per
    page.

    Parsed line-wise rather than buffered — the dump is GBs and we only need one
    column triple per row.
    """
    url = TURSO_URL.replace("libsql://", "https://").rstrip("/")
    req = urllib.request.Request(url + "/dump", headers={"Authorization": f"Bearer {TURSO_TOKEN}"})
    need: dict[str, tuple] = {}
    seen = 0
    cols: list[str] = []
    pending_ddl: list[str] | None = None
    i_did = i_handle = i_pds = -1
    t = time.time()
    print(f"streaming turso dump to find DIDs needing {which}…", file=sys.stderr)
    with urllib.request.urlopen(req, timeout=3600) as r:
        for raw in io.TextIOWrapper(r, encoding="utf-8", errors="replace"):
            if pending_ddl is not None:
                pending_ddl.append(raw)
                if raw.rstrip().endswith(";"):
                    ddl = "".join(pending_ddl)
                    pending_ddl = None
                    cols = _parse_columns(ddl)
                    i_did, i_handle = cols.index("did"), cols.index("handle")
                    i_pds = cols.index("pds")
                    print(f"  schema: {len(cols)} cols, pds at {i_pds}", file=sys.stderr)
                continue
            if not cols and raw.startswith("CREATE TABLE IF NOT EXISTS actors"):
                # the dump wraps DDL across lines, so buffer to the terminating ";"
                pending_ddl = [raw]
                if not raw.rstrip().endswith(";"):
                    continue
                # Column ORDER is read from the dump, never hardcoded: `pds` was
                # added by a later ALTER and sits at index 6 today, but a future
                # migration would silently shift it and we would start reading
                # the wrong column into a field we then write back.
                cols = _parse_columns("".join(pending_ddl or [raw]))
                pending_ddl = None
                i_did, i_handle = cols.index("did"), cols.index("handle")
                i_pds = cols.index("pds")
                print(f"  schema: {len(cols)} cols, pds at {i_pds}", file=sys.stderr)
                continue
            if not raw.startswith("INSERT INTO actors VALUES("):
                continue
            if i_did < 0:
                raise RuntimeError("actors INSERTs appeared before its CREATE TABLE")
            seen += 1
            body = raw[len("INSERT INTO actors VALUES("):]
            parts = _split_sql_values(body)
            if len(parts) <= i_handle:
                continue
            did, handle = parts[i_did], parts[i_handle]
            pds = parts[i_pds] if len(parts) > i_pds else ""
            want = (which in ("handle", "both") and not handle) or (
                which in ("pds", "both") and not pds
            )
            if want:
                need[did] = (None, None, None)  # (createdAt, handle, pds)
            if seen % 1_000_000 == 0:
                print(f"  scanned {seen:,} rows, {len(need):,} need work", file=sys.stderr)
    print(f"dump scan: {seen:,} actor rows in {time.time()-t:.0f}s", file=sys.stderr)
    return need


def _parse_columns(create_sql: str) -> list[str]:
    """Column names in declaration order from a CREATE TABLE line."""
    # Strip line comments FIRST, per line. Doing it per comma-segment loses any
    # column that follows a commented line: `avatar_url ... -- ... ` runs to the
    # newline and swallows `updated_at`, which silently shifts every later index
    # and would have made this read `hidden` as `pds`.
    create_sql = "\n".join(ln.split("--")[0] for ln in create_sql.splitlines())
    inner = create_sql[create_sql.index("(") + 1:]
    cols, depth, cur = [], 0, []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            cols.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    cols.append("".join(cur))
    out = []
    for c in cols:
        c = c.strip()
        if not c:
            continue
        name = c.split()[0].strip('"`[]')
        if name.upper() in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
            continue
        out.append(name)
    return out


def _split_sql_values(body: str) -> list[str]:
    """Split a SQLite dump VALUES(...) tuple, honoring '' escapes. Values we care
    about are all TEXT; anything else is returned raw and ignored by callers."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        if body[i] == "'":
            i += 1
            buf = []
            while i < n:
                if body[i] == "'":
                    if i + 1 < n and body[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(body[i])
                i += 1
            out.append("".join(buf))
        elif body[i] in ",)":
            if body[i] == ")":
                break
            i += 1
        else:
            j = i
            while j < n and body[j] not in ",)":
                j += 1
            out.append(body[i:j].strip())
            i = j
    return out


def iter_ops(stream, local_dir: str | None):
    """Yield PLC op dicts, either from stdin (allegedly backfill) or by reading
    the gzipped bundles directly — the latter so this script is useful before
    anyone installs a Rust toolchain."""
    if local_dir:
        names = sorted(n for n in os.listdir(local_dir) if n.endswith(".jsonl.gz"))
        for n in names:
            with gzip.open(os.path.join(local_dir, n), "rt") as f:
                for line in f:
                    if line.strip():
                        yield json.loads(line)
    else:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="./plc-weekly", help="bundle directory")
    ap.add_argument("--fetch", action="store_true", help="download published bundles first")
    ap.add_argument("--from-dir", action="store_true", help="read bundles directly instead of stdin")
    ap.add_argument("--apply", action="store_true", help="write to turso (default: dry run)")
    ap.add_argument("--batch", type=int, default=1000, help="updates per turso transaction")
    ap.add_argument("--need", choices=("handle", "pds", "both"), default="both",
                    help="which backlog to target")
    ap.add_argument("--page", type=int, default=10000,
                    help="rows per keyset page; 100k timed out against live turso")
    ap.add_argument("--pace-ms", type=int, default=50,
                    help="sleep between write transactions; Turso is single-writer "
                         "and the live ingester shares it")
    args = ap.parse_args()

    if args.fetch:
        fetch_bundles(args.dest)

    # Only track DIDs we actually need. The whole network is ~23M DIDs; holding
    # every one would cost several GB for no benefit. Restricting to rows that
    # are missing a field keeps the working set proportional to the backlog.
    need = load_need(args.need)
    print(f"need: {len(need):,} DIDs", file=sys.stderr)
    if not need:
        return 0

    seen = matched = 0
    t0 = time.time()
    for op in iter_ops(sys.stdin, args.dest if args.from_dir else None):
        seen += 1
        if seen % 2_000_000 == 0:
            print(f"  scanned {seen:,} ops, {matched:,} matched, {time.time()-t0:.0f}s", file=sys.stderr)
        did = op.get("did")
        cur = need.get(did)
        if cur is None:
            continue
        created = op.get("createdAt") or ""
        if cur[0] is not None and created <= cur[0]:
            continue  # backfill output is unordered — keep the newest op only
        o = op.get("operation") or {}
        aka = o.get("alsoKnownAs") or []
        handle = aka[0][5:] if aka and aka[0].startswith("at://") else None
        pds = ((o.get("services") or {}).get("atproto_pds") or {}).get("endpoint")
        need[did] = (created, handle, pds)
        matched += 1

    resolved = {d: v for d, v in need.items() if v[0] is not None and (v[1] or v[2])}
    print(
        f"scanned {seen:,} ops in {time.time()-t0:.0f}s; resolved {len(resolved):,} "
        f"of {len(need):,} needed",
        file=sys.stderr,
    )
    if not args.apply:
        for d, v in list(resolved.items())[:10]:
            print(f"  {d} handle={v[1]} pds={v[2]}")
        print("(dry run — pass --apply to write)", file=sys.stderr)
        return 0

    # COALESCE(NULLIF(...)) so a NULL/empty from PLC never clobbers a good value
    # that getProfiles or the firehose already supplied.
    sql = (
        "UPDATE actors SET handle = COALESCE(NULLIF(?2,''), handle), "
        "pds = COALESCE(NULLIF(?3,''), pds), identity_checked_at = unixepoch(), "
        "updated_at = unixepoch() WHERE did = ?1"
    )
    pending: list[tuple[str, list]] = []
    written = 0
    t_write = time.time()
    for did, (_, handle, pds) in resolved.items():
        pending.append((sql, [did, handle or "", pds or ""]))
        if len(pending) >= args.batch:
            turso_batch(pending)
            written += len(pending)
            pending = []
            # yield the writer. without this a multi-million-row backfill
            # monopolizes the single writer and stalls live ingest.
            if args.pace_ms:
                time.sleep(args.pace_ms / 1000)
            if written % 100_000 == 0:
                rate = written / max(time.time() - t_write, 1)
                print(f"  wrote {written:,} ({rate:.0f}/s)", file=sys.stderr)
    if pending:
        turso_batch(pending)
        written += len(pending)
    print(
        f"applied {written:,} identity updates in {time.time()-t_write:.0f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
