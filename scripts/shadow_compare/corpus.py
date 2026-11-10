"""Walk both backends' identity sets and classify every per-DID difference."""

import hashlib
import json
import os
import time
from pathlib import Path

from sources import PAGE_LIMIT, TantivyAdmin
from transport import BudgetExceeded, TransportError
from views import ABSENT, Doc, f32, label_difference, profile_differences

CHECKPOINT_VERSION = 3
UNCHANGED_IN_SHADOW = "unchanged_in_shadow"
NEWER_IN_SHADOW = "newer_in_shadow"
TIMINGS = (UNCHANGED_IN_SHADOW, NEWER_IN_SHADOW)
HOSTED_HANDLE_SUFFIXES = (".bsky.social",)
BLUESKY_PDS_SUFFIXES = (".bsky.network",)
TOP_PDS_HOSTS = 10

ONLY_IN_PRODUCTION = "only_in_production"
ONLY_IN_SHADOW = "only_in_shadow"
HANDLE_DIFFERS = "handle_differs"
VISIBILITY_DIFFERS = "visibility_differs"
PDS_DIFFERS = "pds_differs"
LABELS_DIFFER = "labels_differ"
QUALITY_SCORE_DIFFERS = "quality_score_differs"
OTHER_FIELD_DIFFERS = "other_field_differs"
DOCUMENT_HASH_DIFFERS = "document_hash_differs"
CLASSES = (
    ONLY_IN_PRODUCTION,
    ONLY_IN_SHADOW,
    HANDLE_DIFFERS,
    VISIBILITY_DIFFERS,
    PDS_DIFFERS,
    LABELS_DIFFER,
    QUALITY_SCORE_DIFFERS,
    OTHER_FIELD_DIFFERS,
    DOCUMENT_HASH_DIFFERS,
)
def classify(production, shadow):
    if not production.live and not shadow.live:
        return [], []
    if production.live and not shadow.live:
        return [ONLY_IN_PRODUCTION], []
    if shadow.live and not production.live:
        return [ONLY_IN_SHADOW], []
    if production.fields is None or shadow.fields is None:
        return ([DOCUMENT_HASH_DIFFERS] if production.sha != shadow.sha else []), []
    a, b = production.fields, shadow.fields
    classes = []
    if a.get("handle") != b.get("handle"):
        classes.append(HANDLE_DIFFERS)
    if bool(a.get("hidden")) != bool(b.get("hidden")):
        classes.append(VISIBILITY_DIFFERS)
    if a.get("pds_host") != b.get("pds_host"):
        classes.append(PDS_DIFFERS)
    labels = label_difference(a, b)
    if labels is not None:
        classes.append(LABELS_DIFFER)
    if f32(a.get("quality_score") or 0) != f32(b.get("quality_score") or 0):
        classes.append(QUALITY_SCORE_DIFFERS)
    other = profile_differences(a, b)
    if a.get("pds") != b.get("pds") and PDS_DIFFERS not in classes:
        other.insert(0, "pds")
    if other:
        classes.append(OTHER_FIELD_DIFFERS)
    return classes, other


def changed_since(doc, cutoff):
    """Whether a view says its content changed after the cutoff; unknown counts as unchanged."""
    changed = (doc.fields or {}).get("changed_at")
    return changed is not None and changed > cutoff


def timing(entry, cutoff):
    return NEWER_IN_SHADOW if changed_since(recorded(entry, "shadow"), cutoff) else UNCHANGED_IN_SHADOW


def sample_key(did):
    return hashlib.sha256(did.encode()).hexdigest()


def sample(dids, size):
    return sorted(dids, key=sample_key)[:size]


def difference(did, production, shadow):
    classes, other = classify(production, shadow)
    if not classes:
        return None
    entry = {
        "did": did,
        "classes": classes,
        "other_fields": other,
        "production": production.summary(),
        "shadow": shadow.summary(),
    }
    if LABELS_DIFFER in classes:
        entry["labels"] = label_difference(production.fields, shadow.fields)
    return entry


