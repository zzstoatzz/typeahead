import unittest
from pds_repair_admission import parse_available


class StorageTests(unittest.TestCase):
    def test_df_response_requires_the_serving_mount_and_unsigned_bytes(self):
        self.assertEqual(parse_available('Avail Mounted on\n36137623552 /data\n'),36137623552)
        for text in ['','36137623552 /','-1 /data','nan /data','12 /data extra','１２ /data']:
            with self.assertRaises(ValueError):parse_available(text)


if __name__=='__main__':unittest.main()
