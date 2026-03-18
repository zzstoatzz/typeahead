#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
one-shot: sweep entire actors table and set hidden flags from bsky moderation labels.

usage:
    ./scripts/backfill-moderation.py
"""

import json
import subprocess
import sys
import time
import urllib.request

BSKY_MOD_DID = "did:plc:ar7c4by46qjdydhdevvrndac"
MOD_HIDE_VALS = {"!hide", "!takedown", "spam"}
ANY_SRC_HIDE_VALS = {"!no-unauthenticated"}
GET_PROFILES_URL = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles"
BATCH_SIZE = 25  # getProfiles limit
PAGE_SIZE = 500  # actors per D1 query
DELAY = 0.3  # seconds between API calls


def should_hide(labels: list | None) -> bool:
    if not labels:
        return False
    now = time.time() * 1000
    for l in labels:
        if l.get("neg"):
            continue
        if l.get("exp") and _parse_ts(l["exp"]) <= now:
            continue
        if l.get("src") == BSKY_MOD_DID and l.get("val") in MOD_HIDE_VALS:
            return True
        if l.get("val") in ANY_SRC_HIDE_VALS:
            return True
    return False


def _parse_ts(s: str) -> float:
    """rough ISO8601 parse — good enough for expiry comparison."""
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return 0


def d1_query(sql: str) -> list[dict]:
    result = subprocess.run(
        ["npx", "wrangler", "d1", "execute", "typeahead-db", "--remote", "--command", sql, "--json"],
        capture_output=True, text=True, cwd="."
    )
    # --json still emits warnings to stdout; extract the JSON array
    stdout = result.stdout
    bracket = stdout.find("[")
    if bracket == -1:
        if result.returncode != 0:
            print(f"  D1 error: {result.stderr[:200]}", file=sys.stderr)
        return []
    try:
        data = json.loads(stdout[bracket:])
        return data[0]["results"] if data else []
    except (json.JSONDecodeError, IndexError, KeyError) as e:
        print(f"  D1 parse error: {e}", file=sys.stderr)
        return []


def fetch_profiles(dids: list[str]) -> list[dict]:
    params = "&".join(f"actors={d}" for d in dids)
    url = f"{GET_PROFILES_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "typeahead-mod-backfill/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("profiles", [])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("  rate limited — pausing 60s")
            time.sleep(60)
            return fetch_profiles(dids)  # retry once
        print(f"  HTTP {e.code}")
        return []
    except Exception as e:
        print(f"  error: {e}")
        return []


def main():
    cursor = 0
    total_checked = 0
    total_hidden = 0
    total_unhidden = 0

    while True:
        rows = d1_query(
            f"SELECT rowid, did FROM actors WHERE rowid > {cursor} ORDER BY rowid ASC LIMIT {PAGE_SIZE}"
        )
        if not rows:
            break

        page_hidden = 0
        page_unhidden = 0

        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            dids = [r["did"] for r in batch]

            if i > 0:
                time.sleep(DELAY)

            profiles = fetch_profiles(dids)
            total_checked += len(profiles)

            hide_dids = []
            unhide_dids = []
            for p in profiles:
                if should_hide(p.get("labels")):
                    hide_dids.append(p["did"])
                else:
                    unhide_dids.append(p["did"])

            if hide_dids:
                did_list = ", ".join(f"'{d}'" for d in hide_dids)
                changed = d1_query(
                    f"UPDATE actors SET hidden = 1 WHERE did IN ({did_list}) AND hidden = 0"
                )
                page_hidden += len(hide_dids)

            if unhide_dids:
                did_list = ", ".join(f"'{d}'" for d in unhide_dids)
                d1_query(
                    f"UPDATE actors SET hidden = 0 WHERE did IN ({did_list}) AND hidden = 1"
                )

        cursor = rows[-1]["rowid"]
        total_hidden += page_hidden
        total_unhidden += page_unhidden

        print(
            f"  cursor={cursor}  checked={total_checked}  hidden={total_hidden}  "
            f"(page: {len(rows)} actors, {page_hidden} newly hidden)"
        )

    print(f"\ndone. checked={total_checked}, hidden={total_hidden}, unhidden={total_unhidden}")


if __name__ == "__main__":
    main()
