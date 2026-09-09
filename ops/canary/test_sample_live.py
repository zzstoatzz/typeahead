import json
import unittest

from sample_live import Events, extract_query


class SampleTests(unittest.TestCase):
    def event(self, path, query='bs', method='GET', outcome='ok'):
        return {'outcome': outcome, 'event': {'request': {'method': method,
                'url': 'https://typeahead.waow.tech' + path + '?q=' + query}}}

    def test_only_search_requests(self):
        for path in ['/xrpc/tech.waow.typeahead.searchActors',
                     '/xrpc/app.bsky.actor.searchActorsTypeahead']:
            self.assertEqual(extract_query(self.event(path)), 'bs')
        for path in ['/admin/ingest', '/stats', '/debug/search']:
            self.assertIsNone(extract_query(self.event(path)))
        self.assertIsNone(extract_query(self.event('/xrpc/tech.waow.typeahead.searchActors', method='POST')))
        self.assertIsNone(extract_query({'outcome': 'ok', 'event': None}))

    def test_sanitize_and_bounds(self):
        path = '/xrpc/tech.waow.typeahead.searchActors'
        self.assertEqual(extract_query(self.event(path, '%20a_%25%5C%22%00b%20')), 'ab')
        self.assertIsNone(extract_query(self.event(path, 'x' * 257)))
        self.assertIsNone(extract_query(self.event(path, '%20')))
        self.assertIsNone(extract_query(self.event(path, 'a', outcome='exception')))

    def test_multiline_and_split_stream(self):
        event = self.event('/xrpc/tech.waow.typeahead.searchActors')
        stream = Events()
        text = 'Connecting to worker...\n' + json.dumps(event, indent=2) + '\n'
        values = []
        for char in text:
            values.extend(stream.feed(char))
        self.assertEqual(values, [event])
        self.assertEqual(stream.feed(json.dumps(event) + '\n' + json.dumps(event)), [event, event])


if __name__ == '__main__':
    unittest.main()
