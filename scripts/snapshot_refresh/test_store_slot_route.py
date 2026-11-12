import contextlib
import copy
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import store_slot_route as route

NOW = 1794386900
SHADOW = 'https://typeahead-shadow-a.fly.dev'
SHADOW_B = 'https://typeahead-shadow-b.fly.dev'
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


def store_status(source_id='typeahead-shadow-a-store', build_id='b1794333857-cp-bootstrap', **changed):
    cursor = {
        'schema_version': 1, 'source_policy': 'store-change-log-v1', 'source_id': source_id, 'cycle': None,
        'build_id': build_id, 'snapshot_watermark': 1794333557, 'through': NOW - 20, 'position': 13433659,
        'reconciliation': {'schema_version': 1, 'phase': 'idle', 'round': 1, 'last_completed_at': NOW - 27000, 'last_completed_started_at': NOW - 33700},
    }
    return status(build_id, SHADOW_BINARY, cursor, **changed)


def store_b_status(**changed):
    """The second store slot: its own store and bootstrap bundle, the same image."""
    return store_status('typeahead-shadow-b-store', 'b1794450000-cp-bootstrap-b', **{'actor_count': 13177350, **changed})


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
        self.slots = {SHADOW: Slot(store_status()), SHADOW_B: Slot(store_b_status(), machine='d8d9947a2e0d08'),
                      SLOT_B: Slot(turso_status(), machine='2869394f4d1e58'), SLOT_A: Slot(turso_status(PREVIOUS), machine='e82de19f7d3528')}
        self.bucket = Bucket({'version': 1, 'current': SERVING, 'previous': PREVIOUS} if record is None else record)
        self.transport_stuck_on = None
        self.transport_origins = {SLOT_A, SLOT_B, SHADOW, SHADOW_B}
        self.clock = 0.0

    def get(self, url):
        for origin, slot in self.slots.items():
            if url.startswith(origin + '/'):
                return slot.reply(url[len(origin):])
        if url.startswith(route.TRANSPORT + '/'):
            current = self.bucket.record()['current'] if self.transport_stuck_on is None else self.transport_stuck_on
            if current['origin'] not in self.transport_origins:
                return 503, {}, b'{"error":"SearchRouteUnavailable"}'
            if url == route.TRANSPORT + '/route':
                return 200, {}, json.dumps(current).encode()
            return self.slots[current['origin']].reply('/health')
        raise AssertionError('unexpected request to ' + url)

    def timing(self):
        def sleep(seconds):
            self.clock += seconds
        return {'get': self.get, 'sleep': sleep, 'clock': lambda: self.clock}

    def cutover(self, write=True, origin=SHADOW, binary=SHADOW_BINARY, **kwargs):
        return route.cutover(self.bucket, origin, binary, write=write, now=NOW, **kwargs, **self.timing())

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

    def test_the_actor_count_is_within_two_percent_of_the_serving_slot_either_way(self):
        serving = turso_status()['actor_count']
        edge = serving * route.ACTOR_TOLERANCE_PERCENT // 100
        for actors in (serving - edge, serving, serving + edge):
            with self.subTest(admitted=actors):
                self.assertEqual(self.admit(candidate=Slot(store_status(actor_count=actors)))[1]['actor_count'], actors)
        for actors in (0, serving // 2 + 1, serving - edge - 1, serving + edge + 1, serving * 2):
            with self.subTest(refused=actors):
                self.refused('actors', candidate=Slot(store_status(actor_count=actors)))

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
        world.transport_stuck_on = SERVING
        with self.assertRaises(route.Refused) as caught:
            world.cutover()
        self.assertIn('prior record was restored', str(caught.exception))
        self.assertEqual(world.bucket.record(), before)
        self.assertGreaterEqual(world.clock, route.VERIFY_SECONDS)

    def test_a_transport_deployed_before_the_store_origin_gets_the_prior_record_back_at_once(self):
        world = World()
        before = world.bucket.record()
        world.transport_origins.discard(SHADOW)
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


class StoreToStore(unittest.TestCase):
    """One store slot takes the route from the other, and gives it back."""

    def world_on_a(self):
        world = World()
        world.cutover()
        return world

    def refused(self, world, fragment, **kwargs):
        before, writes = world.bucket.record(), list(world.bucket.writes)
        with self.assertRaises(route.Refused) as caught:
            world.cutover(**kwargs)
        self.assertTrue(any(fragment in reason for reason in caught.exception.reasons), caught.exception.reasons)
        self.assertEqual((world.bucket.record(), world.bucket.writes), (before, writes))

    def test_the_second_store_slot_takes_the_route_from_the_first(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        plan = world.cutover(write=False, origin=SHADOW_B)
        self.assertEqual((plan['written'], world.bucket.record()), (False, on_a))
        report = world.cutover(origin=SHADOW_B)
        on_b = world.bucket.record()
        self.assertEqual((on_b['current']['origin'], on_b['current']['build_id'], on_b['previous']), (SHADOW_B, 'b1794450000-cp-bootstrap-b', on_a['current']))
        self.assertEqual((report['admission']['gates']['serving_origin'], report['admission']['gates']['source_id']), (SHADOW, 'typeahead-shadow-b-store'))
        self.assertIs(report['admission']['gates']['policy_compared_with_serving'], True)
        admission = json.loads(world.bucket.objects[route.ADMISSION_PREFIX + on_b['current']['admission_sha256'] + '.json'])
        self.assertEqual(admission['prior_route'], on_a)

    def test_rollback_restores_the_record_that_named_the_first_store_slot(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.cutover(origin=SHADOW_B)
        on_b = world.bucket.record()
        self.assertEqual((world.rollback(write=False)['route'], world.bucket.record()), (on_a, on_b))
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), on_a)

    def test_each_rollback_undoes_one_cutover_through_a_b_a(self):
        world = World()
        records = [world.bucket.record()]
        for origin in (SHADOW, SHADOW_B, SHADOW):
            world.cutover(origin=origin)
            records.append(world.bucket.record())
        managed, first_a, on_b, second_a = records
        self.assertEqual((second_a['current']['origin'], second_a['previous']), (SHADOW, on_b['current']))
        self.assertNotEqual(second_a['current']['admission_sha256'], first_a['current']['admission_sha256'])
        for restored in (on_b, first_a, managed):
            routed = world.bucket.record()
            self.assertEqual(world.rollback(write=False)['route']['current'], routed['previous'])
            self.assertTrue(world.rollback()['written'])
            self.assertEqual(world.bucket.record(), restored)
        with self.assertRaises(route.Refused):
            world.rollback()

    def test_two_store_slots_bootstrapped_from_one_bundle_are_still_two_targets(self):
        world = self.world_on_a()
        world.slots[SHADOW_B] = Slot(store_status('typeahead-shadow-b-store'), machine='d8d9947a2e0d08')
        world.cutover(origin=SHADOW_B)
        on_b = world.bucket.record()
        self.assertEqual(on_b['current']['build_id'], on_b['previous']['build_id'])
        self.assertEqual((on_b['current']['origin'], on_b['previous']['origin']), (SHADOW_B, SHADOW))
        world.rollback()
        self.assertEqual(world.bucket.record()['current']['origin'], SHADOW)

    def test_the_serving_slot_is_not_a_candidate(self):
        world = self.world_on_a()
        self.refused(world, 'already routed')
        world.cutover(origin=SHADOW_B)
        self.refused(world, 'already routed', origin=SHADOW_B)
        with self.assertRaises(route.Refused) as caught:
            route.check(SHADOW_B, SHADOW_BINARY, now=NOW, get=world.get)
        self.assertIn('already routed', str(caught.exception))

    def test_the_candidate_is_compared_with_the_serving_store_slot(self):
        moderation = store_status()['moderation']
        serving = store_status()['actor_count']
        cases = [
            ('normalize_version', store_b_status(normalize_version='v3')),
            ('plan_version', store_b_status(plan_version='other-plan')),
            ('pds_scope_policy', store_b_status(pds_scope_policy='other')),
            ('moderation rules differ', store_b_status(moderation={**moderation, 'canonical_rules_source_sha256': '9' * 64})),
            ('moderation policy differs', store_b_status(moderation={**moderation, 'sha256': '9' * 64})),
            ('actors', store_b_status(actor_count=serving - serving * 3 // 100)),
            ('sync lag', store_b_status(watermark=NOW - 121)),
            ('since its bootstrap', {**store_b_status(), 'snapshot_watermark': NOW - 10}),
        ]
        for fragment, body in cases:
            with self.subTest(fragment=fragment):
                world = self.world_on_a()
                world.slots[SHADOW_B] = Slot(body)
                self.refused(world, fragment, origin=SHADOW_B)

    def test_a_serving_store_slot_that_is_not_ready_refuses_an_ordinary_cutover(self):
        for change in ('unready', 'stopped', 'another binary'):
            with self.subTest(change=change):
                world = self.world_on_a()
                if change == 'unready':
                    world.slots[SHADOW].ready = False
                elif change == 'stopped':
                    world.slots[SHADOW].down = True
                else:
                    world.slots[SHADOW] = Slot(store_status(binary_sha256='7' * 64))
                self.refused(world, 'serving slot', origin=SHADOW_B)

    def test_rollback_to_a_store_slot_needs_that_slot_and_not_the_routed_one(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.cutover(origin=SHADOW_B)
        on_b = world.bucket.record()
        world.slots[SHADOW_B].down = True
        for change in ('rebuilt', 'unready', 'stopped'):
            with self.subTest(change=change):
                healthy = world.slots[SHADOW]
                if change == 'rebuilt':
                    world.slots[SHADOW] = Slot(store_status(build_id='b1794500000-cp-rebootstrapped'))
                elif change == 'unready':
                    world.slots[SHADOW] = Slot(store_status(), ready=False)
                else:
                    world.slots[SHADOW] = Slot(store_status())
                    world.slots[SHADOW].down = True
                with self.assertRaises(route.Refused):
                    world.rollback()
                self.assertEqual(world.bucket.record(), on_b)
                world.slots[SHADOW] = healthy
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), on_a)

    def test_a_previous_that_is_not_the_slot_the_cutover_replaced_is_refused(self):
        world = self.world_on_a()
        world.cutover(origin=SHADOW_B)
        on_b = world.bucket.record()
        for previous in (SERVING, {**on_b['previous'], 'admission_sha256': 'd' * 64}, None):
            with self.subTest(previous=previous):
                edited = {'version': 1, 'current': on_b['current'], **({} if previous is None else {'previous': previous})}
                world.bucket.objects[route.ROUTE_KEY] = route.encode_record(edited)
                writes = list(world.bucket.writes)
                with self.assertRaises(route.Refused):
                    world.rollback()
                self.assertEqual((world.bucket.record(), world.bucket.writes), (edited, writes))

    def test_a_record_naming_one_slot_twice_is_left_alone(self):
        world = self.world_on_a()
        current = world.bucket.record()['current']
        world.bucket.objects[route.ROUTE_KEY] = route.encode_record({'version': 1, 'current': current, 'previous': {**current, 'admission_sha256': 'd' * 64}})
        self.refused(world, 'both current and previous', origin=SHADOW_B)
        with self.assertRaises(route.Refused):
            world.rollback()

    def test_a_refresh_lease_fences_a_store_to_store_cutover(self):
        world = self.world_on_a()
        lease = {'owner': 'typeahead-snapshot-refresh', 'request_id': 'a' * 32, 'app': 'typeahead-tantivy-a', 'acquired_at': NOW - 60}
        world.bucket.objects[route.ROUTE_KEY] = route.encode_record({**world.bucket.record(), 'mutation': lease})
        self.refused(world, 'mutation lease', origin=SHADOW_B)

    def test_a_transport_deployed_before_the_second_origin_gets_the_first_slot_back_at_once(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.transport_origins.discard(SHADOW_B)
        clock = world.clock
        with self.assertRaises(route.Refused) as caught:
            world.cutover(origin=SHADOW_B)
        self.assertIn('cannot use the new record', str(caught.exception))
        self.assertEqual((world.bucket.record(), world.clock), (on_a, clock))

    def test_a_transport_that_stays_on_the_first_slot_gets_its_record_back(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.transport_stuck_on = on_a['current']
        with self.assertRaises(route.Refused) as caught:
            world.cutover(origin=SHADOW_B)
        self.assertIn('prior record was restored', str(caught.exception))
        self.assertEqual(world.bucket.record(), on_a)

    def test_a_record_that_changes_during_a_store_to_store_decision_wins(self):
        world = self.world_on_a()
        rolled_back = {'version': 1, 'current': SERVING, 'previous': PREVIOUS}

        def someone_rolls_back():
            world.bucket.objects[route.ROUTE_KEY] = route.encode_record(rolled_back)

        world.bucket.before_route_put = someone_rolls_back
        with self.assertRaises(route.Refused) as caught:
            world.cutover(origin=SHADOW_B)
        self.assertIn('changed while', str(caught.exception))
        self.assertEqual(world.bucket.record(), rolled_back)

    def test_check_compares_the_second_store_slot_with_the_first_without_credentials(self):
        world = self.world_on_a()
        writes = list(world.bucket.writes)
        report = route.check(SHADOW_B, SHADOW_BINARY, now=NOW, get=world.get)
        self.assertEqual((report['serving'], report['target']['origin'], report['written']), (world.bucket.record()['current'], SHADOW_B, False))
        self.assertEqual(world.bucket.writes, writes)


class Failover(unittest.TestCase):
    """The serving slot is not ready, or does not answer, and the other store slot takes the route."""

    def world_on_a(self, failure='stopped'):
        world = World()
        world.cutover()
        if failure == 'stopped':
            world.slots[SHADOW].down = True
        elif failure == 'unready':
            world.slots[SHADOW].ready = False
        elif failure == 'replaced':
            world.slots[SHADOW] = Slot(store_status(build_id='b1794500000-cp-rebootstrapped'))
        return world

    def failover(self, world, origin=SHADOW_B, **kwargs):
        return world.cutover(origin=origin, serving_unhealthy=True, **kwargs)

    def refused(self, world, fragment, **kwargs):
        before, writes = world.bucket.record(), list(world.bucket.writes)
        with self.assertRaises(route.Refused) as caught:
            self.failover(world, **kwargs)
        self.assertTrue(any(fragment in reason for reason in caught.exception.reasons), caught.exception.reasons)
        self.assertEqual((world.bucket.record(), world.bucket.writes), (before, writes))

    def test_a_serving_slot_that_does_not_answer_can_be_left(self):
        world = self.world_on_a('stopped')
        on_a = world.bucket.record()
        with self.assertRaises(route.Refused):
            world.cutover(origin=SHADOW_B)
        plan = self.failover(world, write=False)
        self.assertEqual((plan['written'], world.bucket.record()), (False, on_a))
        report = self.failover(world)
        gates = report['admission']['gates']
        self.assertEqual((gates['serving_healthy'], gates['compared_with_serving'], gates['policy_compared_with_serving'], gates['serving_actor_count']), (False, False, False, None))
        self.assertTrue(any('ConnectionRefusedError' in reason for reason in gates['serving_unhealthy_reasons']))
        on_b = world.bucket.record()
        self.assertEqual((on_b['current']['origin'], on_b['previous']), (SHADOW_B, on_a['current']))
        stored = json.loads(world.bucket.objects[route.ADMISSION_PREFIX + on_b['current']['admission_sha256'] + '.json'])
        self.assertEqual((stored['prior_route'], stored['gates']['compared_with_serving']), (on_a, False))

    def test_a_serving_slot_that_answers_503_is_still_compared(self):
        world = self.world_on_a('unready')
        gates = self.failover(world)['admission']['gates']
        self.assertEqual((gates['serving_healthy'], gates['compared_with_serving'], gates['policy_compared_with_serving']), (False, True, True))
        self.assertEqual(gates['serving_actor_count'], store_status()['actor_count'])
        self.assertEqual(world.bucket.record()['current']['origin'], SHADOW_B)

    def test_what_an_unready_serving_slot_still_reports_can_refuse_the_candidate(self):
        moderation = store_status()['moderation']
        for fragment, body in (
            ('normalize_version', store_b_status(normalize_version='v3')),
            ('moderation rules differ', store_b_status(moderation={**moderation, 'canonical_rules_source_sha256': '9' * 64})),
            ('moderation policy differs', store_b_status(moderation={**moderation, 'sha256': '9' * 64})),
            ('actors', store_b_status(actor_count=6000000)),
        ):
            with self.subTest(fragment=fragment):
                world = self.world_on_a('unready')
                world.slots[SHADOW_B] = Slot(body)
                self.refused(world, fragment)

    def test_a_serving_slot_answering_as_another_build_is_not_a_reference(self):
        world = self.world_on_a('replaced')
        world.slots[SHADOW_B] = Slot(store_b_status(normalize_version='v3'))
        gates = self.failover(world)['admission']['gates']
        self.assertEqual((gates['serving_healthy'], gates['compared_with_serving']), (False, False))

    def test_the_override_is_refused_while_the_serving_slot_is_healthy(self):
        world = self.world_on_a(None)
        self.refused(world, 'only for a serving slot that does not')
        with self.assertRaises(route.Refused):
            route.check(SHADOW_B, SHADOW_BINARY, serving_unhealthy=True, now=NOW, get=world.get)

    def test_every_gate_on_the_candidate_itself_still_applies(self):
        cursor = store_b_status()['source_cursor']
        moderation = store_b_status()['moderation']
        cases = [
            ('/ready is not true', Slot(store_b_status(), ready=False), SHADOW_BINARY),
            ('x-typeahead-binary-sha256', Slot(store_b_status()), '5' * 64),
            ('sync lag', Slot(store_b_status(watermark=NOW - 121, source_cursor={**cursor, 'through': NOW - 121})), SHADOW_BINARY),
            ('partial or stale', Slot(store_b_status(query_config={'allow_partial': True, 'allow_stale': False, 'max_sync_lag_s': 120})), SHADOW_BINARY),
            ('source policy', Slot(store_b_status(source_cursor={**cursor, 'source_policy': 'turso-authoritative-observations-v2'})), SHADOW_BINARY),
            ('does not describe the index', Slot(store_b_status(source_cursor={**cursor, 'through': NOW - 60})), SHADOW_BINARY),
            ('since its bootstrap', Slot(store_b_status(source_cursor={**cursor, 'reconciliation': {'schema_version': 1, 'phase': 'idle', 'last_completed_at': None, 'last_completed_started_at': None}})), SHADOW_BINARY),
            ('middle of a reconciliation round', Slot(store_b_status(source_cursor={**cursor, 'reconciliation': {**cursor['reconciliation'], 'phase': 'backend'}})), SHADOW_BINARY),
            ('moderation policy is missing or stale', Slot(store_b_status(moderation={**moderation, 'fresh': False})), SHADOW_BINARY),
            ('moderation rules hash', Slot(store_b_status(moderation={**moderation, 'canonical_rules_source_sha256': None})), SHADOW_BINARY),
            ('moderation policy hash', Slot(store_b_status(moderation={**moderation, 'sha256': None})), SHADOW_BINARY),
            ('does not report normalize_version', Slot(store_b_status(normalize_version=None)), SHADOW_BINARY),
            ('actors', Slot(store_b_status(actor_count=0)), SHADOW_BINARY),
        ]
        for fragment, candidate, binary in cases:
            with self.subTest(fragment=fragment):
                world = self.world_on_a('stopped')
                world.slots[SHADOW_B] = candidate
                self.refused(world, fragment, binary=binary)
        world = self.world_on_a('stopped')
        world.slots[SHADOW_B].down = True
        self.refused(world, 'valid build id')
        self.refused(world, 'already routed', origin=SHADOW)

    def test_the_slot_that_failed_takes_the_route_back_once_it_answers_again(self):
        world = self.world_on_a('stopped')
        on_a = world.bucket.record()
        self.failover(world)
        on_b = world.bucket.record()
        with self.assertRaises(route.Refused):
            world.rollback()
        with self.assertRaises(route.Refused):
            world.cutover(origin=SHADOW)
        self.assertEqual(world.bucket.record(), on_b)
        world.slots[SHADOW].down = False
        self.assertEqual(world.rollback(write=False)['route'], on_a)
        report = world.cutover(origin=SHADOW)
        self.assertEqual((report['admission']['gates']['serving_origin'], report['admission']['gates']['compared_with_serving']), (SHADOW_B, True))
        self.assertEqual((world.bucket.record()['current']['origin'], world.bucket.record()['previous']), (SHADOW, on_b['current']))

    def test_a_transport_that_does_not_follow_a_failover_gets_the_prior_record_back(self):
        world = self.world_on_a('stopped')
        on_a = world.bucket.record()
        world.transport_origins.discard(SHADOW_B)
        with self.assertRaises(route.Refused) as caught:
            self.failover(world)
        self.assertIn('prior record was restored', str(caught.exception))
        self.assertEqual(world.bucket.record(), on_a)

    def test_a_managed_serving_slot_that_does_not_answer_can_be_left_too(self):
        world = World()
        world.slots[SLOT_B].down = True
        with self.assertRaises(route.Refused):
            world.cutover()
        gates = world.cutover(serving_unhealthy=True)['admission']['gates']
        self.assertEqual((gates['serving_origin'], gates['compared_with_serving']), (SLOT_B, False))
        self.assertEqual(world.bucket.record()['previous'], SERVING)

    def test_check_reports_the_failover_admission_without_credentials(self):
        world = self.world_on_a('unready')
        writes = list(world.bucket.writes)
        with self.assertRaises(route.Refused):
            route.check(SHADOW_B, SHADOW_BINARY, now=NOW, get=world.get)
        report = route.check(SHADOW_B, SHADOW_BINARY, serving_unhealthy=True, now=NOW, get=world.get)
        self.assertEqual((report['target']['origin'], report['gates']['serving_healthy'], report['written']), (SHADOW_B, False, False))
        self.assertEqual(world.bucket.writes, writes)


REBUILT_A = target(SLOT_A, 'b1794420000-cp-standby-a', admission='b' * 64)
REBUILT_B = target(SLOT_B, 'b1794463200-cp-standby-b', admission='c' * 64)


class RollbackAfterStandbyRefresh(unittest.TestCase):
    """my-prefect-server's standby refresh rebuilds the managed slot that is not the
    rollback target, stores the route a rollback should restore, then repoints `previous`."""

    def repoint(self, world, rebuilt, **changed):
        record = world.bucket.record()
        ledger = {
            'schema_version': 1, 'owner': 'typeahead-snapshot-refresh', 'kind': 'standby-rollback-route',
            'store_admission_sha256': record['current']['admission_sha256'], 'request_id': 'a' * 32,
            'route': {'version': 1, 'current': rebuilt, 'previous': record['previous']},
        }
        ledger.update(changed)
        key = f"control/tantivy-standby/{record['current']['admission_sha256']}/{rebuilt['admission_sha256']}.json"
        world.bucket.objects[key] = json.dumps(ledger).encode()
        world.slots[rebuilt['origin']] = Slot(turso_status(rebuilt))
        world.bucket.objects[route.ROUTE_KEY] = route.encode_record({'version': 1, 'current': record['current'], 'previous': rebuilt})
        return key

    def routed_world(self, **changed):
        world = World()
        world.cutover()
        key = self.repoint(world, REBUILT_A, **changed)
        return world, key

    def refused(self, world):
        routed, writes = world.bucket.record(), list(world.bucket.writes)
        for write in (False, True):
            with self.assertRaises(route.Refused) as caught:
                world.rollback(write=write)
            self.assertIn('no standby refresh recorded', str(caught.exception))
        self.assertEqual((world.bucket.record(), world.bucket.writes), (routed, writes))

    def test_rollback_restores_the_freshly_rebuilt_slot(self):
        world, _ = self.routed_world()
        routed = world.bucket.record()
        plan = world.rollback(write=False)
        self.assertEqual((plan['written'], world.bucket.record()), (False, routed))
        self.assertEqual(plan['route'], {'version': 1, 'current': REBUILT_A, 'previous': SERVING})
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), {'version': 1, 'current': REBUILT_A, 'previous': SERVING})

    def test_each_standby_refresh_moves_the_rollback_target_to_the_slot_it_rebuilt(self):
        world, _ = self.routed_world()
        self.repoint(world, REBUILT_B)
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), {'version': 1, 'current': REBUILT_B, 'previous': REBUILT_A})

    def test_the_restored_current_is_always_the_previous_of_the_routed_record(self):
        for repoints in ((), (REBUILT_A,), (REBUILT_A, REBUILT_B)):
            with self.subTest(repoints=len(repoints)):
                world = World()
                world.cutover()
                for rebuilt in repoints:
                    self.repoint(world, rebuilt)
                previous = world.bucket.record()['previous']
                self.assertEqual(world.rollback(write=False)['route']['current'], previous)
                world.rollback()
                self.assertEqual(world.bucket.record()['current'], previous)

    def test_a_ledger_that_restores_another_slot_is_refused(self):
        world, key = self.routed_world()
        routed, writes = world.bucket.record(), list(world.bucket.writes)
        ledger = json.loads(world.bucket.objects[key])
        ledger['route'] = {'version': 1, 'current': SERVING, 'previous': REBUILT_A}
        world.bucket.objects[key] = json.dumps(ledger).encode()
        with self.assertRaises(route.Refused):
            world.rollback()
        self.assertEqual((world.bucket.record(), world.bucket.writes), (routed, writes))

    def test_a_ledger_of_another_kind_schema_or_store_admission_is_refused(self):
        for changed in ({'kind': 'store-slot-route-admission'}, {'schema_version': 2}, {'store_admission_sha256': 'd' * 64}):
            with self.subTest(changed=changed):
                self.refused(self.routed_world(**changed)[0])

    def test_a_ledger_that_is_not_an_object_is_refused(self):
        world, key = self.routed_world()
        world.bucket.objects[key] = b'[]'
        self.refused(world)

    def test_a_repointed_record_without_a_ledger_is_refused(self):
        world, key = self.routed_world()
        del world.bucket.objects[key]
        self.refused(world)

    def test_a_ledger_stored_for_another_rollback_target_is_not_read(self):
        world, key = self.routed_world()
        world.bucket.objects[key.replace(REBUILT_A['admission_sha256'], 'd' * 64)] = world.bucket.objects.pop(key)
        self.refused(world)


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


