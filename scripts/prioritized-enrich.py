#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
prioritized enrichment dry-run: simulate what we'd enrich if we walked
  A. your direct follows
  B. each indie PDS we encounter (with a constellation engagement guard)
  C. your followers (via constellation backlinks)

prints a structured report — no writes. iterate on the guard thresholds
until the picked PDSs look right, then add --apply.

usage:
    ./scripts/prioritized-enrich.py                          # default handle
    ./scripts/prioritized-enrich.py --handle alice.bsky.social
    ./scripts/prioritized-enrich.py --sample 20 --min-engaged 0.5
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

BSKY = "https://public.api.bsky.app/xrpc"
SLINGSHOT = "https://slingshot.microcosm.blue/xrpc"
CONSTELLATION = "https://constellation.microcosm.blue/xrpc"

# mega-PDS hosts to defer to background bulk-enrich (substring match on the PDS
# URL). these have thousands–millions of accounts and don't belong in phase B.
MEGA_PDS_MARKERS = ("bsky.network", "brid.gy", "blacksky.community")

# generic guard: if listRepos count exceeds this, treat as mega and defer.
DEFAULT_MEGA_THRESHOLD = 5000

# dual spam guard (per codex review):
#   pass if (engaged_ratio >= MIN_ENGAGED) OR (sum_followers >= MIN_SUM_FOLLOWERS)
# the ratio catches "every account has a follower" PDSs; the sum catches
# "one or two real high-follower accounts + the rest dormant" PDSs that
# the ratio would otherwise reject.
DEFAULT_SUM_FOLLOWERS = 100

# kill switch on total writes for --apply. dry-run reports the cap separately.
DEFAULT_MAX_TARGETS = 5000

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def http_get_json(url: str, timeout: float = 15.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "typeahead (zzstoatzz.io)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", "replace")[:200]}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"_net_error": str(e)}


