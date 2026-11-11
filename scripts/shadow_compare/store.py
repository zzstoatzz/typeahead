"""The store process's read surface as the comparison needs it, and a corpus source over it.

Both requests are POSTs with a JSON body and a bearer secret:

- `PAGE_PATH`, `{"after_did": "<did or empty>", "limit": <1..500>}` answers
  `{"actors": [record, ...]}`: every record whose DID is greater than
  `after_did`, in ascending byte order, at most `limit`. Fewer than `limit`
  means the walk is over. Records of any status are included.
- `BATCH_PATH`, `{"dids": [<at most 500 DIDs>]}` answers
  `{"actors": [record or null, ...]}` in request order, null where the store
  has no record.

Either reply may carry `next_change`, the sequence the next change-log entry
will take; it is recorded as the store's position and nothing depends on it.

A record names its fields as `services/src/store/record.zig` does:

    {"did": "...", "status": "live" | "deactivated" | "deleted", "hidden": false,
     "handle": "", "display_name": "", "avatar": "", "pds": "", "created_at": "",
     "associated": "<JSON object text, empty for none>", "quality_score": 0.0,
     "updated_at": <unix seconds of the last content change>,
     "labels": [{"source": "", "uri": "", "value": "", "created": "",
                 "cid": "", "expires": "", "negated": false}]}

`did`, `status`, `handle`, `quality_score` and `updated_at` are required. The
store also sends `profile_checked_at` (unix seconds of the last profile
refresh, or of the bootstrap load for a record not refreshed since),
`identity_resolved_at` and `override` (`none`, `show` or `hide`); the view
keeps them as `refreshed_at`, `identity_at` and `override`.

The view of a record is the document the index bridge sends for it, so a
store and an index fed from it compare equal: a record that is not live or
has no handle is a tombstone, negated labels are withdrawn, and a label
expiry the index cannot evaluate is dropped.
"""

import functools
import hashlib
import json
import time

import views
from transport import TransportError
from views import ABSENT, TOMBSTONE_KEY, Row

PAGE_PATH = "/store/actors/page"
BATCH_PATH = "/store/actors/batch"
SECRET_PREFIX = "/store/"
PAGE_LIMIT = 500
BATCH_LIMIT = 500
STATUSES = ("live", "deactivated", "deleted")
MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


@functools.lru_cache(maxsize=65536)
def pds_host(pds):
    """The canonical PDS host, as `source_host` over services/src/index/pds.zig."""
    if not pds or len(pds.encode()) > 263:
        return ""
    value = pds
    if value[:8].lower() == "https://":
        value = value[8:]
    elif value[:7].lower() == "http://":
        value = value[7:]
    if value.endswith("/"):
        value = value[:-1]
    if value.endswith("."):
        value = value[:-1]
    if not value or len(value) > 253 or "." not in value or not value.isascii():
        return ""
    for label in value.split("."):
        if not label or len(label) > 63 or label[0] == "-" or label[-1] == "-":
            return ""
        if any(not (c.isalnum() or c == "-") for c in label):
            return ""
    return value.lower()


def digits(text):
    return int(text) if text and all("0" <= c <= "9" for c in text) else None


def supported_expiry(text):
    """Whether the index bridge forwards a label expiry, as `supportedExpiry` in its wire module."""
    if len(text) < 20 or text[4] != "-" or text[7] != "-" or text[10] != "T" or text[13] != ":" or text[16] != ":":
        return False
    parts = [digits(text[a:b]) for a, b in ((0, 4), (5, 7), (8, 10), (11, 13), (14, 16), (17, 19))]
    if None in parts:
        return False
    year, month, day, hour, minute, second = parts
    if not 1 <= month <= 12:
        return False
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_days = 29 if month == 2 and leap else MONTH_DAYS[month - 1]
    if year < 1000 or day < 1 or day > month_days or hour > 23 or minute > 59 or second > 59:
        return False
    at = 19
    if text[at] == ".":
        at += 1
        start = at
        while at < len(text) and "0" <= text[at] <= "9":
            at += 1
        if at == start or at - start > 9 or at == len(text):
            return False
    if text[at] == "Z":
        return at + 1 == len(text)
    if text[at] not in "+-" or len(text) != at + 6 or text[at + 3] != ":":
        return False
    offset_hours, offset_minutes = digits(text[at + 1 : at + 3]), digits(text[at + 4 : at + 6])
    return offset_hours is not None and offset_minutes is not None and offset_hours <= 23 and offset_minutes <= 59


def index_labels(labels):
    sent = []
    for label in labels:
        if label.get("negated"):
            continue
        entry = {"src": label.get("source", ""), "uri": label.get("uri", ""), "val": label.get("value", ""), "cts": label.get("created", "")}
        if label.get("cid"):
            entry["cid"] = label["cid"]
        if supported_expiry(label.get("expires", "")):
            entry["exp"] = label["expires"]
        sent.append(entry)
    return sent


