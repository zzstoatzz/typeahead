"""Walk both backends' identity sets, count every per-DID difference by class, and keep the ones a verdict needs.

A difference is identity-level (presence, handle, PDS, whether a search would
return the actor) or refresh-derived (quality score, labels, display name,
avatar, associated, creation time). The shadow refreshes profiles on its own
timetable, so refresh-derived fields legitimately differ on a large share of
the corpus: those are counted in full and only a deterministic sample of each
class is kept in detail. Identity-level differences are all kept, up to a cap.
"""

import hashlib
import json
import os
import time
from pathlib import Path

from moderation import ModerationError
from sources import PAGE_LIMIT, TantivyAdmin
from transport import BudgetExceeded, TransportError
from views import ABSENT, Doc, Row, avatar_cid, f32, itemised, label_difference

CHECKPOINT_VERSION = 4
HOSTED_HANDLE_SUFFIXES = (".bsky.social",)
BLUESKY_PDS_SUFFIXES = (".bsky.network",)
TOP_PDS_HOSTS = 10
FLAGGED_INLINE_LIMIT = 50_000
NOTE_SAMPLE_SIZE = 25

ONLY_IN_PRODUCTION = "only_in_production"
ONLY_IN_SHADOW = "only_in_shadow"
HANDLE_DIFFERS = "handle_differs"
PDS_DIFFERS = "pds_differs"
VISIBILITY_DIFFERS = "visibility_differs"
DOCUMENT_HASH_DIFFERS = "document_hash_differs"
IDENTITY_CLASSES = (ONLY_IN_PRODUCTION, ONLY_IN_SHADOW, HANDLE_DIFFERS, PDS_DIFFERS, VISIBILITY_DIFFERS, DOCUMENT_HASH_DIFFERS)

QUALITY_SCORE_DIFFERS = "quality_score_differs"
LABELS_DIFFER = "labels_differ"
DISPLAY_NAME_DIFFERS = "display_name_differs"
AVATAR_DIFFERS = "avatar_differs"
ASSOCIATED_DIFFERS = "associated_differs"
CREATED_AT_DIFFERS = "created_at_differs"
PROFILE_DIFFERS = "profile_differs"
REFRESH_CLASSES = (QUALITY_SCORE_DIFFERS, LABELS_DIFFER, DISPLAY_NAME_DIFFERS, AVATAR_DIFFERS, ASSOCIATED_DIFFERS, CREATED_AT_DIFFERS, PROFILE_DIFFERS)
CLASSES = IDENTITY_CLASSES + REFRESH_CLASSES

STORED_HIDDEN_DIFFERS = "stored_hidden_differs"
LABEL_DETAIL_DIFFERS = "label_detail_differs"
AVATAR_FORM_DIFFERS = "avatar_form_differs"
PDS_FORM_DIFFERS = "pds_form_differs"
VISIBILITY_UNEVALUATED = "visibility_unevaluated"
NOTES = (STORED_HIDDEN_DIFFERS, LABEL_DETAIL_DIFFERS, AVATAR_FORM_DIFFERS, PDS_FORM_DIFFERS, VISIBILITY_UNEVALUATED)

REFRESHED = "refreshed_since_watermark"
LAGGING = "not_refreshed_since_watermark"
STRATA = (REFRESHED, LAGGING)

SCORE_BUCKETS = ("production_zero", "shadow_zero", "under_1", "1_to_50", "50_or_more")
MISSING = Row.of(ABSENT)


def score_bucket(production, shadow):
    """Where a score difference falls: one side holding no score at all, else the size of the gap."""
    if production == 0:
        return "production_zero"
    if shadow == 0:
        return "shadow_zero"
    gap = abs(production - shadow)
    return "under_1" if gap < 1 else "1_to_50" if gap < 50 else "50_or_more"


class Visibility:
    """Whether a search would return a view, under one moderation policy at one instant.

    A view that itemises its labels is judged by the policy. One that does not
    (an admin listener) is judged by its stored flag, the only thing it gives,
    and then so is the view it is compared with.
    A shadow record's own override stands in for the policy's when no policy
    snapshot was supplied, and applies to both sides: overrides are per DID.
    """

    def __init__(self, policy, now_ms):
        self.policy = policy
        self.now_ms = now_ms

    def returned(self, did, fields, override=None, by_flag=False):
        """True or False, or None when the labels are ones the server refuses to evaluate."""
        handle = fields.get("handle") or ""
        if fields.get("deleted") or not handle or handle.lower().endswith(".invalid"):
            return False
        if by_flag or fields.get("labels") is None:
            return not fields.get("hidden")
        if self.policy.source != "none":
            override = None
        try:
            labels = [json.loads(text) for text in fields["labels"]]
            return not self.policy.hidden(did, handle, labels, self.now_ms, override)
        except ModerationError:
            return None


