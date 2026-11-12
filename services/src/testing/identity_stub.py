"""Loopback stand-in for everything identity resolution talks to.

One TLS listener answers as plc.directory, as the DNS-over-HTTPS endpoint and
as every handle host, chosen by the Host header. Its certificate is made when
it starts and deleted when it stops. The first four characters of a DID's
identifier pick the behaviour, so callers need no shared state:

  okay  handle <id>.bsky.social, verified over HTTPS
  cust  handle <id>.handles.test, verified over HTTPS
  noha  document without a handle
  wron  the handle host answers with another DID
  miss  the handle host answers 404, and DNS has no record
  dtxt  the handle host answers 404, and DNS TXT verifies
  redi  the handle host redirects
  clos  the handle host closes the connection inside the response
  slow  the handle host never answers
  pmis  the directory answers 404
  pred  the directory redirects
  pclo  the directory closes the connection inside the response

Prints `READY <port> <certificate path>` and runs until stdin closes.
GET /__stats reports connections accepted, handshakes completed and requests
answered. GET /__close_idle?how=notify|abrupt closes every idle connection.
"""

import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

NAMES = ["plc.directory", "cloudflare-dns.com", "*.bsky.social", "*.handles.test"]
PDS = "https://morel.us-east.host.bsky.network"

lock = threading.Lock()
stats = {"accepts": 0, "handshakes": 0, "requests": 0}
close_round = {"n": 0, "how": "notify"}


def response(status, body, content_type="application/json", extra=""):
    reason = {200: "OK", 302: "Found", 404: "Not Found"}[status]
    data = body if isinstance(body, bytes) else body.encode()
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\n{extra}\r\n"
    return head.encode() + data


def document(did, kind):
    handle = None if kind == "noha" else did[8:] + (".handles.test" if kind == "cust" else ".bsky.social")
    return json.dumps(
        {
            "@context": [
                "https://www.w3.org/ns/did/v1",
                "https://w3id.org/security/multikey/v1",
                "https://w3id.org/security/suites/secp256k1-2019/v1",
            ],
            "id": did,
            "alsoKnownAs": [f"at://{handle}"] if handle else [],
            "verificationMethod": [
                {
                    "id": f"{did}#atproto",
                    "type": "Multikey",
                    "controller": did,
                    "publicKeyMultibase": "zQ3shXjHeiBuRCKmM36cuYnm7YEMzhGnCmCyW92sRJ9pribSF",
                }
            ],
            "service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": PDS}],
        }
    )


CUT = object()
STALL = object()


def answer(host, target):
    url = urlsplit(target)
    if url.path == "/__stats":
        with lock:
            return response(200, json.dumps(stats))
    if url.path == "/__close_idle":
        with lock:
            close_round["how"] = parse_qs(url.query).get("how", ["notify"])[0]
            close_round["n"] += 1
        time.sleep(0.3)
        return response(200, "{}")
    if host == "plc.directory":
        did = url.path[1:]
        kind = did[8:12]
        if kind == "pmis":
            return response(404, '{"message":"DID not registered: %s"}' % did)
        if kind == "pred":
            return response(302, "", extra="Location: https://plc.directory/elsewhere\r\n")
        if kind == "pclo":
            return CUT
        return response(200, document(did, kind), "application/did+ld+json")
    if host == "cloudflare-dns.com":
        query = parse_qs(url.query)
        name, kind_of = query["name"][0], query["type"][0]
        body = {"Status": 0, "TC": False, "RD": True, "RA": True, "AD": False, "CD": False}
        body["Question"] = [{"name": name, "type": {"A": 1, "AAAA": 28, "TXT": 16}[kind_of]}]
        if kind_of == "A":
            body["Answer"] = [{"name": name, "type": 1, "TTL": 300, "data": "93.184.216.34"}]
        if kind_of == "TXT" and name.startswith("_atproto.dtxt"):
            label = name.split(".")[1]
            body["Answer"] = [{"name": name, "type": 16, "TTL": 300, "data": f'"did=did:plc:{label}"'}]
        return response(200, json.dumps(body), "application/dns-json")
    label = host.split(".")[0]
    kind = label[:4]
    if url.path != "/.well-known/atproto-did" or kind in ("miss", "dtxt"):
        return response(404, "not found", "text/plain")
    if kind == "wron":
        return response(200, "did:plc:zzzz" + label[4:], "text/plain")
    if kind == "redi":
        return response(302, "", extra="Location: https://elsewhere.bsky.social/.well-known/atproto-did\r\n")
    if kind == "clos":
        return CUT
    if kind == "slow":
        return STALL
    return response(200, "did:plc:" + label, "text/plain")


def serve(raw, context):
    try:
        conn = context.wrap_socket(raw, server_side=True)
    except (OSError, ssl.SSLError):
        raw.close()
        return
    with lock:
        stats["handshakes"] += 1
        seen_round = close_round["n"]
    pending = b""
    try:
        while True:
            while b"\r\n\r\n" not in pending:
                if not conn.pending():
                    ready, _, _ = select.select([conn], [], [], 0.05)
                    if not ready:
                        with lock:
                            asked, how = close_round["n"], close_round["how"]
                        if asked != seen_round and not pending:
                            if how == "notify":
                                conn.unwrap()
                            return
                        continue
                chunk = conn.recv(65536)
                if not chunk:
                    return
                pending += chunk
            head, pending = pending.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            target = lines[0].split(" ")[1]
            host = ""
            for line in lines[1:]:
                name, _, value = line.partition(":")
                if name.lower() == "host":
                    host = value.strip().split(":")[0].lower()
            with lock:
                seen_round = close_round["n"]
            reply = answer(host, target)
            with lock:
                stats["requests"] += 1
                if target.startswith("/__close_idle"):
                    seen_round = close_round["n"]
            if reply is CUT:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 4096\r\n\r\ndid:")
                return
            if reply is STALL:
                select.select([conn], [], [], 30)
                return
            conn.sendall(reply)
    except (OSError, ssl.SSLError, ValueError, IndexError, KeyError):
        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    work = tempfile.mkdtemp(prefix="identity-stub.")
    cert, key = os.path.join(work, "cert.pem"), os.path.join(work, "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=identity-stub",
            "-addext", "subjectAltName=" + ",".join("DNS:" + name for name in NAMES),
            "-addext", "basicConstraints=critical,CA:TRUE",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.create_server(("127.0.0.1", 0), backlog=512)
    print(f"READY {listener.getsockname()[1]} {cert}", flush=True)

    def accept():
        while True:
            raw, _ = listener.accept()
            with lock:
                stats["accepts"] += 1
            threading.Thread(target=serve, args=(raw, context), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        sys.stdin.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
