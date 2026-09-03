#!/usr/bin/env python3
"""regenerate src/labelers.json: the moderation labelers the reputation page asks.

Discovery is the public appview's own search: `associated.labeler` on a
searchActors hit is the appview asserting the account declares a labeler
service, so nothing here is hand-typed. Kept are labelers whose name or
handle says moderation/safety (topic labelers — sports teams, zodiac signs —
are noise for a "what do moderators think of this account" view), ordered by
followers, capped at LIMIT. Each entry carries the labeler's
`com.atproto.label.queryLabels` base URL resolved from its DID document,
because the unauthenticated appview ignores `atproto-accept-labelers` (checked
2026-09-03: a Blacksky-labeled account came back with no labels however the
header was set), so the page must ask each labeler directly.

Run: uv run scripts/discover-labelers.py   (then commit src/labelers.json)
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

APPVIEW = "https://public.api.bsky.app/xrpc/"
QUERIES = ["labeler", "moderation", "eurosky", "northsky", "blacksky", "moderation service", "mod service", "labels",
           "safety", "spam", "scam", "blocklist", "automod", "trust and safety",
           "community moderation", "bot detection"]
MODERATION_WORDS = re.compile(r"moderation|mod service|safety|automod|anti-?harass|scam|spam", re.I)
LIMIT = 20
# a labeler nobody subscribes to is more likely a dead endpoint than a signal;
# each one costs the report a network round trip
MIN_FOLLOWERS = 50
# moderation services run by appview operators are structurally relevant
# whatever their subscriber count: their labels decide what their appview
# shows. kept even under the floor; the page reports an unreachable one.
PINNED = {"moderation.bsky.app", "moderation.blacksky.app", "moderation.northsky.social", "ozone.eurosky.social"}
OUT = Path(__file__).resolve().parent.parent / "src" / "labelers.json"


def get(url: str):
    req = urllib.request.Request(url, headers={"user-agent": "typeahead discover-labelers"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def appview(path: str, **params):
    return get(APPVIEW + path + "?" + urllib.parse.urlencode(params, doseq=True))


def labeler_endpoint(did: str) -> str | None:
    host = "https://plc.directory/" + did if did.startswith("did:plc:") else \
        "https://" + did.removeprefix("did:web:") + "/.well-known/did.json"
    try:
        doc = get(host)
    except Exception as e:  # noqa: BLE001 - a dead DID doc just drops the labeler
        print(f"  skip {did}: did doc {e}", file=sys.stderr)
        return None
    for svc in doc.get("service", []):
        if svc.get("id", "").endswith("atproto_labeler"):
            return svc.get("serviceEndpoint")
    return None


def main() -> None:
    found: dict[str, dict] = {}
    for q in QUERIES:
        for a in appview("app.bsky.actor.searchActors", q=q, limit=100).get("actors", []):
            if (a.get("associated") or {}).get("labeler"):
                found[a["did"]] = a
    print(f"{len(found)} labelers discovered", file=sys.stderr)

    dids = list(found)
    profiles = []
    for i in range(0, len(dids), 25):
        profiles += appview("app.bsky.actor.getProfiles", actors=dids[i:i + 25]).get("profiles", [])

    kept = [p for p in profiles
            if p["handle"] in PINNED
            or ((p.get("followersCount") or 0) >= MIN_FOLLOWERS
                and (MODERATION_WORDS.search(p.get("displayName") or "") or MODERATION_WORDS.search(p["handle"])))]
    kept.sort(key=lambda p: -(p.get("followersCount") or 0))

    out = []
    for p in kept:
        if len(out) == LIMIT:
            break
        endpoint = labeler_endpoint(p["did"])
        if not endpoint:
            continue
        out.append({
            "did": p["did"], "handle": p["handle"], "name": p.get("displayName") or p["handle"],
            "endpoint": endpoint.rstrip("/"), "followers": p.get("followersCount") or 0,
        })
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {len(out)} labelers to {OUT.relative_to(OUT.parents[1])}", file=sys.stderr)
    for r in out:
        print(f"{r['followers']:>7}  {r['handle']:36}  {r['endpoint']}", file=sys.stderr)


if __name__ == "__main__":
    main()
