#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
smoke tests for typeahead service.

verifies response shape, search behavior, CORS, and optional
comparison against public.api.bsky.app.

usage:
    ./scripts/smoke.py --url https://typeahead.waow.tech
    ./scripts/smoke.py --url http://localhost:8787
    ./scripts/smoke.py --url https://typeahead.waow.tech --compare
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

PASS = "\033[32mpass\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mskip\033[0m"

failures = 0

BSKY_PUBLIC = "https://public.api.bsky.app"
XRPC_PATH = "/xrpc/app.bsky.actor.searchActorsTypeahead"


def check(name: str, ok: bool, detail: str = ""):
    global failures
    tag = PASS if ok else FAIL
    msg = f"  [{tag}] {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    if not ok:
        failures += 1
    return ok


def fetch(url: str, timeout: int = 15, method: str = "GET") -> tuple[dict | None, dict]:
    """fetch JSON + response headers. returns (body, headers)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "typeahead-smoke/1.0"}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return json.loads(resp.read()), headers
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}, {}
    except Exception as e:
        return {"_error": str(e)}, {}


def test_response_shape(base_url: str):
    print("\n--- response shape ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=test&limit=3")
    check("returns valid JSON", data is not None and "_error" not in data)
    if not data or "_error" in data or "_http_error" in data:
        return

    check("has actors array", isinstance(data.get("actors"), list))
    actors = data.get("actors", [])
    if actors:
        a = actors[0]
        check("actor has did", "did" in a)
        check("actor has handle", "handle" in a)
        check("did starts with did:", a.get("did", "").startswith("did:"))


def test_known_handle(base_url: str):
    print("\n--- known handle ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=zzstoatzz&limit=10")
    if not data or "_error" in data or "_http_error" in data:
        check("fetch succeeded", False)
        return

    actors = data.get("actors", [])
    handles = [a.get("handle", "") for a in actors]
    check("zzstoatzz.io in results", "zzstoatzz.io" in handles, f"got {handles[:5]}")


def test_prefix_match(base_url: str):
    print("\n--- prefix match ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=zzst&limit=10")
    if not data or "_error" in data or "_http_error" in data:
        check("fetch succeeded", False)
        return

    actors = data.get("actors", [])
    handles = [a.get("handle", "") for a in actors]
    check("prefix 'zzst' finds zzstoatzz.io", "zzstoatzz.io" in handles, f"got {handles[:5]}")


def test_cors(base_url: str):
    print("\n--- CORS headers ---")
    _, headers = fetch(f"{base_url}{XRPC_PATH}?q=test&limit=1")
    origin = headers.get("access-control-allow-origin", "")
    check("Access-Control-Allow-Origin: *", origin == "*", f"got '{origin}'")


def test_deprecated_param(base_url: str):
    print("\n--- deprecated param ---")
    data_q, _ = fetch(f"{base_url}{XRPC_PATH}?q=nate&limit=5")
    data_term, _ = fetch(f"{base_url}{XRPC_PATH}?term=nate&limit=5")

    if not data_q or "_error" in data_q or not data_term or "_error" in data_term:
        check("both params work", False)
        return

    actors_q = {a.get("did") for a in data_q.get("actors", [])}
    actors_term = {a.get("did") for a in data_term.get("actors", [])}
    check("?term= returns same as ?q=", actors_q == actors_term, f"q={len(actors_q)}, term={len(actors_term)}")


def test_limit_bounds(base_url: str):
    print("\n--- limit bounds ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=a&limit=3")
    if not data or "_error" in data or "_http_error" in data:
        check("fetch succeeded", False)
        return

    actors = data.get("actors", [])
    check("limit=3 returns ≤3", len(actors) <= 3, f"got {len(actors)}")


def test_empty_query(base_url: str):
    print("\n--- empty query ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=")
    check("empty query returns 400", data is not None and data.get("_http_error") == 400, f"got {data}")


def test_limit_over_max(base_url: str):
    print("\n--- limit > 100 ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=test&limit=200")
    check("limit>100 returns 400", data is not None and data.get("_http_error") == 400, f"got {data}")


def test_stats_page(base_url: str):
    print("\n--- stats page ---")
    try:
        req = urllib.request.Request(
            f"{base_url}/stats",
            headers={"User-Agent": "typeahead-smoke/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            ct = resp.headers.get("Content-Type", "")
            body = resp.read().decode()
            check("stats returns 200", resp.status == 200)
            check("stats content-type is html", "text/html" in ct, f"got '{ct}'")
            check("stats contains actors indexed", "actors indexed" in body)
            check("stats contains sparkline heading", "searches / 5 min" in body)
            check("stats has home link", 'href="/"' in body)
            check("stats shows hidden count", "hidden by moderation" in body)
    except urllib.error.HTTPError as e:
        check("stats returns 200", False, f"got {e.code}")
    except Exception as e:
        check("stats fetch succeeded", False, str(e))


def test_request_indexing(base_url: str):
    print("\n--- request indexing ---")
    # GET should redirect to homepage (302)
    try:
        req = urllib.request.Request(
            f"{base_url}/request-indexing?handle=test",
            headers={"User-Agent": "typeahead-smoke/1.0"},
        )
        opener = urllib.request.build_opener(urllib.request.HTTPHandler)
        # don't follow redirects
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise urllib.error.HTTPError(newurl, code, msg, headers, fp)
        opener = urllib.request.build_opener(NoRedirect)
        opener.open(req, timeout=15)
        check("GET redirects (302)", False, "no redirect")
    except urllib.error.HTTPError as e:
        check("GET redirects (302)", e.code == 302, f"got {e.code}")

    # POST with empty handle should return 400
    data, _ = fetch(f"{base_url}/request-indexing", method="POST")
    check("POST empty returns 400", data is not None and data.get("_http_error") == 400, f"got {data}")

    # POST with valid handle should return JSON with handle/did/hidden
    data, _ = fetch(f"{base_url}/request-indexing?handle=zzstoatzz.io", method="POST")
    if not data or "_error" in data or "_http_error" in data:
        check("POST valid handle", False, f"got {data}")
    else:
        check("POST returns handle", data.get("handle") == "zzstoatzz.io", f"got {data.get('handle')}")
        check("POST returns did", data.get("did", "").startswith("did:"), f"got {data.get('did')}")
        check("POST returns hidden field", "hidden" in data, f"keys: {list(data.keys())}")


def test_moderation_filtering(base_url: str):
    print("\n--- moderation filtering ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=test&limit=10")
    if not data or "_error" in data or "_http_error" in data:
        check("fetch succeeded", False)
        return

    actors = data.get("actors", [])

    # labels, createdAt, associated are intentional fields returned for client rendering
    allowed_keys = {"did", "handle", "displayName", "avatar", "labels", "createdAt", "associated"}
    extra_keys: set[str] = set()
    for a in actors:
        extra_keys |= set(a.keys()) - allowed_keys
    check("actor objects have expected shape", len(extra_keys) == 0, f"extra keys: {extra_keys}" if extra_keys else "")

    # verify labels is always an array when present
    for a in actors:
        if "labels" in a:
            check("labels is array", isinstance(a["labels"], list), f"got {type(a['labels']).__name__}")
            break

    # !no-unauthenticated actors should be VISIBLE (it's about content, not identity)
    print("\n--- !no-unauthenticated inclusion ---")
    found = find_noauth_actor(base_url)
    if not found:
        check("found a !no-unauthenticated actor to verify", False, "couldn't find one — skipping")


def find_noauth_actor(base_url: str) -> bool:
    """find an actor with !no-unauthenticated and verify they ARE included in our results."""
    for q in ["alex", "sam", "chris", "jordan"]:
        bsky_data, _ = fetch(f"{BSKY_PUBLIC}/xrpc/app.bsky.actor.searchActors?q={q}&limit=25")
        if not bsky_data or "_error" in bsky_data:
            continue

        for actor in bsky_data.get("actors", []):
            labels = actor.get("labels", [])
            has_noauth = any(
                l.get("val") == "!no-unauthenticated" and not l.get("neg")
                for l in labels
            )
            if not has_noauth:
                continue

            handle = actor.get("handle", "")
            if not handle:
                continue

            # this actor has !no-unauthenticated — check they ARE in our results
            our_data, _ = fetch(f"{base_url}{XRPC_PATH}?q={handle}&limit=10")
            if not our_data or "_error" in our_data:
                continue

            our_handles = {a.get("handle") for a in our_data.get("actors", [])}
            if handle in our_handles:
                check(f"!no-unauthenticated actor @{handle} visible in search", True)
                return True
            else:
                check(f"!no-unauthenticated actor @{handle} visible in search", False, "not found in results")
                return True

    return False


def test_comparison(base_url: str, queries: list[str]):
    print("\n--- comparison vs public.api.bsky.app ---")

    for q in queries:
        sys.stdout.write(f"  comparing '{q}'...")
        sys.stdout.flush()

        ours, _ = fetch(f"{base_url}{XRPC_PATH}?q={q}&limit=10")
        theirs, _ = fetch(f"{BSKY_PUBLIC}{XRPC_PATH}?q={q}&limit=10")

        if not ours or "_error" in ours or not theirs or "_error" in theirs:
            sys.stdout.write(f"\r  [{FAIL}] '{q}': fetch failed\n")
            continue

        our_handles = {a.get("handle") for a in ours.get("actors", [])}
        their_handles = {a.get("handle") for a in theirs.get("actors", [])}

        overlap = our_handles & their_handles
        pct = (len(overlap) / len(their_handles) * 100) if their_handles else 0
        sys.stdout.write(
            f"\r  [{PASS}] '{q}': {len(overlap)}/{len(their_handles)} overlap ({pct:.0f}%)"
            f" — ours={len(our_handles)}, theirs={len(their_handles)}\n"
        )


def main():
    parser = argparse.ArgumentParser(description="typeahead smoke tests")
    parser.add_argument("--url", required=True, help="typeahead service URL")
    parser.add_argument("--compare", action="store_true", help="compare results vs public.api.bsky.app")
    parser.add_argument(
        "--queries",
        nargs="+",
        default=["nate", "zzstoatzz", "paul", "dan", "sky"],
        help="queries for comparison test",
    )
    args = parser.parse_args()

    print(f"typeahead: {args.url}")

    test_response_shape(args.url)
    test_known_handle(args.url)
    test_prefix_match(args.url)
    test_cors(args.url)
    test_deprecated_param(args.url)
    test_limit_bounds(args.url)
    test_empty_query(args.url)
    test_limit_over_max(args.url)
    test_stats_page(args.url)
    test_request_indexing(args.url)
    test_moderation_filtering(args.url)

    if args.compare:
        test_comparison(args.url, args.queries)
    else:
        print(f"\n--- comparison ---")
        print(f"  [{SKIP}] skipped (use --compare)")

    print()
    if failures == 0:
        print("all checks passed.")
    else:
        print(f"{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
