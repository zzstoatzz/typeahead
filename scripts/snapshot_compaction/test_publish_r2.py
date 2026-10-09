import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from publish import Publisher, configuration
from recovery_fixture import create


@unittest.skipUnless(os.environ.get('TYPEAHEAD_TEST_R2') == '1', 'isolated live R2 test requires configuration')
class Tests(unittest.TestCase):
    def test_missing_objects_retry_and_collision_on_r2(self):
        bucket = os.environ['INDEX_R2_BUCKET'] + '/staging/compact-qualification-' + uuid.uuid4().hex
        rclone = os.environ.get('INDEX_RCLONE_BIN', 'rclone')
        with tempfile.TemporaryDirectory() as temporary, configuration(None) as config:
            root = Path(temporary)
            create(root / 'source')
            publisher = Publisher(config, bucket, rclone)
            args = (root / 'source/builds/compact', root / 'source/builds/legacy/index.db',
                    root / 'source/builds/legacy/manifest.json', 'staging/latest.json')
            try:
                self.assertEqual(publisher.publish(*args), 'compact')
                pointer = subprocess.check_output([*publisher.command, 'cat', publisher.remote(args[-1])])
                self.assertEqual(publisher.publish(*args), 'compact')
                corrupt = root / 'corrupt'
                corrupt.write_bytes(b'corrupt qualification object')
                subprocess.run([*publisher.command, 'copyto', str(corrupt),
                                publisher.remote('builds/compact/index.db.prefix')], check=True)
                with self.assertRaisesRegex(ValueError, 'remote verification failed'):
                    publisher.publish(*args)
                self.assertEqual(subprocess.check_output([*publisher.command, 'cat', publisher.remote(args[-1])]), pointer)
                self.assertEqual(subprocess.check_output([*publisher.command, 'cat', publisher.remote('builds/compact/index.db.prefix')]),
                                 corrupt.read_bytes())
            finally:
                subprocess.run([*publisher.command, 'purge', f'r2:{bucket}'], check=True)
                remaining = subprocess.check_output([*publisher.command, 'lsf', '--recursive', '--files-only', f'r2:{bucket}'])
                self.assertEqual(remaining, b'')


if __name__ == '__main__':
    unittest.main()
