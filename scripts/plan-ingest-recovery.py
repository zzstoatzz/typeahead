"""Summarize the saved recovery evidence without making network calls or writes.

Statuses are observations, not authorization to delete or restore accounts.
Any eventual mutation must recheck current state and serving health first.
"""
import argparse
import json
import sqlite3


def main(args):
    db = sqlite3.connect(f'file:{args.audit}?mode=ro', uri=True)
    db.execute('ATTACH DATABASE ? AS capture', (f'file:{args.capture}?mode=ro',))
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    restoration = 'restoration' if 'restoration' in tables else '(SELECT NULL AS did,NULL AS state WHERE 0)'
    inactive = 'inactive_recovery' if 'inactive_recovery' in tables else '(SELECT NULL AS did,NULL AS state WHERE 0)'
    rows = db.execute(f'''
        SELECT
          CASE
            WHEN i.state = 'verified_absent' THEN 'inactive_removed_and_verified_in_database'
            WHEN i.state = 'already_absent' THEN 'inactive_absent_at_recheck'
            WHEN i.state = 'status_or_policy_changed' THEN 'inactive_status_or_policy_changed'
            WHEN r.state = 'enriched' THEN 'restored_and_profile_enriched'
            WHEN r.state = 'already_present' THEN 'present_at_recheck'
            WHEN r.state = 'now_inactive' THEN 'inactive_at_recheck'
            WHEN a.did IS NULL THEN 'audit_pending'
            WHEN a.present = 0 AND s.active = 1 THEN 'absent_at_audit_relay_active'
            WHEN a.present = 0 AND s.active = 0 THEN 'absent_at_audit_relay_inactive'
            WHEN a.present = 0 THEN 'missing_status_unverified'
            WHEN c.account = 1 AND s.status = 'takendown' THEN 'takedown_preserved_for_fallback_policy'
            WHEN c.account = 1 AND s.active = 0 THEN 'present_inactive_review_policy'
            WHEN c.profile = 1 OR c.identity = 1 THEN 'present_profile_or_identity_candidate'
            ELSE 'present_no_profile_or_identity_event'
          END AS action,
          count(*) AS accounts
        FROM capture.candidates c
        LEFT JOIN audit a USING(did)
        LEFT JOIN repo_status s USING(did)
        LEFT JOIN {restoration} r USING(did)
        LEFT JOIN {inactive} i USING(did)
        GROUP BY action ORDER BY action
    ''').fetchall()
    progress = db.execute('SELECT cursor,end_seq,frames FROM capture.progress').fetchone()
    print(json.dumps({'capture_complete': progress[0] >= progress[1],
                      'captured_frames': progress[2], 'accounts': dict(rows),
                      'mode': 'read_only_plan'}, indent=2))
    db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--audit', required=True)
    main(parser.parse_args())
