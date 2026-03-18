#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
one-shot: migrate D1 data to Turso.

reads D1 via Cloudflare REST API (fast), writes to Turso via pipeline API.
uses ON CONFLICT upserts so re-running is safe (idempotent).

prerequisites:
    turso db create typeahead
    turso db shell typeahead < schema.sql

usage:
    TURSO_URL=libsql://... TURSO_AUTH_TOKEN=... ./scripts/migrate-to-turso.py
    TURSO_URL=libsql://... TURSO_AUTH_TOKEN=... ./scripts/migrate-to-turso.py --verify-only
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

PAGE_SIZE = 1000
TURSO_BATCH_SIZE = 200  # rows per Turso pipeline request

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
DIM = "\033[2m"
RESET = "\033[0m"

# D1 config (from wrangler.jsonc)
CF_ACCOUNT_ID = "8feb33b5fb57ce2bc093bc6f4141f40a"
CF_D1_DB_ID = "7e289d5d-dc50-46d1-8084-49aeec2679e5"
D1_API = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_D1_DB_ID}/query"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def get_cf_token() -> str:
    """read wrangler's OAuth token from its config file."""
    config_path = os.path.expanduser(
        "~/Library/Preferences/.wrangler/config/default.toml"
    )
    try:
        with open(config_path) as f:
            for line in f:
                if line.startswith("oauth_token"):
                    return line.split("=", 1)[1].strip().strip('"')
    except FileNotFoundError:
        pass
    # fallback: try CLOUDFLARE_API_TOKEN env var
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if token:
        return token
    print("error: no Cloudflare API token found", file=sys.stderr)
    print("  run `wrangler login` or set CLOUDFLARE_API_TOKEN", file=sys.stderr)
    sys.exit(1)


def get_turso_url() -> str:
    url = os.environ.get("TURSO_URL", "")
    if not url:
        print("error: TURSO_URL not set", file=sys.stderr)
        sys.exit(1)
    return url.replace("libsql://", "https://")


