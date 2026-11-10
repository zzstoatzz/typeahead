"""The comparable view of one actor, as every corpus source reports it.

`fields` always carries `deleted`, `hidden`, `handle`, `pds`, `pds_host`,
`quality_score` and `profile_sha256`. A source that holds whole actors adds
`labels` (one canonical JSON text per label, sorted) and
`profile_rest_sha256` (the profile hash without labels), so label
differences can be told apart from other profile differences. A source that
knows when the actor's content last changed adds `changed_at` in unix seconds.
"""

import hashlib
import json
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Doc:
    sha: str | None
    fields: dict | None = None

    @property
    def state(self):
        if self.sha is None:
            return "absent"
        if self.fields is None:
            return "present"
        return "deleted" if self.fields.get("deleted") else "live"

    @property
    def live(self):
        return self.state in ("present", "live")

    def summary(self):
        return {"state": self.state, "document_sha256": self.sha, "fields": self.fields}


ABSENT = Doc(None)


def f32(value):
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def canonical(value):
    """The bytes serde_json writes for a `Value`: compact, keys sorted, non-ASCII left as is."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8", "surrogatepass")


def sha256(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def profile_sha256(display_name, avatar_url, labels, associated, created_at):
    """`profile_sha256` as services/tantivy/src/engine/source.rs computes it."""
    return sha256([display_name, avatar_url, labels, associated, created_at])


def parsed(value, default):
    if isinstance(value, str):
        value = json.loads(value)
    return default if value is None else value


def from_actor(actor, *, sha, changed_at=None):
    """A view of one actor in the shape the Tantivy index stores (services/tantivy/src/actor.rs)."""
    labels = parsed(actor.get("labels"), [])
    associated = parsed(actor.get("associated"), {})
    display_name = actor.get("display_name", "")
    avatar_url = actor.get("avatar_url", "")
    created_at = actor.get("created_at", "")
    fields = {
        "deleted": bool(actor.get("deleted", False)),
        "hidden": bool(actor.get("hidden", False)),
        "handle": actor.get("handle", ""),
        "pds": actor.get("pds", ""),
        "pds_host": actor.get("pds_host", ""),
        "quality_score": f32(actor.get("quality_score", 0)),
        "version": actor.get("version"),
        "profile_sha256": profile_sha256(display_name, avatar_url, labels, associated, created_at),
        "profile_rest_sha256": sha256([display_name, avatar_url, associated, created_at]),
        "labels": sorted(canonical(label).decode("utf-8", "surrogatepass") for label in labels),
    }
    if changed_at is not None:
        fields["changed_at"] = changed_at
    return Doc(sha, fields)


def label_value(text):
    label = json.loads(text)
    return str(label.get("val", "")) if isinstance(label, dict) else ""


def label_difference(production, shadow):
    """Labels held by one side only, or None when either side did not itemise them or they agree."""
    a, b = production.get("labels"), shadow.get("labels")
    if a is None or b is None or a == b:
        return None
    return {
        "only_in_production": [json.loads(text) for text in a if text not in b],
        "only_in_shadow": [json.loads(text) for text in b if text not in a],
        "values_agree": sorted(map(label_value, a)) == sorted(map(label_value, b)),
    }
