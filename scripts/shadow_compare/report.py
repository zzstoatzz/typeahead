"""Write the dated JSON report and its Markdown summary."""

import json
from pathlib import Path

import corpus
import gates


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def ms(value):
    return "n/a" if value is None else f"{value:.2f}"


def labels_text(labels):
    return ", ".join(f"{count} {label}" for label, count in sorted((labels or {}).items()))


def identity_table(classes):
    rows = []
    for name in corpus.IDENTITY_CLASSES:
        entry = classes[name]
        rows.append([name, entry["count"], len(entry["sample"]), labels_text(entry.get("arbiter_labels")) or ("not run" if entry["count"] else "")])
    return table(["class", "count", "sampled", "arbiter labels for the sample"], rows)


def refresh_table(classes):
    rows = []
    for name in corpus.REFRESH_CLASSES:
        entry = classes[name]
        row = [name, entry["count"]]
        for stratum in corpus.STRATA:
            detail = entry["strata"][stratum]
            row += [detail["count"], detail["sampled"], labels_text(detail.get("arbiter_labels")) or ("not run" if detail["sampled"] else "")]
        row.append(entry["strata"][corpus.LAGGING].get("estimated_production_right", ""))
        rows.append(row)
    header = ["class", "count", "refreshed: count", "sampled", "arbiter labels", "not refreshed: count", "sampled", "arbiter labels", "estimated production right, not refreshed"]
    return table(header, rows)


HANDLE_KINDS = ("hosted handle", "custom domain", "no handle", "unknown")
PDS_KINDS = ("bluesky pds", "independent pds", "no pds", "unknown")
SCORE_BUCKET_TITLES = ("production holds 0", "shadow holds 0", "gap under 1", "gap 1 to 50", "gap 50 or more")
STRATUM_TITLES = {corpus.REFRESHED: "refreshed by the shadow since the watermark", corpus.LAGGING: "not refreshed by the shadow since the watermark"}


def breakdown_lines(classes, names):
    present = [name for name in names if classes[name]["count"]]
    if not present:
        return []
    rows, hosts = [], []
    for name in present:
        detail = classes[name]["breakdown"]
        rows.append([name, *(detail["handle_kind"].get(kind, 0) for kind in HANDLE_KINDS), *(detail["pds_kind"].get(kind, 0) for kind in PDS_KINDS)])
        top = ", ".join(f"{item['host'] or '(none)'} {item['count']}" for item in detail["top_pds_hosts"])
        rest = detail["other_pds_hosts"]
        hosts.append(f"- {name}: {top}" + (f", and {rest['count']} on {rest['hosts']} other hosts" if rest["hosts"] else ""))
    header = ["class", *HANDLE_KINDS[:3], "handle unknown", *PDS_KINDS[:3], "pds unknown"]
    return ["", "By handle and PDS of the actor (production's record when it has a live one, else the shadow's):", "",
            *table(header, rows), "", "Most frequent PDS hosts:", "", *hosts]


def snapshot_lines(result):
    if "snapshot" not in result:
        return ["Both sides are live, so no record counts as lagging: every refresh-derived difference is held to the gate."]
    snapshot = result["snapshot"]
    shadow = snapshot["shadow"]
    return [
        f"Production is a snapshot at watermark {snapshot['watermark']} and the shadow is live. A shadow record counts as refreshed since then "
        f"when its profile was last checked after {snapshot['refresh_threshold']}. {shadow['refreshed_since_watermark']} of {shadow['records']} "
        f"shadow records were; {shadow['records'] - shadow['with_refresh_time']} carry no refresh time and are held to the stricter reading. "
        f"{shadow['changed_since_watermark']} changed content since the watermark."
    ]


def marker_line(side, start, end):
    if start.get("snapshot"):
        return (
            f"{side.capitalize()} is bundle {start['build_id']} at watermark {start['watermark']}: {start['actor_count']} live actors, "
            f"{start['tombstone_count']} tombstones, {start['duplicate_dids']} duplicate DIDs dropped."
        )
    if "next_change" in start:
        return f"{side.capitalize()} store change-log position {start['next_change']} to {end['next_change']}."
    return f"{side.capitalize()} watermark {start['watermark']} to {end['watermark']}, build {start['build_id']} to {end['build_id']}."


