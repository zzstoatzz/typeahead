#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""
benchmark: typeahead vs bluesky searchActorsTypeahead.

two modes:

  default (perf) — latency (cold + warm), coverage/overlap, field
    completeness, display-name search, and stress-tests our API under
    concurrent load. hits whatever URL `--url` points at (worker by default).

  --ranking — focused ranking-quality run against a canonical query set,
    side-by-side with bluesky as oracle. when paired with `--appb-direct`,
    additionally fetches /debug/search for per-candidate score breakdowns.
    exits 1 if any acceptance criterion fails.

usage:
    just bench                                       # perf mode against prod worker
    just bench-ranking                               # ranking mode against search service direct
    ./scripts/bench.py --quick                       # 10 queries, 1 run
    ./scripts/bench.py --no-stress                   # skip stress test
    ./scripts/bench.py --queries nate boorkie        # specific queries only
    ./scripts/bench.py --ranking                     # ranking mode via worker
    ./scripts/bench.py --ranking --appb-direct \
        --url https://typeahead-search.fly.dev       # ranking + /debug/search
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

OURS_DEFAULT = "https://typeahead.waow.tech"
BSKY = "https://public.api.bsky.app"
XRPC = "/xrpc/app.bsky.actor.searchActorsTypeahead"

# colors
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

# canonical ranking-quality query set. each entry exists for a specific reason
# (regression we hunted down, behavior we want to lock in). extend this list
# rather than discard entries — once a query exposes a ranking bug, it should
# guard against the same bug forever.
RANKING_CORPUS = [
    "fig",              # display-name match dominates bsky top
    "fig (",            # punctuation must collapse CONSISTENTLY: same set as "fig"
    "fig aka",          # multi-token display-name
    "phil",             # bad-example.com has phil in display name
    "zzstoatzz",        # known handle prefix
    "zzstoatzz.io",     # exact full handle — must rank 1
    "nate",             # common prefix
    "alex",             # common prefix, easily spammed
    "bob",              # short common prefix
    "aoc",              # popular account
    "car",              # short, ambiguous
]

# spam heuristic for flagging "obvious junk" in top-3 (matches ingester's
# suspiciousNumberedHandle but expressed as a regex for portability).
NUMBERED_HANDLE_RE = re.compile(r"^[a-z]+[-_]?\d+(\.|$)", re.IGNORECASE)


def looks_like_spam(actor: dict) -> bool:
    if NUMBERED_HANDLE_RE.match(actor.get("handle", "")):
        return True
    toks = (actor.get("displayName") or "").lower().split()
    if len(toks) >= 3 and max(toks.count(t) for t in set(toks)) >= 3:
        return True
    return False


FULL_CORPUS = [
    # short prefixes
    "a", "na", "sky", "bl", "j",
    # common names
    "nate", "sarah", "alex", "dan", "paul", "sam", "chris", "jordan", "mike", "anna",
    # display-name-only terms (no matching handle expected)
    "boorkie", "kohari",
    # specific handles
    "zzstoatzz", "pfrazee", "jay.bsky.team", "alice", "bob",
    # unicode
    "André", "naïve", "café",
    # multi-word display names
    "nate kohari", "paul frazee",
    # edge cases
    "nate.io", "the", "test", "bot", "news", "art", "dev", "music",
    # longer/rarer
    "photographer", "designer", "engineer", "journalist",
    # more diversity
    "tokyo", "berlin", "podcast", "crypto", "gaming",
]

QUICK_CORPUS = [
    "nate", "zzstoatzz", "paul", "boorkie", "sky",
    "sarah", "André", "nate kohari", "a", "dev",
]

DISPLAY_NAME_QUERIES = [
    "boorkie", "kohari", "nate kohari", "paul frazee",
]

FIELDS_TO_CHECK = ["displayName", "avatar", "createdAt", "associated"]


