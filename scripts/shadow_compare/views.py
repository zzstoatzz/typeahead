"""The comparable view of one actor, as every corpus source reports it.

`fields` always carries `deleted`, `hidden`, `handle`, `pds`, `pds_host`,
`quality_score` and `profile_sha256`. A source that holds whole actors also
itemises what that hash covers: `labels` (one canonical JSON text per label,
sorted), `display_name`, `avatar_url`, `associated` (canonical JSON text) and
`created_at`, so a difference can be named instead of reported as a hash. A
source that knows when the actor's content last changed adds `changed_at` in
unix seconds.
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


def text(value):
    return canonical(value).decode("utf-8", "surrogatepass")


def profile_sha256(display_name, avatar_url, labels, associated, created_at):
    """`profile_sha256` as services/tantivy/src/engine/source.rs computes it."""
    return hashlib.sha256(canonical([display_name, avatar_url, labels, associated, created_at])).hexdigest()


def avatar_cid(stored):
    """The CID the public response is built from, as `avatar_cid` in services/tantivy/src/actor.rs."""
    if "/" not in stored:
        return stored
    if "cid=" in stored:
        return stored.split("cid=", 1)[1].split("&")[0]
    part = stored.rsplit("/", 1)[-1].split("@")[0]
    return "" if "?" in part else part


ITEMISED = ("labels", "display_name", "avatar_url", "associated", "created_at")


def itemised(fields):
    return all(fields.get(name) is not None for name in ITEMISED)


def profile_differences(production, shadow):
    """Names of the profile fields that differ, labels aside; the hash alone when a side is not itemised."""
    if not (itemised(production) and itemised(shadow)):
        return ["profile_sha256"] if production.get("profile_sha256") != shadow.get("profile_sha256") else []
    names = [name for name in ("display_name", "associated", "created_at") if production[name] != shadow[name]]
    if avatar_cid(production["avatar_url"]) != avatar_cid(shadow["avatar_url"]):
        names.append("avatar")
    elif production["avatar_url"] != shadow["avatar_url"]:
        names.append("avatar_form")
    return names


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
        "labels": sorted(text(label) for label in labels),
        "display_name": display_name,
        "avatar_url": avatar_url,
        "associated": text(associated),
        "created_at": created_at,
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