def spread_sample(items: list, n: int) -> list:
    """Deterministic evenly-spaced sample of `n` items across `items`.
    Avoids the order-bias of taking the first N from listRepos (codex)."""
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def turso_query(sql: str, args: list | None = None) -> list[list]:
    """Run a single SELECT against Turso via the v2/pipeline HTTP API.
    Returns the raw rows (list of [{type, value}, ...]).
    Requires TURSO_URL + TURSO_AUTH_TOKEN in env (pull from .env if needed)."""
    url = os.environ.get("TURSO_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url or not token:
        return []
    host = url.replace("libsql://", "https://").rstrip("/")
    body = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [{"type": "text", "value": a} for a in (args or [])]}},
            {"type": "close"},
        ]
    }
    req = urllib.request.Request(
        f"{host}/v2/pipeline",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["results"][0]["response"]["result"]["rows"]
    except Exception:
        return []


def resolve_did(handle: str) -> dict | None:
    """slingshot resolveMiniDoc → {did, handle, pds, signing_key} (or error dict)."""
    q = urllib.parse.urlencode({"identifier": handle})
    return http_get_json(f"{SLINGSHOT}/blue.microcosm.identity.resolveMiniDoc?{q}")


def fetch_follows(actor_did: str) -> list[dict]:
    """bsky getFollows, fully paginated. returns list of {did, handle, pds?}."""
    out: list[dict] = []
    cursor = None
    while True:
        params = {"actor": actor_did, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = http_get_json(f"{BSKY}/app.bsky.graph.getFollows?{urllib.parse.urlencode(params)}")
        if not data or "follows" not in data:
            break
        out.extend(data["follows"])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def pds_for_did(did: str) -> str | None:
    """slingshot lookup for a single DID's PDS."""
    doc = resolve_did(did)
    if not doc or doc.get("_http_error") or doc.get("_net_error"):
        return None
    return doc.get("pds")


def is_mega_pds(pds_url: str) -> bool:
    return any(m in pds_url for m in MEGA_PDS_MARKERS)


def pds_host(pds_url: str) -> str:
    """strip scheme for display."""
    return pds_url.replace("https://", "").replace("http://", "").rstrip("/")


def list_repos(pds_url: str, max_collect: int = 5000) -> list[str]:
    """enumerate DIDs hosted on a PDS via com.atproto.sync.listRepos. stops
    early at max_collect to avoid pulling all of a mega-PDS just to count."""
    dids: list[str] = []
    cursor = None
    while len(dids) < max_collect:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        url = f"{pds_url.rstrip('/')}/xrpc/com.atproto.sync.listRepos?{urllib.parse.urlencode(params)}"
        data = http_get_json(url, timeout=20.0)
        if not data or "repos" not in data:
            break
        dids.extend(r["did"] for r in data["repos"] if "did" in r)
        cursor = data.get("cursor")
        if not cursor:
            break
    return dids


def follower_count(did: str) -> int:
    """constellation: how many DIDs follow this one (the global index)."""
    q = urllib.parse.urlencode({
        "subject": did,
        "source": "app.bsky.graph.follow:subject",
    })
    data = http_get_json(f"{CONSTELLATION}/blue.microcosm.links.getBacklinksCount?{q}")
    if not data or "_http_error" in data or "_net_error" in data:
        return -1
    return int(data.get("total") or 0)


def engagement_sample(dids: list[str], sample_size: int, threads: int = 10) -> tuple[int, int, int]:
    """Deterministic spread sample of `sample_size` DIDs across the PDS, then
    constellation follower count for each. Returns
    `(engaged_count, sum_followers, sampled_count)`. Spread (not first-N)
    avoids ordering bias in listRepos (codex)."""
    pool = spread_sample(dids, sample_size)
    engaged = 0
    sampled = 0
    total = 0
    with ThreadPoolExecutor(max_workers=threads) as ex:
        for f in as_completed(ex.submit(follower_count, d) for d in pool):
            n = f.result()
            if n >= 0:
                sampled += 1
                total += n
                if n >= 1:
                    engaged += 1
    return engaged, total, sampled


def constellation_follower_dids(did: str) -> list[str]:
    """phase C: every DID that follows `did` per the global index. Paginates
    constellation `getBacklinks` (limit 1000)."""
    out: list[str] = []
    cursor = None
    while True:
        params = {
            "subject": did,
            "source": "app.bsky.graph.follow:subject",
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        data = http_get_json(
            f"{CONSTELLATION}/blue.microcosm.links.getBacklinks?{urllib.parse.urlencode(params)}"
        )
        if not data or "records" not in data:
            break
        out.extend(r["did"] for r in data["records"] if "did" in r)
        cursor = data.get("cursor")
        if not cursor:
            break
    # dedup: a record per follow, same did shouldn't repeat but be safe.
    return list(dict.fromkeys(out))


def phase_a_follows(handle: str) -> tuple[str, list[dict]]:
    print(f"{BOLD}PHASE A — direct follows of {handle}{RESET}")
    doc = resolve_did(handle)
    if not doc or "did" not in doc:
        print(f"  {RED}error resolving handle{RESET}: {doc}")
        sys.exit(1)
    did = doc["did"]
    print(f"  {DIM}resolved {handle} → {did}{RESET}")
    follows = fetch_follows(did)
    print(f"  total follows: {BOLD}{len(follows)}{RESET}")
    return did, follows


def phase_a_pds_histogram(follows: list[dict]) -> dict[str, list[dict]]:
    """group follows by PDS. resolves DIDs lacking a `pds` field via slingshot
    in parallel (bsky's getFollows usually doesn't include pds)."""
    needs_resolve = [f for f in follows if not f.get("pds")]
    if needs_resolve:
        print(f"  {DIM}resolving PDS for {len(needs_resolve)} follows via slingshot…{RESET}")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(pds_for_did, f["did"]): f for f in needs_resolve}
            for fut in as_completed(futs):
                f = futs[fut]
                pds = fut.result()
                if pds:
                    f["pds"] = pds

    by_pds: dict[str, list[dict]] = {}
    for f in follows:
        pds = f.get("pds") or "(unknown)"
        by_pds.setdefault(pds, []).append(f)
    return by_pds


def print_pds_histogram(by_pds: dict[str, list[dict]]) -> None:
    print(f"  {BOLD}per PDS:{RESET}")
    rows = sorted(by_pds.items(), key=lambda kv: -len(kv[1]))
    for pds, items in rows:
        host = pds_host(pds) if pds != "(unknown)" else "(unknown)"
        tag = f"{YELLOW}MEGA → defer{RESET}" if is_mega_pds(pds) else f"{GREEN}indie{RESET}"
        print(f"    {len(items):>5}  {host:<48} [{tag}]")


def phase_b_indie_pds_walk(
    by_pds: dict[str, list[dict]],
    sample_size: int,
    min_engaged_ratio: float,
    min_sum_followers: int,
    mega_threshold: int,
) -> dict:
    """Walk each non-mega indie PDS. Dual spam guard: ENRICH if
    (engaged_ratio >= MIN_ENGAGED) OR (sum_followers >= MIN_SUM_FOLLOWERS).
    Returns {decisions: [...], enrich_dids: [...]} — the DID list is the union
    of every repo on every ENRICHed PDS, for the A∪B∪C dedup."""
    print()
    print(f"{BOLD}PHASE B — indie PDS hop (engagement sanity check){RESET}")
    candidates = [(p, items) for p, items in by_pds.items() if p != "(unknown)" and not is_mega_pds(p)]
    print(f"  {len(candidates)} non-mega PDS(s) to evaluate")
    print(f"  guard: engaged_ratio >= {min_engaged_ratio:.0%} OR sum_followers >= {min_sum_followers}")
    decisions = []
    enrich_dids: list[str] = []
    for pds, items in candidates:
        host = pds_host(pds)
        repos = list_repos(pds, max_collect=mega_threshold + 1)
        if len(repos) > mega_threshold:
            decisions.append({"pds": pds, "host": host, "decision": "DEFER", "count": len(repos)})
            print(f"  {YELLOW}DEFER{RESET}  {host:<48} listRepos > {mega_threshold} (got {len(repos)}+) → bulk")
            continue
        if not repos:
            decisions.append({"pds": pds, "host": host, "decision": "SKIP-empty", "count": 0})
            print(f"  {DIM}SKIP {RESET}  {host:<48} listRepos failed/empty")
            continue
        engaged, sum_followers, sampled = engagement_sample(repos, sample_size)
        ratio = engaged / sampled if sampled else 0
        pass_ratio = ratio >= min_engaged_ratio
        pass_sum = sum_followers >= min_sum_followers
        ok = pass_ratio or pass_sum
        reason = "ratio" if pass_ratio else ("sum" if pass_sum else "neither")
        verdict = f"{GREEN}ENRICH{RESET}" if ok else f"{RED}SKIP  {RESET}"
        decisions.append({
            "pds": pds, "host": host,
            "decision": "ENRICH" if ok else "SKIP",
            "count": len(repos),
            "engaged": engaged, "sampled": sampled, "ratio": ratio,
            "sum_followers": sum_followers, "pass_by": reason,
        })
        print(f"  {verdict} {host:<48} repos={len(repos):<5} {engaged}/{sampled} engaged ({ratio:.0%}) Σ={sum_followers:<5} [{reason}]")
        if ok:
            enrich_dids.extend(repos)
    return {"decisions": decisions, "enrich_dids": enrich_dids}


def phase_c_followers(did: str) -> list[str]:
    print()
    print(f"{BOLD}PHASE C — your followers (constellation backlinks){RESET}")
    dids = constellation_follower_dids(did)
    print(f"  total followers indexed: {BOLD}{len(dids)}{RESET}")
    return dids


def already_enriched_dids(candidate_dids: list[str]) -> set[str]:
    """Query Turso for the subset of `candidate_dids` that already have
    quality_score > 0 (i.e., already enriched). Returns a set for fast diff.
    Skipped silently if TURSO_* env not set — the dedup section will note it."""
    if not candidate_dids:
        return set()
    if not os.environ.get("TURSO_URL") or not os.environ.get("TURSO_AUTH_TOKEN"):
        return set()
    # chunk to stay well under SQLite's bound-param limit (32766).
    found: set[str] = set()
    CHUNK = 5000
    for i in range(0, len(candidate_dids), CHUNK):
        batch = candidate_dids[i:i + CHUNK]
        placeholders = ",".join("?" * len(batch))
        sql = f"SELECT did FROM actors WHERE quality_score > 0 AND did IN ({placeholders})"
        rows = turso_query(sql, batch)
        for row in rows:
            # row is list of {"type": "text", "value": "did:plc:..."} per pipeline shape
            did = row[0].get("value") if row else None
            if did:
                found.add(did)
    return found


def print_targets_breakdown(
    follows: list[dict],
    b_report: dict,
    follower_dids: list[str],
    max_targets: int,
) -> dict:
    a_dids = [f["did"] for f in follows]
    b_dids = b_report["enrich_dids"]
    c_dids = follower_dids
    raw_total = len(a_dids) + len(b_dids) + len(c_dids)

    union = list(dict.fromkeys(a_dids + b_dids + c_dids))  # preserves A→B→C priority
    enriched = already_enriched_dids(union)
    net = [d for d in union if d not in enriched]
    capped = net[:max_targets]

    print()
    print(f"{BOLD}ESTIMATES{RESET}")
    print(f"  raw candidates:           {raw_total}  (A={len(a_dids)} B={len(b_dids)} C={len(c_dids)})")
    print(f"  unique DIDs:              {len(union)}  ({raw_total - len(union)} dup)")
    if not os.environ.get("TURSO_URL"):
        print(f"  already-enriched:         {DIM}skipped (TURSO_URL/TURSO_AUTH_TOKEN not set){RESET}")
    else:
        print(f"  already qs>0 (skip):      {len(enriched)}")
        print(f"  net needing enrichment:   {len(net)}")
    print(f"  capped at --max-targets:  {len(capped)}  ({BOLD}{max_targets}{RESET} max)")
    print(f"  bsky getProfiles calls (batch 25): ~{(len(capped) + 24) // 25}")
    print(f"  wall-clock estimate (5 concurrent / 200 ms pacing): ~{max(1, (len(capped) // 25) // 5)} min")
    return {"union": union, "net": net, "capped": capped, "enriched": enriched}


def apply_enrichment(capped_dids: list[str], typeahead_url: str, admin_secret: str, batch_size: int = 100) -> dict:
    """POST each batch of DIDs to /admin/enrich-profiles. The endpoint owns
    bsky fetch + extractProfileFields + resolveHidden + write — we just send
    DIDs. Sequential by default; the endpoint already runs its 25-chunks
    concurrently internally so the bottleneck is bsky's pacing, not us.
    Prints first/last/every-5th batch progress + final totals."""
    print()
    print(f"{BOLD}--apply: enriching {len(capped_dids)} DIDs via /admin/enrich-profiles{RESET}")
    totals = {"requested": 0, "profiles_found": 0, "written": 0, "hidden": 0, "errors": 0}
    error_reasons: Counter = Counter()
    n_batches = (len(capped_dids) + batch_size - 1) // batch_size

    for i in range(0, len(capped_dids), batch_size):
        batch = capped_dids[i:i + batch_size]
        b_idx = i // batch_size + 1
        body = json.dumps({"dids": batch}).encode("utf-8")
        req = urllib.request.Request(
            f"{typeahead_url.rstrip('/')}/admin/enrich-profiles",
            data=body,
            headers={
                "Authorization": f"Bearer {admin_secret}",
                "Content-Type": "application/json",
                "User-Agent": "typeahead-enrich-apply/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90.0) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"  {RED}batch {b_idx}/{n_batches}: HTTP {e.code}{RESET} — {e.read().decode('utf-8', 'replace')[:200]}")
            totals["errors"] += len(batch)
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  {RED}batch {b_idx}/{n_batches}: network{RESET} — {e}")
            totals["errors"] += len(batch)
            continue

        totals["requested"] += int(resp.get("requested", 0))
        totals["profiles_found"] += int(resp.get("profiles_found", 0))
        totals["written"] += int(resp.get("written", 0))
        totals["hidden"] += int(resp.get("hidden", 0))
        for err in resp.get("errors", []) or []:
            error_reasons[err.get("reason", "?")] += 1
            totals["errors"] += 1

        if b_idx == 1 or b_idx == n_batches or b_idx % 5 == 0:
            print(f"  batch {b_idx}/{n_batches}: written={resp.get('written',0)}/{resp.get('requested',0)} "
                  f"hidden={resp.get('hidden',0)} errors={len(resp.get('errors') or [])}")

    print()
    print(f"{BOLD}--apply totals{RESET}")
    print(f"  requested:      {totals['requested']}")
    print(f"  profiles_found: {totals['profiles_found']}")
    print(f"  written:        {totals['written']}")
    print(f"  hidden:         {totals['hidden']}")
    if error_reasons:
        breakdown = ", ".join(f"{k}={v}" for k, v in error_reasons.most_common())
        print(f"  errors:         {totals['errors']}  ({breakdown})")
    else:
        print(f"  errors:         {totals['errors']}")
    return totals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default="zzstoatzz.io")
    ap.add_argument("--sample", type=int, default=10,
                    help="DIDs to spread-sample per indie PDS for engagement guard")
    ap.add_argument("--min-engaged", type=float, default=0.10,
                    help="dual guard: pass if engaged_ratio >= this OR sum-followers passes (default 0.10)")
    ap.add_argument("--min-sum-followers", type=int, default=DEFAULT_SUM_FOLLOWERS,
                    help=f"dual guard: pass if sample sum_followers >= this (default {DEFAULT_SUM_FOLLOWERS})")
    ap.add_argument("--mega-threshold", type=int, default=DEFAULT_MEGA_THRESHOLD,
                    help="listRepos count above which a PDS is treated as mega and deferred")
    ap.add_argument("--max-targets", type=int, default=DEFAULT_MAX_TARGETS,
                    help=f"kill switch: cap on total writes for --apply (default {DEFAULT_MAX_TARGETS})")
    ap.add_argument("--skip-b", action="store_true", help="skip the indie-PDS hop entirely")
    ap.add_argument("--apply", action="store_true",
                    help="actually enrich (requires TYPEAHEAD_URL + TYPEAHEAD_SECRET in env; --yes required)")
    ap.add_argument("--yes", action="store_true",
                    help="bypass interactive confirmation for --apply (required in non-tty contexts)")
    args = ap.parse_args()

    t0 = time.time()
    did, follows = phase_a_follows(args.handle)
    by_pds = phase_a_pds_histogram(follows)
    print_pds_histogram(by_pds)

    b_report = {"decisions": [], "enrich_dids": []}
    if not args.skip_b:
        b_report = phase_b_indie_pds_walk(
            by_pds, args.sample, args.min_engaged, args.min_sum_followers, args.mega_threshold,
        )

    follower_dids = phase_c_followers(did)
    breakdown = print_targets_breakdown(follows, b_report, follower_dids, args.max_targets)

    if args.apply:
        url = os.environ.get("TYPEAHEAD_URL")
        secret = os.environ.get("TYPEAHEAD_SECRET")
        if not url or not secret:
            print()
            print(f"{RED}--apply requires TYPEAHEAD_URL + TYPEAHEAD_SECRET in env. aborting.{RESET}")
            sys.exit(1)
        n = len(breakdown["capped"])
        if n == 0:
            print()
            print(f"{DIM}nothing to enrich (net=0).{RESET}")
            return
        if not args.yes:
            if not sys.stdin.isatty():
                print()
                print(f"{RED}--apply in non-tty requires --yes. aborting.{RESET}")
                sys.exit(1)
            print()
            resp = input(f"{BOLD}about to enrich {n} DIDs at {url}/admin/enrich-profiles. type 'YES' to proceed: {RESET}").strip()
            if resp != "YES":
                print("aborted.")
                return
        apply_enrichment(breakdown["capped"], url, secret)
        print()
        print(f"{DIM}elapsed: {time.time() - t0:.1f}s{RESET}")
    else:
        print()
        print(f"{DIM}elapsed: {time.time() - t0:.1f}s — dry-run, no writes{RESET}")


if __name__ == "__main__":
    main()