def classify(did, production, shadow, visibility):
    """The classes a DID falls in, the informational notes beside them, and what each side would return."""
    if not production.live and not shadow.live:
        return [], [], None
    if production.live and not shadow.live:
        return [ONLY_IN_PRODUCTION], [], None
    if shadow.live and not production.live:
        return [ONLY_IN_SHADOW], [], None
    if production.fields is None or shadow.fields is None:
        return ([DOCUMENT_HASH_DIFFERS] if production.sha != shadow.sha else []), [], None
    a, b = production.fields, shadow.fields
    classes, notes = [], []
    if a.get("handle") != b.get("handle"):
        classes.append(HANDLE_DIFFERS)
    if a.get("pds_host") != b.get("pds_host"):
        classes.append(PDS_DIFFERS)
    elif a.get("pds") != b.get("pds"):
        notes.append(PDS_FORM_DIFFERS)
    override, by_flag = {"show": False, "hide": True}.get(b.get("override")), not (itemised(a) and itemised(b))
    visible = {"production": visibility.returned(did, a, override, by_flag), "shadow": visibility.returned(did, b, override, by_flag)}
    if None in visible.values():
        notes.append(VISIBILITY_UNEVALUATED)
    elif visible["production"] != visible["shadow"]:
        classes.append(VISIBILITY_DIFFERS)
    if bool(a.get("hidden")) != bool(b.get("hidden")):
        notes.append(STORED_HIDDEN_DIFFERS)
    if f32(a.get("quality_score") or 0) != f32(b.get("quality_score") or 0):
        classes.append(QUALITY_SCORE_DIFFERS)
    labels = label_difference(a, b)
    if labels is not None:
        (notes if labels["values_agree"] else classes).append(LABEL_DETAIL_DIFFERS if labels["values_agree"] else LABELS_DIFFER)
    if not (itemised(a) and itemised(b)):
        if a.get("profile_sha256") != b.get("profile_sha256"):
            classes.append(PROFILE_DIFFERS)
        return classes, notes, visible
    for name, field in ((DISPLAY_NAME_DIFFERS, "display_name"), (ASSOCIATED_DIFFERS, "associated"), (CREATED_AT_DIFFERS, "created_at")):
        if a[field] != b[field]:
            classes.append(name)
    if avatar_cid(a["avatar_url"]) != avatar_cid(b["avatar_url"]):
        classes.append(AVATAR_DIFFERS)
    elif a["avatar_url"] != b["avatar_url"]:
        notes.append(AVATAR_FORM_DIFFERS)
    return classes, notes, visible


def sample_key(did):
    return hashlib.sha256(did.encode()).hexdigest()


def sample(dids, size):
    return sorted(dids, key=sample_key)[:size]


def difference(did, production, shadow, visibility, refreshed=True):
    classes, notes, visible = classify(did, production, shadow, visibility)
    if not classes and not notes:
        return None
    entry = {
        "did": did,
        "classes": classes,
        "notes": notes,
        "refreshed": refreshed,
        "production": production.summary(),
        "shadow": shadow.summary(),
    }
    if visible is not None:
        entry["returned"] = visible
    if LABELS_DIFFER in classes or LABEL_DETAIL_DIFFERS in notes:
        entry["labels"] = label_difference(production.fields, shadow.fields)
    return entry


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


def bump(counts, key, by=1):
    counts[key] = counts.get(key, 0) + by


def new_tally():
    return {"count": 0, "handle_kind": {}, "pds_kind": {}, "hosts": {}}


def tally(into, entry):
    fields = subject(entry)
    into["count"] += 1
    bump(into["handle_kind"], handle_kind(fields))
    bump(into["pds_kind"], pds_kind(fields))
    bump(into["hosts"], (fields or {}).get("pds_host") or "")


def merged(tallies):
    total = new_tally()
    for part in tallies:
        total["count"] += part["count"]
        for name in ("handle_kind", "pds_kind", "hosts"):
            for key, count in part[name].items():
                bump(total[name], key, count)
    return total


