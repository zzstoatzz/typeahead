import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import store_slot_route as route

NOW = 1794386900
SHADOW = 'https://typeahead-shadow-a.fly.dev'
SLOT_A = 'https://typeahead-tantivy-a.fly.dev'
SLOT_B = 'https://typeahead-tantivy-b.fly.dev'
SHADOW_BINARY = '4' * 64
RULES = '3' * 64
POLICY = '1' * 64


def target(origin, build_id, binary='e' * 64, admission='f' * 64):
    return {
        'engine': 'tantivy', 'origin': origin, 'build_id': build_id, 'format': route.FORMAT,
        'admission_sha256': admission, 'binary_sha256': binary, 'retrieval_strategy': 'global-merge',
    }


SERVING = target(SLOT_B, 'b1794377098-cp-serving')
PREVIOUS = target(SLOT_A, 'b1794360174-cp-previous', admission='a' * 64)


def status(build_id, binary, cursor, **changed):
    value = {
        'engine': 'tantivy', 'build_id': build_id, 'format': route.FORMAT, 'binary_sha256': binary,
        'retrieval_strategy': 'global-merge', 'ready': True, 'full_source': True, 'import_complete': True,
        'expiry_coverage_ready': True, 'normalize_version': 'v2-cjk-bigram', 'plan_version': 'complete-prefix-groups-v1',
        'pds_scope_version': 1, 'pds_scope_policy': 'all-recorded-canonical-hosts',
        'actor_count': 13177301, 'watermark': NOW - 20, 'snapshot_watermark': 1794333557,
        'source_observation_revision': 70090, 'cutover_source_ready': False,
        'query_config': {'allow_partial': False, 'allow_stale': False, 'max_sync_lag_s': 120, 'retrieval_strategy': 'global-merge'},
        'moderation': {'enabled': True, 'complete': True, 'fresh': True, 'source_observed_at': NOW - 28, 'sha256': POLICY, 'canonical_rules_source_sha256': RULES},
        'source_cursor': cursor,
    }
    value.update(changed)
    return value


def store_status(**changed):
    cursor = {
        'schema_version': 1, 'source_policy': 'store-change-log-v1', 'source_id': 'typeahead-shadow-a-store', 'cycle': None,
        'build_id': 'b1794333857-cp-bootstrap', 'snapshot_watermark': 1794333557, 'through': NOW - 20, 'position': 13433659,
        'reconciliation': {'schema_version': 1, 'phase': 'idle', 'round': 1, 'last_completed_at': NOW - 27000, 'last_completed_started_at': NOW - 33700},
    }
    return status('b1794333857-cp-bootstrap', SHADOW_BINARY, cursor, **changed)


def turso_status(serving=SERVING, **changed):
    cursor = {'schema_version': 1, 'source_policy': 'turso-authoritative-observations-v2', 'cycle': None, 'build_id': serving['build_id'], 'snapshot_watermark': 1794376798, 'through': NOW - 5}
    return status(serving['build_id'], serving['binary_sha256'], cursor, snapshot_watermark=1794376798, watermark=NOW - 5, actor_count=13177373, **changed)


class Slot:
    """One backend's three read endpoints, with the identity headers the reader sends."""

    def __init__(self, body, ready=True, machine='185d5e7b045118'):
        self.body, self.ready, self.machine, self.down = body, ready, machine, False

    def reply(self, path):
        if self.down:
            raise ConnectionRefusedError('fixture slot is stopped')
        headers = {
            'x-typeahead-engine': 'tantivy', 'x-typeahead-build': self.body['build_id'], 'x-typeahead-format': self.body['format'],
            'x-typeahead-binary-sha256': self.body['binary_sha256'], 'x-typeahead-retrieval-strategy': self.body['retrieval_strategy'],
            'x-typeahead-machine-id': self.machine,
        }
        if path == '/ready':
            return (200 if self.ready else 503), headers, json.dumps({'ready': self.ready}).encode()
        if path == '/health':
            return (200 if self.ready else 503), headers, json.dumps({'ready': self.ready, 'build_id': self.body['build_id']}).encode()
        if path == '/index/status':
            return 200, headers, json.dumps(self.body).encode()
        return 404, {}, b'{}'


