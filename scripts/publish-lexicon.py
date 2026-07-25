# /// script
# requires-python = ">=3.11"
# ///
"""publish the lexicon schema record so tech.waow.typeahead.* NSIDs resolve.

writes a com.atproto.lexicon.schema record (rkey = NSID) to the authority DID's
repo. the authority DID is whatever _lexicon.typeahead.waow.tech points at —
currently did:plc:xbtmt2zjwlrfegqvch7fboei (zzstoatzz.io, pds.zzstoatzz.io).

usage:
    ATP_PASSWORD=<app-password> uv run scripts/publish-lexicon.py

idempotent: putRecord overwrites the existing record, so re-run after editing
lexicons/*.json to republish.
"""

import json
import os
import pathlib
import sys
import urllib.request

PDS = "https://pds.zzstoatzz.io"
HANDLE = "zzstoatzz.io"
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
    password = os.environ.get("ATP_PASSWORD")
    if not password:
        sys.exit("set ATP_PASSWORD (an app password for zzstoatzz.io)")

    session = xrpc("com.atproto.server.createSession", {"identifier": HANDLE, "password": password})
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


if __name__ == "__main__":
    main()
