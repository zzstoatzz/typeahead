import contextlib
import copy
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import slot_route as route

NOW = 1794386900
SLOT_A = 'https://typeahead-shadow-a.fly.dev'
SLOT_B = 'https://typeahead-shadow-b.fly.dev'
DESTROYED = 'https://typeahead-tantivy-a.fly.dev'
BINARY = '4' * 64
RULES = '3' * 64
POLICY = '1' * 64


def target(origin, build_id, binary='e' * 64, admission='f' * 64):
    return {
        'engine': 'tantivy', 'origin': origin, 'build_id': build_id, 'format': route.FORMAT,
        'admission_sha256': admission, 'binary_sha256': binary, 'retrieval_strategy': 'global-merge',
    }


# The world starts with slot B serving and slot A as the slot it replaced.
SERVING = target(SLOT_B, 'b1794450000-cp-bootstrap-b', BINARY)
PREVIOUS = target(SLOT_A, 'b1794333857-cp-bootstrap', BINARY, admission='a' * 64)


def status(build_id, binary, cursor, **changed):
    value = {
        'engine': 'tantivy', 'build_id': build_id, 'format': route.FORMAT, 'binary_sha256': binary,
        'retrieval_strategy': 'global-merge', 'ready': True, 'full_source': True, 'import_complete': True,
        'expiry_coverage_ready': True, 'normalize_version': 'v2-cjk-bigram', 'plan_version': 'complete-prefix-groups-v1',
        'pds_scope_version': 1, 'pds_scope_policy': 'all-recorded-canonical-hosts',
        'actor_count': 13177301, 'watermark': NOW - 20, 'snapshot_watermark': 1794333557,
        'source_observation_revision': 70090,
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
    return status(build_id, BINARY, cursor, **changed)


def store_b_status(**changed):
    """The second store slot: its own store and its own build id, the same image."""
    return store_status('typeahead-shadow-b-store', 'b1794450000-cp-bootstrap-b', **{'actor_count': 13177350, **changed})


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
        self.slots = {SLOT_A: Slot(store_status()), SLOT_B: Slot(store_b_status(), machine='d8d9947a2e0d08')}
        self.bucket = Bucket({'version': 1, 'current': SERVING, 'previous': PREVIOUS} if record is None else record)
        self.transport_stuck_on = None
        self.transport_origins = {SLOT_A, SLOT_B}
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

    def cutover(self, write=True, origin=SLOT_A, binary=BINARY, **kwargs):
        return route.cutover(self.bucket, origin, binary, write=write, now=NOW, **kwargs, **self.timing())

    def rollback(self, write=True):
        return route.rollback(self.bucket, write=write, **self.timing())


class Admission(unittest.TestCase):
    def admit(self, candidate=None, serving=None, binary=BINARY):
        world = World()
        if candidate is not None:
            world.slots[SLOT_A] = candidate
        if serving is not None:
            world.slots[SLOT_B] = serving
        return route.admit(SLOT_A, route.observe(SLOT_A, world.get), SERVING, route.observe(SLOT_B, world.get), binary, NOW)

    def refused(self, fragment, **kwargs):
        with self.assertRaises(route.Refused) as caught:
            self.admit(**kwargs)
        self.assertTrue(any(fragment in reason for reason in caught.exception.reasons), caught.exception.reasons)

    def test_a_healthy_slot_is_admitted_against_the_serving_slot(self):
        identity, gates = self.admit()
        self.assertEqual(identity, {'engine': 'tantivy', 'origin': SLOT_A, 'build_id': 'b1794333857-cp-bootstrap', 'format': route.FORMAT,
                                    'binary_sha256': BINARY, 'retrieval_strategy': 'global-merge'})
        self.assertEqual((gates['sync_lag_s'], gates['policy_age_s'], gates['reconciliation_age_s'], gates['reconciliation_duration_s']), (20, 28, 27000, 6700))
        self.assertEqual(gates['source_policy'], 'store-change-log-v1')
        self.assertIs(gates['policy_compared_with_serving'], True)

    def test_a_round_older_than_a_day_still_counts_for_a_change_log(self):
        cursor = store_status()['source_cursor']
        settled = {**cursor['reconciliation'], 'last_completed_at': NOW - 5 * 86400, 'last_completed_started_at': NOW - 5 * 86400 - 6700}
        _, gates = self.admit(candidate=Slot(store_status(snapshot_watermark=NOW - 6 * 86400, source_cursor={**cursor, 'snapshot_watermark': NOW - 6 * 86400, 'reconciliation': settled})))
        self.assertEqual(gates['reconciliation_age_s'], 5 * 86400)

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
            ('source policy', Slot(store_status(source_cursor={**cursor, 'source_policy': 'some-other-policy'}))),
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
        serving = store_b_status()['actor_count']
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
        self.refused('serving slot /index/status build_id', serving=Slot(store_status('typeahead-shadow-b-store', 'b1794450000-cp-replaced')))

    def test_a_stale_serving_slot_does_not_veto_on_policy_hash(self):
        stale = store_b_status()
        stale['moderation'] = {**stale['moderation'], 'fresh': False, 'sha256': '9' * 64}
        _, gates = self.admit(serving=Slot(stale))
        self.assertIs(gates['policy_compared_with_serving'], False)

    def test_only_a_store_slot_can_be_a_candidate(self):
        world = World()
        with self.assertRaises(route.Refused) as caught:
            route.admit(DESTROYED, route.observe(SLOT_A, world.get), SERVING, route.observe(SLOT_B, world.get), BINARY, NOW)
        self.assertIn('not a store slot origin', str(caught.exception))


class Cutover(unittest.TestCase):
    def test_a_plan_writes_nothing(self):
        world = World()
        report = world.cutover(write=False)
        self.assertFalse(report['written'])
        self.assertEqual(world.bucket.writes, [])
        self.assertEqual(report['route']['previous'], SERVING)
        self.assertEqual(report['route']['current']['admission_sha256'], route.digest_json(report['admission']))

    def test_cutover_then_rollback_restores_the_record_that_was_there(self):
        world = World()
        before = copy.deepcopy(world.bucket.record())
        report = world.cutover()
        self.assertTrue(report['written'])
        routed = world.bucket.record()
        self.assertEqual(set(routed), {'version', 'current', 'previous'})
        self.assertEqual((routed['current']['origin'], routed['current']['binary_sha256'], routed['previous']), (SLOT_A, BINARY, SERVING))
        admission = json.loads(world.bucket.objects[route.ADMISSION_PREFIX + routed['current']['admission_sha256'] + '.json'])
        self.assertEqual(admission['prior_route'], before)

        self.assertFalse(world.rollback(write=False)['written'])
        self.assertEqual(world.bucket.record(), routed)
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), before)

    def test_a_record_that_changes_during_the_decision_wins(self):
        world = World()
        promoted = {'version': 1, 'current': PREVIOUS, 'previous': SERVING}

        def someone_else_writes():
            world.bucket.objects[route.ROUTE_KEY] = route.encode_record(promoted)

        world.bucket.before_route_put = someone_else_writes
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
        world.transport_origins.discard(SLOT_A)
        with self.assertRaises(route.Refused) as caught:
            world.cutover()
        self.assertIn('cannot use the new record', str(caught.exception))
        self.assertEqual(world.bucket.record(), before)
        self.assertEqual(world.clock, 0)

    def test_records_this_tool_does_not_understand_are_left_alone(self):
        for record in (
            {'version': 1, 'current': SERVING, 'note': 'kept by someone else'},
            {'version': 1, 'current': SERVING, 'previous': PREVIOUS, 'mutation': {'owner': 'someone', 'acquired_at': NOW - 60}},
            {'version': 2, 'current': SERVING},
            {'version': 1, 'current': target(DESTROYED, 'b1792908655-cp-destroyed')},
            {'version': 1, 'current': SERVING, 'previous': target(DESTROYED, 'b1792908655-cp-destroyed', admission='a' * 64)},
            {'version': 1, 'current': target(SLOT_A, 'b1794333857-cp-bootstrap', BINARY)},
        ):
            with self.subTest(record=record):
                world = World(record)
                with self.assertRaises(route.Refused):
                    world.cutover()
                self.assertEqual(world.bucket.writes, [])


