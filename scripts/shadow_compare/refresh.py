"""What a profile refresh stores for an appview profile, so a stored value can be checked against the appview now.

A port of `extractProfileFields` and `computeAuthorityScore` in src/utils.ts,
which services/src/store_process/profile.zig and authority_score.zig also
port; all three are held to the same vectors. The score here uses the
platform's `log10` where the Worker uses V8's, which can differ in the last
bit before rounding to two decimals; the vectors pass and the arbiter compares
scores within a tolerance.
"""

import math
import re
import urllib.parse

import views
from moderation import date_time_ms, truthy

ASSOCIATED_KEYS = ("lists", "feedgens", "starterPacks", "labeler", "chat", "activitySubscription")
DAY_MS = 86_400_000
CID_PARAMETER = re.compile(r"[?&]cid=([^&]+)")
LAST_SEGMENT = re.compile(r"/([^/]+?)(?:@[a-z]+)?$")


def js_round(value):
    """`Math.round`: the nearest integer, a tie going towards positive infinity."""
    floor = math.floor(value)
    return floor + 1 if value - floor >= 0.5 else floor


def count(value):
    return max(0, value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def authority_score(followers, follows, posts, created_ms, now_ms):
    followers, follows, posts = count(followers), count(follows), count(posts)
    score = math.log10(followers + 1) * 100 + math.log10(posts + 1) * 20
    if followers > 100 and follows > 0:
        ratio = followers / follows
        score += 50 * (ratio > 10) + 100 * (ratio > 100)
    if created_ms is not None:
        age_days = (now_ms - created_ms) / DAY_MS
        score += -100 if age_days < 1 else -50 if age_days < 3 else 0
        score += 30 * (age_days > 365) + 30 * (age_days > 365 * 3)
    return js_round(max(0, score) * 100) / 100


def avatar_cid(stored):
    """`extractAvatarCid`: the CID out of a bare CID, a getBlob URL or a CDN URL."""
    if not stored or "/" not in stored:
        return stored or ""
    parameter = CID_PARAMETER.search(stored)
    if parameter:
        return urllib.parse.unquote(parameter.group(1))
    segment = LAST_SEGMENT.search(stored)
    return segment.group(1) if segment else ""


def clean_associated(associated):
    if not isinstance(associated, dict):
        return {}
    return {key: associated[key] for key in ASSOCIATED_KEYS if truthy(associated.get(key))}


def stored_fields(profile, now_ms):
    """The refresh-derived fields a refresh at `now_ms` would store for this appview profile."""
    created_at = profile.get("createdAt") or ""
    counts = [profile.get(name) for name in ("followersCount", "followsCount", "postsCount")]
    score = 0.0
    if any(value is not None for value in counts):
        score = authority_score(*counts, date_time_ms(created_at) if created_at else None, now_ms)
    return {
        "handle": profile.get("handle") or "",
        "display_name": profile.get("displayName") or "",
        "avatar_cid": avatar_cid(profile.get("avatar") or ""),
        "labels": profile.get("labels") or [],
        "created_at": created_at,
        "associated": views.text(clean_associated(profile.get("associated"))),
        "followers_count": count(counts[0]),
        "follows_count": count(counts[1]),
        "posts_count": count(counts[2]),
        "quality_score": score,
        "has_counts": any(value is not None for value in counts),
    }
