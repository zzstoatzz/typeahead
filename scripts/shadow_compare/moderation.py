"""Whether a search would return an actor, by the rule the Tantivy server applies per query.

A port of `hidden_at_ms` in services/tantivy/src/moderation.rs and the
eligibility test around it in services/tantivy/src/engine.rs. With a policy
loaded the server ignores the stored `hidden` flag and decides from the
actor's labels and handle, the blocked domains and the per-DID overrides.
The slur patterns, the labeler and the hiding values are read from the
artifact the server embeds, `src/moderation-regexes.json`. The blocked
domains and overrides are not in any actor document: they come from a policy
snapshot in the shape the server validates, or are left empty.
"""

import json
import re
from pathlib import Path

from transport import TransportError

RULES_FILE = Path(__file__).resolve().parents[2] / "src" / "moderation-regexes.json"
JS_DATE_LIMIT_MS = 8_640_000_000_000_000
MAX_POLICY_ENTRIES = 10_000
SEPARATORS = str.maketrans("", "", ".-_")
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)
DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:[Zz]|([+-])(\d{2}):(\d{2}))", re.ASCII)
MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class ModerationError(TransportError):
    """The labels or the policy are something the server refuses to evaluate."""


def truthy(value):
    """JavaScript truthiness of a JSON value."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value != ""
    return True


def days_from_civil(year, month, day):
    year -= month <= 2
    era = year // 400
    of_era = year - era * 400
    of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    return era * 146097 + of_era * 365 + of_era // 4 - of_era // 100 + of_year - 719468


def valid_date(year, month, day):
    if not 1 <= month <= 12:
        return False
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 1 <= day <= (29 if month == 2 and leap else MONTH_DAYS[month - 1])


def expiry_ms(value):
    """A label expiry in milliseconds since the epoch, for the forms the server accepts."""
    unsupported = ModerationError("unsupported moderation expiry")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        if abs(value) > JS_DATE_LIMIT_MS:
            raise unsupported
        return int(value)
    if not isinstance(value, str):
        raise unsupported
    match = DATE.fullmatch(value)
    if match:
        year, month, day = map(int, match.groups())
        if not valid_date(year, month, day):
            raise unsupported
        return days_from_civil(year, month, day) * 86_400_000
    match = DATE_TIME.fullmatch(value)
    if not match:
        raise unsupported
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    fraction, sign, offset_hour, offset_minute = match.groups()[6:]
    if not valid_date(year, month, day) or hour > 23 or minute > 59 or second > 59:
        raise unsupported
    offset = 0
    if sign:
        if int(offset_hour) > 23 or int(offset_minute) > 59:
            raise unsupported
        offset = (int(offset_hour) * 60 + int(offset_minute)) * 60_000 * (1 if sign == "+" else -1)
    millis = int((fraction or "")[:3].ljust(3, "0"))
    return days_from_civil(year, month, day) * 86_400_000 + (hour * 3600 + minute * 60 + second) * 1000 + millis - offset


class Rules:
    def __init__(self, artifact):
        try:
            sources = artifact["explicit_slur_regexes"]
            self.hide_values = frozenset(artifact["hide_values"])
            self.labeler = artifact["labeler_did"]
            self.sha256 = artifact["canonical_source_sha256"]
            valid = artifact["schema_version"] == 1 and len(self.sha256) == 64 and 0 < len(sources) <= 64 and self.hide_values and self.labeler.startswith("did:")
        except (KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ModerationError("invalid canonical moderation artifact")
        try:
            self.regexes = [re.compile(source.replace(r"\b", r"(?a:\b)")) for source in sources]
        except re.error:
            raise ModerationError("unsupported canonical moderation regex") from None

    @classmethod
    def load(cls, path=RULES_FILE):
        try:
            return cls(json.loads(Path(path).read_text()))
        except (OSError, ValueError) as error:
            raise ModerationError(f"cannot read the moderation rules at {path}: {error}") from None

    def slur(self, handle):
        stripped = handle.translate(SEPARATORS)
        return any(regex.search(handle) or regex.search(stripped) for regex in self.regexes)

    def labels_hide(self, labels, now_ms):
        if not isinstance(labels, list) or now_ms > JS_DATE_LIMIT_MS:
            raise ModerationError("malformed moderation labels")
        for label in labels:
            if label is None:
                raise ModerationError("malformed moderation labels")
            if not isinstance(label, dict):
                continue
            if truthy(label.get("neg")):
                continue
            if truthy(label.get("exp")) and expiry_ms(label["exp"]) <= now_ms:
                continue
            value = label.get("val")
            if label.get("src") == self.labeler and isinstance(value, str) and value in self.hide_values:
                return True
        return False


class Policy:
    """The rules plus the operator's blocked domains and per-DID overrides."""

    def __init__(self, rules, overrides=None, domains=(), source="none"):
        self.rules = rules
        self.overrides = dict(overrides or {})
        self.domains = frozenset(domain.lower() for domain in domains)
        self.source = source

    @classmethod
    def from_snapshot(cls, rules, snapshot, source="snapshot"):
        """A policy from the snapshot the server loads, refused where the server refuses it."""
        try:
            entries, domains = snapshot["overrides"], snapshot["blocked_domains"]
            valid = (
                snapshot["schema_version"] == 1 and snapshot["complete"] is True and snapshot["source_observed_at"] > 0
                and len(entries) <= MAX_POLICY_ENTRIES and len(domains) <= MAX_POLICY_ENTRIES
                and all(isinstance(domain, str) for domain in domains)
            )
            overrides = {}
            for entry in entries if valid else ():
                did, action = entry["did"], entry["action"]
                if not did.startswith("did:") or len(did) > 2048 or action not in ("show", "hide") or did in overrides:
                    valid = False
                overrides[did] = action == "hide"
        except (KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ModerationError("incomplete or unsupported moderation snapshot")
        return cls(rules, overrides, domains, source)

    def describe(self):
        return {
            "source": self.source,
            "rules_sha256": self.rules.sha256,
            "overrides": len(self.overrides),
            "blocked_domains": len(self.domains),
        }

    def domain_blocked(self, handle):
        if not handle or not self.domains:
            return False
        handle = handle.lower()
        if handle in self.domains:
            return True
        return any(handle[at + 1 :] in self.domains for at, char in enumerate(handle) if char == ".")

    def hidden(self, did, handle, labels, now_ms, override=None):
        """`override` is used for a DID the policy does not name; None leaves it to the rules."""
        hidden = self.rules.labels_hide(labels, now_ms) or self.rules.slur(handle) or self.domain_blocked(handle)
        return self.overrides.get(did, hidden if override is None else override)

    def returned(self, did, deleted, handle, labels, now_ms, override=None):
        """Whether a search could return the actor: live, a usable handle, and not hidden."""
        if deleted or not handle or handle.lower().endswith(".invalid"):
            return False
        return not self.hidden(did, handle, labels, now_ms, override)
