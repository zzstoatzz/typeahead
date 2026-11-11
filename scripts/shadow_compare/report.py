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


def class_table(classes):
    rows = []
    for name in corpus.CLASSES:
        entry = classes[name]
        labels = ", ".join(f"{count} {label}" for label, count in sorted(entry.get("arbiter_labels", {}).items()))
        rows.append([name, entry["count"], len(entry["sample"]), labels or ("not run" if entry["count"] else "")])
    return table(["class", "count", "sampled", "arbiter labels for the sample"], rows)


HANDLE_KINDS = ("hosted handle", "custom domain", "no handle", "unknown")
PDS_KINDS = ("bluesky pds", "independent pds", "no pds", "unknown")


def breakdown_lines(classes):
    present = [name for name in corpus.CLASSES if classes[name]["count"] and "breakdown" in classes[name]]
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


TIMING_TITLES = {
    corpus.UNCHANGED_IN_SHADOW: "Unchanged in the shadow since the watermark: divergence",
    corpus.NEWER_IN_SHADOW: "Newer in the shadow than the snapshot: judged by the arbiter sample only",
}


def snapshot_section(result):
    snapshot = result["snapshot"]
    shadow = snapshot["shadow"]
    lines = [
        f"Production is a snapshot at watermark {snapshot['watermark']} and the shadow is live. A shadow record counts as "
        f"changed since the snapshot when its change time is after {snapshot['cutoff']} (the watermark less {snapshot['skew_s']} s). "
        f"{shadow['changed_since_cutoff']} of {shadow['records']} shadow records changed since then; "
        f"{shadow['records'] - shadow['with_change_time']} carry no change time and count as unchanged."
    ]
    for name in corpus.TIMINGS:
        if "timing" not in result:
            break
        part = result["timing"][name]
        lines += ["", f"### {TIMING_TITLES[name]}", "", f"{part['differing_dids']} DIDs.", "", *class_table(part["classes"])]
        lines += breakdown_lines(part["classes"])
    return lines


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
        f"{result['live']['production']} live in production, {result['live']['shadow']} live in the shadow."
    )
    if result.get("rechecked"):
        lines.append(
            f"{result['differences_before_recheck']} DIDs differed during the walk; "
            f"{result['transient_differences']} agreed when re-read after {result['settle_delay_s']} s."
        )
        for side in ("production", "shadow"):
            lines.append(marker_line(side, result["start_markers"][side], result["end_markers"][side]))
    if result.get("fields_available") is False:
        lines.append("A server returned no comparison fields, so differences could only be classified by presence and document hash.")
    lines.append("")
    lines += class_table(result["classes"])
    lines += breakdown_lines(result["classes"])
    if "snapshot" in result:
        lines += ["", *snapshot_section(result)]
    values = result.get("label_value_counts") or {}
    rows = [
        [value, values["only_in_production"].get(value, 0), values["only_in_shadow"].get(value, 0)]
        for value in sorted(set(values.get("only_in_production", {})) | set(values.get("only_in_shadow", {})))
    ]
    if rows:
        lines += ["", f"Labels held by one side only, by value ({result['label_detail_only']} DIDs agree on values and differ in other label detail):", ""]
        lines += table(["label value", "only in production", "only in shadow"], rows)
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
    "corpus": "(a) corpus differences zero or explained",
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
