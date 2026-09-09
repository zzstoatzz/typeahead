import contextlib
import io
import json
import runpy
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path

main = runpy.run_path(str(Path(__file__).with_name('plan-ingest-recovery.py')))['main']


class RecoveryPlan(unittest.TestCase):
    def test_actions_replace_stale_presence_without_double_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root/'capture.sqlite'; audit = root/'audit.sqlite'
            with sqlite3.connect(capture) as c:
                c.executescript('''CREATE TABLE candidates(did TEXT,profile INTEGER,identity INTEGER,account INTEGER);
                    INSERT INTO candidates VALUES('inactive',0,0,1),('restored',1,0,0),('pending',0,0,0);
                    CREATE TABLE progress(cursor INTEGER,end_seq INTEGER,frames INTEGER);
                    INSERT INTO progress VALUES(3,3,3);''')
            with sqlite3.connect(audit) as c:
                c.executescript('''CREATE TABLE audit(did TEXT,present INTEGER);
                    INSERT INTO audit VALUES('inactive',1),('restored',0);
                    CREATE TABLE repo_status(did TEXT,active INTEGER,status TEXT);
                    INSERT INTO repo_status VALUES('inactive',0,'deleted'),('restored',1,'active');''')
            def result():
                output = io.StringIO()
                with contextlib.redirect_stdout(output): main(types.SimpleNamespace(audit=audit,capture=capture))
                return json.loads(output.getvalue())['accounts']
            self.assertEqual(result(), {'audit_pending':1,'absent_at_audit_relay_active':1,'present_inactive_review_policy':1})
            with sqlite3.connect(audit) as c:
                c.executescript('''CREATE TABLE restoration(did TEXT,state TEXT);
                    INSERT INTO restoration VALUES('restored','enriched');
                    CREATE TABLE inactive_recovery(did TEXT,state TEXT);
                    INSERT INTO inactive_recovery VALUES('inactive','verified_absent');''')
            self.assertEqual(result(), {'audit_pending':1,'restored_and_profile_enriched':1,'inactive_removed_and_verified_in_database':1})


if __name__ == '__main__': unittest.main()