class Checkpoint:
    def __init__(self, path, identity):
        self.path = Path(path) if path else None
        self.identity = identity
        self.cursor = ""
        self.complete = False
        self.walked = 0
        self.live = {"production": 0, "shadow": 0}
        self.pages = 0
        self.differences = {}
        self.untracked = 0
        self.shadow = {"records": 0, "with_change_time": 0, "changed_since_cutoff": 0}
        self.start_markers = None
        self.started_at = None

    def load(self):
        if not self.path or not self.path.exists():
            return False
        saved = json.loads(self.path.read_text())
        if saved.get("version") != CHECKPOINT_VERSION or saved.get("identity") != self.identity:
            raise TransportError("checkpoint belongs to a different pair of backends or page size")
        self.cursor = saved["cursor"]
        self.complete = saved["complete"]
        self.walked = saved["walked"]
        self.live = saved["live"]
        self.pages = saved["pages"]
        self.differences = {entry["did"]: entry for entry in saved["differences"]}
        self.untracked = saved["untracked"]
        self.shadow = saved["shadow"]
        self.start_markers = saved["start_markers"]
        self.started_at = saved["started_at"]
        return True

    def save(self):
        if not self.path:
            return
        body = {
            "version": CHECKPOINT_VERSION,
            "identity": self.identity,
            "cursor": self.cursor,
            "complete": self.complete,
            "walked": self.walked,
            "live": self.live,
            "pages": self.pages,
            "differences": list(self.differences.values()),
            "untracked": self.untracked,
            "shadow": self.shadow,
            "start_markers": self.start_markers,
            "started_at": self.started_at,
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(body, separators=(",", ":")))
        os.replace(temporary, self.path)


class Side:
    def __init__(self, source):
        self.source = source
        self.buffer = []
        self.done = False

    def fill(self, cursor, limit):
        if self.buffer or self.done:
            return
        self.buffer = self.source.read(cursor, limit)
        dids = [did for did, _ in self.buffer]
        if len(dids) > limit or any(not left < right for left, right in zip([cursor, *dids], dids)):
            raise TransportError(f"{self.source.name} did not return DIDs strictly increasing after the cursor")
        self.done = len(dids) < limit

    def take(self, bound):
        count = len(self.buffer) if bound is None else sum(did <= bound for did, _ in self.buffer)
        taken, self.buffer = dict(self.buffer[:count]), self.buffer[count:]
        return taken


def window(production, shadow):
    """The DIDs both sides have fully reported, each side's view of them, and whether both are exhausted."""
    bounds = [side.buffer[-1][0] for side in (production, shadow) if not side.done]
    bound = min(bounds) if bounds else None
    a, b = production.take(bound), shadow.take(bound)
    dids = sorted(set(a) | set(b))
    if bound is None:
        bound = dids[-1] if dids else None
    return dids, a, b, bound, not bounds


def walk(production, shadow, checkpoint, *, page_size, max_pages, max_tracked, cutoff=None):
    """Advance the merge walk; returns a stop reason and leaves progress in the checkpoint."""
    if not 1 <= page_size <= PAGE_LIMIT:
        raise TransportError(f"page size must be 1..{PAGE_LIMIT}")
    if checkpoint.start_markers is None:
        checkpoint.start_markers = {"production": production.marker(), "shadow": shadow.marker()}
        checkpoint.started_at = int(time.time())
        checkpoint.save()
    sides = Side(production), Side(shadow)
    pages_this_run = 0
    while not checkpoint.complete:
        if max_pages is not None and pages_this_run >= max_pages:
            return "page_limit"
        try:
            for side in sides:
                side.fill(checkpoint.cursor, page_size)
        except BudgetExceeded:
            checkpoint.save()
            return "budget"
        dids, production_docs, shadow_docs, bound, exhausted = window(*sides)
        for did in dids:
            a, b = production_docs.get(did, ABSENT), shadow_docs.get(did, ABSENT)
            checkpoint.live["production"] += a.live
            checkpoint.live["shadow"] += b.live
            checkpoint.shadow["records"] += b.sha is not None
            checkpoint.shadow["with_change_time"] += (b.fields or {}).get("changed_at") is not None
            checkpoint.shadow["changed_since_cutoff"] += cutoff is not None and changed_since(b, cutoff)
            entry = difference(did, a, b)
            if entry is None:
                continue
            if len(checkpoint.differences) < max_tracked:
                checkpoint.differences[did] = entry
            else:
                checkpoint.untracked += 1
        checkpoint.walked += len(dids)
        checkpoint.pages += 1
        pages_this_run += 1
        if exhausted:
            checkpoint.complete = True
        else:
            checkpoint.cursor = bound
        checkpoint.save()
    return "complete"