class Bucket:
    """Conditional object writes with the semantics the tool relies on in R2."""

    def __init__(self, record):
        self.objects = {}
        self.writes = []
        if record is not None:
            self.objects[route.ROUTE_KEY] = route.encode_record(record)
        self.before_route_put = None

    @staticmethod
    def etag(body):
        return '"' + hashlib.md5(body).hexdigest() + '"'

    def get(self, key):
        body = self.objects.get(key)
        return None if body is None else (body, self.etag(body))

    def put(self, key, body, *, if_match=None, if_none_match=False):
        if key == route.ROUTE_KEY and self.before_route_put is not None:
            hook, self.before_route_put = self.before_route_put, None
            hook()
        existing = self.objects.get(key)
        if if_none_match:
            if existing is not None:
                raise route.Conflict(key)
        elif existing is None or self.etag(existing) != if_match:
            raise route.Conflict(key)
        self.objects[key] = body
        self.writes.append(key)
        return self.etag(body)

    def record(self):
        return json.loads(self.objects[route.ROUTE_KEY])


class World:
    """The slots, the bucket, and a transport that serves whatever the bucket routes."""

    def __init__(self, record=None):
        self.slots = {SHADOW: Slot(store_status()), SLOT_B: Slot(turso_status(), machine='2869394f4d1e58'),
                      SLOT_A: Slot(turso_status(PREVIOUS), machine='e82de19f7d3528')}
        self.bucket = Bucket({'version': 1, 'current': SERVING, 'previous': PREVIOUS} if record is None else record)
        self.transport_follows = True
        self.transport_knows_store_origin = True
        self.clock = 0.0

    def get(self, url):
        for origin, slot in self.slots.items():
            if url.startswith(origin + '/'):
                return slot.reply(url[len(origin):])
        if url.startswith(route.TRANSPORT + '/'):
            current = self.bucket.record()['current'] if self.transport_follows else SERVING
            if current['origin'] == SHADOW and not self.transport_knows_store_origin:
                return 503, {}, b'{"error":"SearchRouteUnavailable"}'
            if url == route.TRANSPORT + '/route':
                return 200, {}, json.dumps(current).encode()
            return self.slots[current['origin']].reply('/health')
        raise AssertionError('unexpected request to ' + url)

    def timing(self):
        def sleep(seconds):
            self.clock += seconds
        return {'get': self.get, 'sleep': sleep, 'clock': lambda: self.clock}

    def cutover(self, write=True):
        return route.cutover(self.bucket, SHADOW, SHADOW_BINARY, write=write, now=NOW, **self.timing())

    def rollback(self, write=True):
        return route.rollback(self.bucket, write=write, **self.timing())


