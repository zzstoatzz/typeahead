#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
targeted backfill: enrich a focused set of actors via getProfiles + upsert
to turso, including ranking columns (followers_count, follows_count,
posts_count, last_activity_at).

two modes:
  default        — walks atprotocol.dev/atmosphereconf.org graph
  --benchmark    — uses bsky typeahead for the canonical ranking corpus,
                   so `just bench-ranking` has signal to score against

usage:
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/targeted-backfill.py
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/targeted-backfill.py --benchmark
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

BSKY = "https://public.api.bsky.app/xrpc"
WRITE_BATCH = 50
WRITE_PAUSE = 0.2

# Mirrors RANKING_CORPUS in scripts/bench.py. Kept here as a separate constant
# because importing across the scripts/ directory adds path-hackery without
# any real upside — both lists exist to lock in regressions; if they drift,
# the benchmark surface area shrinks accordingly.
BENCHMARK_QUERIES = [
    "fig", "fig (", "fig aka", "phil",
    "zzstoatzz", "zzstoatzz.io",
    "nate", "alex", "bob", "aoc", "car",
]

def env(k):
    v = os.environ.get(k, "")
    if not v: print(f"error: {k} not set", file=sys.stderr); sys.exit(1)
    return v

def turso_batch_write(stmts, url, token):
    reqs = [{"type": "execute", "stmt": s} for s in stmts]
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode()
    req = urllib.request.Request(f"{url}/v3/pipeline", data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            json.loads(resp.read())
            return True
    except Exception as e:
        print(f"  turso write failed: {e}", file=sys.stderr)
        return False

def fetch_all(endpoint, actor, key):
    """page through a list endpoint, return all items"""
    items = []
    cursor = None
    while True:
        url = f"{BSKY}/{endpoint}?actor={actor}&limit=100"
        if cursor: url += f"&cursor={cursor}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        batch = data.get(key, [])
        items.extend(batch)
        cursor = data.get("cursor")
        print(f"  fetched {len(items)} {key}...", end="\r")
        if not cursor or not batch:
            break
        time.sleep(0.1)
    print()
    return items

def fetch_profiles(dids):
    params = "&".join(f"actors={urllib.request.quote(d)}" for d in dids)
    req = urllib.request.Request(
        f"{BSKY}/app.bsky.actor.getProfiles?{params}",
        headers={"User-Agent": "typeahead-enrich/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("profiles", [])
    except urllib.error.HTTPError as e:
        if e.code == 429: return "rate_limited"
        return []
    except Exception:
        return []

def extract_avatar_cid(url):
    if not url: return ""
    m = re.search(r'/([^/]+?)(?:@[a-z]+)?$', url)
    return m.group(1) if m else ""

def clean_associated(assoc):
    if not assoc or not isinstance(assoc, dict): return "{}"
    clean = {k: v for k, v in assoc.items() if v not in (0, False, None)}
    return json.dumps(clean) if clean else "{}"

def profile_to_stmt(p):
    hide_vals = {"!hide", "!takedown", "!suspend", "spam"}
    mod_did = "did:plc:ar7c4by46qjdydhdevvrndac"
    hidden = 0
    for lbl in (p.get("labels") or []):
        if lbl.get("val", "") in hide_vals or (lbl.get("val") == "!no-unauthenticated" and lbl.get("src") == mod_did):
            hidden = 1; break
    return {
        "sql": """INSERT INTO actors (did, handle, display_name, avatar_url, labels, hidden, created_at, associated, followers_count, follows_count, posts_count, updated_at)
          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, unixepoch())
          ON CONFLICT(did) DO UPDATE SET
            handle = COALESCE(NULLIF(?2, ''), actors.handle),
            display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
            avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
            labels = ?5, hidden = ?6,
            created_at = COALESCE(NULLIF(?7, ''), actors.created_at),
            associated = COALESCE(NULLIF(?8, '{}'), actors.associated),
            followers_count = COALESCE(NULLIF(?9, 0), actors.followers_count),
            follows_count = COALESCE(NULLIF(?10, 0), actors.follows_count),
            posts_count = COALESCE(NULLIF(?11, 0), actors.posts_count),
            profile_checked_at = unixepoch(),
            updated_at = unixepoch()""",
        "args": [
            {"type": "text", "value": p["did"]},
            {"type": "text", "value": p.get("handle", "")},
            {"type": "text", "value": p.get("displayName", "")},
            {"type": "text", "value": extract_avatar_cid(p.get("avatar", ""))},
            {"type": "text", "value": json.dumps(p.get("labels", []))},
            {"type": "integer", "value": str(hidden)},
            {"type": "text", "value": p.get("createdAt", "")},
            {"type": "text", "value": clean_associated(p.get("associated"))},
            {"type": "integer", "value": str(int(p.get("followersCount") or 0))},
            {"type": "integer", "value": str(int(p.get("followsCount") or 0))},
            {"type": "integer", "value": str(int(p.get("postsCount") or 0))},
        ],
    }


APPB_DEFAULT = "https://typeahead-search.fly.dev"


def collect_benchmark_dids(appb_url=APPB_DEFAULT):
    """For each canonical query, union DIDs from BOTH sides of the ranking
    competition: bsky's top results AND App B's current top results.

    Enriching only bsky-top biases the benchmark — App B's competing candidates
    (handle-prefix / FTS matches) stay at 0 followers, so they lose lexical-tier
    ties they shouldn't be winning in the first place. Pulling App B's top too
    means every candidate that influences the ranking has real signal.
    """
    dids = set()
    for q in BENCHMARK_QUERIES:
        bsky_count = 0
        appb_count = 0
        # bsky side
        try:
            url = f"{BSKY}/app.bsky.actor.searchActorsTypeahead?q={urllib.parse.quote(q)}&limit=10"
            req = urllib.request.Request(url, headers={"User-Agent": "typeahead-bench-prep/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for a in data.get("actors", []):
                if a.get("did"):
                    dids.add(a["did"])
                    bsky_count += 1
        except Exception as e:
            print(f"  q={q!r:14s}: bsky error {e}", file=sys.stderr)

        # App B side — pulls 25 so we cover both prefix and FTS tier candidates
        try:
            url = f"{appb_url}/search?q={urllib.parse.quote(q)}&limit=25"
            req = urllib.request.Request(url, headers={"User-Agent": "typeahead-bench-prep/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for a in data.get("actors", []):
                if a.get("did"):
                    if a["did"] not in dids:
                        appb_count += 1
                    dids.add(a["did"])
        except Exception as e:
            print(f"  q={q!r:14s}: appb error {e}", file=sys.stderr)

        print(f"  q={q!r:14s}: bsky={bsky_count} appb_new={appb_count}")
        time.sleep(0.2)
    return list(dids)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--benchmark", action="store_true",
                   help="seed DIDs from bsky typeahead of the canonical ranking corpus")
    args = p.parse_args()

    turso_url = env("TURSO_URL").replace("libsql://", "https://")
    turso_token = env("TURSO_AUTH_TOKEN")

    # collect DIDs
    if args.benchmark:
        print(f"benchmark mode — seeding from bsky typeahead of {len(BENCHMARK_QUERIES)} canonical queries:")
        dids = collect_benchmark_dids()
    else:
        dids_set = set()
        print("atprotocol.dev → following:")
        for f in fetch_all("app.bsky.graph.getFollows", "atprotocol.dev", "follows"):
            dids_set.add(f["did"])
        print("atmosphereconf.org → followers:")
        for f in fetch_all("app.bsky.graph.getFollowers", "atmosphereconf.org", "followers"):
            dids_set.add(f["did"])
        dids = list(dids_set)

    print(f"\n{len(dids)} unique DIDs to enrich")

    # enrich via getProfiles in batches of 25
    enriched = 0
    t0 = time.time()
    all_stmts = []

    for i in range(0, len(dids), 25):
        batch = dids[i:i+25]
        result = fetch_profiles(batch)
        if result == "rate_limited":
            print("  rate limited — pausing 30s...")
            time.sleep(30)
            result = fetch_profiles(batch)
            if result == "rate_limited":
                print("  still limited, skipping")
                continue
        for p in result:
            all_stmts.append(profile_to_stmt(p))
            enriched += 1
        print(f"  enriched {enriched}/{len(dids)}...", end="\r")
        time.sleep(0.05)

    print(f"\n\nenriched {enriched} profiles, writing to turso...")

    # write in small batches
    for i in range(0, len(all_stmts), WRITE_BATCH):
        chunk = all_stmts[i:i+WRITE_BATCH]
        turso_batch_write(chunk, turso_url, turso_token)
        print(f"  wrote {min(i+WRITE_BATCH, len(all_stmts))}/{len(all_stmts)}", end="\r")
        time.sleep(WRITE_PAUSE)

    elapsed = time.time() - t0
    print(f"\n\ndone in {elapsed:.0f}s. enriched {enriched} actors.")

if __name__ == "__main__":
    main()
