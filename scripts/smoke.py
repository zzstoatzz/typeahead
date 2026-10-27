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
import os
import sys
import time
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



def test_page_scripts(base_url: str):
    """every HTML page's inline <script> must parse — a template-literal
    escaping slip ships a SyntaxError that kills the whole page silently
    (2026-08-04: both / and /reputation dead, API smoke all green)."""
    import re
    import subprocess
    print("\n--- page script syntax ---")
    for path in ("/", "/reputation", "/stats", "/docs"):
        try:
            req = urllib.request.Request(base_url + path, headers={"User-Agent": "typeahead-smoke/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode()
        except Exception as e:
            check(f"{path} fetch", False, str(e)[:60])
            continue
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        ok = True
        detail = ""
        for i, body in enumerate(scripts):
            r = subprocess.run(["node", "--check", "-"], input=body.encode(),
                               capture_output=True, timeout=20)
            if r.returncode != 0:
                ok = False
                detail = r.stderr.decode().strip().splitlines()[-1][:80] if r.stderr else f"script {i} failed"
                break
        check(f"{path} inline scripts parse ({len(scripts)})", ok, detail)


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


def test_profiles_shape(base_url: str):
    print("\n--- getProfiles / getProfile ---")
    data, _ = fetch(f"{base_url}/xrpc/app.bsky.actor.getProfiles?actors=zzstoatzz.io")
    profiles = (data or {}).get("profiles") or []
    check("getProfiles returns zzstoatzz.io", bool(profiles) and profiles[0].get("handle") == "zzstoatzz.io")
    if profiles:
        p = profiles[0]
        check("profile has description", isinstance(p.get("description"), str) and p["description"] != "")
        check("profile has banner", str(p.get("banner", "")).startswith("https://cdn.bsky.app/img/banner/"))
        check("profile has followersCount", isinstance(p.get("followersCount"), int) and p["followersCount"] > 0)
        check("profile has indexedAt", str(p.get("indexedAt", "")).endswith("Z"))
    single, _ = fetch(f"{base_url}/xrpc/app.bsky.actor.getProfile?actor=zzstoatzz.io")
    check("getProfile unwraps the same view", bool(single) and single.get("did") == (profiles[0].get("did") if profiles else None))
    missing, _ = fetch(f"{base_url}/xrpc/app.bsky.actor.getProfile?actor=definitely-not-a-real-handle.invalid")
    check("getProfile missing actor → 400", (missing or {}).get("_http_error") == 400)


def test_canonical_endpoint(base_url: str):
    """the self-owned NSID must serve the identical response as the bsky-compat alias."""
    print("\n--- canonical endpoint ---")
    native, _ = fetch(f"{base_url}/xrpc/tech.waow.typeahead.searchActors?q=zzstoatzz&limit=5")
    alias, _ = fetch(f"{base_url}{XRPC_PATH}?q=zzstoatzz&limit=5")
    check("canonical returns valid JSON", native is not None and "_error" not in native and "_http_error" not in native)
    if not native or "_error" in native or "_http_error" in native:
        return
    check("canonical has actors array", isinstance(native.get("actors"), list))
    if alias and "_error" not in alias and "_http_error" not in alias:
        native_dids = [a.get("did") for a in native.get("actors", [])]
        alias_dids = [a.get("did") for a in alias.get("actors", [])]
        check("canonical matches alias results", native_dids == alias_dids,
              f"native={native_dids[:3]} alias={alias_dids[:3]}")

    try:
        req = urllib.request.Request(f"{base_url}/llms.txt", headers={"User-Agent": "typeahead-smoke/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            llms = resp.read().decode("utf-8")
    except Exception:
        llms = ""
    check("llms.txt documents the canonical endpoint", "tech.waow.typeahead.searchActors" in llms)


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

    # the index stores prefixes up to 16 bytes; a longer query must still
    # resolve (2026-09-18: 17+ character queries returned nothing)
    long_q = "typeahead.waow.te"
    assert len(long_q) == 17
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q={long_q}&limit=10")
    handles = [a.get("handle", "") for a in (data or {}).get("actors", [])]
    check(f"prefix '{long_q}' (17 chars) finds typeahead.waow.tech", "typeahead.waow.tech" in handles, f"got {handles[:5]}")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=typeahead.waow.tx&limit=10")
    handles = [a.get("handle", "") for a in (data or {}).get("actors", [])]
    check("prefix 'typeahead.waow.tx' (17 chars, no such handle) finds nothing", handles == [], f"got {handles[:5]}")


def test_ranking(base_url: str):
    """popularity ranking — a high-follower account must rank above low-follower
    namesakes that share its prefix. Guards the quality_score-clobber class of
    bug (see project_quality_score_clobber): zzstoatzz.io (2k followers) once
    sank to position 7 behind its own ~30-follower bots because a countless
    profile froze its score. Presence checks pass that silently — only POSITION
    catches it."""
    print("\n--- popularity ranking ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=zzst&limit=10")
    if not data or "_error" in data or "_http_error" in data:
        check("fetch succeeded", False)
        return

    handles = [a.get("handle", "") for a in data.get("actors", [])]

    def pos(h: str) -> int:
        return handles.index(h) if h in handles else 10**6

    main = pos("zzstoatzz.io")
    check("zzstoatzz.io present for 'zzst'", main < 10**6, f"got {handles[:5]}")
    # the high-follower account must outrank its low-follower namesakes
    for bot in ("phi.zzstoatzz.io", "zzstoatzzdevlog.bsky.social"):
        if bot in handles:
            check(f"zzstoatzz.io ranks above {bot}", main < pos(bot),
                  f"main@{main} vs {bot}@{pos(bot)} — handles={handles[:5]}")


def test_cors(base_url: str):
    print("\n--- CORS headers ---")
    _, headers = fetch(f"{base_url}{XRPC_PATH}?q=test&limit=1")
    origin = headers.get("access-control-allow-origin", "")
    check("Access-Control-Allow-Origin: *", origin == "*", f"got '{origin}'")


def test_deprecated_param(base_url: str):
    print("\n--- deprecated param ---")
    data_q, _ = fetch(f"{base_url}{XRPC_PATH}?q=nate&limit=5")
    data_term, _ = fetch(f"{base_url}{XRPC_PATH}?term=nate&limit=5")

    if not data_q or "_error" in data_q or "_http_error" in data_q or not data_term or "_error" in data_term or "_http_error" in data_term:
        check("both params work", False, f"q={data_q!r}, term={data_term!r}")
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

    # POST with valid handle should return JSON with handle/did/hidden.
    # 60s timeout: the synchronous turso upsert queues behind the ingester's
    # batch transactions and spikes 10-45s under firehose floods (measured
    # 2026-08-04, server-timing turso_write;dur=10178). that variance is the
    # endpoint's documented behavior, not a regression for this gate to catch.
    data, _ = fetch(f"{base_url}/request-indexing?handle=zzstoatzz.io", method="POST", timeout=60)
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
        # every fixture failed live validation — the fixture list needs
        # refreshing (see NOAUTH_FIXTURES), which is our problem, so FAIL.
        check("found a !no-unauthenticated actor to verify", False,
              "all fixtures stale — refresh NOAUTH_FIXTURES")


# known !no-unauthenticated accounts, validated live against bsky before use so
# a removed label can never false-fail us. dynamic discovery via searchActors is
# impossible: bsky's unauthenticated search excludes labeled actors entirely
# (getProfile still shows them). refresh candidates with:
#   turso db shell typeahead "SELECT did, handle FROM actors WHERE labels LIKE
#     '%no-unauthenticated%' AND hidden = 0 ORDER BY followers_count DESC LIMIT 8"
NOAUTH_FIXTURES = [
    "did:plc:vc7f4oafdgxsihk4cry2xpze",  # jcsalterego.bsky.social
    "did:plc:3ijusmsw4eizbtvn3k263rhv",  # philinvestigates.com
    "did:plc:5hwy5afvfhonj6mrpcxm3rfy",  # emily.gorcen.ski
    "did:plc:nehcwc3ng5igejopsi5fnqrr",  # alexanderchee.bsky.social
]


def _verify_noauth_inclusion(base_url: str, handle: str) -> bool:
    """given a confirmed-labeled handle, check it appears in OUR results.
    returns True if a verdict was reached (pass or fail recorded)."""
    for attempt in range(3):  # our endpoint erroring is retryable, not a verdict
        our_data, _ = fetch(f"{base_url}{XRPC_PATH}?q={handle}&limit=10")
        if our_data and "_error" not in our_data:
            our_handles = {a.get("handle") for a in our_data.get("actors", [])}
            check(f"!no-unauthenticated actor @{handle} visible in search",
                  handle in our_handles, "" if handle in our_handles else "not found in results")
            return True
        time.sleep(1)
    return False


def _has_noauth_label(actor: dict) -> bool:
    return any(
        l.get("val") == "!no-unauthenticated" and not l.get("neg")
        for l in actor.get("labels", [])
    )


def find_noauth_actor(base_url: str) -> bool:
    """verify a known !no-unauthenticated actor IS included in our results.
    fixtures are validated live (label still applied per getProfile) before use."""
    for did in NOAUTH_FIXTURES:
        profile, _ = fetch(f"{BSKY_PUBLIC}/xrpc/app.bsky.actor.getProfile?actor={did}")
        if not profile or "_error" in profile or not _has_noauth_label(profile):
            continue  # label removed or lookup failed — fixture is stale, try the next
        if _verify_noauth_inclusion(base_url, profile.get("handle", "")):
            return True
    return False


LINK_DID = "did:plc:63hvnyjvqi2nzzcsjgnry5we"
LINK_HANDLE = "spacelawshitpost.me"


def admin_fetch(url: str, secret: str, method: str = "GET", body: dict | None = None) -> tuple[dict | None, int]:
    """fetch with admin auth. returns (body, status_code)."""
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "typeahead-smoke/1.0",
            "Authorization": f"Bearer {secret}",
            **({"Content-Type": "application/json"} if data else {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return None, e.code


def test_link_visible(base_url: str):
    """verify Link (spacelawshitpost.me) is visible — requires a permanent show override."""
    print("\n--- Link visibility ---")
    data, _ = fetch(f"{base_url}{XRPC_PATH}?q=spacelawshitpost&limit=5")
    if not data or "_error" in data or "_http_error" in data:
        check("search for Link", False, f"got {data}")
        return
    handles = [a.get("handle", "") for a in data.get("actors", [])]
    check(f"Link visible in search", LINK_HANDLE in handles, f"got {handles}")


def test_mod_overrides(base_url: str, secret: str):
    """exercise the override admin CRUD lifecycle."""
    print("\n--- mod_overrides admin API ---")

    # 1. ensure Link is indexed
    data, _ = fetch(f"{base_url}/request-indexing?handle={LINK_HANDLE}", method="POST")
    if not data or "_error" in data:
        check("index Link", False, f"got {data}")
        return
    check("index Link", data.get("did") == LINK_DID, f"got {data.get('did')}")

    # 2. set show override
    data, status = admin_fetch(
        f"{base_url}/admin/mod-override", secret, "POST",
        {"did": LINK_DID, "action": "show", "reason": "FREE LINK"},
    )
    check("set show override", status == 200 and data and data.get("action") == "show",
          f"status={status}, got {data}")
    if data:
        check("override sets hidden=0", data.get("hidden") == 0, f"got hidden={data.get('hidden')}")

    # 3. validate input — bad action should 400
    data, status = admin_fetch(
        f"{base_url}/admin/mod-override", secret, "POST",
        {"did": LINK_DID, "action": "yolo"},
    )
    check("bad action returns 400", status == 400, f"status={status}")

    # 4. list overrides — Link should be there
    data, status = admin_fetch(f"{base_url}/admin/mod-overrides", secret)
    if data:
        override_dids = [o.get("did") for o in data.get("overrides", [])]
        check("Link in override list", LINK_DID in override_dids)
    else:
        check("list overrides", False, f"status={status}")

    # 5. delete override — hidden recomputed from labels
    data, status = admin_fetch(f"{base_url}/admin/mod-override?did={LINK_DID}", secret, "DELETE")
    check("delete override", status == 200, f"status={status}, got {data}")

    # 6. verify override is gone
    data, status = admin_fetch(f"{base_url}/admin/mod-overrides", secret)
    if data:
        override_dids = [o.get("did") for o in data.get("overrides", [])]
        check("Link removed from overrides", LINK_DID not in override_dids)
    else:
        check("list overrides after delete", False, f"status={status}")

    # 7. re-set the override — Link should stay visible permanently
    data, status = admin_fetch(
        f"{base_url}/admin/mod-override", secret, "POST",
        {"did": LINK_DID, "action": "show", "reason": "FREE LINK"},
    )
    check("re-set show override", status == 200 and data and data.get("hidden") == 0,
          f"status={status}, got {data}")


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


def test_sync_keyset_plan():
    """The sync drain's paged keyset query must plan as an index SEARCH on
    Turso *with bound parameters*. The plain OR keyset form planned fine with
    literals but degraded to a full index SCAN of the ~8M-row corpus when the
    values arrived as hrana params, blowing the 60s fetch deadline on every
    page-2 query (Aug 3-14 2026 staleness incident). Stock local SQLite plans
    both forms fine, so only a live EXPLAIN against Turso can catch it."""
    print("\n--- turso keyset query plan ---")
    turso_url = os.environ.get("TURSO_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not turso_url or not token:
        print(f"  [{SKIP}] skipped (set TURSO_URL + TURSO_AUTH_TOKEN)")
        return
    if turso_url.startswith("libsql://"):
        turso_url = "https://" + turso_url.removeprefix("libsql://")

    sql = (
        "EXPLAIN QUERY PLAN SELECT did FROM actors WHERE handle != '' "
        "AND updated_at >= ?1 AND (updated_at > ?1 OR did > ?2) "
        "ORDER BY updated_at, did LIMIT 500"
    )
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [
                {"type": "text", "value": "1786740000"},
                {"type": "text", "value": "did:plc:aaaa"},
            ]}},
            {"type": "close"},
        ]
    }).encode()
    try:
        req = urllib.request.Request(
            f"{turso_url}/v2/pipeline", data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        rows = result["results"][0]["response"]["result"]["rows"]
        details = [r[3]["value"] for r in rows]
    except Exception as e:
        check("keyset plan fetch", False, str(e))
        return
    plan = "; ".join(details)
    check("keyset page query is an index SEARCH (not SCAN)",
          any("SEARCH" in d for d in details) and not any("SCAN" in d for d in details),
          plan)


def test_cron_selector_plans():
    """THE CRON CONTRACT: every cron selector must plan as an index SEARCH on
    Turso, never a corpus scan. The moderation-refresh selector used to run two
    full-table ORDER BY scans over ~11M rows every hour; these EXPLAINs pin its
    replacement (quality-keyset walk + firehose event queue, src/cron.ts) to
    the indexes that serve them. Like test_sync_keyset_plan, only a live
    EXPLAIN against Turso is trustworthy — local SQLite plans differently."""
    print("\n--- turso cron selector plans ---")
    turso_url = os.environ.get("TURSO_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not turso_url or not token:
        print(f"  [{SKIP}] skipped (set TURSO_URL + TURSO_AUTH_TOKEN)")
        return
    if turso_url.startswith("libsql://"):
        turso_url = "https://" + turso_url.removeprefix("libsql://")

    # each selector must be served by its named index with the ORDER BY coming
    # from index order (no temp b-tree — a sort spill means the LIMIT no longer
    # bounds the work and the whole matching set gets materialized). The event
    # queue legitimately plans as `SCAN ... USING INDEX`: SQLite says SCAN when
    # there is no constraint on the index's key column, but the partial index
    # predicate does the filtering and LIMIT + index order cap it at 200
    # entries. The dangerous plan is a scan of the TABLE or the wrong index.
    selectors = {
        # quality walk: keyset literals inlined exactly as src/cron.ts builds them
        "quality walk uses idx_actors_visible_quality_score": (
            "EXPLAIN QUERY PLAN SELECT did FROM actors "
            "WHERE handle != '' AND hidden = 0 AND quality_score > 0 "
            "AND (quality_score < 123.45 OR (quality_score = 123.45 AND rowid < 678)) "
            "ORDER BY quality_score DESC, rowid DESC LIMIT 800",
            "idx_actors_visible_quality_score",
        ),
        "event queue uses idx_actors_refresh_queue": (
            "EXPLAIN QUERY PLAN SELECT did FROM actors "
            "WHERE profile_checked_at = 0 AND handle != '' "
            "ORDER BY updated_at DESC LIMIT 200",
            "idx_actors_refresh_queue",
        ),
    }
    for label, (sql, index_name) in selectors.items():
        body = json.dumps({
            "requests": [
                {"type": "execute", "stmt": {"sql": sql}},
                {"type": "close"},
            ]
        }).encode()
        try:
            req = urllib.request.Request(
                f"{turso_url}/v2/pipeline", data=body, method="POST",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
            rows = result["results"][0]["response"]["result"]["rows"]
            details = [r[3]["value"] for r in rows]
        except Exception as e:
            check(f"{label} (plan fetch)", False, str(e))
            continue
        plan = "; ".join(details)
        check(label,
              any(f"USING INDEX {index_name}" in d for d in details)
              and not any("TEMP B-TREE" in d.upper() for d in details),
              plan)


def main():
    parser = argparse.ArgumentParser(description="typeahead smoke tests")
    parser.add_argument("--url", required=True, help="typeahead service URL")
    parser.add_argument("--secret", default=os.environ.get("TYPEAHEAD_SECRET", ""),
                        help="admin secret (or set TYPEAHEAD_SECRET env var)")
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
    test_canonical_endpoint(args.url)
    test_profiles_shape(args.url)
    test_known_handle(args.url)
    test_prefix_match(args.url)
    test_ranking(args.url)
    test_cors(args.url)
    test_deprecated_param(args.url)
    test_limit_bounds(args.url)
    test_empty_query(args.url)
    test_limit_over_max(args.url)
    test_stats_page(args.url)
    test_request_indexing(args.url)
    test_moderation_filtering(args.url)
    test_link_visible(args.url)

    if args.secret:
        test_mod_overrides(args.url, args.secret)
    else:
        print(f"\n--- mod_overrides admin API ---")
        print(f"  [{SKIP}] skipped (use --secret or set TYPEAHEAD_SECRET)")

    test_page_scripts(args.url)
    test_sync_keyset_plan()
    test_cron_selector_plans()

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