def corpus_section(result):
    lines = ["## Corpus", ""]
    state = "complete" if result["complete"] else f"incomplete ({result['stop_reason']}), resume from the checkpoint"
    lines.append(
        f"Walk {state}: {result['dids_walked']} DIDs in {result['pages']} windows; "
        f"{result['live']['production']} live in production, {result['live']['shadow']} live in the shadow. "
        f"{result['differing_dids']} DIDs differ in at least one class."
    )
    if "seconds" in result:
        seconds = result["seconds"]
        lines.append(
            f"The walk spent {seconds['production_read']} s reading production, {seconds['shadow_read']} s reading the shadow "
            f"(the delay between requests included) and {seconds['compare']} s comparing."
        )
    kept = result["identity_differences"]
    if result.get("rechecked"):
        lines.append(
            f"{kept['tracked']} DIDs with an identity-level difference were kept and re-read after {result['settle_delay_s']} s; "
            f"{result['transient_differences']} of them no longer had one."
        )
        for side in ("production", "shadow"):
            lines.append(marker_line(side, result["start_markers"][side], result["end_markers"][side]))
    if kept["untracked"]:
        lines.append(f"{kept['untracked']} identity-level differences beyond the cap of {kept['cap']} were counted and not kept.")
    if result.get("fields_available") is False:
        lines.append("A server returned no comparison fields, so differences could only be classified by presence and document hash.")
    if "policy" in result:
        policy = result["policy"]
        supplied = (
            f"a policy snapshot with {policy['overrides']} overrides and {policy['blocked_domains']} blocked domains"
            if policy["source"] != "none"
            else "no policy snapshot, so blocked domains were not applied and overrides were taken from the shadow's records"
        )
        lines.append(f"What a search returns was computed at {policy['evaluated_at']} from each side's labels and handle, with {supplied}.")
    classes = result["classes"]
    lines += ["", "### Identity-level differences", "", *identity_table(classes), *breakdown_lines(classes, corpus.IDENTITY_CLASSES)]
    lines += ["", "### Refresh-derived differences", "", *snapshot_lines(result), "", *refresh_table(classes)]
    magnitude = result["score_magnitude"]
    if any(sum(buckets.values()) for buckets in magnitude.values()):
        rows = [[STRATUM_TITLES[stratum], *(magnitude[stratum][bucket] for bucket in corpus.SCORE_BUCKETS)] for stratum in corpus.STRATA]
        lines += ["", "Quality score differences by size:", "", *table(["shadow record", *SCORE_BUCKET_TITLES], rows)]
    lines += breakdown_lines(classes, corpus.REFRESH_CLASSES)
    notes = [[name, entry["count"]] for name, entry in result["notes"].items() if entry["count"]]
    if notes:
        lines += ["", "### Counted for information", "", *table(["what differs", "DIDs"], notes)]
    values = result.get("label_value_counts") or {}
    rows = [
        [value, values["only_in_production"].get(value, 0), values["only_in_shadow"].get(value, 0)]
        for value in sorted(set(values.get("only_in_production", {})) | set(values.get("only_in_shadow", {})))
    ]
    if rows:
        lines += ["", "Labels held by one side only, by value:", "", *table(["label value", "only in production", "only in shadow"], rows)]
    return lines


def search_section(result):
    lines = ["## Search", ""]
    lines.append(
        f"{result['queries']} queries: {result['identical']} identical, {result['differing_explained']} differing with a flagged DID "
        f"in the results, {result['differing_unexplained']} differing and unexplained. "
        f"Check failures: {result['shadow_check_failures']} on the shadow, {result['production_check_failures']} on production."
    )
    if not result["spam_guard_evaluated"]:
        lines.append("The spam guard from bench.py needs Bluesky's answer and was not evaluated.")
    rows = [
        [
            "`" + row["id"] + "`",
            row["first_difference_rank"] or "",
            f"{row['overlap']:.2f}",
            len(row["explained_by"]),
            "; ".join(row["shadow"]["check_failures"]),
        ]
        for row in result["cases"]
        if not row["identical"] or row["shadow"]["check_failures"]
    ]
    if rows:
        lines.append("")
        lines += table(["query", "first differing rank", "overlap", "flagged DIDs in results", "shadow check failures"], rows)
    return lines