def recorded(entry, side):
    return Doc(entry[side]["document_sha256"], entry[side]["fields"])


def recheck(production, shadow, differences, *, settle_delay, sleep=time.sleep):
    """Re-read every differing DID once after the settle delay; keep what still differs."""
    if not differences:
        return {}, 0
    sleep(settle_delay)
    dids = sorted(differences)
    docs = {}
    for side, source in (("production", production), ("shadow", shadow)):
        if source.fixed:
            docs[side] = {did: recorded(differences[did], side) for did in dids}
        else:
            docs[side] = source.lookup(dids)
    settled = {}
    for did in dids:
        entry = difference(did, docs["production"][did], docs["shadow"][did])
        if entry is not None:
            settled[did] = entry
    return settled, len(dids) - len(settled)


def subject(entry):
    """The fields that describe the actor: production's when it holds a live one, else the shadow's."""
    for side in ("production", "shadow"):
        fields = entry[side]["fields"]
        if fields and not fields.get("deleted"):
            return fields
    return None


def handle_kind(fields):
    if fields is None:
        return "unknown"
    handle = (fields.get("handle") or "").lower()
    if not handle or handle.endswith(".invalid"):
        return "no handle"
    return "hosted handle" if handle.endswith(HOSTED_HANDLE_SUFFIXES) else "custom domain"


def pds_kind(fields):
    if fields is None:
        return "unknown"
    host = fields.get("pds_host") or ""
    if not host:
        return "no pds"
    return "bluesky pds" if host == "bsky.social" or host.endswith(BLUESKY_PDS_SUFFIXES) else "independent pds"