class Admission(unittest.TestCase):
    def admit(self, candidate=None, serving=None, binary=SHADOW_BINARY):
        world = World()
        if candidate is not None:
            world.slots[SHADOW] = candidate
        if serving is not None:
            world.slots[SLOT_B] = serving
        return route.admit(SHADOW, route.observe(SHADOW, world.get), SERVING, route.observe(SLOT_B, world.get), binary, NOW)

    def refused(self, fragment, **kwargs):
        with self.assertRaises(route.Refused) as caught:
            self.admit(**kwargs)
        self.assertTrue(any(fragment in reason for reason in caught.exception.reasons), caught.exception.reasons)

    def test_a_store_fed_slot_is_admitted_without_the_turso_flag(self):
        identity, gates = self.admit()
        self.assertEqual(identity, {'engine': 'tantivy', 'origin': SHADOW, 'build_id': 'b1794333857-cp-bootstrap', 'format': route.FORMAT,
                                    'binary_sha256': SHADOW_BINARY, 'retrieval_strategy': 'global-merge'})
        self.assertIs(gates['cutover_source_ready_reported'], False)
        self.assertEqual((gates['sync_lag_s'], gates['policy_age_s'], gates['reconciliation_age_s'], gates['reconciliation_duration_s']), (20, 28, 27000, 6700))
        self.assertEqual(gates['source_policy'], 'store-change-log-v1')
        self.assertIs(gates['policy_compared_with_serving'], True)

    def test_a_round_older_than_a_day_still_counts_for_a_change_log(self):
        cursor = store_status()['source_cursor']
        settled = {**cursor['reconciliation'], 'last_completed_at': NOW - 5 * 86400, 'last_completed_started_at': NOW - 5 * 86400 - 6700}
        _, gates = self.admit(candidate=Slot(store_status(snapshot_watermark=NOW - 6 * 86400, source_cursor={**cursor, 'snapshot_watermark': NOW - 6 * 86400, 'reconciliation': settled})))
        self.assertEqual(gates['reconciliation_age_s'], 5 * 86400)

    def test_the_turso_flag_cannot_admit_a_slot_that_fails_a_gate(self):
        self.refused('sync lag', candidate=Slot(store_status(cutover_source_ready=True, watermark=NOW - 121)))

    def test_every_gate_refuses(self):
        cursor = store_status()['source_cursor']
        moderation = store_status()['moderation']
        cases = [
            ('/ready is not true', Slot(store_status(), ready=False)),
            ('/index/status is not ready', Slot(store_status(ready=False))),
            ('sync lag', Slot(store_status(watermark=NOW - 121, source_cursor={**cursor, 'through': NOW - 121}))),
            ('sync lag', Slot(store_status(watermark=NOW + 1, source_cursor={**cursor, 'through': NOW + 1}))),
            ('partial or stale', Slot(store_status(query_config={'allow_partial': False, 'allow_stale': True, 'max_sync_lag_s': 120}))),
            ('bound its own sync lag', Slot(store_status(query_config={'allow_partial': False, 'allow_stale': False, 'max_sync_lag_s': 600}))),
            ('full_source', Slot(store_status(full_source=False))),
            ('expiry_coverage_ready', Slot(store_status(expiry_coverage_ready=False))),
            ('normalize_version', Slot(store_status(normalize_version='v3'))),
            ('plan_version', Slot(store_status(plan_version='other-plan'))),
            ('pds_scope_policy', Slot(store_status(pds_scope_policy='other'))),
            ('x-typeahead-format', Slot(store_status(format='tantivy-0.27-f32-did-v1'))),
            ('x-typeahead-retrieval-strategy', Slot(store_status(retrieval_strategy='per-segment'))),
            ('source policy', Slot(store_status(source_cursor={**cursor, 'source_policy': 'turso-authoritative-observations-v2'}))),
            ('source policy', Slot(store_status(source_cursor={**cursor, 'schema_version': 2}))),
            ('middle of a source cycle', Slot(store_status(source_cursor={**cursor, 'cycle': {'after_did': 'did:plc:m'}}))),
            ('does not describe the index', Slot(store_status(source_cursor={**cursor, 'through': NOW - 60}))),
            ('does not describe the index', Slot(store_status(source_cursor={**cursor, 'build_id': 'b1794333857-cp-other'}))),
            ('no source observations', Slot(store_status(source_observation_revision=0))),
            ('since its bootstrap', Slot(store_status(source_cursor={**cursor, 'reconciliation': {'schema_version': 1, 'phase': 'idle', 'last_completed_at': None, 'last_completed_started_at': None}}))),
            ('since its bootstrap', Slot(store_status(source_cursor={**cursor, 'reconciliation': {'schema_version': 1, 'phase': 'idle', 'last_completed_at': 1794333000, 'last_completed_started_at': 1794332000}}))),
            ('middle of a reconciliation round', Slot(store_status(source_cursor={**cursor, 'reconciliation': {**cursor['reconciliation'], 'phase': 'backend'}}))),
            ('moderation policy is missing or stale', Slot(store_status(moderation={**moderation, 'fresh': False}))),
            ('moderation policy is missing or stale', Slot(store_status(moderation={**moderation, 'source_observed_at': NOW - 301}))),
            ('moderation rules differ', Slot(store_status(moderation={**moderation, 'canonical_rules_source_sha256': '9' * 64}))),
            ('moderation policy differs', Slot(store_status(moderation={**moderation, 'sha256': '9' * 64}))),
            ('actors', Slot(store_status(actor_count=6000000))),
        ]
        for fragment, candidate in cases:
            with self.subTest(fragment=fragment):
                self.refused(fragment, candidate=candidate)

    def test_the_binary_must_be_the_one_the_operator_expects(self):
        self.refused('x-typeahead-binary-sha256', binary='5' * 64)
        with self.assertRaises(route.Refused):
            self.admit(binary='not-a-digest')

    def test_a_stopped_store_slot_is_refused(self):
        stopped = Slot(store_status())
        stopped.down = True
        self.refused('valid build id', candidate=stopped)

    def test_the_serving_slot_must_be_the_one_the_record_names(self):
        self.refused('serving slot /index/status build_id', serving=Slot(turso_status({**SERVING, 'build_id': 'b1794377098-cp-replaced'})))

    def test_a_stale_serving_slot_does_not_veto_on_policy_hash(self):
        stale = turso_status()
        stale['moderation'] = {**stale['moderation'], 'fresh': False, 'sha256': '9' * 64}
        _, gates = self.admit(serving=Slot(stale))
        self.assertIs(gates['policy_compared_with_serving'], False)

    def test_only_the_store_origin_can_be_a_candidate(self):
        world = World()
        with self.assertRaises(route.Refused):
            route.admit(SLOT_A, route.observe(SLOT_A, world.get), SERVING, route.observe(SLOT_B, world.get), PREVIOUS['binary_sha256'], NOW)


