"""Label sampled corpus differences against each DID's own authority."""

import re
import urllib.parse

import corpus
from transport import TransportError

SHADOW_RIGHT = "shadow is right"
PRODUCTION_RIGHT = "production is right"
BOTH_STALE = "both stale"
UNDETERMINED = "undetermined"
PROFILE_BATCH = 25
HANDLE_SYNTAX = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


def host_of(endpoint):
    if not isinstance(endpoint, str):
        return None
    host = urllib.parse.urlsplit(endpoint if "://" in endpoint else "https://" + endpoint).hostname
    return host.rstrip(".").lower() if host else None


def claimed_handle(document):
    for entry in document.get("alsoKnownAs") or []:
        if isinstance(entry, str) and entry.startswith("at://"):
            handle = entry[5:].lower()
            if HANDLE_SYNTAX.match(handle):
                return handle
    return None


def claimed_pds(document):
    for service in document.get("service") or []:
        if not isinstance(service, dict):
            continue
        if str(service.get("id", "")).endswith("#atproto_pds") and service.get("type") == "AtprotoPersonalDataServer":
            return host_of(service.get("serviceEndpoint"))
    return None


def did_document_location(did):
    if did.startswith("did:plc:"):
        return "plc", "/" + did
    if did.startswith("did:web:"):
        name = did[len("did:web:") :]
        if ":" in name or not name:
            return None, None
        return "web", "https://" + urllib.parse.unquote(name) + "/.well-known/did.json"
    return None, None


class Authority:
    """`plc` and `appview` are transport clients; `web` builds one for a did:web origin."""

    def __init__(self, plc, appview, web=None):
        self.plc = plc
        self.appview = appview
        self.web = web

    def did_document(self, did):
        kind, location = did_document_location(did)
        if kind == "plc":
            reply = self.plc.get(location)
        elif kind == "web" and self.web is not None:
            parts = urllib.parse.urlsplit(location)
            reply = self.web(parts.scheme + "://" + parts.netloc).get(parts.path)
        else:
            return {"resolved": False, "reason": "unsupported DID method"}
        if reply.status in (404, 410):
            return {"resolved": True, "exists": False}
        if reply.status != 200:
            return {"resolved": False, "reason": f"authority returned {reply.status}"}
        try:
            document = reply.json()
        except ValueError:
            return {"resolved": False, "reason": "authority returned a body that is not JSON"}
        if not isinstance(document, dict) or document.get("id") != did:
            return {"resolved": False, "reason": "DID document does not name this DID"}
        return {"resolved": True, "exists": True, "handle": claimed_handle(document), "pds_host": claimed_pds(document)}

    def profiles(self, dids):
        found = {}
        for start in range(0, len(dids), PROFILE_BATCH):
            chunk = dids[start : start + PROFILE_BATCH]
            reply = self.appview.get("/xrpc/app.bsky.actor.getProfiles", [("actors", did) for did in chunk])
            if reply.status != 200:
                raise TransportError(f"getProfiles returned {reply.status}")
            for profile in reply.json().get("profiles", []):
                found[profile.get("did")] = profile
        return found

    def facts(self, dids):
        try:
            profiles = self.profiles(dids)
            profile_error = None
        except (TransportError, ValueError) as error:
            profiles, profile_error = {}, str(error)
        facts = {}
        for did in dids:
            try:
                document = self.did_document(did)
            except TransportError as error:
                document = {"resolved": False, "reason": str(error)}
            fact = {"document": document, "profile_error": profile_error, "active": None, "handle": None, "pds_host": None}
            if profile_error is None:
                profile = profiles.get(did)
                fact["active"] = profile is not None and bool(document.get("exists", True))
                if document.get("resolved") and document.get("exists") and profile is not None:
                    seen = str(profile.get("handle", "")).lower()
                    if document.get("handle") and seen == document["handle"]:
                        fact["handle"] = seen
            if document.get("resolved") and document.get("exists"):
                fact["pds_host"] = document.get("pds_host")
            facts[did] = fact
        return facts


def side_value(entry, side, name):
    fields = entry[side]["fields"] or {}
    value = fields.get(name)
    return value.lower() if isinstance(value, str) else value


def by_value(entry, name, truth):
    if truth is None:
        return UNDETERMINED, f"the authority gave no verified {name}"
    shadow, production = side_value(entry, "shadow", name), side_value(entry, "production", name)
    if shadow == truth:
        return SHADOW_RIGHT, f"authority {name} is {truth}"
    if production == truth:
        return PRODUCTION_RIGHT, f"authority {name} is {truth}"
    return BOTH_STALE, f"authority {name} is {truth}"


def judge(entry, fact):
    """One label per class the DID falls in."""
    labels = {}
    for name in entry["classes"]:
        if name in (corpus.ONLY_IN_PRODUCTION, corpus.ONLY_IN_SHADOW):
            if fact["active"] is None:
                labels[name] = (UNDETERMINED, "the appview could not be read")
            else:
                holder = PRODUCTION_RIGHT if name == corpus.ONLY_IN_PRODUCTION else SHADOW_RIGHT
                other = SHADOW_RIGHT if name == corpus.ONLY_IN_PRODUCTION else PRODUCTION_RIGHT
                labels[name] = (
                    (holder, "the account is active at its authority and in the appview")
                    if fact["active"]
                    else (other, "the account is gone from its authority or the appview")
                )
        elif name == corpus.HANDLE_DIFFERS:
            labels[name] = by_value(entry, "handle", fact["handle"])
        elif name == corpus.PDS_DIFFERS:
            labels[name] = by_value(entry, "pds_host", fact["pds_host"])
        elif name == corpus.VISIBILITY_DIFFERS:
            labels[name] = (UNDETERMINED, "hiding is decided by the service's own moderation policy; no public authority states it")
        elif name == corpus.LABELS_DIFFER:
            labels[name] = (UNDETERMINED, "labels come from each labeler's stream; no single public authority states the set")
        elif name == corpus.QUALITY_SCORE_DIFFERS:
            labels[name] = (UNDETERMINED, "the quality score is the service's own computation; no public authority states it")
        elif name == corpus.OTHER_FIELD_DIFFERS:
            labels[name] = (UNDETERMINED, "profile content and the raw PDS value are not checked against an authority")
        else:
            labels[name] = (UNDETERMINED, "the server returned no fields to compare with the authority")
    return {name: {"label": label, "reason": reason} for name, (label, reason) in labels.items()}


def class_groups(summary):
    """Every per-class table in a corpus summary: the whole, and each timing partition when present."""
    return [summary["classes"], *(part["classes"] for part in summary.get("timing", {}).values())]


def label_samples(summary, authority):
    """Attach an `arbiter` verdict to every sampled difference in a corpus summary."""
    sampled = {}
    for classes in class_groups(summary):
        for entry in classes.values():
            for item in entry["sample"]:
                sampled[item["did"]] = item
    facts = authority.facts(sorted(sampled))
    for classes in class_groups(summary):
        for name, entry in classes.items():
            tally = {}
            for item in entry["sample"]:
                verdict = judge(item, facts[item["did"]])[name]
                item.setdefault("arbiter", {})[name] = verdict
                tally[verdict["label"]] = tally.get(verdict["label"], 0) + 1
            entry["arbiter_labels"] = tally
    summary["arbiter_ran"] = True
    return summary