NUMBERS = (int, float)


def checked(record, name):
    """A record from a reply, or a TransportError naming what is wrong with it."""
    try:
        sound = (
            type(record["did"]) is str and type(record["handle"]) is str and record["status"] in STATUSES
            and type(record["quality_score"]) in NUMBERS and type(record["updated_at"]) in NUMBERS
        )
        labels = record.get("labels")
    except (KeyError, TypeError, AttributeError):
        sound, labels = False, None
    if not sound:
        raise TransportError(f"{name} returned a record without a did, status, handle, quality_score or updated_at")
    if labels is not None and not (type(labels) is list and all(type(label) is dict for label in labels)):
        raise TransportError(f"{name} returned a record with invalid labels")
    return record


def moment(record, name):
    value = record.get(name)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@functools.lru_cache(maxsize=65536)
def associated_text(associated):
    return views.text(json.loads(associated) if associated else {})


def row(record):
    """The record as a walk row: its key is the key of the view `view` would build."""
    live = record["status"] == "live" and bool(record["handle"])
    key = TOMBSTONE_KEY
    if live:
        pds, labels, associated = record.get("pds", ""), record.get("labels"), record.get("associated") or ""
        key = views.comparison_key(
            True, record["handle"], bool(record.get("hidden", False)), pds, pds_host(pds), views.f32(record["quality_score"]),
            record.get("display_name", ""), record.get("avatar", ""),
            associated_text(associated) if isinstance(associated, str) else views.text(associated), record.get("created_at", ""),
            sorted(views.text(label) for label in index_labels(labels)) if labels else [],
        )
    return Row(key, live, view, record, int(record["updated_at"]), moment(record, "profile_checked_at"))


def view(record):
    sha = hashlib.sha256(views.canonical(record)).hexdigest()
    if record["status"] != "live" or not record["handle"]:
        doc = views.from_actor({"deleted": True}, sha=sha, changed_at=int(record["updated_at"]))
    else:
        pds = record.get("pds", "")
        associated = record.get("associated") or {}
        doc = views.from_actor(
            {
                "deleted": False,
                "hidden": bool(record.get("hidden", False)),
                "handle": record["handle"],
                "display_name": record.get("display_name", ""),
                "avatar_url": record.get("avatar", ""),
                "labels": index_labels(record.get("labels", [])),
                "associated": json.loads(associated) if isinstance(associated, str) else associated,
                "created_at": record.get("created_at", ""),
                "pds": pds,
                "pds_host": pds_host(pds),
                "quality_score": record["quality_score"],
            },
            sha=sha,
            changed_at=int(record["updated_at"]),
        )
    doc.fields["store_status"] = record["status"]
    for name, source in (("refreshed_at", "profile_checked_at"), ("identity_at", "identity_resolved_at")):
        if moment(record, source) is not None:
            doc.fields[name] = moment(record, source)
    if record.get("override") in ("show", "hide"):
        doc.fields["override"] = record["override"]
    return doc


class StoreHttp:
    """The shadow's store, read through the store process's authenticated HTTP endpoints."""

    fixed = False
    want_fields = True

    def __init__(self, client):
        self.client = client
        self.name = client.name
        self.identity = {"kind": "store-http", "base": client.base}

    def marker(self):
        reply = self.client.post_json(BATCH_PATH, {"dids": []})
        return {"build_id": None, "watermark": None, "next_change": reply.get("next_change"), "observed_at": int(time.time())}

    def records(self, reply, path):
        actors = reply.get("actors") if isinstance(reply, dict) else None
        if not isinstance(actors, list):
            raise TransportError(f"{self.name} {path} returned no actors list")
        return actors

    def read(self, after, limit):
        if not 1 <= limit <= PAGE_LIMIT:
            raise TransportError(f"page size must be 1..{PAGE_LIMIT}")
        reply = self.client.post_json(PAGE_PATH, {"after_did": after, "limit": limit})
        try:
            return [(record["did"], row(record)) for record in (checked(r, self.name) for r in self.records(reply, PAGE_PATH))]
        except ValueError:
            raise TransportError(f"{self.name} returned a record whose associated is not JSON") from None

    def lookup(self, dids):
        found = {}
        for start in range(0, len(dids), BATCH_LIMIT):
            chunk = dids[start : start + BATCH_LIMIT]
            actors = self.records(self.client.post_json(BATCH_PATH, {"dids": chunk}), BATCH_PATH)
            if len(actors) != len(chunk):
                raise TransportError(f"{self.name} did not answer every requested DID")
            for did, record in zip(chunk, actors):
                if record is None:
                    found[did] = ABSENT
                elif checked(record, self.name)["did"] != did:
                    raise TransportError(f"{self.name} did not answer the requested DIDs in order")
                else:
                    found[did] = view(record)
        return found
