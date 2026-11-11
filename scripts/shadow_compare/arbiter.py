"""Label kept corpus differences against the authority for the field that differs.

Identity is read from where it is defined (`identity.py`): the DID document
for the PDS, the document plus resolution back for the handle, the PDS's
`getRepoStatus` for whether the account exists. What a search would return is
the moderation rule applied to that handle and the appview's current labels.

Refresh-derived fields come from the appview, because the appview is what
both sides' profile refreshes read: one `getProfiles` per 25 DIDs, mapped to
the values a refresh would store now (`refresh.py`). A side is right when it
holds that value. A refresh never replaces a stored value with an empty one,
so an empty answer faults neither side.
"""

import corpus
import refresh
import views
from identity import INVALID_HANDLE
from moderation import ModerationError
from transport import TransportError

SHADOW_RIGHT = "shadow is right"
PRODUCTION_RIGHT = "production is right"
BOTH_STALE = "both stale"
UNDETERMINED = "undetermined"
LABELS = (SHADOW_RIGHT, PRODUCTION_RIGHT, BOTH_STALE, UNDETERMINED)
PROFILE_BATCH = 25
SCORE_TOLERANCE = 1.0
NEEDS_PROFILE = (*corpus.REFRESH_CLASSES, corpus.VISIBILITY_DIFFERS)
PROFILE_FIELDS = {
    corpus.DISPLAY_NAME_DIFFERS: ("display_name", "display name"),
    corpus.ASSOCIATED_DIFFERS: ("associated", "associated"),
    corpus.CREATED_AT_DIFFERS: ("created_at", "creation time"),
}


def verdict(shadow, production, truth):
    """Which side matches, given whether each does."""
    if shadow and not production:
        return SHADOW_RIGHT, truth
    if production and not shadow:
        return PRODUCTION_RIGHT, truth
    return BOTH_STALE, truth + ("; both sides are within tolerance of it" if shadow else "; neither side holds it")


def side(entry, name, field):
    return (entry[name]["fields"] or {}).get(field)


def label_values(labels):
    return sorted([str(label.get("src", "")), str(label.get("val", ""))] for label in labels if isinstance(label, dict) and not label.get("neg"))


