"""An account's identity read from the authorities that define it, not from an appview.

- The DID document: plc.directory for did:plc, the host's own
  `/.well-known/did.json` for did:web. It names the handle (the first `at://`
  entry of `alsoKnownAs`) and the PDS (the `#atproto_pds` service endpoint).
- The handle, both ways: the document claims it, and the handle must resolve
  back to the DID, by a DNS TXT record at `_atproto.<handle>` or by
  `https://<handle>/.well-known/atproto-did`. Either one naming the DID is
  proof. The rules are those of zat's resolver (`handle_resolver.zig`, zat
  0.7.0-alpha.1): TXT answers are read over DNS-over-HTTPS, exactly one may
  carry `did=`, the well-known body is at most 8 KiB, redirects are refused
  and a host that resolves to a non-public address is not contacted. zat
  connects to the address it checked; this module checks and then lets the
  HTTP client resolve again, so the check is advisory here.
- Whether the account is active: `com.atproto.sync.getRepoStatus` on its PDS.
"""

import ipaddress
import re
import socket
import urllib.parse

from transport import Client, TransportError

DOH_PATH = "/dns-query"
DOH_HEADERS = {"Accept": "application/dns-json"}
WELL_KNOWN_HANDLE = "/.well-known/atproto-did"
MAX_HANDLE_BODY = 8 * 1024
DNS_NO_ERROR, DNS_NAME_ERROR = 0, 3
HANDLE_SYNTAX = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
DID_SYNTAX = re.compile(r"^did:[a-z]+:[a-zA-Z0-9._:%-]*[a-zA-Z0-9._-]$")
INVALID_HANDLE = "handle.invalid"

PROVEN, DISPROVEN, UNKNOWN = "proven", "disproven", "unknown"


def host_of(endpoint):
    if not isinstance(endpoint, str):
        return None
    host = urllib.parse.urlsplit(endpoint if "://" in endpoint else "https://" + endpoint).hostname
    return host.rstrip(".").lower() if host else None


def claimed_handle(document):
    """The handle a DID document claims: its first `at://` entry, when that is a handle."""
    for entry in document.get("alsoKnownAs") or []:
        if isinstance(entry, str) and entry.startswith("at://"):
            handle = entry[5:].lower()
            return handle if HANDLE_SYNTAX.match(handle) else None
    return None


def claimed_pds(document):
    """The origin of the PDS a DID document names, or None."""
    for service in document.get("service") or []:
        if not isinstance(service, dict):
            continue
        if str(service.get("id", "")).endswith("#atproto_pds") and service.get("type") == "AtprotoPersonalDataServer":
            parts = urllib.parse.urlsplit(str(service.get("serviceEndpoint", "")))
            if parts.scheme == "https" and parts.hostname and not parts.username:
                return "https://" + parts.netloc.lower()
    return None


def did_document_location(did):
    if did.startswith("did:plc:"):
        return "plc", "/" + did
    if did.startswith("did:web:"):
        name = did[len("did:web:") :]
        if ":" in name or not name:
            return None, None
        return "web", "https://" + urllib.parse.unquote(name)
    return None, None


def did_from_txt(answers):
    """The DID in a DNS-over-HTTPS TXT answer, or None unless exactly one record carries `did=`."""
    found = []
    for answer in answers:
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, str):
            continue
        if len(data) >= 2 and data[0] == '"' and data[-1] == '"':
            data = data[1:-1]
        if data.startswith("did="):
            found.append(data[4:])
    return found[0] if len(found) == 1 and DID_SYNTAX.match(found[0]) else None


def system_addresses(host):
    return {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}


class Unreachable(TransportError):
    """The host has no public address, which settles the question instead of leaving it open."""


class Origins:
    """GET from any https origin through one budget and one pace, refusing hosts without a public address."""

    def __init__(self, budget, *, delay=0.0, timeout=10.0, sleep=None, http=None, addresses=system_addresses):
        self.budget = budget
        self.pacer = Client("identity hosts", "https://identity.invalid", budget, delay=delay, timeout=timeout, retries=0, http=http,
                            **({"sleep": sleep} if sleep else {}))
        self.addresses = addresses
        self.checked = {}

    def close(self):
        self.pacer.close()

    def check(self, host):
        if host not in self.checked:
            try:
                found = self.addresses(host)
            except OSError:
                found = set()
            public = bool(found) and all(ipaddress.ip_address(address.split("%")[0]).is_global for address in found)
            self.checked[host] = None if public else "does not resolve" if not found else "resolves to a non-public address"
        if self.checked[host]:
            raise Unreachable(f"{host} {self.checked[host]}")

    def get(self, origin, path, params=None):
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise Unreachable(f"{origin} is not an https origin")
        self.check(parts.hostname)
        self.pacer.name, self.pacer.base = parts.hostname, origin.rstrip("/")
        return self.pacer.get(path, params)


