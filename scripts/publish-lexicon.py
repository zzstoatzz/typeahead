# /// script
# requires-python = ">=3.11"
# ///
"""publish the lexicon schema records so tech.waow.typeahead.* NSIDs resolve.

writes each lexicons/*.json as a com.atproto.lexicon.schema record (rkey = NSID)
into the SERVICE account's repo — the @typeahead.waow.tech account, which is
what the _lexicon.typeahead.waow.tech DNS TXT record points at. the service owns
its lexicons; no personal account is involved.

usage:
    ATP_IDENTIFIER=typeahead.waow.tech ATP_PASSWORD=<app-password> \
        uv run scripts/publish-lexicon.py

optional env:
    ATP_PDS  — PDS base URL (default https://pds.zzstoatzz.io)

idempotent: putRecord overwrites, so re-run after editing lexicons/*.json.
"""

import json
import os
import pathlib
import sys
import urllib.request

PDS = os.environ.get("ATP_PDS", "https://pds.zzstoatzz.io")
LEXICON_DIR = pathlib.Path(__file__).parent.parent / "lexicons"


def xrpc(path: str, body: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{PDS}/xrpc/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    identifier = os.environ.get("ATP_IDENTIFIER")
    password = os.environ.get("ATP_PASSWORD")
    if not identifier or not password:
        sys.exit("set ATP_IDENTIFIER (the service account handle/DID) and ATP_PASSWORD")

    session = xrpc("com.atproto.server.createSession", {"identifier": identifier, "password": password})
    did, token = session["did"], session["accessJwt"]
    print(f"authenticated as {did}")

    for path in sorted(LEXICON_DIR.glob("*.json")):
        doc = json.loads(path.read_text())
        nsid = doc["id"]
        record = {"$type": "com.atproto.lexicon.schema", **doc}
        result = xrpc("com.atproto.repo.putRecord", {
            "repo": did,
            "collection": "com.atproto.lexicon.schema",
            "rkey": nsid,
            "record": record,
        }, token)
        print(f"published {nsid} -> {result['uri']}")

    print("\nif this DID differs from the _lexicon.typeahead.waow.tech TXT record, update DNS.")


if __name__ == "__main__":
    main()