class CommandLine(unittest.TestCase):
    def parse(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                return route.build_parser().parse_args(argv)
            except SystemExit:
                return stderr.getvalue()

    def test_the_slot_to_route_to_is_always_named(self):
        for command in ('check', 'cutover'):
            with self.subTest(command=command):
                self.assertIn('--origin', self.parse(command, '--binary-sha256', SHADOW_BINARY))
                self.assertEqual(self.parse(command, '--origin', SHADOW, '--binary-sha256', SHADOW_BINARY).origin, SHADOW)

    def test_only_a_store_slot_can_be_named(self):
        self.assertIn('invalid choice', self.parse('cutover', '--origin', SLOT_A, '--binary-sha256', SHADOW_BINARY))

    def test_the_failover_override_is_never_implied(self):
        for command in ('check', 'cutover'):
            self.assertIs(self.parse(command, '--origin', SHADOW_B, '--binary-sha256', SHADOW_BINARY).serving_slot_unhealthy, False)
            self.assertIs(self.parse(command, '--origin', SHADOW_B, '--binary-sha256', SHADOW_BINARY, '--serving-slot-unhealthy').serving_slot_unhealthy, True)
        self.assertIn('unrecognized', self.parse('rollback', '--serving-slot-unhealthy'))

    def test_nothing_is_written_unless_asked(self):
        self.assertIs(self.parse('cutover', '--origin', SHADOW, '--binary-sha256', SHADOW_BINARY).write, False)
        self.assertIs(self.parse('rollback').write, False)


if __name__ == '__main__':
    unittest.main()
