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

`allegedly bundle` does NOT skip weeks already on disk — it opens targets with
File::create_new and PANICS with EEXIST (exit 101) on the first collision, and
its default --after is PLC genesis. Always pass --after set past the newest
bundle present. This script's own --scrape-tail does skip, by design. `bundle` also deliberately stops ~73h short of now, since PLC ops
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
import json
import os
import sys
import time
import urllib.parse
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


def turso_batch(statements: list[tuple[str, list]], tries: int = 5) -> None:
    """Many statements, ONE round trip, NO explicit transaction.

    Retries with backoff. Reads already did; writes did not, and that asymmetry
    cost a run: after 496s building the need-set and 1118s scanning 86.9M ops to
    resolve 986,836 DIDs, ONE read-timeout in the write loop discarded all of
    it (2026-07-28). Every statement here is an idempotent COALESCE UPDATE, so
    replaying a batch is always safe.

    Turso gives a transaction a 5-second window to complete; wrapping a thousand
    UPDATEs in BEGIN/COMMIT reliably exceeds it and the whole batch rolls back.
    Letting each statement autocommit keeps the single round trip, and because
    every UPDATE here is idempotent, a batch that dies halfway leaves resumable
    progress rather than nothing."""
    for attempt in range(tries):
        try:
            return _turso_batch_once(statements)
        except Exception as e:
            if attempt == tries - 1:
                raise
            delay = 2**attempt * 5
            print(
                f"  write batch failed ({type(e).__name__}), retry {attempt + 1}/{tries - 1} in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)


def _turso_batch_once(statements: list[tuple[str, list]]) -> None:
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
    with urllib.request.urlopen(req, timeout=300) as r:
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


def load_need(which: str, page: int) -> dict[str, tuple]:
    """Which DIDs still need handle/pds, via rowid keyset pagination.

    Three details, each worth a measurement:

    1. `COLLATE NOCASE` on the handle predicate. `idx_actors_handle` is declared
       COLLATE NOCASE, so a plain `handle = ''` cannot use it and degrades to a
       table scan. With the collation the plan is
       `SEARCH actors USING INDEX idx_actors_handle (handle=? AND rowid>?)`.
       Measured: 25s+/page without, ~11s per 10k page with.
    2. Keyset on `rowid`, matching services/src/db/sync.zig. Ordering by `did`
       forces a sort of the whole matching set per page; rowid is the index's
       own secondary key, so it is free.
    3. Page size 10k. Measured 585 rows/s at 2k, 905 at 10k, 741 at 25k — the
       round trip dominates below 10k and the response size above it.

    An earlier revision used 100k `did`-ordered pages, timed out, concluded
    "pagination does not work", and switched to streaming GET /dump — which
    starved the ingest write path enough to trip the watchdog ("last ok flush
    637s ago", forced restart, 2026-07-28). The query was wrong, not the
    approach.
    """
    where = {
        "handle": "handle = '' COLLATE NOCASE",
        "pds": "pds = ''",
        "both": "handle = '' COLLATE NOCASE OR pds = ''",
    }[which]
    if which != "handle":
        print(
            f"  NOTE: `{which}` has no supporting index — this scans the table. "
            "Expect hours, not minutes.",
            file=sys.stderr,
        )
    need: dict[str, tuple] = {}
    last = 0
    pages = 0
    t = time.time()
    print(f"loading DIDs needing {which} (rowid keyset, page={page})…", file=sys.stderr)
    while True:
        rows = turso(
            f"SELECT rowid, did FROM actors WHERE ({where}) AND rowid > ?1 "
            "ORDER BY rowid LIMIT ?2",
            [last, page],
        )
        if not rows:
            break
        for rowid, did in rows:
            need[did] = (None, None, None)  # (createdAt, handle, pds)
        last = int(rows[-1][0])
        pages += 1
        if pages % 10 == 0:
            rate = len(need) / max(time.time() - t, 1)
            print(f"  {len(need):,} ({rate:.0f}/s)", file=sys.stderr)
    print(f"need: {len(need):,} DIDs in {time.time()-t:.0f}s", file=sys.stderr)
    return need


def load_stored_handles(page: int) -> dict[str, str]:
    """Every DID we hold a handle for, as did -> handle.

    Same keyset shape as load_need, but it deliberately loads the WHOLE corpus
    rather than a backlog, because the thing being looked for is a handle that
    is present and wrong. ~11M entries costs a few GB of RAM — fine on the home
    box, which peaks far higher during an index build, and this runs weekly.
    """
    out: dict[str, str] = {}
    last = 0
    t = time.time()
    print(f"loading stored handles (rowid keyset, page={page})…", file=sys.stderr)
    while True:
        rows = turso(
            "SELECT rowid, did, handle FROM actors WHERE handle != '' COLLATE NOCASE "
            "AND rowid > ?1 ORDER BY rowid LIMIT ?2",
            [last, page],
        )
        if not rows:
            break
        for rowid, did, handle in rows:
            out[did] = handle
        last = int(rows[-1][0])
        if len(out) % (page * 20) < page:
            print(f"  {len(out):,} ({len(out)/max(time.time()-t,1):.0f}/s)", file=sys.stderr)
    print(f"stored handles: {len(out):,} in {time.time()-t:.0f}s", file=sys.stderr)
    return out


def confirm_handles(dids: list[str], pace_ms: int = 200) -> dict[str, str]:
    """Ask the appview what these DIDs' handles actually are, 25 at a time.

    PLC is used only to FIND candidates, never to write one. alsoKnownAs is a
    claim by the DID; a handle is only real once the domain points back, and the
    appview is what performs that bidirectional check. Writing PLC's claim
    directly would let anyone rename themselves in our index by editing their
    own DID document.
    """
    out: dict[str, str] = {}
    for i in range(0, len(dids), 25):
        batch = dids[i:i + 25]
        qs = "&".join("actors=" + urllib.parse.quote(d) for d in batch)
        try:
            with urllib.request.urlopen(
                "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles?" + qs, timeout=20
            ) as r:
                data = json.load(r)
        except Exception as e:
            print(f"  getProfiles failed ({e}); skipping batch", file=sys.stderr)
            continue
        for p in data.get("profiles", []):
            h = p.get("handle", "")
            if h and h != "handle.invalid":
                out[p["did"]] = h
        if i and i % 2500 == 0:
            print(f"  confirmed {len(out):,}/{i:,}", file=sys.stderr)
        time.sleep(pace_ms / 1000)
    return out


def verify_bundles(dest: str, delete: bool = True, newest: int = 3) -> int:
    """Drop truncated bundles so a later run re-scrapes them.

    `allegedly bundle` streams gzip directly into the final `<WEEK>.jsonl.gz`
    (weekly.rs: File::create_new, no temp+rename), so a killed or crashed run
    leaves a truncated file that is indistinguishable from a complete one by
    name or listing. Worse, --after is computed from the newest bundle present,
    so a truncated newest week would be treated as done and never refetched —
    a permanent silent gap in exactly the recent window we care most about.

    Only the NEWEST few files are checked by default. Truncation can only happen
    to the bundle being written when a run dies, and fully parsing the whole
    cache costs minutes of CPU over ~46GB for no added safety — the first
    version did exactly that and burned 100% CPU before scraping anything.
    Pass newest=0 to check everything.
    """
    bad = 0
    names = sorted(n for n in os.listdir(dest) if n.endswith(".jsonl.gz"))
    if newest:
        names = sorted(names, key=lambda n: os.path.getmtime(os.path.join(dest, n)))[-newest:]
    for name in names:
        if not name.endswith(".jsonl.gz"):
            continue
        path = os.path.join(dest, name)
        try:
            with gzip.open(path, "rt") as f:
                # gzip framing AND value validity, via the same format-agnostic
                # reader — a line-based check calls every allegedly-written
                # bundle corrupt because they carry no newlines.
                for _ in _iter_json_values(f):
                    pass
        except Exception as e:
            bad += 1
            print(f"  TRUNCATED {name}: {type(e).__name__}", file=sys.stderr)
            if delete:
                os.remove(path)
    print(f"bundle check: {len(names)} checked, {bad} truncated{' (removed)' if delete and bad else ''}", file=sys.stderr)
    return bad


def _iso(ts: int) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def scrape_tail(dest: str) -> int:
    """Write weekly bundles for the weeks the public host has not published.

    This is now the PREFERRED tail scraper, not the fallback. `allegedly bundle`
    hung indefinitely on a socket read twice (2026-07-28 dc4cce22 for 3h07m,
    2026-07-29 a8af8f5e for 7.5h) — 0.15% CPU, zero bytes written, same call
    both times. It has no read timeout. This path sets an explicit 120s timeout
    per request with retries, and writes .part + rename so a kill never leaves a
    file that looks complete.
    Output is byte-compatible, so a later `allegedly bundle` skips what is here.

    Stops ~73h short of now for the same reason Allegedly does: PLC ops can be
    invalidated inside that window, so bundling them freezes a non-final value.
    """
    os.makedirs(dest, exist_ok=True)
    cutoff = (int(time.time()) - 73 * 3600) // WEEK * WEEK
    have = {
        int(n.split(".")[0])
        for n in os.listdir(dest)
        if n.endswith(".jsonl.gz") and n.split(".")[0].isdigit()
    }
    todo = [w for w in range(1668643200 // WEEK * WEEK, cutoff, WEEK) if w not in have]
    if not todo:
        print("tail: nothing to scrape", file=sys.stderr)
        return 0
    # plc.directory rate-limits to 500 requests / 5 min, i.e. one per 600ms.
    # allegedly's --upstream-throttle-ms default documents exactly this; match
    # it rather than eat 429s.
    THROTTLE = 0.6
    last_req = 0.0
    print(f"tail: scraping {len(todo)} unpublished week(s) from plc.directory", file=sys.stderr)
    written = 0
    for w in todo:
        end = _iso(w + WEEK)
        path = os.path.join(dest, f"{w}.jsonl.gz")
        tmp = path + ".part"
        n = 0
        cursor = _iso(w)
        # .part + rename: unlike allegedly, never leave a partial file under the
        # real name where a later run would mistake it for a complete week.
        with gzip.open(tmp, "wt") as out:
            while True:
                u = f"https://plc.directory/export?count=1000&after={cursor}"
                req = urllib.request.Request(u, headers={"User-Agent": "typeahead-plc-sync"})
                body = None
                for attempt in range(4):
                    try:
                        wait = THROTTLE - (time.time() - last_req)
                        if wait > 0:
                            time.sleep(wait)
                        last_req = time.time()
                        body = urllib.request.urlopen(req, timeout=120).read().decode()
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        time.sleep(2**attempt * 5)
                lines = [ln for ln in body.strip().split("\n") if ln]
                if not lines:
                    break
                stop = False
                for ln in lines:
                    created = json.loads(ln).get("createdAt", "")
                    if created >= end:
                        stop = True
                        break
                    out.write(ln + "\n")
                    n += 1
                    cursor = created
                if stop or len(lines) < 1000:
                    break
        os.rename(tmp, path)
        written += 1
        print(f"  + {w} ({_iso(w)[:10]}): {n:,} ops", file=sys.stderr)
    return written


def _iter_json_values(fh, chunk=1 << 20):
    """Yield JSON values from a stream of them, JSONL **or** concatenated.

    The two bundle sources disagree on format and the filename lies about it:
      - plc.t3.storage.dev publishes true JSONL (newline per op)
      - `allegedly bundle` writes serde_json::to_string(&op) with NO separator
        (weekly.rs:172), so a whole week is ONE ~950MB "line" of back-to-back
        objects — still named .jsonl.gz

    Line-based reading therefore worked on 149 published bundles and failed on
    all 26 allegedly-written ones ("Extra data: line 1 column 706"). raw_decode
    handles both, and reading in chunks means a 950MB single-line file never
    has to be materialized as one string.
    """
    dec = json.JSONDecoder()
    buf = ""
    while True:
        data = fh.read(chunk)
        if not data:
            break
        buf += data
        idx = 0
        while True:
            while idx < len(buf) and buf[idx].isspace():
                idx += 1
            if idx >= len(buf):
                break
            try:
                obj, end = dec.raw_decode(buf, idx)
            except ValueError:
                break  # object straddles the chunk boundary — wait for more
            yield obj
            idx = end
        buf = buf[idx:]
    idx = 0
    while idx < len(buf):
        while idx < len(buf) and buf[idx].isspace():
            idx += 1
        if idx >= len(buf):
            break
        obj, end = dec.raw_decode(buf, idx)
        yield obj
        idx = end


def iter_ops(stream, local_dir: str | None):
    """Yield PLC op dicts from the bundle cache (or stdin).

    A corrupt bundle is skipped, not fatal: these come from a third-party host
    and a local scraper, and one bad file killed a run that had already spent
    770s building the need-set (2026-07-28). Failures are reported so corruption
    stays visible rather than silently tolerated.
    """
    if local_dir:
        for n in sorted(x for x in os.listdir(local_dir) if x.endswith(".jsonl.gz")):
            try:
                with gzip.open(os.path.join(local_dir, n), "rt") as f:
                    yield from _iter_json_values(f)
            except Exception as e:
                print(f"  skipping unreadable bundle {n}: {type(e).__name__}", file=sys.stderr)
    else:
        yield from _iter_json_values(stream)


def reconcile_handles(args) -> int:
    """Repair handles that are present and WRONG.

    Every other identity path in this system selects on ABSENCE — enrichment
    phase 1 on `handle = \'\'`, this script\'s own --need modes, the same again in
    the plc job. Nothing verifies a handle once we have one, so a rename we miss
    for any reason is permanent: the actor stays searchable only under a name
    they no longer use, and is not findable under the one they do.

    Measured 2026-07-30 by sampling 1,000 actors against getProfiles: 7 of 727
    verifiable handles were wrong, ~1%, so roughly 100k actors corpus-wide. All
    seven were bsky-hosted, so this is not the jetstream gap — it is renames
    that predate the #identity fix (2026-05-11) plus any missed during an
    ingester outage.

    Detection is free: the PLC bundles are already on disk for the fill pass,
    and give an authoritative current handle for every did:plc. Only the
    candidates — thousands, not millions — cost an API call, and only the
    appview\'s answer is ever written.
    """
    stored = load_stored_handles(args.page)
    if not stored:
        return 0

    seen = candidates = 0
    newest: dict[str, tuple[str, str]] = {}   # did -> (createdAt, plc_handle)
    t0 = time.time()
    for op in iter_ops(sys.stdin, args.dest if args.from_dir else None):
        seen += 1
        if seen % 2_000_000 == 0:
            print(f"  scanned {seen:,} ops, {len(newest):,} candidates, {time.time()-t0:.0f}s",
                  file=sys.stderr)
        did = op.get("did")
        ours = stored.get(did)
        if ours is None:
            continue
        aka = ((op.get("operation") or {}).get("alsoKnownAs")) or []
        if not aka or not aka[0].startswith("at://"):
            continue
        plc_handle = aka[0][5:]
        created = op.get("createdAt") or ""
        prev = newest.get(did)
        if plc_handle.lower() == ours.lower():
            # Agrees. Only DISAGREEING dids are kept, so the working set stays
            # proportional to the damage rather than to the corpus — but a later
            # agreeing op has to be able to cancel an earlier disagreeing one,
            # or every actor who has ever renamed looks stale forever and burns
            # an API call proving otherwise.
            if prev is not None and created > prev[0]:
                del newest[did]
            continue
        if prev is None or created > prev[0]:
            newest[did] = (created, plc_handle)

    # a DID whose LATEST op agrees with us is not a candidate, even if an
    # earlier op disagreed — that is just its rename history
    cands = {d: h for d, (_, h) in newest.items() if h.lower() != stored[d].lower()}
    candidates = len(cands)
    print(f"scanned {seen:,} ops in {time.time()-t0:.0f}s; {candidates:,} candidate "
          f"mismatches of {len(stored):,} stored handles "
          f"({candidates/max(len(stored),1)*100:.2f}%)", file=sys.stderr)
    if not candidates:
        return 0

    confirmed = confirm_handles(list(cands), args.pace_ms)
    # write only what the appview confirms AND still differs from what we hold
    writes = {d: h for d, h in confirmed.items()
              if d in stored and h.lower() != stored[d].lower()}
    print(f"appview knew {len(confirmed):,}; {len(writes):,} confirmed stale", file=sys.stderr)

    if not args.apply:
        for d, h in list(writes.items())[:15]:
            print(f"  {d}  {stored[d]}  ->  {h}")
        print("(dry run — pass --apply to write)", file=sys.stderr)
        return 0

    # handle only: this pass knows nothing about pds and must not blank it
    sql = ("UPDATE actors SET handle = j.value ->> \'$.h\', "
           "identity_checked_at = unixepoch(), updated_at = unixepoch() "
           "FROM json_each(?1) AS j WHERE actors.did = j.value ->> \'$.d\'")
    pending, written = [], 0
    for did, handle in writes.items():
        pending.append({"d": did, "h": handle})
        if len(pending) >= args.batch:
            turso_batch([(sql, [json.dumps(pending)])])
            written += len(pending)
            pending = []
            print(f"  written {written:,}/{len(writes):,}", file=sys.stderr)
            time.sleep(args.pace_ms / 1000)
    if pending:
        turso_batch([(sql, [json.dumps(pending)])])
        written += len(pending)
    print(f"repaired {written:,} stale handles", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="./plc-weekly", help="bundle directory")
    ap.add_argument("--fetch", action="store_true", help="download published bundles first")
    ap.add_argument("--scrape-tail", action="store_true",
                    help="bundle the weeks the public host has not published "
                         "(pure-python stand-in for `allegedly bundle`)")
    ap.add_argument("--from-dir", action="store_true", help="read bundles directly instead of stdin")
    ap.add_argument("--verify-bundles", action="store_true",
                    help="gzip-test the bundle cache and remove truncated files")
    ap.add_argument("--bundles-only", action="store_true",
                    help="stop after the bundle stages; skip identity resolution entirely")
    ap.add_argument("--apply", action="store_true", help="write to turso (default: dry run)")
    ap.add_argument("--batch", type=int, default=500,
                    help="statements per request; matches the worker's chunk size")
    ap.add_argument("--reconcile-handles", action="store_true",
                    help="find handles we hold that are WRONG (renames we missed) and "
                         "repair them; detection via PLC, every write confirmed by the appview")
    ap.add_argument("--need", choices=("handle", "pds", "both"), default="handle",
                    help="which backlog to target")
    ap.add_argument("--page", type=int, default=10000,
                    help="rows per keyset page; 905 rows/s measured at 10k")
    ap.add_argument("--pace-ms", type=int, default=500,
                    help="sleep between write transactions; Turso is single-writer "
                         "and the live ingester shares it")
    args = ap.parse_args()

    if args.fetch:
        fetch_bundles(args.dest)
    if args.verify_bundles:
        verify_bundles(args.dest)
    if args.scrape_tail:
        scrape_tail(args.dest)

    # Bundle stages are separable so an orchestrator can run them as their own
    # retryable steps without paying for the identity phase each time.
    if args.bundles_only:
        return 0

    # Checked here rather than at first use: the bundle stages above need no
    # credentials, so an unset TURSO_URL would otherwise surface as a urllib
    # traceback after a multi-hour download.
    if not TURSO_URL or not TURSO_TOKEN:
        sys.exit("TURSO_URL / TURSO_AUTH_TOKEN not set (source .env)")

    if args.reconcile_handles:
        return reconcile_handles(args)

    # Only track DIDs we actually need. The whole network is ~23M DIDs; holding
    # every one would cost several GB for no benefit. Restricting to rows that
    # are missing a field keeps the working set proportional to the backlog.
    need = load_need(args.need, args.page)
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

    # ONE statement per batch, not one per row.
    #
    # Writing 500 separate UPDATEs per request measured 6.1 rows/s -> a 44-hour
    # job. Each row still fires the actors_au trigger (handle changes => FTS5
    # rewrite), which is irreducible, but the per-statement protocol and
    # planning overhead was paid 500 times per request for no reason.
    # json_each turns the batch into a single statement over a JSON array.
    #
    # COALESCE(NULLIF(...)) so an empty value from PLC never clobbers something
    # getProfiles or the firehose already supplied.
    sql = (
        "UPDATE actors SET "
        "handle = COALESCE(NULLIF(j.value ->> '$.h', ''), handle), "
        "pds = COALESCE(NULLIF(j.value ->> '$.p', ''), pds), "
        "identity_checked_at = unixepoch(), updated_at = unixepoch() "
        "FROM json_each(?1) AS j WHERE actors.did = j.value ->> '$.d'"
    )

    pending: list[dict] = []
    written = 0
    slow_batches = 0
    t_write = time.time()
    for did, (_, handle, pds) in resolved.items():
        pending.append({"d": did, "h": handle or "", "p": pds or ""})
        if len(pending) >= args.batch:
            t_batch = time.time()
            turso_batch([(sql, [json.dumps(pending)])])
            elapsed = time.time() - t_batch
            written += len(pending)
            pending = []
            # ADAPTIVE yield. A fixed 50ms sleep was not backpressure: on
            # 2026-07-28 this job saturated Turso's single writer, the live
            # ingester's flushes began timing out, and its 5s retry of an
            # oversized batch turned that into a self-sustaining wedge —
            # ~25 minutes with no writes reaching the corpus.
            #
            # Slow batches mean the writer is contended, so back off in
            # proportion rather than at a constant rate. Fast batches keep the
            # floor pace and the job still finishes in a sane time.
            # Batches are INHERENTLY slow here: every row changes `handle`,
            # which fires the actors_au trigger and rewrites FTS5, so ~20s per
            # 500 rows is real work, not contention. The first version treated
            # any batch over 1s as contention and added a 10s pause — measured
            # 6.1 rows/s, a 44-hour job, roughly a third of it idle.
            #
            # Each batch is its own request, so the writer is already free
            # between them; a small floor is enough politeness. Only genuinely
            # anomalous batches (>30s) mean someone else is fighting us.
            pace = args.pace_ms / 1000
            if elapsed > 30.0:
                pace = max(pace, min(elapsed / 2, 5.0))
                slow_batches += 1
                if slow_batches % 20 == 1:
                    print(
                        f"  writer contended ({elapsed:.1f}s/batch) — backing off to {pace:.1f}s",
                        file=sys.stderr,
                    )
            if pace:
                time.sleep(pace)
            if written % 100_000 == 0:
                rate = written / max(time.time() - t_write, 1)
                print(f"  wrote {written:,} ({rate:.0f}/s)", file=sys.stderr)
    if pending:
        turso_batch([(sql, [json.dumps(pending)])])
        written += len(pending)
    print(
        f"applied {written:,} identity updates in {time.time()-t_write:.0f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
