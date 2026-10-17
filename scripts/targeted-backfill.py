#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
targeted backfill: enrich a focused set of actors via getProfiles + upsert
to turso (via `turso db shell typeahead`, no token plumbing — uses your
already-authed CLI), including ranking columns (followers_count,
follows_count, posts_count, quality_score).

two modes:
  default        — walks atprotocol.dev/atmosphereconf.org graph
  --benchmark    — seeds DIDs from both bsky-top-10 AND search service-top-25 for the
                   canonical ranking corpus so `just bench-ranking` has signal
                   for both sides of every comparison

usage:
    ./scripts/targeted-backfill.py
    ./scripts/targeted-backfill.py --benchmark
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

BSKY = "https://public.api.bsky.app/xrpc"
WRITE_BATCH = 50
WRITE_PAUSE = 0.2
TURSO_DB = "typeahead"

# Mirrors RANKING_CORPUS in scripts/bench.py. Kept here as a separate constant
# because importing across the scripts/ directory adds path-hackery without
# any real upside — both lists exist to lock in regressions; if they drift,
# the benchmark surface area shrinks accordingly.
BENCHMARK_QUERIES = [
    "fig", "fig (", "fig aka", "phil",
    "zzstoatzz", "zzstoatzz.io",
    "nate", "alex", "bob", "aoc", "car",
]

def sql_quote(s):
    """SQLite single-quoted string literal, with embedded ' doubled."""
    if s is None:
        return "''"
    return "'" + str(s).replace("'", "''") + "'"


def turso_batch_write(sql_statements):
    """Pipe a batch of SQL statements to `turso db shell`. CLI is already
    authed (browser flow), so no token plumbing here."""
    body = ";\n".join(sql_statements) + ";\n"
    try:
        r = subprocess.run(
            ["turso", "db", "shell", TURSO_DB],
            input=body, text=True, capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            print(f"  turso shell failed (rc={r.returncode}): {r.stderr[:300]}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"  turso shell error: {e}", file=sys.stderr)
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

import math
import datetime


def compute_authority_score(followers, follows, posts, created_at):
    """Mirror of computeAuthorityScore in src/utils.ts. Both implementations
    must stay aligned — divergence between worker writes and script writes
    would cause inconsistent quality_score across enrichment paths."""
    f = max(0, int(followers or 0))
    fw = max(0, int(follows or 0))
    pc = max(0, int(posts or 0))
    score = math.log10(f + 1) * 100 + math.log10(pc + 1) * 20
    if f > 100 and fw > 0:
        ratio = f / fw
        if ratio > 10: score += 50
        if ratio > 100: score += 100
    if created_at:
        try:
            t = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            age_days = (time.time() - t) / 86400
            if age_days > 365: score += 30
            if age_days > 365 * 3: score += 30
        except Exception:
            pass
    return round(score * 100) / 100


def profile_to_sql(p):
    """Build a single SQLite UPSERT for one bsky profile. Returns a SQL
    string suitable for piping to `turso db shell`."""
    hide_vals = {"!hide", "!takedown", "!suspend", "spam"}
    mod_did = "did:plc:ar7c4by46qjdydhdevvrndac"
    hidden = 0
    for lbl in (p.get("labels") or []):
        if lbl.get("val", "") in hide_vals or (lbl.get("val") == "!no-unauthenticated" and lbl.get("src") == mod_did):
            hidden = 1; break
    followers = int(p.get("followersCount") or 0)
    follows = int(p.get("followsCount") or 0)
    posts = int(p.get("postsCount") or 0)
    quality = compute_authority_score(followers, follows, posts, p.get("createdAt", ""))

    did = sql_quote(p["did"])
    handle = sql_quote(p.get("handle", ""))
    display = sql_quote(p.get("displayName", ""))
    avatar = sql_quote(extract_avatar_cid(p.get("avatar", "")))
    labels = sql_quote(json.dumps(p.get("labels", [])))
    created = sql_quote(p.get("createdAt", ""))
    associated = sql_quote(clean_associated(p.get("associated")))

    return f"""INSERT INTO actors (did, handle, display_name, avatar_url, labels, hidden, created_at, associated, followers_count, follows_count, posts_count, quality_score, updated_at)
VALUES ({did}, {handle}, {display}, {avatar}, {labels}, {hidden}, {created}, {associated}, {followers}, {follows}, {posts}, {quality}, unixepoch())
ON CONFLICT(did) DO UPDATE SET
  handle = COALESCE(NULLIF({handle}, ''), actors.handle),
  display_name = COALESCE(NULLIF({display}, ''), actors.display_name),
  avatar_url = COALESCE(NULLIF({avatar}, ''), actors.avatar_url),
  labels = {labels}, hidden = {hidden},
  created_at = COALESCE(NULLIF({created}, ''), actors.created_at),
  associated = COALESCE(NULLIF({associated}, '{{}}'), actors.associated),
  followers_count = COALESCE(NULLIF({followers}, 0), actors.followers_count),
  follows_count = COALESCE(NULLIF({follows}, 0), actors.follows_count),
  posts_count = COALESCE(NULLIF({posts}, 0), actors.posts_count),
  quality_score = COALESCE(NULLIF({quality}, 0), actors.quality_score),
  profile_checked_at = unixepoch(),
  updated_at = unixepoch()"""


APPB_DEFAULT = "https://typeahead-search-compact.fly.dev"


def collect_benchmark_dids(appb_url=APPB_DEFAULT):
    """For each canonical query, union DIDs from BOTH sides of the ranking
    competition: bsky's top results AND search service's current top results.

    Enriching only bsky-top biases the benchmark — search service's competing candidates
    (handle-prefix / FTS matches) stay at 0 followers, so they lose lexical-tier
    ties they shouldn't be winning in the first place. Pulling search service's top too
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

        # search service side — pulls 25 so we cover both prefix and FTS tier candidates
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
    all_sql = []

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
            all_sql.append(profile_to_sql(p))
            enriched += 1
        print(f"  enriched {enriched}/{len(dids)}...", end="\r")
        time.sleep(0.05)

    print(f"\n\nenriched {enriched} profiles, piping to `turso db shell {TURSO_DB}`...")

    # write in small batches via turso CLI
    for i in range(0, len(all_sql), WRITE_BATCH):
        chunk = all_sql[i:i+WRITE_BATCH]
        turso_batch_write(chunk)
        print(f"  wrote {min(i+WRITE_BATCH, len(all_sql))}/{len(all_sql)}", end="\r")
        time.sleep(WRITE_PAUSE)

    elapsed = time.time() - t0
    print(f"\n\ndone in {elapsed:.0f}s. enriched {enriched} actors.")

if __name__ == "__main__":
    main()