def breakdown(total):
    ranked = sorted(total["hosts"].items(), key=lambda item: (-item[1], item[0]))
    return {
        "handle_kind": dict(sorted(total["handle_kind"].items())),
        "pds_kind": dict(sorted(total["pds_kind"].items())),
        "top_pds_hosts": [{"host": host, "count": count} for host, count in ranked[:TOP_PDS_HOSTS]],
        "other_pds_hosts": {"hosts": max(0, len(ranked) - TOP_PDS_HOSTS), "count": sum(count for _, count in ranked[TOP_PDS_HOSTS:])},
    }


def new_group():
    return {"dids": {}, "worst": None}


def offer(group, did, key, size):
    """Keep the `size` DIDs with the smallest keys; true when this one is kept."""
    members = group["dids"]
    if len(members) >= size:
        if key >= group["worst"]:
            return False
        del members[max(members, key=members.get)]
    members[did] = key
    group["worst"] = max(members.values())
    return True


class DidLog:
    """Every differing DID, appended to a file beside the checkpoint so memory does not hold them."""

    def __init__(self, path):
        self.path = path
        self.stream = None
        self.memory = []
        self.size = 0

    def resume(self, size):
        held = self.path.stat().st_size if self.path.exists() else 0
        if held < size:
            raise TransportError(f"{self.path} is missing or shorter than the checkpoint says")
        if held > size:
            with open(self.path, "r+b") as stream:
                stream.truncate(size)
        self.size = size

    def add(self, did):
        if self.path is None:
            if len(self.memory) <= FLAGGED_INLINE_LIMIT:
                self.memory.append(did)
            return
        if self.stream is None:
            self.stream = open(self.path, "r+b" if self.size else "wb")
            self.stream.seek(self.size)
        line = did.encode("utf-8", "surrogatepass") + b"\n"
        self.stream.write(line)
        self.size += len(line)

    def flush(self):
        if self.stream is not None:
            self.stream.flush()
        return self.size

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def dids(self):
        if self.path is None:
            return list(self.memory)
        self.flush()
        if not self.path.exists():
            return []
        with open(self.path, "rb") as stream:
            return [line.rstrip(b"\n").decode("utf-8", "surrogatepass") for line in stream]