class Cutover(unittest.TestCase):
    def test_a_plan_writes_nothing(self):
        world = World()
        report = world.cutover(write=False)
        self.assertFalse(report['written'])
        self.assertEqual(world.bucket.writes, [])
        self.assertEqual(report['route']['previous'], SERVING)
        self.assertEqual(report['route']['current']['admission_sha256'], route.digest_json(report['admission']))

    def test_cutover_then_rollback_restores_the_record_the_refresh_controller_wrote(self):
        world = World()
        before = copy.deepcopy(world.bucket.record())
        report = world.cutover()
        self.assertTrue(report['written'])
        routed = world.bucket.record()
        self.assertEqual(set(routed), {'version', 'current', 'previous'})
        self.assertEqual((routed['current']['origin'], routed['current']['binary_sha256'], routed['previous']), (SHADOW, SHADOW_BINARY, SERVING))
        admission = json.loads(world.bucket.objects[route.ADMISSION_PREFIX + routed['current']['admission_sha256'] + '.json'])
        self.assertEqual(admission['prior_route'], before)

        self.assertFalse(world.rollback(write=False)['written'])
        self.assertEqual(world.bucket.record(), routed)
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), before)

    def test_a_refresh_lease_fences_the_cutover(self):
        lease = {'owner': 'typeahead-snapshot-refresh', 'request_id': 'a' * 32, 'app': 'typeahead-tantivy-a', 'acquired_at': NOW - 60}
        world = World({'version': 1, 'current': SERVING, 'previous': PREVIOUS, 'mutation': lease})
        with self.assertRaises(route.Refused) as caught:
            world.cutover()
        self.assertIn('mutation lease', str(caught.exception))
        self.assertEqual(world.bucket.writes, [])

    def test_a_record_that_changes_during_the_decision_wins(self):
        world = World()
        promoted = {'version': 1, 'current': PREVIOUS, 'previous': SERVING}

        def refresh_promotes():
            world.bucket.objects[route.ROUTE_KEY] = route.encode_record(promoted)

        world.bucket.before_route_put = refresh_promotes
        with self.assertRaises(route.Refused) as caught:
            world.cutover()
        self.assertIn('changed while', str(caught.exception))
        self.assertEqual(world.bucket.record(), promoted)

    def test_a_transport_that_does_not_follow_gets_the_prior_record_back(self):
        world = World()
        before = world.bucket.record()
        world.transport_follows = False
        with self.assertRaises(route.Refused) as caught:
            world.cutover()
        self.assertIn('prior record was restored', str(caught.exception))
        self.assertEqual(world.bucket.record(), before)
        self.assertGreaterEqual(world.clock, route.VERIFY_SECONDS)

    def test_a_transport_deployed_before_the_store_origin_gets_the_prior_record_back_at_once(self):
        world = World()
        before = world.bucket.record()
        world.transport_knows_store_origin = False
        with self.assertRaises(route.Refused) as caught:
            world.cutover()
        self.assertIn('cannot use the new record', str(caught.exception))
        self.assertEqual(world.bucket.record(), before)
        self.assertEqual(world.clock, 0)

    def test_unknown_or_unmanaged_records_are_left_alone(self):
        for record in (
            {'version': 1, 'current': SERVING, 'note': 'kept by someone else'},
            {'version': 2, 'current': SERVING},
            {'version': 1, 'current': target('https://typeahead-tantivy-dev.fly.dev', 'b1792908655-dev')},
            {'version': 1, 'current': target(SHADOW, 'b1794333857-cp-bootstrap', SHADOW_BINARY)},
        ):
            with self.subTest(record=record):
                world = World(record)
                with self.assertRaises(route.Refused):
                    world.cutover()
                self.assertEqual(world.bucket.writes, [])

    def test_retired_envelope_fields_are_dropped_as_the_refresh_controller_drops_them(self):
        world = World({'version': 1, 'current': SERVING, 'previous': PREVIOUS, 'native_rollback': {'origin': 'https://typeahead-startup-20261024.fly.dev'}})
        world.cutover()
        world.rollback()
        self.assertEqual(world.bucket.record(), {'version': 1, 'current': SERVING, 'previous': PREVIOUS})


