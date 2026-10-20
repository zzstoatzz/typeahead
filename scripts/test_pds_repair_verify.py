import struct
import unittest
from pds_repair_verify import unpack_pds


class BlobTests(unittest.TestCase):
    def test_version_one_and_truncation(self):
        fields=[b'custom.test','Name 日本'.encode(),b'',b'[]',b'https://pds.test',b'',b'{}']
        blob=bytes([1,0])+struct.pack('<f',123)+b''.join(struct.pack('<H',len(f))+f for f in fields)
        self.assertEqual(unpack_pds(blob),'https://pds.test')
        for bad in [blob[:5],blob[:-1],blob+b'x',bytes([2])+blob[1:]]:
            with self.assertRaises(ValueError):unpack_pds(bad)


if __name__=='__main__':unittest.main()
