#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
targeted backfill: fetch follows/followers from specific accounts,
enrich via getProfiles, upsert into turso.

usage:
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/targeted-backfill.py
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

BSKY = "https://public.api.bsky.app/xrpc"
WRITE_BATCH = 50
WRITE_PAUSE = 0.2

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
        "sql": """INSERT INTO actors (did, handle, display_name, avatar_url, labels, hidden, created_at, associated, updated_at)
          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, unixepoch())
          ON CONFLICT(did) DO UPDATE SET
            handle = COALESCE(NULLIF(?2, ''), actors.handle),
            display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
            avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
            labels = ?5, hidden = ?6,
            created_at = COALESCE(NULLIF(?7, ''), actors.created_at),
            associated = COALESCE(NULLIF(?8, '{}'), actors.associated),
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
        ],
    }

def main():
    turso_url = env("TURSO_URL").replace("libsql://", "https://")
    turso_token = env("TURSO_AUTH_TOKEN")

    # collect DIDs
    dids = set()

    print("atprotocol.dev → following:")
    follows = fetch_all("app.bsky.graph.getFollows", "atprotocol.dev", "follows")
    for f in follows:
        dids.add(f["did"])

    print("atmosphereconf.org → followers:")
    followers = fetch_all("app.bsky.graph.getFollowers", "atmosphereconf.org", "followers")
    for f in followers:
        dids.add(f["did"])

    dids = list(dids)
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
