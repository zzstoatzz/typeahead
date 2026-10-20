#!/usr/bin/env python3
import argparse
from pathlib import Path
import random
import sqlite3


def main():
    parser = argparse.ArgumentParser(description="Create isolated synthetic PDS search fixtures")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.db"
    if source.exists() or (root / "acme.db").exists():
        parser.error("fixture files already exist; use a fresh directory")
    rng = random.Random(113)
    rows = []
    for i in range(100000):
        host = "acme.test" if i < 9998 else "big.test"
        handle = f"bob{i}.custom.test" if i % 2 else f"person{i}.custom.test"
        name = ["Bob Smith", "佐藤 太郎", "Éloïse Müller", "Bob definitelyarealcompany"][i % 4]
        rows.append((f"did:plc:{i:024d}", handle, name, "", "[]", "https://" + host,
                     "", "{}", int(i % 997 == 0), i + rng.random(), 1800000000))
    for did, handle, pds, hidden, score in [
        ("exact", "bob.test", "https://acme.test", 0, -1),
        ("spoof", "bob.acme.test", "https://elsewhere.test", 0, 999999),
        ("subdomain", "bob.sub.test", "https://sub.acme.test", 0, 999998),
        ("unknown", "bob.unknown.test", "", 0, 999997),
        ("hidden", "bob.hidden.test", "https://acme.test", 1, 999996),
    ]:
        rows.append(("did:plc:" + did, handle, "Bob", "", "[]", pds, "", "{}", hidden, score, 1800000000))
    with sqlite3.connect(source) as db:
        db.execute("""CREATE TABLE actors(
            did TEXT PRIMARY KEY, handle TEXT, display_name TEXT, avatar_url TEXT,
            labels TEXT, pds TEXT, created_at TEXT, associated TEXT,
            hidden INTEGER, quality_score REAL, updated_at INTEGER)""")
        db.executemany("INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    with sqlite3.connect(root / "acme.db") as db:
        db.execute("ATTACH DATABASE ? AS src", (str(source),))
        db.execute("CREATE TABLE actors AS SELECT * FROM src.actors WHERE pds='https://acme.test'")
    print(root, len(rows))


if __name__ == "__main__":
    main()