class Rollback(unittest.TestCase):
    def routed_world(self):
        world = World()
        world.cutover()
        return world

    def test_rollback_needs_no_healthy_store_slot(self):
        world = self.routed_world()
        world.slots[SHADOW].down = True
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record()['current'], SERVING)

    def test_a_prior_slot_that_was_rebuilt_or_stopped_is_refused(self):
        for change in ('rebuilt', 'stopped', 'unready'):
            with self.subTest(change=change):
                world = self.routed_world()
                routed = world.bucket.record()
                if change == 'rebuilt':
                    world.slots[SLOT_B] = Slot(turso_status({**SERVING, 'build_id': 'b1794420000-cp-rebuilt'}))
                elif change == 'stopped':
                    world.slots[SLOT_B].down = True
                else:
                    world.slots[SLOT_B].ready = False
                with self.assertRaises(route.Refused):
                    world.rollback()
                self.assertEqual(world.bucket.record(), routed)

    def test_a_tampered_admission_or_record_is_refused(self):
        world = self.routed_world()
        routed = world.bucket.record()
        key = route.ADMISSION_PREFIX + routed['current']['admission_sha256'] + '.json'
        admission = json.loads(world.bucket.objects[key])
        admission['prior_route']['current'] = PREVIOUS
        world.bucket.objects[key] = route.canonical(admission)
        with self.assertRaises(route.Refused):
            world.rollback()
        self.assertEqual(world.bucket.record(), routed)

    def test_rollback_only_leaves_a_store_slot(self):
        world = World()
        with self.assertRaises(route.Refused):
            world.rollback()
        self.assertEqual(world.bucket.writes, [])


class Check(unittest.TestCase):
    def test_check_reads_the_transport_and_writes_nowhere(self):
        world = World()
        report = route.check(SHADOW, SHADOW_BINARY, now=NOW, get=world.get)
        self.assertEqual((report['serving'], report['target']['origin'], report['written']), (SERVING, SHADOW, False))
        self.assertEqual(world.bucket.writes, [])

    def test_check_refuses_once_the_store_slot_is_routed(self):
        world = World()
        world.cutover()
        with self.assertRaises(route.Refused) as caught:
            route.check(SHADOW, SHADOW_BINARY, now=NOW, get=world.get)
        self.assertIn('already routed', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