class Checkpoint:
    def __init__(self, path, identity, interval=0.0, clock=time.monotonic, *, max_tracked=0, sample_size=0):
        self.path = Path(path) if path else None
        self.identity = identity
        self.interval = interval
        self.clock = clock
        self.max_tracked = max_tracked
        self.sample_size = sample_size
        self.saved_at = None
        self.cursor = ""
        self.complete = False
        self.walked = 0
        self.live = {"production": 0, "shadow": 0}
        self.pages = 0
        self.differing = 0
        self.entries = {}
        self.tracked = {}
        self.untracked = 0
        self.tallies = {name: {stratum: new_tally() for stratum in STRATA} for name in CLASSES}
        self.magnitude = {stratum: dict.fromkeys(SCORE_BUCKETS, 0) for stratum in STRATA}
        self.samples = {name: {stratum: new_group() for stratum in STRATA} for name in REFRESH_CLASSES}
        self.notes = {name: {"count": 0, "sample": new_group()} for name in NOTES}
        self.label_values = {"only_in_production": {}, "only_in_shadow": {}}
        self.shadow = {"records": 0, "with_change_time": 0, "changed_since_watermark": 0, "with_refresh_time": 0, "refreshed_since_watermark": 0}
        self.seconds = {"production_read": 0.0, "shadow_read": 0.0, "compare": 0.0}
        self.start_markers = None
        self.started_at = None
        self.log = DidLog(self.path.with_name(self.path.name + ".dids") if self.path else None)

    STATE = ("cursor", "complete", "walked", "live", "pages", "differing", "tracked", "untracked", "tallies", "magnitude", "samples",
             "notes", "label_values", "shadow", "seconds", "start_markers", "started_at")

    def load(self):
        if not self.path or not self.path.exists():
            return False
        saved = json.loads(self.path.read_text())
        if saved.get("version") != CHECKPOINT_VERSION or saved.get("identity") != self.identity:
            raise TransportError("checkpoint belongs to a different pair of backends, policy or settings")
        for name in self.STATE:
            setattr(self, name, saved[name])
        self.entries = {entry["did"]: entry for entry in saved["entries"]}
        self.log.resume(saved["dids_bytes"])
        return True

    def referenced(self):
        """The DIDs whose detail a verdict can still need: identity-level ones and every sample's members."""
        dids = set(self.tracked)
        for strata in self.samples.values():
            for group in strata.values():
                dids.update(group["dids"])
        for note in self.notes.values():
            dids.update(note["sample"]["dids"])
        return dids

    def prune(self):
        keep = self.referenced()
        self.entries = {did: entry for did, entry in self.entries.items() if did in keep}

    def save(self, due=False):
        """Write the file; with `due`, only when `interval` seconds have passed since the last write."""
        if not self.path:
            return
        now = self.clock()
        if due and self.saved_at is not None and now - self.saved_at < self.interval:
            return
        self.saved_at = now
        self.prune()
        body = {"version": CHECKPOINT_VERSION, "identity": self.identity, **{name: getattr(self, name) for name in self.STATE}}
        body["entries"] = list(self.entries.values())
        body["dids_bytes"] = self.log.flush()
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(body, separators=(",", ":")))
        os.replace(temporary, self.path)

    def record(self, entry):
        """Count one difference everywhere it belongs and keep its detail if a verdict could need it."""
        did, stratum = entry["did"], REFRESHED if entry["refreshed"] else LAGGING
        key = sample_key(did)
        keep = False
        for name in entry["notes"]:
            self.notes[name]["count"] += 1
            keep |= offer(self.notes[name]["sample"], did, key, NOTE_SAMPLE_SIZE)
        if not entry["classes"]:
            if keep:
                self.entries[did] = entry
            return
        self.differing += 1
        self.log.add(did)
        if any(name in IDENTITY_CLASSES for name in entry["classes"]):
            if len(self.tracked) < self.max_tracked:
                self.tracked[did] = key
                keep = True
            else:
                self.untracked += 1
        for name in entry["classes"]:
            tally(self.tallies[name][stratum], entry)
            if name in REFRESH_CLASSES:
                keep |= offer(self.samples[name][stratum], did, key, self.sample_size)
        if LABELS_DIFFER in entry["classes"]:
            for side, values in self.label_values.items():
                for label in entry["labels"][side]:
                    bump(values, str(label.get("val", "")))
        if QUALITY_SCORE_DIFFERS in entry["classes"]:
            scores = [f32(entry[side]["fields"].get("quality_score") or 0) for side in ("production", "shadow")]
            self.magnitude[stratum][score_bucket(*scores)] += 1
        if keep:
            self.entries[did] = entry


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
    """The DIDs both sides have fully reported, each side's rows for them, and whether both are exhausted."""
    bounds = [side.buffer[-1][0] for side in (production, shadow) if not side.done]
    bound = min(bounds) if bounds else None
    a, b = production.take(bound), shadow.take(bound)
    dids = sorted(set(a) | set(b))
    if bound is None:
        bound = dids[-1] if dids else None
    return dids, a, b, bound, not bounds


def refreshed_after(row, threshold):
    """Whether the shadow refreshed this record after the threshold; unknown counts as refreshed, the stricter reading."""
    return threshold is None or row.refreshed_at is None or row.refreshed_at > threshold


def walk(production, shadow, checkpoint, *, page_size, max_pages, visibility, watermark=None, threshold=None, clock=time.monotonic):
    """Advance the merge walk; returns a stop reason and leaves progress in the checkpoint."""
    if not 1 <= page_size <= PAGE_LIMIT:
        raise TransportError(f"page size must be 1..{PAGE_LIMIT}")
    sides = Side(production), Side(shadow)
    seconds, stats, live = checkpoint.seconds, checkpoint.shadow, checkpoint.live
    pages_this_run = 0
    while not checkpoint.complete:
        if max_pages is not None and pages_this_run >= max_pages:
            checkpoint.save()
            return "page_limit"
        try:
            for side, name in zip(sides, ("production_read", "shadow_read")):
                started = clock()
                side.fill(checkpoint.cursor, page_size)
                seconds[name] += clock() - started
        except BudgetExceeded:
            checkpoint.save()
            return "budget"
        started = clock()
        dids, production_rows, shadow_rows, bound, exhausted = window(*sides)
        for did in dids:
            a, b = production_rows.get(did, MISSING), shadow_rows.get(did, MISSING)
            live["production"] += a.live
            live["shadow"] += b.live
            if b is not MISSING:
                stats["records"] += 1
                if b.changed_at is not None:
                    stats["with_change_time"] += 1
                    stats["changed_since_watermark"] += watermark is not None and b.changed_at > watermark
                if b.refreshed_at is not None:
                    stats["with_refresh_time"] += 1
                    stats["refreshed_since_watermark"] += threshold is not None and b.refreshed_at > threshold
            if a.key is not None and a.key == b.key or not (a.live or b.live):
                continue
            entry = difference(did, a.doc(), b.doc(), visibility, refreshed_after(b, threshold))
            if entry is not None:
                checkpoint.record(entry)
        checkpoint.walked += len(dids)
        checkpoint.pages += 1
        pages_this_run += 1
        if exhausted:
            checkpoint.complete = True
        else:
            checkpoint.cursor = bound
        seconds["compare"] += clock() - started
        checkpoint.save(due=not exhausted)
    return "complete"