def latency_section(result):
    lines = ["## Latency", ""]
    lines.append(
        f"{result['queries']} queries, one cold and {result['warm_runs']} warm requests each per backend, sequential. "
        f"The gate uses `{result['gate_basis']}`."
    )
    lines.append("")
    rows = []
    for phase in ("cold", "warm"):
        for basis in ("server_ms", "client_ms"):
            a = result["summary"]["production"][phase][basis]
            b = result["summary"]["shadow"][phase][basis]
            rows.append([f"{phase} {basis}", a["n"], *(f"{ms(a[p])} / {ms(b[p])}" for p in gates.LATENCY_PERCENTILES)])
    lines += table(["samples (production / shadow, ms)", "n", "p50", "p95", "p99"], rows)
    lines.append("")
    for name, entry in result["paired"].items():
        lines.append(
            f"Paired per query, {name}: median shadow minus production {ms(entry['median_shadow_minus_production'])} ms, "
            f"shadow slower on {entry['queries_shadow_slower']} of {entry['n']}."
        )
    lines += ["", "Controlled:", ""] + ["- " + item for item in result["controlled"]]
    lines += ["", "Not controlled:", ""] + ["- " + item for item in result["not_controlled"]]
    return lines


def freshness_section(result):
    lines = ["## Freshness", ""]
    rows = []
    for side in ("production", "shadow"):
        summary, samples = result["summary"][side], result["samples"][side]
        rows.append(
            [
                side,
                len(samples),
                sum(s["ready"] for s in samples),
                summary["min_sync_lag_s"],
                summary["max_sync_lag_s"],
                summary["watermark_advance_s"],
                sum(s["moderation_fresh"] for s in samples),
            ]
        )
    lines += table(["backend", "samples", "ready", "min sync lag s", "max sync lag s", "watermark advance s", "moderation fresh"], rows)
    lines.append("")
    rows = [[name, "yes" if entry["matched"] else "no"] for name, entry in result["agreement"].items()]
    lines += table(["must match between backends", "matched"], rows)
    if "changed_dids" in result:
        changed = result["changed_dids"]
        lines += ["", f"{changed['agreed_within_window']} of {changed['count']} listed DIDs agreed on both backends within the {result['window_s']} s window."]
    return lines


SECTIONS = {"corpus": corpus_section, "search": search_section, "latency": latency_section, "freshness": freshness_section}
GATE_TITLES = {
    "corpus": "(a) shadow never wrong where production is right",
    "search": "(b) search identical or explained",
    "latency": "(c) shadow latency not worse",
    "freshness": "(d) both ready and fresh",
}


def backend(report, side):
    if report["backends"][side]:
        return report["backends"][side]
    source = (report.get("corpus_sources") or {}).get(side) or {}
    return source.get("base") or source.get("build_id") or source.get("kind") or "not given"


def markdown(report):
    verdict = report["verdict"]
    lines = [f"# Shadow comparison, {report['date']}", ""]
    lines.append(
        f"Production `{backend(report, 'production')}` against shadow `{backend(report, 'shadow')}`. "
        f"Overall: **{verdict['overall']}**. Every figure is in [report.json](report.json)."
    )
    lines.append("")
    rows = []
    for name in gates.GATES:
        entry = verdict["gates"][name]
        rows.append([GATE_TITLES[name], f"**{entry['status']}**", "; ".join(entry["reasons"][:3]) + (" …" if len(entry["reasons"]) > 3 else "")])
    lines += table(["gate", "result", "first reasons"], rows)
    for name in gates.GATES:
        if report.get(name) is not None:
            lines += ["", *SECTIONS[name](report[name])]
    lines += ["", "## Load placed on each target", ""]
    rows = [[name, usage["requests"], usage["bytes"], usage["seconds_in_requests"], usage["max_requests"], usage["max_bytes"]] for name, usage in report["budgets"].items()]
    lines += table(["target", "requests", "bytes", "seconds answering", "request budget", "byte budget"], rows)
    return "\n".join(lines) + "\n"


def write(report, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    (directory / "README.md").write_text(markdown(report))
    return directory