class Authority:
    """`resolver` is an `identity.Resolver`, `appview` a transport client, `policy` the moderation policy the walk used."""

    def __init__(self, resolver, appview, policy, now_ms):
        self.resolver = resolver
        self.appview = appview
        self.policy = policy
        self.now_ms = now_ms
        self.profiles = {}
        self.failures = []

    def load(self, dids):
        """Read the appview's profiles for these DIDs, 25 to a request; a DID it does not return is recorded as not served."""
        wanted = [did for did in dict.fromkeys(dids) if did not in self.profiles]
        for start in range(0, len(wanted), PROFILE_BATCH):
            chunk = wanted[start : start + PROFILE_BATCH]
            try:
                reply = self.appview.get("/xrpc/app.bsky.actor.getProfiles", [("actors", did) for did in chunk])
                if reply.status != 200:
                    raise TransportError(f"getProfiles returned {reply.status}")
                found = {profile.get("did"): profile for profile in reply.json().get("profiles", []) if isinstance(profile, dict)}
            except (TransportError, ValueError, AttributeError) as error:
                self.failures.append(str(error))
                self.profiles.update({did: ("unread", str(error)) for did in chunk})
                continue
            self.profiles.update({did: ("served", found[did]) if did in found else ("absent", None) for did in chunk})

    def profile(self, did):
        """`("served", profile)`, `("absent", None)` or `("unread", why)`."""
        if did not in self.profiles:
            self.load([did])
        return self.profiles[did]

    def stored(self, did):
        """What a refresh would store now, or the reason it cannot be said."""
        state, detail = self.profile(did)
        if state == "served":
            return refresh.stored_fields(detail, self.now_ms), None
        return None, "the appview does not serve the account" if state == "absent" else "the appview could not be read: " + detail

    def presence(self, entry, name):
        did = entry["did"]
        repo = self.resolver.repo(did)
        active, reason = repo["active"], repo["reason"]
        if active is None:
            state, _ = self.profile(did)
            if state != "served":
                return UNDETERMINED, reason + ("; the appview does not serve the account" if state == "absent" else "; the appview could not be read")
            active, reason = True, reason + "; the appview serves the account"
        holder, other = (PRODUCTION_RIGHT, SHADOW_RIGHT) if name == corpus.ONLY_IN_PRODUCTION else (SHADOW_RIGHT, PRODUCTION_RIGHT)
        return (holder if active else other), reason

    def handle(self, entry):
        truth = self.resolver.handle(entry["did"])
        if truth["value"] is None:
            return UNDETERMINED, truth["reason"]

        def holds(name):
            held = (side(entry, name, "handle") or "").lower()
            return held.endswith(".invalid") if truth["value"] == INVALID_HANDLE else held == truth["value"]

        return verdict(holds("shadow"), holds("production"), truth["reason"])

    def pds(self, entry):
        document = self.resolver.document(entry["did"])
        if document["state"] != "found" or not document["pds_host"]:
            return UNDETERMINED, "no PDS in a DID document: " + document.get("reason", "the document names none")
        truth = document["pds_host"]
        return verdict(side(entry, "shadow", "pds_host") == truth, side(entry, "production", "pds_host") == truth, f"the DID document names {truth}")

    def visibility(self, entry):
        did, returned = entry["did"], entry.get("returned")
        handle = self.resolver.handle(did)
        if handle["value"] is None or returned is None:
            return UNDETERMINED, handle["reason"]
        state, profile = self.profile(did)
        if state != "served":
            return UNDETERMINED, "the account's labels cannot be read: the appview " + ("does not serve it" if state == "absent" else "could not be read")
        override = {"show": False, "hide": True}.get(side(entry, "shadow", "override")) if self.policy.source == "none" else None
        try:
            truth = self.policy.returned(did, False, handle["value"], profile.get("labels") or [], self.now_ms, override)
        except ModerationError as error:
            return UNDETERMINED, str(error)
        reason = f"with handle {handle['value']} and the appview's labels a search would {'return' if truth else 'not return'} the account"
        return verdict(returned["shadow"] == truth, returned["production"] == truth, reason)

    def score(self, entry, truth):
        if not truth["has_counts"] or truth["quality_score"] == 0:
            return UNDETERMINED, "the appview gave no counts to score, and a zero score never replaces a stored one"
        gaps = {name: abs((side(entry, name, "quality_score") or 0) - truth["quality_score"]) for name in ("shadow", "production")}
        reason = f"the appview's counts score {truth['quality_score']}; shadow is off by {gaps['shadow']:.2f}, production by {gaps['production']:.2f}"
        return verdict(gaps["shadow"] <= SCORE_TOLERANCE, gaps["production"] <= SCORE_TOLERANCE, reason)

    def refreshed(self, entry, name):
        truth, why = self.stored(entry["did"])
        if truth is None:
            return UNDETERMINED, why
        if name == corpus.QUALITY_SCORE_DIFFERS:
            return self.score(entry, truth)
        if name == corpus.LABELS_DIFFER:
            want = label_values(truth["labels"])
            held = {who: sorted(map(views.label_value, side(entry, who, "labels") or [])) for who in ("shadow", "production")}
            return verdict(held["shadow"] == want, held["production"] == want, f"the appview's labels are {want}")
        if name == corpus.AVATAR_DIFFERS:
            want, what = truth["avatar_cid"], "avatar"
            held = {who: views.avatar_cid(side(entry, who, "avatar_url") or "") for who in ("shadow", "production")}
        elif name in PROFILE_FIELDS:
            field, what = PROFILE_FIELDS[name]
            want = truth[field]
            held = {who: side(entry, who, field) for who in ("shadow", "production")}
        else:
            return UNDETERMINED, "the server returned a profile hash, not fields to compare with the appview"
        if want in ("", "{}"):
            return UNDETERMINED, f"the appview's {what} is empty, and an empty value never replaces a stored one"
        return verdict(held["shadow"] == want, held["production"] == want, f"the appview's {what} is {want}")

    def judge(self, entry, name):
        """The label and its reason for one class a kept difference falls in."""
        if name in (corpus.ONLY_IN_PRODUCTION, corpus.ONLY_IN_SHADOW):
            return self.presence(entry, name)
        if name == corpus.HANDLE_DIFFERS:
            return self.handle(entry)
        if name == corpus.PDS_DIFFERS:
            return self.pds(entry)
        if name == corpus.VISIBILITY_DIFFERS:
            return self.visibility(entry)
        if name in corpus.REFRESH_CLASSES:
            return self.refreshed(entry, name)
        return UNDETERMINED, "the server returned no fields to compare with an authority"


def label_samples(summary, authority):
    """Attach a verdict to every sampled difference, and tally them per class and per refresh stratum."""
    classes = summary["classes"]
    authority.load(item["did"] for name in NEEDS_PROFILE for item in classes[name]["sample"])
    for name, entry in classes.items():
        tallies = {stratum: {} for stratum in corpus.STRATA}
        for item in entry["sample"]:
            label, reason = authority.judge(item, name)
            item.setdefault("arbiter", {})[name] = {"label": label, "reason": reason}
            corpus.bump(tallies[corpus.REFRESHED if item["refreshed"] else corpus.LAGGING], label)
        entry["arbiter_labels"] = {label: sum(tally.get(label, 0) for tally in tallies.values()) for label in LABELS if any(label in tally for tally in tallies.values())}
        for stratum, detail in entry.get("strata", {}).items():
            detail["arbiter_labels"] = tallies[stratum]
            wrong = tallies[stratum].get(PRODUCTION_RIGHT, 0)
            detail["estimated_production_right"] = round(detail["count"] * wrong / detail["sampled"]) if detail["sampled"] else 0
    summary["arbiter_ran"] = True
    summary["arbiter_failures"] = authority.failures
    return summary