def recorded(entry, side):
    return Doc(entry[side]["document_sha256"], entry[side]["fields"])


def recheck(production, shadow, entries, visibility, *, settle_delay, sleep=time.sleep):
    """Re-read every kept DID once after the settle delay; the entry it has now, or None when nothing differs."""
    if not entries:
        return {}
    sleep(settle_delay)
    dids = sorted(entries)
    docs = {}
    for side, source in (("production", production), ("shadow", shadow)):
        if source.fixed:
            docs[side] = {did: recorded(entries[did], side) for did in dids}
        else:
            docs[side] = source.lookup(dids)
    return {did: difference(did, docs["production"][did], docs["shadow"][did], visibility, entries[did]["refreshed"]) for did in dids}


def has(entry, name):
    return entry is not None and (name in entry["classes"] or name in entry["notes"])


def members(group, entries, name):
    """A sample's entries that still carry `name`, in sample order, and how many no longer do."""
    kept = [entries[did] for did in sorted(group["dids"], key=group["dids"].get) if has(entries.get(did), name)]
    return kept, len(group["dids"]) - len(kept)


def summarize(checkpoint, entries, identity_sample_size, settled):
    """The per-class report: counts from the walk, detail from `entries`.

    `settled` says `entries` were re-read after the settle delay. Identity-level
    counts are then recounted from them, unless some were never tracked.
    Refresh-derived counts stay as the walk counted them: a class that covers a
    twentieth of the corpus is not re-read, only its sample is.
    """
    classes = {}
    recount = settled and not checkpoint.untracked
    tracked = [entries[did] for did in sorted(checkpoint.tracked, key=checkpoint.tracked.get) if entries.get(did) is not None]
    for name in IDENTITY_CLASSES:
        holders = [entry for entry in tracked if name in entry["classes"]]
        total = new_tally()
        for entry in holders:
            tally(total, entry)
        if not recount:
            total = merged(checkpoint.tallies[name].values())
        classes[name] = {
            "level": "identity",
            "count": total["count"],
            "counted": "after the re-read" if recount else "during the walk",
            "sample": holders[:identity_sample_size],
            "breakdown": breakdown(total),
        }
    for name in REFRESH_CLASSES:
        strata, chosen = {}, []
        for stratum in STRATA:
            kept, dropped = members(checkpoint.samples[name][stratum], entries, name)
            chosen += kept
            strata[stratum] = {"count": checkpoint.tallies[name][stratum]["count"], "sampled": len(kept), "sample_no_longer_differs": dropped}
        total = merged(checkpoint.tallies[name].values())
        classes[name] = {
            "level": "refresh",
            "count": total["count"],
            "counted": "during the walk",
            "sample": chosen,
            "breakdown": breakdown(total),
            "strata": strata,
        }
    notes = {}
    for name in NOTES:
        kept, _ = members(checkpoint.notes[name]["sample"], entries, name)
        notes[name] = {"count": checkpoint.notes[name]["count"], "sample": kept}
    return {
        "differing_dids": checkpoint.differing,
        "identity_differences": {"tracked": len(tracked), "untracked": checkpoint.untracked, "cap": checkpoint.max_tracked},
        "classes": classes,
        "notes": notes,
        "score_magnitude": checkpoint.magnitude,
        "label_value_counts": {side: dict(sorted(values.items())) for side, values in checkpoint.label_values.items()},
    }


def identity_level(entry):
    return entry is not None and any(name in IDENTITY_CLASSES for name in entry["classes"])


def flagged(checkpoint, entries):
    """The DIDs a search difference may be explained by: every differing one when few enough, else the kept ones."""
    if checkpoint.differing > FLAGGED_INLINE_LIMIT:
        return sorted(did for did, entry in entries.items() if entry is not None and entry["classes"]), False
    settled = {did for did, entry in entries.items() if entry is None or not entry["classes"]}
    return sorted(set(checkpoint.log.dids()) - settled), True


