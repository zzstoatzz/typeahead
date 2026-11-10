"""Deterministic synthetic actor JSONL in the stored `actor` shape, for harness checks only."""

import argparse
import json
import random
import sys

BASE32 = "abcdefghijklmnopqrstuvwxyz234567"
WORDS = [
    "river",
    "quiet",
    "pixel",
    "ember",
    "north",
    "atlas",
    "moss",
    "lumen",
    "harbor",
    "violet",
    "copper",
    "delta",
    "sable",
    "juniper",
    "orbit",
    "tidal",
    "willow",
    "onyx",
]
NAMES = [
    "Ada Lovelace",
    "山田 太郎",
    "Zoë Müller",
    "أحمد علي",
    "Søren Ø.",
    "Priya 🌿",
    "João Silva",
    "名前日本太郎",
    "Olu Adébáyò",
    "Kim 민지",
    "Renée d'Arc",
    "Lev Яковлев",
]
HOSTS = [
    "morel",
    "amanita",
    "shiitake",
    "enoki",
    "porcini",
    "lionsmane",
    "oyster",
    "chaga",
]


def token(rng, length):
    return "".join(rng.choice(BASE32) for _ in range(length))


def actor(rng, n):
    did = "did:plc:" + token(rng, 24)
    deleted = rng.random() < 0.005
    custom = rng.random() < 0.08
    stem = f"{rng.choice(WORDS)}{rng.choice(WORDS)}{n % 100000}"
    handle = f"{stem}.{rng.choice(WORDS)}.dev" if custom else f"{stem}.bsky.social"
    if rng.random() < 0.03:
        handle = handle.title()
    host = rng.choice(HOSTS)
    followers = int(rng.paretovariate(1.2)) - 1
    labels = []
    if rng.random() < 0.04:
        labels = [
            {
                "src": did,
                "uri": f"at://{did}/app.bsky.actor.profile/self",
                "cid": "bafyrei" + token(rng, 52),
                "val": rng.choice(["!no-unauthenticated", "porn", "spam"]),
                "cts": "2026-03-01T00:00:00.000Z",
            }
        ]
    associated = {}
    if rng.random() < 0.15:
        associated = {
            "chat": {"allowIncoming": rng.choice(["all", "following", "none"])}
        }
    if rng.random() < 0.02:
        associated["labeler"] = True
    return {
        "did": did,
        "handle": "" if deleted and rng.random() < 0.5 else handle,
        "display_name": ""
        if rng.random() < 0.35
        else f"{rng.choice(NAMES)} {rng.choice(WORDS)}",
        "avatar_url": ""
        if rng.random() < 0.6
        else f"https://cdn.bsky.app/img/avatar/plain/{did}/bafkrei{token(rng, 52)}@jpeg",
        "labels": labels,
        "associated": associated,
        "created_at": f"202{rng.randrange(3, 7)}-{rng.randrange(1, 13):02}-{rng.randrange(1, 29):02}T{rng.randrange(24):02}:{rng.randrange(60):02}:{rng.randrange(60):02}.{rng.randrange(1000):03}Z",
        "pds": f"https://{host}.us-east.host.bsky.network",
        "pds_host": f"{host}.us-east.host.bsky.network",
        "quality_score": 0.0
        if deleted or rng.random() < 0.2
        else round(rng.random() * 40 + followers**0.5, 3),
        "version": 1_760_000_000 + rng.randrange(30_000_000),
        "deleted": deleted,
        "hidden": rng.random() < 0.01,
        "followers_count": followers,
        "follows_count": rng.randrange(2000),
        "posts_count": rng.randrange(20000),
        "last_activity_at": 1_760_000_000 + rng.randrange(30_000_000),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=32)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    for n in range(args.count):
        sys.stdout.write(
            json.dumps(actor(rng, n), ensure_ascii=False, separators=(",", ":")) + "\n"
        )


if __name__ == "__main__":
    main()
