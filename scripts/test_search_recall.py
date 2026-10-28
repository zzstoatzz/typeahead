"""Native search regressions: synthetic actors, real builder and loopback HTTP server."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'joint-broad': 'Stored token caps discard joint display-name matches.',
    'joint-reversed': 'Stored token caps discard reversed joint matches.',
    'joint-narrow': None,
    'joint-complete': None,
    'single-selective': None,
    'display-only-complete': None,
    'scoped-joint-broad': 'PDS scoping does not recover discarded joint matches.',
    'scoped-joint-narrow': None,
    'unknown-scope': None,
    'long-handle-builder-cap': 'Stored prefix cap discards a matching long handle.',
    'long-handle-hydration-cap': 'Full handle verification follows the candidate cap.',
    'long-handle-exact-promotion': None,
    'long-display-hydration-cap': 'Full display-token verification follows the candidate cap.',
    'cjk-single-token': 'Unspaced CJK fragments are unioned instead of intersected.',
    'cjk-spaced': None,
    'mixed-script-single-token': 'Unspaced mixed-script fragments are unioned.',
    'mixed-script-spaced': None,
    'query-nine-tokens': 'The ninth query constraint is silently ignored.',
    'display-ninth-token': 'Known coverage limit: only eight display tokens are indexed.',
    'hidden-top50': 'Hidden candidates exhaust hydration without refill.',
    'hidden-top50-scoped': 'Hidden candidates exhaust scoped hydration without refill.',
    'deleted-base-top200': 'Deleted base entries exhaust the stored candidate list.',
    'native-reprojection-rescues': None,
    'native-reprojection-rescues-scoped': None,
    'fresh-rebuild-relapses': 'Rebuild loses matches previously supplied by the live overlay.',
    'rebuild-refills-hidden': None,
    'rebuild-refills-deleted': None,
}


class NativeSearchRecall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory(prefix='typeahead-recall-tests-')
        cls.addClassCleanup(scratch.cleanup)
        output = Path(scratch.name) / 'evidence'
        result = subprocess.run(
            [sys.executable, str(ROOT / 'experiments/recall-scope/run.py'), str(output)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.returncode:
            logs = '\n'.join(
                f'{path.name}:\n{path.read_text(errors="replace")[-6000:]}'
                for path in sorted(output.glob('*.log'))
            )
            raise RuntimeError(f'Native fixture failed:\n{result.stdout}\n{logs}')
        report = json.loads((output / 'results.json').read_text())
        if not all(report.get(key) is True for key in ('complete', 'scratch_removed', 'children_terminal')):
            raise RuntimeError('Native fixture did not complete and clean up.')
        if report.get('error') or report.get('production_access') is not False or report.get('membership_cap_override') is not None:
            raise RuntimeError('Regression fixture must use unchanged native caps and synthetic data.')
        cls.cases = {case['id']: case for case in report['cases']}
        if len(cls.cases) != len(report['cases']) or cls.cases.keys() != CASES.keys():
            raise RuntimeError('Fixture inventory changed; review regression expectations explicitly.')


def regression(case_id, reason):
    def check(self):
        case = self.cases[case_id]
        self.assertCountEqual(case['actual_dids'], case['expected_dids'], msg=f'{case_id}: q={case["query"]!r}, pds={case["pds"]!r}')
    check.__doc__ = reason or case_id
    return unittest.expectedFailure(check) if reason else check


for case_id, reason in CASES.items():
    setattr(NativeSearchRecall, 'test_' + case_id.replace('-', '_'), regression(case_id, reason))


if __name__ == '__main__':
    unittest.main(verbosity=2)