def tally(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def breakdown(entries):
    subjects = [subject(entry) for entry in entries]
    hosts = tally((fields or {}).get("pds_host") or "" for fields in subjects)
    ranked = sorted(hosts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "handle_kind": tally(map(handle_kind, subjects)),
        "pds_kind": tally(map(pds_kind, subjects)),
        "top_pds_hosts": [{"host": host, "count": count} for host, count in ranked[:TOP_PDS_HOSTS]],
        "other_pds_hosts": {"hosts": max(0, len(ranked) - TOP_PDS_HOSTS), "count": sum(count for _, count in ranked[TOP_PDS_HOSTS:])},
    }


def summarize(differences, sample_size):
    classes = {}
    for name in CLASSES:
        members = [did for did, entry in differences.items() if name in entry["classes"]]
        chosen = sample(members, sample_size)
        classes[name] = {
            "count": len(members),
            "sample": [differences[did] for did in chosen],
            "breakdown": breakdown([differences[did] for did in members]),
        }
    other = {}
    values = {"only_in_production": {}, "only_in_shadow": {}}
    detail_only = 0
    for entry in differences.values():
        for name in entry["other_fields"]:
            other[name] = other.get(name, 0) + 1
        labels = entry.get("labels")
        if labels is None:
            continue
        detail_only += labels["values_agree"]
        for side, tally in values.items():
            for label in labels[side]:
                value = str(label.get("val", ""))
                tally[value] = tally.get(value, 0) + 1
    return {
        "differing_dids": len(differences),
        "classes": classes,
        "other_field_counts": other,
        "label_value_counts": {side: dict(sorted(tally.items())) for side, tally in values.items()},
        "label_detail_only": detail_only,
    }


def by_timing(differences, cutoff, sample_size):
    """The differences split by whether the shadow's record changed after the cutoff, each summarized."""
    parts = {name: {} for name in TIMINGS}
    for did, entry in differences.items():
        entry["timing"] = timing(entry, cutoff)
        parts[entry["timing"]][did] = entry
    return {name: summarize(members, sample_size) for name, members in parts.items()}


def run_sources(
    production,
    shadow,
    *,
    checkpoint_path,
    page_size,
    max_pages,
    max_tracked,
    settle_delay,
    sample_size,
    watermark_skew=0,
    sleep=time.sleep,
):
    """Walk two sources and summarize what differs.

    When production is a fixed snapshot with a watermark, the two sides are not
    simultaneous: each difference is also classed by whether the shadow's record
    changed after the watermark less `watermark_skew`.
    """
    want_fields = all(getattr(source, "want_fields", True) for source in (production, shadow))
    watermark = getattr(production, "watermark", None) if production.fixed else None
    cutoff = None if watermark is None else watermark - watermark_skew
    identity = {"production": production.identity, "shadow": shadow.identity, "page_size": page_size, "cutoff": cutoff}
    checkpoint = Checkpoint(checkpoint_path, identity)
    resumed = checkpoint.load()
    stop = walk(production, shadow, checkpoint, page_size=page_size, max_pages=max_pages, max_tracked=max_tracked, cutoff=cutoff)
    result = {
        "complete": checkpoint.complete,
        "stop_reason": stop,
        "resumed": resumed,
        "cursor": checkpoint.cursor,
        "pages": checkpoint.pages,
        "dids_walked": checkpoint.walked,
        "live": checkpoint.live,
        "untracked_differences": checkpoint.untracked,
        "started_at": checkpoint.started_at,
        "start_markers": checkpoint.start_markers,
        "fields_requested": want_fields,
    }
    if cutoff is not None:
        result["snapshot"] = {"watermark": watermark, "skew_s": watermark_skew, "cutoff": cutoff, "shadow": checkpoint.shadow}
    if not checkpoint.complete:
        result.update(rechecked=False, differences_before_recheck=len(checkpoint.differences))
        result.update(summarize(checkpoint.differences, sample_size))
        result["flagged_dids"] = sorted(checkpoint.differences)
        return result
    try:
        settled, transient = recheck(production, shadow, checkpoint.differences, settle_delay=settle_delay, sleep=sleep)
        result["end_markers"] = {"production": production.marker(), "shadow": shadow.marker()}
    except BudgetExceeded:
        result.update(rechecked=False, stop_reason="budget", differences_before_recheck=len(checkpoint.differences))
        result.update(summarize(checkpoint.differences, sample_size))
        result["flagged_dids"] = sorted(checkpoint.differences)
        return result
    result.update(
        rechecked=True,
        settle_delay_s=settle_delay,
        differences_before_recheck=len(checkpoint.differences),
        transient_differences=transient,
        finished_at=int(time.time()),
    )
    result["fields_available"] = want_fields and all(
        entry[side]["fields"] is not None
        for entry in settled.values()
        for side in ("production", "shadow")
        if entry[side]["state"] != "absent"
    )
    result["generation_changed"] = {
        side: result["start_markers"][side]["build_id"] != result["end_markers"][side]["build_id"]
        for side in ("production", "shadow")
    }
    result.update(summarize(settled, sample_size))
    if cutoff is not None:
        result["timing"] = by_timing(settled, cutoff, sample_size)
    result["flagged_dids"] = sorted(settled)
    return result


def run(production, shadow, *, want_fields=True, **options):
    """Compare two Tantivy admin listeners, given their transport clients."""
    return run_sources(TantivyAdmin(production, want_fields), TantivyAdmin(shadow, want_fields), **options)