class Resolver:
    """`plc` and `doh` are transport clients for plc.directory and a DNS-over-HTTPS resolver; `origins` reaches everything else."""

    def __init__(self, plc, doh, origins):
        self.plc = plc
        self.doh = doh
        self.origins = origins
        self.documents = {}
        self.proofs = {}

    def document(self, did):
        """`state` is `found`, `gone` or `unknown`; a found document carries `handle`, `pds` and `pds_host`."""
        if did not in self.documents:
            self.documents[did] = self.read_document(did)
        return self.documents[did]

    def read_document(self, did):
        kind, location = did_document_location(did)
        try:
            if kind == "plc":
                reply = self.plc.get(location)
            elif kind == "web":
                reply = self.origins.get(location, "/.well-known/did.json")
            else:
                return {"state": UNKNOWN, "reason": "unsupported DID method"}
        except Unreachable as error:
            return {"state": "gone", "reason": str(error)}
        except TransportError as error:
            return {"state": UNKNOWN, "reason": str(error)}
        if reply.status == 410:
            return {"state": "gone", "reason": "the directory holds a tombstone"}
        if reply.status == 404:
            return {"state": "gone", "reason": "the authority has no document"}
        if reply.status != 200:
            return {"state": UNKNOWN, "reason": f"authority returned {reply.status}"}
        try:
            document = reply.json()
        except ValueError:
            return {"state": UNKNOWN, "reason": "authority returned a body that is not JSON"}
        if not isinstance(document, dict) or document.get("id") != did:
            return {"state": UNKNOWN, "reason": "DID document does not name this DID"}
        pds = claimed_pds(document)
        return {"state": "found", "handle": claimed_handle(document), "pds": pds, "pds_host": host_of(pds)}

    def by_dns(self, handle):
        try:
            reply = self.doh.get(DOH_PATH, {"name": "_atproto." + handle, "type": "TXT"}, DOH_HEADERS)
        except TransportError as error:
            return UNKNOWN, str(error)
        if reply.status != 200:
            return UNKNOWN, f"the resolver returned {reply.status}"
        try:
            body = reply.json()
            status, answers = body.get("Status"), body.get("Answer") or []
        except (ValueError, AttributeError):
            return UNKNOWN, "the resolver returned a body that is not a DNS answer"
        if status not in (DNS_NO_ERROR, DNS_NAME_ERROR) or not isinstance(answers, list):
            return UNKNOWN, f"the resolver answered with status {status}"
        did = did_from_txt(answers)
        return (PROVEN, did) if did else (DISPROVEN, "no single did= TXT record")

    def by_well_known(self, handle):
        try:
            reply = self.origins.get("https://" + handle, WELL_KNOWN_HANDLE)
        except Unreachable as error:
            return DISPROVEN, str(error)
        except TransportError as error:
            return UNKNOWN, str(error)
        if reply.status >= 500:
            return UNKNOWN, f"the host returned {reply.status}"
        if reply.status != 200 or len(reply.body) > MAX_HANDLE_BODY:
            return DISPROVEN, f"the host returned {reply.status}" if reply.status != 200 else "the well-known body is too large"
        did = reply.body.decode("utf-8", "replace").strip()
        return (PROVEN, did) if DID_SYNTAX.match(did) else (DISPROVEN, "the well-known body is not a DID")

    def handle_proof(self, handle, did):
        """Whether `handle` resolves to `did`: `state` is `proven`, `disproven` or `unknown`."""
        key = (handle, did)
        if key not in self.proofs:
            self.proofs[key] = self.prove(handle, did)
        return self.proofs[key]

    def prove(self, handle, did):
        if not HANDLE_SYNTAX.match(handle):
            return {"state": DISPROVEN, "reason": "not a valid handle"}
        outcomes = {}
        for via, method in (("dns", self.by_dns), ("https", self.by_well_known)):
            state, detail = method(handle)
            if state == PROVEN and detail == did:
                return {"state": PROVEN, "via": via, "reason": f"{via} resolves the handle to this DID"}
            outcomes[via] = (state, "resolves to " + detail if state == PROVEN else detail)
        reason = "; ".join(f"{via}: {detail}" for via, (_, detail) in outcomes.items())
        if any(state == UNKNOWN for state, _ in outcomes.values()):
            return {"state": UNKNOWN, "reason": reason}
        return {"state": DISPROVEN, "reason": reason}

    def handle(self, did):
        """The handle the account holds now: `value` is the proven handle, `handle.invalid`, or None when it cannot be told."""
        document = self.document(did)
        if document["state"] != "found":
            return {"value": None, "reason": "no DID document: " + document["reason"]}
        if not document["handle"]:
            return {"value": INVALID_HANDLE, "reason": "the DID document claims no handle"}
        proof = self.handle_proof(document["handle"], did)
        if proof["state"] == PROVEN:
            return {"value": document["handle"], "reason": f"the DID document claims {document['handle']} and {proof['reason']}"}
        if proof["state"] == DISPROVEN:
            return {"value": INVALID_HANDLE, "reason": f"the DID document claims {document['handle']}, which does not resolve back ({proof['reason']})"}
        return {"value": None, "claimed": document["handle"], "reason": f"the DID document claims {document['handle']}; resolution could not be completed ({proof['reason']})"}

    def repo(self, did):
        """Whether the account's PDS says it is active: `active` is True, False or None."""
        document = self.document(did)
        if document["state"] == "gone":
            return {"active": False, "reason": "no DID document: " + document["reason"]}
        if document["state"] != "found" or not document["pds"]:
            return {"active": None, "reason": "no PDS to ask: " + document.get("reason", "the DID document names none")}
        try:
            reply = self.origins.get(document["pds"], "/xrpc/com.atproto.sync.getRepoStatus", {"did": did})
        except TransportError as error:
            return {"active": None, "reason": str(error)}
        try:
            body = reply.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return {"active": None, "reason": f"{document['pds_host']} returned {reply.status} without a JSON object"}
        if reply.status == 200 and isinstance(body.get("active"), bool):
            status = body.get("status")
            return {"active": body["active"], "reason": f"{document['pds_host']} says the repo is " + ("active" if body["active"] else str(status or "not active"))}
        if reply.status == 400 and body.get("error") == "RepoNotFound":
            return {"active": False, "reason": f"{document['pds_host']} has no such repo"}
        return {"active": None, "reason": f"{document['pds_host']} returned {reply.status} {body.get('error', '')}".rstrip()}
