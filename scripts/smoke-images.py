#!/usr/bin/env python3
import argparse
import hashlib
import struct
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--url', required=True)
args = parser.parse_args()
for path, width, height, ttl in [('/apple-touch-icon.png', 180, 180, 86400), ('/og-image', 1200, 630, 3600), ('/og-image?apps=plyr.fm,pckt&range=1h', 1200, 630, 3600)]:
    separator = '&' if '?' in path else '?'
    request = urllib.request.Request(args.url.rstrip('/') + path + separator + 'render_probe=' + str(time.time_ns()), headers={'User-Agent': 'Mozilla/5.0 typeahead-image-smoke/1.0'})
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        assert response.status == 200, (path, response.status)
        assert response.headers.get_content_type() == 'image/png', (path, response.headers.get('Content-Type'))
        assert 'max-age=' + str(ttl) in response.headers.get('Cache-Control', ''), path
    assert len(body) <= 4 * 1024 * 1024, path
    assert body[:8] == b'\x89PNG\r\n\x1a\n', path
    assert body[12:16] == b'IHDR', path
    assert struct.unpack('>II', body[16:24]) == (width, height), path
    print(path, str(width) + 'x' + str(height), 'sha256=' + hashlib.sha256(body).hexdigest())