def get_turso_token() -> str:
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not token:
        print("error: TURSO_AUTH_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def d1_query(sql: str, cf_token: str, params: list | None = None) -> list[dict]:
    """query D1 via Cloudflare REST API."""
    payload: dict = {"sql": sql}
    if params:
        payload["params"] = params
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        D1_API,
        data=body,
        headers={
            "Authorization": f"Bearer {cf_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            if data.get("success"):
                return data["result"][0]["results"]
            print(f"  D1 API error: {data.get('errors')}", file=sys.stderr)
            return []
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:300]
        print(f"  D1 HTTP {e.code}: {body_text}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  D1 request failed: {e}", file=sys.stderr)
        return []


def d1_query_wrangler(sql: str) -> list[dict]:
    """fallback: query D1 via wrangler CLI."""
    result = subprocess.run(
        [
            "npx", "wrangler", "d1", "execute", "typeahead-db",
            "--remote", "--command", sql, "--json",
        ],
        capture_output=True, text=True, cwd=".",
    )
    stdout = _ANSI_RE.sub("", result.stdout)
    bracket = stdout.find("[")
    if bracket == -1:
        return []
    try:
        data = json.loads(stdout[bracket:])
        return data[0]["results"] if data else []
    except (json.JSONDecodeError, IndexError, KeyError):
        return []


def turso_batch(stmts: list[dict], turso_url: str, turso_token: str) -> bool:
    """execute a batch of statements against Turso via HTTP pipeline API."""
    requests = [{"type": "execute", "stmt": s} for s in stmts]
    requests.append({"type": "close"})

    body = json.dumps({"requests": requests}).encode()
    req = urllib.request.Request(
        f"{turso_url}/v3/pipeline",
        data=body,
        headers={
            "Authorization": f"Bearer {turso_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            for r in result.get("results", []):
                if r.get("type") == "error":
                    print(f"  Turso error: {r.get('error', {}).get('message', 'unknown')}")
                    return False
            return True
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:300]
        print(f"  Turso HTTP {e.code}: {err_body}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  Turso request failed: {e}", file=sys.stderr)
        return False


def turso_count(table: str, turso_url: str, turso_token: str) -> int | str:
    """get row count from Turso."""
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": {"sql": f"SELECT COUNT(*) AS cnt FROM {table}", "args": []}},
            {"type": "close"},
        ]
    }).encode()
    req = urllib.request.Request(
        f"{turso_url}/v3/pipeline",
        data=body,
        headers={
            "Authorization": f"Bearer {turso_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return int(result["results"][0]["response"]["result"]["rows"][0][0]["value"])
    except Exception as e:
        return f"error: {e}"


def progress(msg: str):
    """overwrite current line with progress."""
    sys.stdout.write(f"\r  {msg}")
    sys.stdout.flush()


def migrate_actors(turso_url: str, turso_token: str, cf_token: str) -> int:
    print("\n--- actors ---")

    # get total for progress reporting
    count_rows = d1_query("SELECT COUNT(*) AS cnt FROM actors", cf_token)
    d1_total = count_rows[0]["cnt"] if count_rows else "?"
    print(f"  D1 has {d1_total} actors")

    cursor = 0
    total = 0

    while True:
        rows = d1_query(
            "SELECT rowid, did, handle, display_name, avatar_url, updated_at, hidden "
            f"FROM actors WHERE rowid > {cursor} ORDER BY rowid ASC LIMIT {PAGE_SIZE}",
            cf_token,
        )
        if not rows:
            break

        for i in range(0, len(rows), TURSO_BATCH_SIZE):
            batch = rows[i : i + TURSO_BATCH_SIZE]
            stmts = []
            for r in batch:
                stmts.append({
                    "sql": (
                        "INSERT INTO actors (did, handle, display_name, avatar_url, updated_at, hidden) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(did) DO UPDATE SET "
                        "handle = COALESCE(NULLIF(excluded.handle, ''), actors.handle), "
                        "display_name = COALESCE(NULLIF(excluded.display_name, ''), actors.display_name), "
                        "avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), actors.avatar_url), "
                        "updated_at = excluded.updated_at, hidden = excluded.hidden"
                    ),
                    "args": [
                        {"type": "text", "value": r["did"]},
                        {"type": "text", "value": r.get("handle") or ""},
                        {"type": "text", "value": r.get("display_name") or ""},
                        {"type": "text", "value": r.get("avatar_url") or ""},
                        {"type": "integer", "value": str(r.get("updated_at") or 0)},
                        {"type": "integer", "value": str(r.get("hidden") or 0)},
                    ],
                })

            if not turso_batch(stmts, turso_url, turso_token):
                print(f"\n  batch failed at cursor={cursor}")
                return total
            total += len(batch)

        cursor = rows[-1]["rowid"]
        pct = f" ({total * 100 // d1_total}%)" if isinstance(d1_total, int) else ""
        progress(f"{total}/{d1_total} actors{pct}   {DIM}cursor={cursor}{RESET}")

    print(f"\r  {PASS} actors: {total} rows" + " " * 30)
    return total


def migrate_table(
    table: str,
    columns: list[str],
    col_types: list[str],
    turso_url: str,
    turso_token: str,
    cf_token: str,
) -> int:
    print(f"\n--- {table} ---")
    col_list = ", ".join(columns)
    rows = d1_query(f"SELECT {col_list} FROM {table}", cf_token)

    if not rows:
        print(f"  {PASS} {table}: 0 rows (empty)")
        return 0

    total = 0
    for i in range(0, len(rows), TURSO_BATCH_SIZE):
        batch = rows[i : i + TURSO_BATCH_SIZE]
        stmts = []
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"

        for r in batch:
            args = []
            for col, ctype in zip(columns, col_types):
                val = r.get(col) or 0
                if ctype == "text":
                    args.append({"type": "text", "value": str(val)})
                elif ctype == "float":
                    args.append({"type": "float", "value": float(val)})
                else:
                    args.append({"type": "integer", "value": str(int(val))})
            stmts.append({"sql": sql, "args": args})

        if not turso_batch(stmts, turso_url, turso_token):
            print(f"  batch failed")
            return total
        total += len(batch)
        progress(f"{total}/{len(rows)} {table}")

    print(f"\r  {PASS} {table}: {total} rows" + " " * 20)
    return total


def verify_counts(turso_url: str, turso_token: str, cf_token: str):
    print("\n--- verification ---")
    tables = ["actors", "metrics", "snapshots"]
    for table in tables:
        d1_rows = d1_query(f"SELECT COUNT(*) AS cnt FROM {table}", cf_token)
        d1_count = d1_rows[0]["cnt"] if d1_rows else "?"
        turso_cnt = turso_count(table, turso_url, turso_token)
        match = str(d1_count) == str(turso_cnt)
        tag = PASS if match else FAIL
        print(f"  [{tag}] {table}: D1={d1_count}, Turso={turso_cnt}")


def main():
    parser = argparse.ArgumentParser(description="migrate D1 → Turso")
    parser.add_argument("--verify-only", action="store_true", help="only compare row counts")
    args = parser.parse_args()

    cf_token = get_cf_token()
    turso_url = get_turso_url()
    turso_token = get_turso_token()

    # quick API check
    test = d1_query("SELECT 1 AS ok", cf_token)
    if not test:
        print("error: D1 API connection failed", file=sys.stderr)
        sys.exit(1)

    if args.verify_only:
        verify_counts(turso_url, turso_token, cf_token)
        return

    print("migrating D1 → Turso")

    migrate_actors(turso_url, turso_token, cf_token)
    migrate_table(
        "metrics",
        ["hour", "searches", "total_ms"],
        ["integer", "integer", "float"],
        turso_url, turso_token, cf_token,
    )
    migrate_table(
        "snapshots",
        ["hour", "total", "with_handles", "with_avatars"],
        ["integer", "integer", "integer", "integer"],
        turso_url, turso_token, cf_token,
    )

    verify_counts(turso_url, turso_token, cf_token)
    print("\ndone.")


if __name__ == "__main__":
    main()
