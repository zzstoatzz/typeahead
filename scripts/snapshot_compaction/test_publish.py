import json
from pathlib import Path
import shutil
import tempfile
import unittest

from publish import Publisher
from recovery_fixture import create


@unittest.skipUnless(shutil.which('rclone'), 'real rclone required')
class Tests(unittest.TestCase):
    def test_verified_pair_retries_and_collision_preserve_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            create(source)
            remote = root / 'remote'
            config = root / 'rclone.conf'
            config.write_text('[r2]\ntype = local\n')
            publisher = Publisher(config, str(remote))
            args = (source / 'builds/compact', source / 'builds/legacy/index.db',
                    source / 'builds/legacy/manifest.json', 'staging/latest.json')
            self.assertEqual(publisher.publish(*args), 'compact')
            pointer = remote / 'staging/latest.json'
            before = pointer.read_bytes()
            self.assertEqual(json.loads(before)['active_build'], 'compact')
            for path in (source / 'builds').rglob('*'):
                if path.is_file():
                    self.assertEqual(path.read_bytes(), (remote / path.relative_to(source)).read_bytes())
            self.assertEqual(publisher.publish(*args), 'compact')
            side = remote / 'builds/compact/index.db.prefix'
            bytes_ = bytearray(side.read_bytes())
            bytes_[-1] ^= 1
            side.write_bytes(bytes_)
            with self.assertRaisesRegex(ValueError, 'remote verification failed'):
                publisher.publish(*args)
            self.assertEqual(pointer.read_bytes(), before)
            self.assertEqual(side.read_bytes(), bytes(bytes_))

    def test_bad_local_pair_never_changes_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            create(source)
            remote = root / 'remote'
            remote.mkdir()
            pointer = remote / 'staging/latest.json'
            pointer.parent.mkdir()
            pointer.write_bytes(b'keep')
            config = root / 'rclone.conf'
            config.write_text('[r2]\ntype = local\n')
            publisher = Publisher(config, str(remote))
            side = source / 'builds/compact/index.db.prefix'
            side.write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError, 'local verification failed'):
                publisher.publish(source / 'builds/compact', source / 'builds/legacy/index.db',
                                  source / 'builds/legacy/manifest.json', 'staging/latest.json')
            self.assertEqual(pointer.read_bytes(), b'keep')
            self.assertFalse((remote / 'builds').exists())


if __name__ == '__main__':
    unittest.main()