def run_sources(
    production,
    shadow,
    *,
    policy,
    checkpoint_path,
    page_size,
    max_pages,
    max_tracked,
    settle_delay,
    sample_size,
    identity_sample_size,
    bootstrap_at=None,
    checkpoint_interval=0.0,
    sleep=time.sleep,
):
    """Walk two sources and summarize what differs.

    When production is a fixed snapshot with a watermark, the two sides are not
    simultaneous: each refresh-derived difference is also placed by whether the
    shadow refreshed the record after the watermark (or after `bootstrap_at`,
    when the shadow was loaded later than that).
    """
    want_fields = all(getattr(source, "want_fields", True) for source in (production, shadow))
    watermark = getattr(production, "watermark", None) if production.fixed else None
    threshold = None if watermark is None else max(watermark, bootstrap_at or 0)
    identity = {
        "production": production.identity, "shadow": shadow.identity, "page_size": page_size, "refresh_threshold": threshold,
        "policy": policy.describe(), "max_tracked": max_tracked, "sample_size": sample_size,
    }
    checkpoint = Checkpoint(checkpoint_path, identity, checkpoint_interval, max_tracked=max_tracked, sample_size=sample_size)
    try:
        resumed = checkpoint.load()
        if checkpoint.start_markers is None:
            checkpoint.start_markers = {"production": production.marker(), "shadow": shadow.marker()}
            checkpoint.started_at = int(time.time())
            checkpoint.save()
        visibility = Visibility(policy, checkpoint.started_at * 1000)
        stop = walk(production, shadow, checkpoint, page_size=page_size, max_pages=max_pages, visibility=visibility, watermark=watermark, threshold=threshold)
        checkpoint.prune()
        result = {
            "complete": checkpoint.complete,
            "stop_reason": stop,
            "resumed": resumed,
            "cursor": checkpoint.cursor,
            "pages": checkpoint.pages,
            "dids_walked": checkpoint.walked,
            "live": checkpoint.live,
            "started_at": checkpoint.started_at,
            "start_markers": checkpoint.start_markers,
            "fields_requested": want_fields,
            "policy": {**policy.describe(), "evaluated_at": checkpoint.started_at},
            "seconds": {name: round(value, 1) for name, value in checkpoint.seconds.items()},
            "differing_dids_log": str(checkpoint.log.path) if checkpoint.log.path else None,
        }
        if watermark is not None:
            result["snapshot"] = {"watermark": watermark, "bootstrap_at": bootstrap_at, "refresh_threshold": threshold, "shadow": checkpoint.shadow}

        def unsettled(reason):
            result.update(rechecked=False, stop_reason=reason, differences_kept=len(checkpoint.entries))
            result.update(summarize(checkpoint, checkpoint.entries, identity_sample_size, settled=False))
            result["flagged_dids"], result["flagged_dids_complete"] = flagged(checkpoint, checkpoint.entries)
            return result

        if not checkpoint.complete:
            return unsettled(stop)
        try:
            settled = recheck(production, shadow, checkpoint.entries, visibility, settle_delay=settle_delay, sleep=sleep)
            result["end_markers"] = {"production": production.marker(), "shadow": shadow.marker()}
        except BudgetExceeded:
            return unsettled("budget")
        result.update(
            rechecked=True,
            settle_delay_s=settle_delay,
            differences_kept=len(settled),
            transient_differences=sum(not identity_level(settled[did]) for did in checkpoint.tracked),
            finished_at=int(time.time()),
        )
        result["fields_available"] = want_fields and all(
            entry[side]["fields"] is not None
            for entry in settled.values()
            if entry is not None
            for side in ("production", "shadow")
            if entry[side]["state"] != "absent"
        )
        result["generation_changed"] = {
            side: result["start_markers"][side]["build_id"] != result["end_markers"][side]["build_id"]
            for side in ("production", "shadow")
        }
        result.update(summarize(checkpoint, settled, identity_sample_size, settled=True))
        result["flagged_dids"], result["flagged_dids_complete"] = flagged(checkpoint, settled)
        return result
    finally:
        checkpoint.log.close()


def run(production, shadow, *, want_fields=True, **options):
    """Compare two Tantivy admin listeners, given their transport clients."""
    return run_sources(TantivyAdmin(production, want_fields), TantivyAdmin(shadow, want_fields), **options)