class Rollback(unittest.TestCase):
    def routed_world(self):
        world = World()
        world.cutover()
        return world

    def test_rollback_needs_no_healthy_store_slot(self):
        world = self.routed_world()
        world.slots[SLOT_A].down = True
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record()['current'], SERVING)

    def test_a_prior_slot_that_was_rebuilt_or_stopped_is_refused(self):
        for change in ('rebuilt', 'stopped', 'unready'):
            with self.subTest(change=change):
                world = self.routed_world()
                routed = world.bucket.record()
                if change == 'rebuilt':
                    world.slots[SLOT_B] = Slot(store_status('typeahead-shadow-b-store', 'b1794420000-cp-rebuilt'))
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

    def test_rollback_needs_the_admission_this_tool_stored(self):
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
        plan = world.cutover(write=False, origin=SLOT_B)
        self.assertEqual((plan['written'], world.bucket.record()), (False, on_a))
        report = world.cutover(origin=SLOT_B)
        on_b = world.bucket.record()
        self.assertEqual((on_b['current']['origin'], on_b['current']['build_id'], on_b['previous']), (SLOT_B, 'b1794450000-cp-bootstrap-b', on_a['current']))
        self.assertEqual((report['admission']['gates']['serving_origin'], report['admission']['gates']['source_id']), (SLOT_A, 'typeahead-shadow-b-store'))
        self.assertIs(report['admission']['gates']['policy_compared_with_serving'], True)
        admission = json.loads(world.bucket.objects[route.ADMISSION_PREFIX + on_b['current']['admission_sha256'] + '.json'])
        self.assertEqual(admission['prior_route'], on_a)

    def test_rollback_restores_the_record_that_named_the_first_store_slot(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.cutover(origin=SLOT_B)
        on_b = world.bucket.record()
        self.assertEqual((world.rollback(write=False)['route'], world.bucket.record()), (on_a, on_b))
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), on_a)

    def test_each_rollback_undoes_one_cutover_through_a_b_a(self):
        world = World()
        records = [world.bucket.record()]
        for origin in (SLOT_A, SLOT_B, SLOT_A):
            world.cutover(origin=origin)
            records.append(world.bucket.record())
        start, first_a, on_b, second_a = records
        self.assertEqual((second_a['current']['origin'], second_a['previous']), (SLOT_A, on_b['current']))
        self.assertNotEqual(second_a['current']['admission_sha256'], first_a['current']['admission_sha256'])
        for restored in (on_b, first_a, start):
            routed = world.bucket.record()
            self.assertEqual(world.rollback(write=False)['route']['current'], routed['previous'])
            self.assertTrue(world.rollback()['written'])
            self.assertEqual(world.bucket.record(), restored)
        with self.assertRaises(route.Refused):
            world.rollback()

    def test_two_slots_with_one_build_id_are_still_two_targets(self):
        world = self.world_on_a()
        world.slots[SLOT_B] = Slot(store_status('typeahead-shadow-b-store'), machine='d8d9947a2e0d08')
        world.cutover(origin=SLOT_B)
        on_b = world.bucket.record()
        self.assertEqual(on_b['current']['build_id'], on_b['previous']['build_id'])
        self.assertEqual((on_b['current']['origin'], on_b['previous']['origin']), (SLOT_B, SLOT_A))
        world.rollback()
        self.assertEqual(world.bucket.record()['current']['origin'], SLOT_A)

    def test_changed_moderation_rules_are_admitted_only_when_their_hash_is_named(self):
        world = self.world_on_a()
        moderation = store_status()['moderation']
        world.slots[SLOT_B] = Slot(store_b_status(moderation={**moderation, 'canonical_rules_source_sha256': '9' * 64}))
        self.refused(world, 'moderation rules differ', origin=SLOT_B)
        self.refused(world, 'not the ones named', origin=SLOT_B, rules_sha256='8' * 64)
        report = world.cutover(origin=SLOT_B, rules_sha256='9' * 64)
        gates = report['admission']['gates']
        self.assertEqual((gates['canonical_rules_source_sha256'], gates['rules_compared_with_serving']), ('9' * 64, False))
        self.assertEqual(world.bucket.record()['current']['origin'], SLOT_B)

    def test_naming_a_rules_hash_does_not_admit_unchanged_rules_under_another_name(self):
        world = self.world_on_a()
        self.refused(world, 'not the ones named', origin=SLOT_B, rules_sha256='9' * 64)
        self.assertIs(world.cutover(origin=SLOT_B)['admission']['gates']['rules_compared_with_serving'], True)

    def test_the_serving_slot_is_not_a_candidate(self):
        world = self.world_on_a()
        self.refused(world, 'already routed')
        world.cutover(origin=SLOT_B)
        self.refused(world, 'already routed', origin=SLOT_B)
        with self.assertRaises(route.Refused) as caught:
            route.check(SLOT_B, BINARY, now=NOW, get=world.get)
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
                world.slots[SLOT_B] = Slot(body)
                self.refused(world, fragment, origin=SLOT_B)

    def test_a_serving_store_slot_that_is_not_ready_refuses_an_ordinary_cutover(self):
        for change in ('unready', 'stopped', 'another binary'):
            with self.subTest(change=change):
                world = self.world_on_a()
                if change == 'unready':
                    world.slots[SLOT_A].ready = False
                elif change == 'stopped':
                    world.slots[SLOT_A].down = True
                else:
                    world.slots[SLOT_A] = Slot(store_status(binary_sha256='7' * 64))
                self.refused(world, 'serving slot', origin=SLOT_B)

    def test_rollback_to_a_store_slot_needs_that_slot_and_not_the_routed_one(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.cutover(origin=SLOT_B)
        on_b = world.bucket.record()
        world.slots[SLOT_B].down = True
        for change in ('rebuilt', 'unready', 'stopped'):
            with self.subTest(change=change):
                healthy = world.slots[SLOT_A]
                if change == 'rebuilt':
                    world.slots[SLOT_A] = Slot(store_status(build_id='b1794500000-cp-rebootstrapped'))
                elif change == 'unready':
                    world.slots[SLOT_A] = Slot(store_status(), ready=False)
                else:
                    world.slots[SLOT_A] = Slot(store_status())
                    world.slots[SLOT_A].down = True
                with self.assertRaises(route.Refused):
                    world.rollback()
                self.assertEqual(world.bucket.record(), on_b)
                world.slots[SLOT_A] = healthy
        self.assertTrue(world.rollback()['written'])
        self.assertEqual(world.bucket.record(), on_a)

    def test_a_previous_that_is_not_the_slot_the_cutover_replaced_is_refused(self):
        world = self.world_on_a()
        world.cutover(origin=SLOT_B)
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
        self.refused(world, 'both current and previous', origin=SLOT_B)
        with self.assertRaises(route.Refused):
            world.rollback()

    def test_a_transport_deployed_before_the_second_origin_gets_the_first_slot_back_at_once(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.transport_origins.discard(SLOT_B)
        clock = world.clock
        with self.assertRaises(route.Refused) as caught:
            world.cutover(origin=SLOT_B)
        self.assertIn('cannot use the new record', str(caught.exception))
        self.assertEqual((world.bucket.record(), world.clock), (on_a, clock))

    def test_a_transport_that_stays_on_the_first_slot_gets_its_record_back(self):
        world = self.world_on_a()
        on_a = world.bucket.record()
        world.transport_stuck_on = on_a['current']
        with self.assertRaises(route.Refused) as caught:
            world.cutover(origin=SLOT_B)
        self.assertIn('prior record was restored', str(caught.exception))
        self.assertEqual(world.bucket.record(), on_a)

    def test_a_record_that_changes_during_a_store_to_store_decision_wins(self):
        world = self.world_on_a()
        rolled_back = {'version': 1, 'current': SERVING, 'previous': PREVIOUS}

        def someone_rolls_back():
            world.bucket.objects[route.ROUTE_KEY] = route.encode_record(rolled_back)

        world.bucket.before_route_put = someone_rolls_back
        with self.assertRaises(route.Refused) as caught:
            world.cutover(origin=SLOT_B)
        self.assertIn('changed while', str(caught.exception))
        self.assertEqual(world.bucket.record(), rolled_back)

    def test_check_compares_the_second_store_slot_with_the_first_without_credentials(self):
        world = self.world_on_a()
        writes = list(world.bucket.writes)
        report = route.check(SLOT_B, BINARY, now=NOW, get=world.get)
        self.assertEqual((report['serving'], report['target']['origin'], report['written']), (world.bucket.record()['current'], SLOT_B, False))
        self.assertEqual(world.bucket.writes, writes)


class Failover(unittest.TestCase):
    """The serving slot is not ready, or does not answer, and the other store slot takes the route."""

    def world_on_a(self, failure='stopped'):
        world = World()
        world.cutover()
        if failure == 'stopped':
            world.slots[SLOT_A].down = True
        elif failure == 'unready':
            world.slots[SLOT_A].ready = False
        elif failure == 'replaced':
            world.slots[SLOT_A] = Slot(store_status(build_id='b1794500000-cp-rebootstrapped'))
        return world

    def failover(self, world, origin=SLOT_B, **kwargs):
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
            world.cutover(origin=SLOT_B)
        plan = self.failover(world, write=False)
        self.assertEqual((plan['written'], world.bucket.record()), (False, on_a))
        report = self.failover(world)
        gates = report['admission']['gates']
        self.assertEqual((gates['serving_healthy'], gates['compared_with_serving'], gates['policy_compared_with_serving'], gates['serving_actor_count']), (False, False, False, None))
        self.assertTrue(any('ConnectionRefusedError' in reason for reason in gates['serving_unhealthy_reasons']))
        on_b = world.bucket.record()
        self.assertEqual((on_b['current']['origin'], on_b['previous']), (SLOT_B, on_a['current']))
        stored = json.loads(world.bucket.objects[route.ADMISSION_PREFIX + on_b['current']['admission_sha256'] + '.json'])
        self.assertEqual((stored['prior_route'], stored['gates']['compared_with_serving']), (on_a, False))

    def test_a_serving_slot_that_answers_503_is_still_compared(self):
        world = self.world_on_a('unready')
        gates = self.failover(world)['admission']['gates']
        self.assertEqual((gates['serving_healthy'], gates['compared_with_serving'], gates['policy_compared_with_serving']), (False, True, True))
        self.assertEqual(gates['serving_actor_count'], store_status()['actor_count'])
        self.assertEqual(world.bucket.record()['current']['origin'], SLOT_B)

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
                world.slots[SLOT_B] = Slot(body)
                self.refused(world, fragment)

    def test_a_serving_slot_answering_as_another_build_is_not_a_reference(self):
        world = self.world_on_a('replaced')
        world.slots[SLOT_B] = Slot(store_b_status(normalize_version='v3'))
        gates = self.failover(world)['admission']['gates']
        self.assertEqual((gates['serving_healthy'], gates['compared_with_serving']), (False, False))

    def test_the_override_is_refused_while_the_serving_slot_is_healthy(self):
        world = self.world_on_a(None)
        self.refused(world, 'only for a serving slot that does not')
        with self.assertRaises(route.Refused):
            route.check(SLOT_B, BINARY, serving_unhealthy=True, now=NOW, get=world.get)

    def test_every_gate_on_the_candidate_itself_still_applies(self):
        cursor = store_b_status()['source_cursor']
        moderation = store_b_status()['moderation']
        cases = [
            ('/ready is not true', Slot(store_b_status(), ready=False), BINARY),
            ('x-typeahead-binary-sha256', Slot(store_b_status()), '5' * 64),
            ('sync lag', Slot(store_b_status(watermark=NOW - 121, source_cursor={**cursor, 'through': NOW - 121})), BINARY),
            ('partial or stale', Slot(store_b_status(query_config={'allow_partial': True, 'allow_stale': False, 'max_sync_lag_s': 120})), BINARY),
            ('source policy', Slot(store_b_status(source_cursor={**cursor, 'source_policy': 'some-other-policy'})), BINARY),
            ('does not describe the index', Slot(store_b_status(source_cursor={**cursor, 'through': NOW - 60})), BINARY),
            ('since its bootstrap', Slot(store_b_status(source_cursor={**cursor, 'reconciliation': {'schema_version': 1, 'phase': 'idle', 'last_completed_at': None, 'last_completed_started_at': None}})), BINARY),
            ('middle of a reconciliation round', Slot(store_b_status(source_cursor={**cursor, 'reconciliation': {**cursor['reconciliation'], 'phase': 'backend'}})), BINARY),
            ('moderation policy is missing or stale', Slot(store_b_status(moderation={**moderation, 'fresh': False})), BINARY),
            ('moderation rules hash', Slot(store_b_status(moderation={**moderation, 'canonical_rules_source_sha256': None})), BINARY),
            ('moderation policy hash', Slot(store_b_status(moderation={**moderation, 'sha256': None})), BINARY),
            ('does not report normalize_version', Slot(store_b_status(normalize_version=None)), BINARY),
            ('actors', Slot(store_b_status(actor_count=0)), BINARY),
        ]
        for fragment, candidate, binary in cases:
            with self.subTest(fragment=fragment):
                world = self.world_on_a('stopped')
                world.slots[SLOT_B] = candidate
                self.refused(world, fragment, binary=binary)
        world = self.world_on_a('stopped')
        world.slots[SLOT_B].down = True
        self.refused(world, 'valid build id')
        self.refused(world, 'already routed', origin=SLOT_A)

    def test_the_slot_that_failed_takes_the_route_back_once_it_answers_again(self):
        world = self.world_on_a('stopped')
        on_a = world.bucket.record()
        self.failover(world)
        on_b = world.bucket.record()
        with self.assertRaises(route.Refused):
            world.rollback()
        with self.assertRaises(route.Refused):
            world.cutover(origin=SLOT_A)
        self.assertEqual(world.bucket.record(), on_b)
        world.slots[SLOT_A].down = False
        self.assertEqual(world.rollback(write=False)['route'], on_a)
        report = world.cutover(origin=SLOT_A)
        self.assertEqual((report['admission']['gates']['serving_origin'], report['admission']['gates']['compared_with_serving']), (SLOT_B, True))
        self.assertEqual((world.bucket.record()['current']['origin'], world.bucket.record()['previous']), (SLOT_A, on_b['current']))

    def test_a_transport_that_does_not_follow_a_failover_gets_the_prior_record_back(self):
        world = self.world_on_a('stopped')
        on_a = world.bucket.record()
        world.transport_origins.discard(SLOT_B)
        with self.assertRaises(route.Refused) as caught:
            self.failover(world)
        self.assertIn('prior record was restored', str(caught.exception))
        self.assertEqual(world.bucket.record(), on_a)

    def test_check_reports_the_failover_admission_without_credentials(self):
        world = self.world_on_a('unready')
        writes = list(world.bucket.writes)
        with self.assertRaises(route.Refused):
            route.check(SLOT_B, BINARY, now=NOW, get=world.get)
        report = route.check(SLOT_B, BINARY, serving_unhealthy=True, now=NOW, get=world.get)
        self.assertEqual((report['target']['origin'], report['gates']['serving_healthy'], report['written']), (SLOT_B, False, False))
        self.assertEqual(world.bucket.writes, writes)


class Check(unittest.TestCase):
    def test_check_reads_the_transport_and_writes_nowhere(self):
        world = World()
        report = route.check(SLOT_A, BINARY, now=NOW, get=world.get)
        self.assertEqual((report['serving'], report['target']['origin'], report['written']), (SERVING, SLOT_A, False))
        self.assertEqual(world.bucket.writes, [])

    def test_check_refuses_once_the_store_slot_is_routed(self):
        world = World()
        world.cutover()
        with self.assertRaises(route.Refused) as caught:
            route.check(SLOT_A, BINARY, now=NOW, get=world.get)
        self.assertIn('already routed', str(caught.exception))


class Standby(unittest.TestCase):
    """The read-only report on the store slot that is not serving."""

    def world_on_a(self):
        world = World()
        world.cutover()
        self.writes = list(world.bucket.writes)
        return world

    def report(self, world, binary=None):
        report = route.standby(binary, now=NOW, get=world.get)
        self.assertEqual((report['operation'], report['written'], world.bucket.writes), ('standby', False, self.writes))
        return report

    def only(self, report):
        self.assertEqual(len(report['standby']), 1)
        return report['standby'][0]

    def test_a_healthy_second_slot_is_fit_to_take_the_route(self):
        world = self.world_on_a()
        report = self.report(world)
        slot = self.only(report)
        self.assertEqual((report['fit'], report['serving'], report['serving_healthy']), (True, world.bucket.record()['current'], True))
        self.assertEqual((slot['origin'], slot['fit'], slot['cutover_needs_serving_slot_unhealthy'], slot['binary_sha256_expected_from']), (SLOT_B, True, False, 'serving route'))
        self.assertEqual(slot['gates']['sync_lag_s'], 20)
        planned = world.cutover(write=False, origin=SLOT_B)
        self.assertEqual((slot['target'], slot['gates']), (planned['admission']['target'], planned['admission']['gates']))

    def test_a_second_slot_that_fails_a_gate_is_not_fit_and_says_why(self):
        cursor = store_b_status()['source_cursor']
        moderation = store_b_status()['moderation']
        for fragment, candidate in (
            ('/ready is not true', Slot(store_b_status(), ready=False)),
            ('sync lag', Slot(store_b_status(watermark=NOW - 400, source_cursor={**cursor, 'through': NOW - 400}))),
            ('since its bootstrap', Slot(store_b_status(source_cursor={**cursor, 'reconciliation': {'schema_version': 1, 'phase': 'idle', 'last_completed_at': None, 'last_completed_started_at': None}}))),
            ('moderation policy differs', Slot(store_b_status(moderation={**moderation, 'sha256': '9' * 64}))),
            ('actors', Slot(store_b_status(actor_count=12000000))),
        ):
            with self.subTest(fragment=fragment):
                world = self.world_on_a()
                world.slots[SLOT_B] = candidate
                report = self.report(world)
                slot = self.only(report)
                self.assertEqual((report['fit'], slot['fit'], report['serving_healthy']), (False, False, True))
                self.assertTrue(any(fragment in reason for reason in slot['refused']), slot['refused'])

    def test_a_second_slot_that_does_not_answer_is_not_fit(self):
        world = self.world_on_a()
        world.slots[SLOT_B].down = True
        report = self.report(world)
        self.assertIs(report['fit'], False)
        self.assertTrue(any('ConnectionRefusedError' in reason for reason in self.only(report)['refused']))

    def test_a_standby_runs_the_serving_slots_binary_unless_the_operator_names_another(self):
        world = self.world_on_a()
        world.slots[SLOT_B] = Slot(store_b_status(binary_sha256='7' * 64))
        slot = self.only(self.report(world))
        self.assertIs(slot['fit'], False)
        self.assertTrue(any('x-typeahead-binary-sha256' in reason for reason in slot['refused']))
        slot = self.only(self.report(world, '7' * 64))
        self.assertEqual((slot['fit'], slot['binary_sha256_expected_from'], slot['target']['binary_sha256']), (True, 'operator', '7' * 64))

    def test_an_unhealthy_serving_slot_is_reported_and_the_standby_is_judged_for_failover(self):
        for failure in ('stopped', 'unready'):
            with self.subTest(failure=failure):
                world = self.world_on_a()
                if failure == 'stopped':
                    world.slots[SLOT_A].down = True
                else:
                    world.slots[SLOT_A].ready = False
                report = self.report(world)
                slot = self.only(report)
                self.assertEqual((report['fit'], report['serving_healthy'], slot['cutover_needs_serving_slot_unhealthy']), (True, False, True))
                self.assertTrue(report['serving_unhealthy_reasons'])
                self.assertEqual(slot['gates']['compared_with_serving'], failure == 'unready')
                world.slots[SLOT_B].ready = False
                self.assertIs(self.report(world)['fit'], False)

    def test_the_first_slot_is_the_standby_once_the_second_serves(self):
        world = self.world_on_a()
        world.cutover(origin=SLOT_B)
        self.writes = list(world.bucket.writes)
        report = self.report(world)
        self.assertEqual((report['serving']['origin'], self.only(report)['origin'], report['fit']), (SLOT_B, SLOT_A, True))

    def test_a_transport_that_cannot_name_the_route_is_refused(self):
        world = self.world_on_a()
        world.transport_origins.discard(SLOT_A)
        with self.assertRaises(route.Refused) as caught:
            route.standby(now=NOW, get=world.get)
        self.assertIn('503', str(caught.exception))

    def test_the_exit_status_is_the_verdict(self):
        world = self.world_on_a()

        def run(get):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                status = route.main(['standby'], get=get, now=NOW)
            return status, json.loads(stdout.getvalue())

        status, report = run(world.get)
        self.assertEqual((status, report['fit'], report['standby'][0]['fit']), (0, True, True))
        world.slots[SLOT_B].ready = False
        status, report = run(world.get)
        self.assertEqual((status, report['fit'], report['standby'][0]['origin']), (1, False, SLOT_B))
        world.transport_origins.discard(SLOT_A)
        status, report = run(world.get)
        self.assertEqual((status, report['operation'], report['written']), (1, 'standby', False))
        self.assertIn('503', report['refused'][0])


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
                self.assertIn('--origin', self.parse(command, '--binary-sha256', BINARY))
                self.assertEqual(self.parse(command, '--origin', SLOT_A, '--binary-sha256', BINARY).origin, SLOT_A)

    def test_only_a_store_slot_can_be_named(self):
        self.assertIn('invalid choice', self.parse('cutover', '--origin', DESTROYED, '--binary-sha256', BINARY))

    def test_the_failover_override_is_never_implied(self):
        for command in ('check', 'cutover'):
            self.assertIs(self.parse(command, '--origin', SLOT_B, '--binary-sha256', BINARY).serving_slot_unhealthy, False)
            self.assertIs(self.parse(command, '--origin', SLOT_B, '--binary-sha256', BINARY, '--serving-slot-unhealthy').serving_slot_unhealthy, True)
        self.assertIn('unrecognized', self.parse('rollback', '--serving-slot-unhealthy'))

    def test_nothing_is_written_unless_asked(self):
        self.assertIs(self.parse('cutover', '--origin', SLOT_A, '--binary-sha256', BINARY).write, False)
        self.assertIs(self.parse('rollback').write, False)


if __name__ == '__main__':
    unittest.main()
