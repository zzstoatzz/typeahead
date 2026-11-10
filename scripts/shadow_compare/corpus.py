"""Walk both backends' identity sets and classify every per-DID difference."""

import hashlib
import json
import os
import time
from pathlib import Path

from sources import PAGE_LIMIT, TantivyAdmin
from transport import BudgetExceeded, TransportError
from views import ABSENT, Doc

CHECKPOINT_VERSION = 2

ONLY_IN_PRODUCTION = "only_in_production"
ONLY_IN_SHADOW = "only_in_shadow"
HANDLE_DIFFERS = "handle_differs"
VISIBILITY_DIFFERS = "visibility_differs"
PDS_DIFFERS = "pds_differs"
OTHER_FIELD_DIFFERS = "other_field_differs"
DOCUMENT_HASH_DIFFERS = "document_hash_differs"
CLASSES = (
    ONLY_IN_PRODUCTION,
    ONLY_IN_SHADOW,
    HANDLE_DIFFERS,
    VISIBILITY_DIFFERS,
    PDS_DIFFERS,
    OTHER_FIELD_DIFFERS,
    DOCUMENT_HASH_DIFFERS,
)
OTHER_FIELDS = ("pds", "profile_sha256", "quality_score")
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
    other = [name for name in OTHER_FIELDS if a.get(name) != b.get(name)]
    if PDS_DIFFERS in classes and "pds" in other:
        other.remove("pds")
    if other:
        classes.append(OTHER_FIELD_DIFFERS)
    return classes, other


def sample_key(did):
    return hashlib.sha256(did.encode()).hexdigest()


def sample(dids, size):
    return sorted(dids, key=sample_key)[:size]


def difference(did, production, shadow):
    classes, other = classify(production, shadow)
    if not classes:
        return None
    return {
        "did": did,
        "classes": classes,
        "other_fields": other,
        "production": production.summary(),
        "shadow": shadow.summary(),
    }


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


def walk(production, shadow, checkpoint, *, page_size, max_pages, max_tracked):
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


def summarize(differences, sample_size):
    classes = {}
    for name in CLASSES:
        members = [did for did, entry in differences.items() if name in entry["classes"]]
        chosen = sample(members, sample_size)
        classes[name] = {
            "count": len(members),
            "sample": [differences[did] for did in chosen],
        }
    other = {}
    for entry in differences.values():
        for name in entry["other_fields"]:
            other[name] = other.get(name, 0) + 1
    return {"differing_dids": len(differences), "classes": classes, "other_field_counts": other}


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
    sleep=time.sleep,
):
    want_fields = all(getattr(source, "want_fields", True) for source in (production, shadow))
    identity = {"production": production.identity, "shadow": shadow.identity, "page_size": page_size}
    checkpoint = Checkpoint(checkpoint_path, identity)
    resumed = checkpoint.load()
    stop = walk(production, shadow, checkpoint, page_size=page_size, max_pages=max_pages, max_tracked=max_tracked)
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
    result["flagged_dids"] = sorted(settled)
    return result


def run(production, shadow, *, want_fields=True, **options):
    """Compare two Tantivy admin listeners, given their transport clients."""
    return run_sources(TantivyAdmin(production, want_fields), TantivyAdmin(shadow, want_fields), **options)
