"""Counterexample for the proposed two-statement overlay lookup, entirely local.

This intentionally asserts the discrepancy so the risk remains reproducible.
It models a writer commit between the two SELECTs; no threads are required.
"""
import sqlite3


def test_rescore_between_selects_omits_new_top_candidate():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE p(key TEXT, did TEXT, score REAL, op INTEGER, PRIMARY KEY(key,did)) WITHOUT ROWID')
        conn.executemany('INSERT INTO p VALUES(?,?,?,1)',
                         [('k', f'd{i:03}', 100-i) for i in range(51)])
        top = list(conn.execute("SELECT did,score FROM p WHERE key='k' AND op=1 ORDER BY score DESC,did ASC LIMIT 50"))
        # d000 is also in the base. Enrichment lowers its score between reads.
        conn.execute("UPDATE p SET score=-1 WHERE did='d000'")
        corrections = list(conn.execute("SELECT did,score FROM p WHERE key='k' AND did IN ('d000')"))
        combined = sorted(dict(top + corrections).items(), key=lambda row: (-row[1], row[0]))[:50]
        full = list(conn.execute("SELECT did,score FROM p WHERE key='k' AND op=1 ORDER BY score DESC,did ASC LIMIT 50"))
        assert combined[-1] == ('d000', -1.0)
        assert full[-1] == ('d050', 50.0)
        assert 'd050' not in dict(combined)


def test_single_statement_retains_snapshot_across_writer_commit():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / 'overlay.db')
        reader = sqlite3.connect(path)
        writer = sqlite3.connect(path)
        try:
            reader.execute('PRAGMA journal_mode=WAL')
            reader.execute('CREATE TABLE p(key TEXT,did TEXT,score REAL,op INTEGER,PRIMARY KEY(key,did)) WITHOUT ROWID')
            reader.executemany('INSERT INTO p VALUES(?,?,?,1)', [('k',f'd{i:03}',100-i) for i in range(51)])
            reader.commit()
            cursor = reader.execute("SELECT did,score FROM (SELECT did,score FROM p WHERE key='k' AND op=1 ORDER BY score DESC,did ASC LIMIT 50) UNION ALL SELECT did,score FROM p WHERE key='k' AND did IN ('d000')")
            first = cursor.fetchone()
            writer.execute("UPDATE p SET score=-1 WHERE did='d000'")
            writer.commit()
            rows = [first, *cursor.fetchall()]
            assert dict(rows)['d000'] == 100
            assert reader.execute("SELECT score FROM p WHERE did='d000'").fetchone()[0] == -1
        finally:
            reader.close()
            writer.close()


if __name__ == '__main__':
    test_rescore_between_selects_omits_new_top_candidate()
    print('Confirmed: two-statement lookup can omit a replacement candidate during a rescore.')

    test_single_statement_retains_snapshot_across_writer_commit()
    print('Verified: compound SELECT retains one snapshot across a concurrent WAL commit.')