@dataclass
class LatencyStats:
    query: str
    ours_cold_ms: list[float] = field(default_factory=list)
    ours_warm_ms: list[float] = field(default_factory=list)
    bsky_ms: list[float] = field(default_factory=list)

    def _summarize(self, ms: list[float]) -> dict:
        if not ms:
            return {}
        s = sorted(ms)
        return {
            "min": round(min(s), 1),
            "max": round(max(s), 1),
            "mean": round(statistics.mean(s), 1),
            "p50": round(s[len(s) // 2], 1),
            "p95": round(s[int(len(s) * 0.95)], 1) if len(s) >= 2 else round(max(s), 1),
        }

    def summarize(self, side: str) -> dict:
        if side == "ours_cold":
            return self._summarize(self.ours_cold_ms)
        if side == "ours_warm":
            return self._summarize(self.ours_warm_ms)
        return self._summarize(self.bsky_ms)


@dataclass
class CoverageResult:
    query: str
    ours_actors: list[dict]
    bsky_actors: list[dict]
    ours_dids: list[str]
    bsky_dids: list[str]
    overlap: list[str]
    ours_extras: list[str]
    bsky_extras: list[str]
    rank_deltas: list[int]


@dataclass
class StressResult:
    concurrency: int
    total: int
    ok: int
    rate_limited: int
    errors: int
    latencies_ms: list[float] = field(default_factory=list)


@dataclass
class RankingResult:
    query: str
    bsky_actors: list[dict]
    ours_actors: list[dict]
    debug: dict | None  # /debug/search response (None if not fetched or failed)
    bsky_status: int
    ours_status: int
    ours_ms: float

    @property
    def bsky_top_handle(self) -> str | None:
        if not self.bsky_actors:
            return None
        return self.bsky_actors[0].get("handle", "").lower() or None

    @property
    def ours_handles(self) -> set[str]:
        return {a.get("handle", "").lower() for a in self.ours_actors}

    @property
    def parity_top_in_ours(self) -> bool:
        top = self.bsky_top_handle
        return top is None or top in self.ours_handles


def progress(label: str, done: int, total: int):
    sys.stdout.write(f"\r  {label}: {done}/{total}")
    sys.stdout.flush()


def clear_line():
    sys.stdout.write("\r" + " " * 70 + "\r")


async def timed_fetch(
    client: httpx.AsyncClient, url: str, timeout: float = 30.0
) -> tuple[dict | None, float, int]:
    """fetch JSON, return (body, latency_ms, status_code)."""
    t0 = time.monotonic()
    try:
        r = await client.get(url, timeout=timeout)
        ms = (time.monotonic() - t0) * 1000
        if r.status_code == 200:
            return r.json(), ms, r.status_code
        return None, ms, r.status_code
    except Exception:
        ms = (time.monotonic() - t0) * 1000
        return None, ms, 0


def search_url(base: str, q: str, limit: int = 10) -> str:
    return f"{base}{XRPC}?q={quote(q)}&limit={limit}"


def ours_search_url(base: str, q: str, limit: int, appb_direct: bool) -> str:
    """Build the search URL for "ours" — XRPC path via worker, /search via search service."""
    if appb_direct:
        return f"{base}/search?q={quote(q)}&limit={limit}"
    return search_url(base, q, limit)


def debug_search_url(base: str, q: str, limit: int = 25) -> str:
    """Debug endpoint is search service-only; not exposed through the worker."""
    return f"{base}/debug/search?q={quote(q)}&limit={limit}"


async def run_latency(
    client: httpx.AsyncClient,
    ours_url: str,
    corpus: list[str],
    runs: int,
    appb_direct: bool = False,
) -> list[LatencyStats]:
    """sequential latency: cold (cache-busted) + warm (cached) + bsky baseline.

    appb_direct hits the search service's /search (engine latency, no edge
    cache) instead of the worker's /xrpc. In that mode "warm" measures the
    engine on a repeated query, not a CF cache hit — there is no edge cache."""
    results = []
    # cold: runs * 2 reqs/query (ours+bsky), warm: 2 reqs/query (ours+bsky)
    total = len(corpus) * (runs * 2 + 2)
    done = 0

    for q in corpus:
        stats = LatencyStats(query=q)

        # cold runs: vary limit to bust CF cache
        for run_i in range(runs):
            limit = 8 + (run_i % 3)

            _, ms, status = await timed_fetch(client, ours_search_url(ours_url, q, limit, appb_direct))
            if status == 200:
                stats.ours_cold_ms.append(ms)
            done += 1
            progress("latency", done, total)
            await asyncio.sleep(1.05)

            _, ms, status = await timed_fetch(client, search_url(BSKY, q, limit))
            if status == 200:
                stats.bsky_ms.append(ms)
            done += 1
            progress("latency", done, total)
            await asyncio.sleep(0.2)

        # warm run: repeat exact same request (limit=10, should hit CF cache)
        _, ms, status = await timed_fetch(client, ours_search_url(ours_url, q, 10, appb_direct))
        done += 1
        progress("latency", done, total)
        await asyncio.sleep(1.05)
        # second hit — this one should be cached
        _, ms, status = await timed_fetch(client, ours_search_url(ours_url, q, 10, appb_direct))
        if status == 200:
            stats.ours_warm_ms.append(ms)
        done += 1
        progress("latency", done, total)
        await asyncio.sleep(1.05)

        results.append(stats)

    clear_line()
    return results


async def run_coverage_and_fields(
    client: httpx.AsyncClient,
    ours_url: str,
    corpus: list[str],
    appb_direct: bool = False,
) -> tuple[list[CoverageResult], dict]:
    """compare result sets at limit=10; also compute field completeness."""
    coverage_results = []

    # aggregate field counts
    ours_field_counts = {f: 0 for f in FIELDS_TO_CHECK}
    bsky_field_counts = {f: 0 for f in FIELDS_TO_CHECK}
    ours_actor_total = 0
    bsky_actor_total = 0

    for i, q in enumerate(corpus):
        progress("coverage+fields", i + 1, len(corpus))

        ours_data, _, _ = await timed_fetch(client, ours_search_url(ours_url, q, 10, appb_direct))
        await asyncio.sleep(1.05)
        bsky_data, _, _ = await timed_fetch(client, search_url(BSKY, q))
        await asyncio.sleep(0.2)

        ours_actors = (ours_data or {}).get("actors", [])
        bsky_actors = (bsky_data or {}).get("actors", [])

        ours_dids = [a["did"] for a in ours_actors]
        bsky_dids = [a["did"] for a in bsky_actors]

        ours_set = set(ours_dids)
        bsky_set = set(bsky_dids)
        overlap = list(ours_set & bsky_set)

        ours_pos = {d: i for i, d in enumerate(ours_dids)}
        bsky_pos = {d: i for i, d in enumerate(bsky_dids)}
        rank_deltas = [abs(ours_pos[d] - bsky_pos[d]) for d in overlap if d in ours_pos and d in bsky_pos]

        coverage_results.append(CoverageResult(
            query=q,
            ours_actors=ours_actors,
            bsky_actors=bsky_actors,
            ours_dids=ours_dids,
            bsky_dids=bsky_dids,
            overlap=overlap,
            ours_extras=list(ours_set - bsky_set),
            bsky_extras=list(bsky_set - ours_set),
            rank_deltas=rank_deltas,
        ))

        # field completeness
        ours_actor_total += len(ours_actors)
        bsky_actor_total += len(bsky_actors)
        for f in FIELDS_TO_CHECK:
            ours_field_counts[f] += sum(1 for a in ours_actors if a.get(f))
            bsky_field_counts[f] += sum(1 for a in bsky_actors if a.get(f))

    clear_line()

    field_summary = {
        "ours_total": ours_actor_total,
        "bsky_total": bsky_actor_total,
        "ours": ours_field_counts,
        "bsky": bsky_field_counts,
    }
    return coverage_results, field_summary


async def run_display_name_check(
    client: httpx.AsyncClient,
    ours_url: str,
    queries: list[str],
    appb_direct: bool = False,
) -> list[dict]:
    """verify display-name-only queries return results."""
    results = []
    for q in queries:
        ours_data, _, _ = await timed_fetch(client, ours_search_url(ours_url, q, 10, appb_direct))
        await asyncio.sleep(1.05)
        bsky_data, _, _ = await timed_fetch(client, search_url(BSKY, q))
        await asyncio.sleep(0.2)

        ours_actors = (ours_data or {}).get("actors", [])
        bsky_actors = (bsky_data or {}).get("actors", [])
        results.append({
            "query": q,
            "ours_count": len(ours_actors),
            "bsky_count": len(bsky_actors),
            "found": len(ours_actors) > 0,
            "ours_sample": [a.get("handle", a.get("did", "?")) for a in ours_actors[:3]],
            "bsky_sample": [a.get("handle", a.get("did", "?")) for a in bsky_actors[:3]],
        })

    return results


async def run_ranking(
    client: httpx.AsyncClient,
    ours_url: str,
    corpus: list[str],
    appb_direct: bool,
    limit: int = 8,
) -> list[RankingResult]:
    """Hit bsky + ours (+ /debug/search if appb_direct) for each canonical query."""
    results = []
    for i, q in enumerate(corpus):
        progress("ranking", i + 1, len(corpus))

        bsky_data, _, bs = await timed_fetch(client, search_url(BSKY, q, limit), timeout=6.0)
        await asyncio.sleep(0.2)
        ours_data, ours_ms, oss = await timed_fetch(
            client, ours_search_url(ours_url, q, limit, appb_direct), timeout=6.0
        )

        debug_data = None
        if appb_direct:
            await asyncio.sleep(0.1)
            dd, _, ds = await timed_fetch(client, debug_search_url(ours_url, q, 25), timeout=6.0)
            if ds == 200:
                debug_data = dd

        results.append(RankingResult(
            query=q,
            bsky_actors=(bsky_data or {}).get("actors", []),
            ours_actors=(ours_data or {}).get("actors", []),
            debug=debug_data,
            bsky_status=bs,
            ours_status=oss,
            ours_ms=ours_ms,
        ))
        await asyncio.sleep(0.4)

    clear_line()
    return results


def acceptance_checks(results: list[RankingResult]) -> list[str]:
    """Return list of acceptance-criterion failures. Empty list = pass.

    Two tiers of criteria:

      Structural — must always pass. Failure means the search infrastructure
      itself is broken: wrong response, exact handle ranked wrong, etc.

      Parity — bsky's top result must appear in our top 8 for the listed
      queries. These are the queries that define whether typeahead "feels
      real or spammy". They are EXPECTED to fail until authority data
      coverage is real; that's deliberate — PASS shouldn't be possible
      to achieve by tuning lexical weights alone.
    """
    failures = []

    # parity queries: bsky's top result must be findable in ours top 8.
    # Listed explicitly rather than "all canonical queries" so future-me
    # has to consciously add or remove an entry, not silently soften.
    PARITY_QUERIES = {"fig", "phil", "nate", "alex", "bob", "aoc", "car"}

    by_query = {r.query: r for r in results}

    for r in results:
        # ── structural ──
        if r.ours_status != 200:
            failures.append(f"[structural] non-200 from ours for q={r.query!r} (status={r.ours_status})")
            continue

        if r.query == "zzstoatzz.io" and r.ours_actors:
            top = r.ours_actors[0].get("handle", "").lower()
            if top != "zzstoatzz.io":
                failures.append(f"[structural] zzstoatzz.io not rank 1 for q={r.query!r} (got {top!r})")

        # Display-name reachability. `bad-example.com` is named "fig (aka:[phil])",
        # so a multi-token display-name query must find it: `n:fig ∩ n:aka`
        # intersects to exactly that actor. This is the assertion with teeth —
        # if display-name tokens stop being indexed or intersect breaks, it fires.
        if r.query == "fig aka":
            if not any(a.get("handle", "").lower() == "bad-example.com" for a in r.ours_actors):
                failures.append(f"[structural] bad-example.com missing from ours top {len(r.ours_actors)} for q={r.query!r}")

        # `fig (` asserted bad-example.com in the top 8 until 2026-08-12. That
        # was an FTS-era assertion: it needed `raw_phrase_prefix_name` to score
        # the raw string, and the prefix index deliberately dropped per-request
        # lexical scoring ("just has-all-tokens + authority ranking" —
        # docs/typeahead-index-design.md). `(` is a separator in normalize.query,
        # so `fig (` IS `fig`, and a 458-score actor loses that union to
        # high-authority fig* accounts by design. Ranking quality is Track 2
        # (pagerank → quality_score), not a lexical-weight knob.
        #
        # What punctuation must still guarantee is that it collapses
        # CONSISTENTLY — a normalize or key-generation drift that made `fig (`
        # diverge from `fig` is a real bug, and this catches it.
        if r.query == "fig (":
            plain = by_query.get("fig")
            if plain and r.ours_handles != plain.ours_handles:
                failures.append(
                    f"[structural] q='fig (' diverged from q='fig' — punctuation must "
                    f"normalize away (got {r.ours_handles[:3]} vs {plain.ours_handles[:3]})"
                )

        # ── parity ──
        if r.query in PARITY_QUERIES:
            bsky_top = r.bsky_top_handle
            if bsky_top and bsky_top not in r.ours_handles:
                failures.append(
                    f"[parity] bsky's top ({bsky_top!r}) missing from ours top {len(r.ours_actors)} "
                    f"for q={r.query!r}"
                )

        # spam guard (kept as structural — if it fires, something is seriously wrong)
        if r.query in ("alex", "nate", "bob"):
            top = (r.bsky_actors[:1] or [{}])[0]
            if top and not looks_like_spam(top):
                top3_spam = sum(1 for a in r.ours_actors[:3] if looks_like_spam(a))
                if top3_spam >= 2:
                    failures.append(f"[structural] ours top 3 dominated by spam for q={r.query!r} ({top3_spam}/3 flagged)")
    return failures


async def run_stress(
    client: httpx.AsyncClient,
    ours_url: str,
    corpus: list[str],
    levels: list[int],
    appb_direct: bool = False,
) -> list[StressResult]:
    """concurrent request stress test (our API only)."""
    results = []

    for n in levels:
        sys.stdout.write(f"\r  stress: concurrency={n}...")
        sys.stdout.flush()

        queries = (corpus * ((n // len(corpus)) + 1))[:n]
        tasks = [
            timed_fetch(client, ours_search_url(ours_url, q, 7 + (i % 4), appb_direct))
            for i, q in enumerate(queries)
        ]

        responses = await asyncio.gather(*tasks)

        sr = StressResult(concurrency=n, total=n, ok=0, rate_limited=0, errors=0)
        for body, ms, status in responses:
            sr.latencies_ms.append(ms)
            if status == 200:
                sr.ok += 1
            elif status == 429:
                sr.rate_limited += 1
            else:
                sr.errors += 1

        results.append(sr)
        await asyncio.sleep(5)

    clear_line()
    return results


# ── printing ────────────────────────────────────────────────────────

def pct(n: int, total: int) -> str:
    return f"{n * 100 / total:.0f}%" if total else "n/a"


def fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms:.0f}ms"


def print_latency_table(stats_list: list[LatencyStats]):
    print(f"\n{BOLD}--- latency (cold, cache-busted) ---{RESET}")
    header = f"  {'query':<20} {'ours':>10} {'bsky':>10} {'delta':>10} {'winner':>8}"
    print(header)
    print(f"  {'─' * 20} {'─' * 10} {'─' * 10} {'─' * 10} {'─' * 8}")

    all_ours_cold = []
    all_bsky = []
    ours_wins = 0
    total_compared = 0

    for s in stats_list:
        oc = s.summarize("ours_cold")
        b = s.summarize("bsky")
        if not oc or not b:
            continue

        all_ours_cold.extend(s.ours_cold_ms)
        all_bsky.extend(s.bsky_ms)

        op50, bp50 = oc["p50"], b["p50"]
        delta = op50 - bp50
        total_compared += 1
        if delta < 0:
            ours_wins += 1
            w_str = f"{GREEN}ours{RESET}"
        elif delta > 0:
            w_str = f"{RED}bsky{RESET}"
        else:
            w_str = "tie"

        d_str = f"{'+' if delta > 0 else ''}{fmt_ms(abs(delta)) if delta >= 0 else '-' + fmt_ms(abs(delta))}"
        print(f"  {s.query:<20} {fmt_ms(op50):>10} {fmt_ms(bp50):>10} {d_str:>10} {w_str:>17}")

    if all_ours_cold and all_bsky:
        oc50 = sorted(all_ours_cold)[len(all_ours_cold) // 2]
        oc95 = sorted(all_ours_cold)[int(len(all_ours_cold) * 0.95)]
        b50 = sorted(all_bsky)[len(all_bsky) // 2]
        b95 = sorted(all_bsky)[int(len(all_bsky) * 0.95)]
        print()
        print(f"  {BOLD}cold:{RESET}  ours p50={fmt_ms(oc50)} p95={fmt_ms(oc95)} | bsky p50={fmt_ms(b50)} p95={fmt_ms(b95)}")
        print(f"  ours faster on {ours_wins}/{total_compared} queries (cold)")

    # warm summary
    all_warm = []
    for s in stats_list:
        all_warm.extend(s.ours_warm_ms)
    if all_warm:
        w50 = sorted(all_warm)[len(all_warm) // 2]
        w95 = sorted(all_warm)[int(len(all_warm) * 0.95)] if len(all_warm) >= 2 else max(all_warm)
        print(f"  {BOLD}warm:{RESET}  ours p50={fmt_ms(w50)} p95={fmt_ms(w95)} (repeat query — CF cache hit via worker; engine warm via --appb-direct)")


def print_coverage_table(results: list[CoverageResult]):
    print(f"\n{BOLD}--- coverage ---{RESET}")
    print(f"  {'query':<20} {'overlap':>10} {'ours':>6} {'bsky':>6} {'pct':>6} {'rank Δ':>8}")
    print(f"  {'─' * 20} {'─' * 10} {'─' * 6} {'─' * 6} {'─' * 6} {'─' * 8}")

    total_overlap = 0
    total_bsky = 0
    complete_misses = 0
    we_have_more = 0

    for r in results:
        n_overlap = len(r.overlap)
        n_bsky = len(r.bsky_dids)
        n_ours = len(r.ours_dids)
        total_overlap += n_overlap
        total_bsky += n_bsky

        p = f"{n_overlap * 100 // n_bsky}%" if n_bsky else "n/a"
        avg_delta = f"{statistics.mean(r.rank_deltas):.1f}" if r.rank_deltas else "—"

        if n_ours == 0 and n_bsky > 0:
            complete_misses += 1
        if n_ours > n_bsky:
            we_have_more += 1

        print(f"  {r.query:<20} {n_overlap:>3}/{n_bsky:<6} {n_ours:>6} {n_bsky:>6} {p:>6} {avg_delta:>8}")

    print()
    mean_pct = total_overlap * 100 / total_bsky if total_bsky else 0
    print(f"  {BOLD}mean overlap:{RESET} {mean_pct:.0f}%")
    print(f"  complete misses (ours=0, bsky>0): {complete_misses}/{len(results)}")
    print(f"  queries where we have more results: {we_have_more}/{len(results)}")


def print_field_table(field_summary: dict):
    print(f"\n{BOLD}--- field completeness ---{RESET}")
    ours_total = field_summary["ours_total"]
    bsky_total = field_summary["bsky_total"]

    print(f"  {'field':<16} {'ours':>8} {'bsky':>8}")
    print(f"  {'─' * 16} {'─' * 8} {'─' * 8}")
    for f in FIELDS_TO_CHECK:
        o = pct(field_summary["ours"][f], ours_total)
        b = pct(field_summary["bsky"][f], bsky_total)
        print(f"  {f:<16} {o:>8} {b:>8}")
    print(f"  {'─' * 16} {'─' * 8} {'─' * 8}")
    print(f"  {'total actors':<16} {ours_total:>8} {bsky_total:>8}")


def print_display_name_table(results: list[dict]):
    print(f"\n{BOLD}--- display name search ---{RESET}")
    print(f"  {'query':<20} {'found?':>8} {'ours':>6} {'bsky':>6}  samples")
    print(f"  {'─' * 20} {'─' * 8} {'─' * 6} {'─' * 6}  {'─' * 30}")
    for r in results:
        found = f"{GREEN}yes{RESET}" if r["found"] else f"{RED}no{RESET}"
        samples = ", ".join(r["ours_sample"][:3]) if r["ours_sample"] else "—"
        print(f"  {r['query']:<20} {found:>17} {r['ours_count']:>6} {r['bsky_count']:>6}  {samples}")


def _fmt_actor_line(idx: int, actor: dict) -> str:
    handle = (actor.get("handle") or "?")[:36]
    name = (actor.get("displayName") or "")[:42]
    flag = f" {YELLOW}[spam?]{RESET}" if looks_like_spam(actor) else ""
    return f"  {idx:2d}. {handle:36s}  {name}{flag}"


def _fmt_breakdown(b: dict) -> str:
    parts = []
    for k, v in b.items():
        if not v:
            continue
        formatted = int(v) if isinstance(v, (int, float)) and v == int(v) else round(v, 1)
        parts.append(f"{k}={formatted}")
    return " ".join(parts) if parts else "(no signal)"


def print_ranking_results(results: list[RankingResult]) -> None:
    print(f"\n{BOLD}--- ranking quality ---{RESET}")
    for r in results:
        print()
        print("=" * 82)
        print(f"  q={r.query!r}    ours={fmt_ms(r.ours_ms)}    bsky status={r.bsky_status} ours status={r.ours_status}")
        print("=" * 82)

        print(f"  {BOLD}bsky{RESET}")
        if not r.bsky_actors:
            print("    (no results)")
        for i, a in enumerate(r.bsky_actors, 1):
            print(_fmt_actor_line(i, a))

        print(f"  {BOLD}ours{RESET}")
        if not r.ours_actors:
            print("    (no results)")
        for i, a in enumerate(r.ours_actors, 1):
            print(_fmt_actor_line(i, a))

        if r.bsky_top_handle:
            if r.parity_top_in_ours:
                print(f"  [{GREEN}ok{RESET}]   bsky's top ({r.bsky_top_handle}) is in ours top {len(r.ours_actors)}")
            else:
                print(f"  [{RED}FAIL{RESET}] bsky's top ({r.bsky_top_handle}) is MISSING from ours top {len(r.ours_actors)}")

        if r.debug:
            cands = (r.debug.get("candidates") or [])[:8]
            if cands:
                # Two engines, two explanations. The index path ranks on authority
                # alone (docs/typeahead-index-design.md), so there are no lexical
                # components to print — what explains a result there is which keys
                # were consulted and how deep their top-N lists go. Rendering the
                # FTS fields against an index response printed "(no signal)" for
                # every row, which reads like a broken scorer rather than a
                # different one.
                if r.debug.get("engine") == "index":
                    keys = " ".join(
                        f"{k['key']}={k['base']}+{k['overlay']}" for k in (r.debug.get("keys") or [])
                    )
                    print(f"  {BOLD}index path{RESET}  norm={r.debug.get('normalized')!r}  "
                          f"mode={r.debug.get('mode')}  merged={r.debug.get('merged')}"
                          f"/{r.debug.get('hydrate_cap')}  keys: {keys}")
                    for i, c in enumerate(cands, 1):
                        handle = (c.get("handle") or "?")[:36]
                        name = (c.get("display_name") or "")[:30]
                        print(f"    {i:2d}. {handle:36s}  authority={int(c.get('score', 0)):>6d}  {name}")
                else:
                    print(f"  {BOLD}score breakdown (top 8 candidates from /debug/search){RESET}")
                    for i, c in enumerate(cands, 1):
                        handle = (c.get("handle") or "?")[:36]
                        score = int(c.get("score", 0))
                        src = c.get("source", "?")
                        f_count = c.get("followers", 0)
                        p_count = c.get("posts", 0)
                        br = c.get("breakdown") or {}
                        print(
                            f"    {i:2d}. {handle:36s}  score={score:>7d}  src={src:<6s}  "
                            f"f={f_count} p={p_count}  | {_fmt_breakdown(br)}"
                        )


def print_stress_table(results: list[StressResult]):
    print(f"\n{BOLD}--- stress test (ours only) ---{RESET}")
    print(f"  {'concurrency':>12} {'ok':>6} {'429s':>6} {'5xx':>6} {'p50':>8} {'p95':>8}")
    print(f"  {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 6} {'─' * 8} {'─' * 8}")
    for r in results:
        lats = sorted(r.latencies_ms)
        p50 = fmt_ms(lats[len(lats) // 2]) if lats else "—"
        p95 = fmt_ms(lats[int(len(lats) * 0.95)]) if len(lats) >= 2 else p50
        print(f"  {r.concurrency:>12} {r.ok:>6} {r.rate_limited:>6} {r.errors:>6} {p50:>8} {p95:>8}")


# ── JSON report ─────────────────────────────────────────────────────

def build_report(
    ours_url: str,
    corpus: list[str],
    runs: int,
    latency: list[LatencyStats],
    coverage: list[CoverageResult],
    field_summary: dict,
    display_name: list[dict],
    stress: list[StressResult],
) -> dict:
    def latency_entry(s: LatencyStats) -> dict:
        return {
            "query": s.query,
            "ours_cold": s.summarize("ours_cold"),
            "ours_warm": s.summarize("ours_warm"),
            "bsky": s.summarize("bsky"),
        }

    def coverage_entry(r: CoverageResult) -> dict:
        return {
            "query": r.query,
            "ours_count": len(r.ours_dids),
            "bsky_count": len(r.bsky_dids),
            "overlap_count": len(r.overlap),
            "overlap_pct": round(len(r.overlap) * 100 / len(r.bsky_dids), 1) if r.bsky_dids else None,
            "ours_extras": len(r.ours_extras),
            "bsky_extras": len(r.bsky_extras),
            "avg_rank_delta": round(statistics.mean(r.rank_deltas), 2) if r.rank_deltas else None,
        }

    def stress_entry(r: StressResult) -> dict:
        lats = sorted(r.latencies_ms)
        return {
            "concurrency": r.concurrency,
            "ok": r.ok,
            "rate_limited": r.rate_limited,
            "errors": r.errors,
            "p50_ms": round(lats[len(lats) // 2], 1) if lats else None,
            "p95_ms": round(lats[int(len(lats) * 0.95)], 1) if len(lats) >= 2 else None,
        }

    return {
        "meta": {
            "target": ours_url,
            "baseline": BSKY,
            "corpus_size": len(corpus),
            "runs": runs,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        "latency": [latency_entry(s) for s in latency],
        "coverage": [coverage_entry(r) for r in coverage],
        "field_completeness": field_summary,
        "display_name_search": display_name,
        "stress": [stress_entry(r) for r in stress],
    }


# ── main ────────────────────────────────────────────────────────────

async def run_ranking_mode(args: argparse.Namespace) -> int:
    """Run the ranking-quality benchmark and return shell exit code."""
    corpus = args.queries or RANKING_CORPUS

    print(f"\n{BOLD}=== ranking quality ==={RESET}")
    print(f"  target:   {args.url}{'  (direct search service)' if args.appb_direct else '  (via worker)'}")
    print(f"  oracle:   {BSKY}")
    print(f"  corpus:   {len(corpus)} queries")
    print(f"  debug:    {'yes (/debug/search)' if args.appb_direct else 'no (worker mode)'}")

    async with httpx.AsyncClient(
        headers={"User-Agent": "typeahead-bench/1.0", "X-Client": "bench-ranking"},
        follow_redirects=True,
    ) as client:
        results = await run_ranking(client, args.url, corpus, args.appb_direct)
        print_ranking_results(results)

        failures = acceptance_checks(results)

        print()
        print("=" * 82)
        if failures:
            print(f"{RED}{BOLD}FAIL{RESET} — {len(failures)} acceptance criteria failed:")
            for f in failures:
                print(f"  - {f}")
        else:
            print(f"{GREEN}{BOLD}PASS{RESET} — all acceptance criteria met. "
                  "proxy backstop can be flipped off cautiously.")
        print("=" * 82)
        return 1 if failures else 0


async def main():
    parser = argparse.ArgumentParser(description="typeahead benchmark — perf + ranking quality")
    parser.add_argument("--url", default=OURS_DEFAULT, help=f"our API URL (default: {OURS_DEFAULT})")
    parser.add_argument("--quick", action="store_true", help="10 queries, 1 run")
    parser.add_argument("--no-stress", action="store_true", help="skip stress test")
    parser.add_argument("--queries", nargs="+", help="specific queries only")
    parser.add_argument("--runs", type=int, default=3, help="runs per query for latency (default: 3)")
    parser.add_argument("--output", default="scripts/bench-results.json", help="JSON report path")
    parser.add_argument("--ranking", action="store_true",
                        help="run ranking-quality check (canonical corpus + acceptance criteria) "
                             "instead of perf phases")
    parser.add_argument("--appb-direct", action="store_true",
                        help="treat --url as search service (uses /search instead of /xrpc/..., enables "
                             "/debug/search fetching for ranking mode)")
    args = parser.parse_args()

    if args.quick:
        args.runs = 1

    if args.ranking:
        return await run_ranking_mode(args)

    corpus = args.queries or (QUICK_CORPUS if args.quick else FULL_CORPUS)
    dn_queries = [q for q in DISPLAY_NAME_QUERIES if q in corpus] or DISPLAY_NAME_QUERIES[:2]

    print(f"\n{BOLD}=== typeahead benchmark ==={RESET}")
    print(f"  target:   {args.url}{'  (direct search service — engine latency, no edge cache)' if args.appb_direct else '  (via worker — edge latency)'}")
    print(f"  baseline: {BSKY}")
    print(f"  corpus:   {len(corpus)} queries, {args.runs} run(s) each")
    print(f"  date:     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    async with httpx.AsyncClient(
        headers={"User-Agent": "typeahead-bench/1.0", "X-Client": "bench"},
        follow_redirects=True,
    ) as client:
        # 1. latency (cold + warm)
        print(f"\n{CYAN}[1/4] latency comparison (cold + warm){RESET}")
        latency = await run_latency(client, args.url, corpus, args.runs, args.appb_direct)
        print_latency_table(latency)

        # 2. coverage + field completeness (single pass)
        print(f"\n{CYAN}[2/4] coverage + field completeness{RESET}")
        coverage, field_summary = await run_coverage_and_fields(client, args.url, corpus, args.appb_direct)
        print_coverage_table(coverage)
        print_field_table(field_summary)

        # 3. display name search
        print(f"\n{CYAN}[3/4] display name search{RESET}")
        display_name = await run_display_name_check(client, args.url, dn_queries, args.appb_direct)
        print_display_name_table(display_name)

        # 4. stress test
        stress = []
        if args.no_stress:
            print(f"\n{CYAN}[4/4] stress test{RESET}")
            print(f"  {YELLOW}skipped{RESET}")
        else:
            print(f"\n{CYAN}[4/4] stress test (ours only){RESET}")
            stress = await run_stress(client, args.url, corpus, [5, 10, 20], args.appb_direct)
            print_stress_table(stress)

    # write report
    report = build_report(args.url, corpus, args.runs, latency, coverage, field_summary, display_name, stress)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n  full report: {out_path}")
    print()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
