#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3>=1.36"]
# ///
"""Move the search route between the two store slots, with a conditional write to R2.

A slot is admitted on what it reports about itself, compared with the slot
that is serving now.

In the record a cutover writes, `previous` is the target that was serving when
the cutover was written: the slot a rollback returns to. A rollback restores
the whole record the cutover replaced, taken from the admission stored for the
routed slot, so that record's own `previous` comes back with it and a second
rollback undoes the cutover before.

    slot_route.py check    --origin URL --binary-sha256 HEX   reads only public endpoints
    slot_route.py cutover  --origin URL --binary-sha256 HEX   prints the plan
    slot_route.py cutover  --origin URL --binary-sha256 HEX --write
    slot_route.py rollback                                    prints the plan
    slot_route.py rollback --write
    slot_route.py standby  [--binary-sha256 HEX]              reads only public endpoints

`standby` says whether the store slot that is not serving could take the route
now, and exits 1 when it could not.

The slot that would take the route is always named: there is no default origin.

`check` and `cutover` refuse while the serving slot is not ready, because the
candidate is judged against it. `--serving-slot-unhealthy` is the failover
override for exactly that case; see `admit`.

`cutover` and `rollback` read INDEX_R2_ENDPOINT, INDEX_R2_BUCKET,
INDEX_R2_ACCESS_KEY_ID and INDEX_R2_SECRET_ACCESS_KEY.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROUTE_KEY = 'control/search-routing.json'
ADMISSION_PREFIX = 'control/store-slot-admissions/'
TRANSPORT = 'https://typeahead-search-transport.n8-3e9.workers.dev'
FORMAT = 'tantivy-0.27-f32-did-expiry-v4'
STORE_POLICY = 'store-change-log-v1'
STORE_ORIGINS = ('https://typeahead-shadow-a.fly.dev', 'https://typeahead-shadow-b.fly.dev')
BUILD = re.compile(r'b[0-9]{10}-[A-Za-z0-9_-]+')
DIGEST = re.compile(r'[a-f0-9]{64}')
STRATEGIES = ('per-segment', 'global-merge')
TARGET_FIELDS = ('engine', 'origin', 'build_id', 'format', 'admission_sha256', 'binary_sha256', 'retrieval_strategy')
IDENTITY_HEADERS = {
    'build_id': 'x-typeahead-build',
    'format': 'x-typeahead-format',
    'binary_sha256': 'x-typeahead-binary-sha256',
    'retrieval_strategy': 'x-typeahead-retrieval-strategy',
}
# The ceilings on poll, policy and round age.
MAX_SYNC_LAG_S = 120
MAX_POLICY_AGE_S = 300
# Two slots that follow the same network differ by what arrived in the last
# seconds. Further apart than this, one of them has lost or kept actors it
# should not have.
ACTOR_TOLERANCE_PERCENT = 2
MAX_BODY = 4 * 1024 * 1024
VERIFY_SECONDS = 60


class Refused(Exception):
    def __init__(self, reasons):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__('; '.join(self.reasons))


class Conflict(Exception):
    """A conditional write lost: the object changed, or already exists."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest_json(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_target(target):
    if not isinstance(target, dict) or set(target) != set(TARGET_FIELDS):
        raise Refused('route target does not have exactly the routed fields')
    if (
        target['engine'] != 'tantivy'
        or target['origin'] not in STORE_ORIGINS
        or target['format'] != FORMAT
        or target['retrieval_strategy'] not in STRATEGIES
        or not isinstance(target['build_id'], str)
        or not BUILD.fullmatch(target['build_id'])
        or any(not isinstance(target[key], str) or not DIGEST.fullmatch(target[key]) for key in ('admission_sha256', 'binary_sha256'))
    ):
        raise Refused(f"route target is not a valid Tantivy target for {', '.join(STORE_ORIGINS)}")
    return target


def validate_record(record):
    """The routing record: version, current, and the previous target if there is one."""
    if not isinstance(record, dict) or record.get('version') != 1 or type(record['version']) is not int:
        raise Refused('routing record is not version 1')
    unknown = set(record) - {'version', 'current', 'previous'}
    if unknown:
        raise Refused(f"routing record carries fields this tool would drop: {', '.join(sorted(unknown))}")
    validate_target(record.get('current'))
    if record.get('previous') is not None:
        validate_target(record['previous'])
        if record['previous']['origin'] == record['current']['origin']:
            raise Refused('routing record names one slot as both current and previous')
    return record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('redirects are not followed')


def http_get(url):
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'typeahead-store-slot-route/1'})
    try:
        with opener.open(request, timeout=5) as response:
            status, headers, raw = response.status, response.headers, response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as error:
        status, headers, raw = error.code, error.headers, error.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError(f'{url} answered more than {MAX_BODY} bytes')
    return status, {key.lower(): value for key, value in headers.items()}, raw


def observe(origin, get=http_get):
    """/ready, /health and /index/status of one slot; a failed read is recorded, not raised."""
    seen = {}
    for name, path in (('ready', '/ready'), ('health', '/health'), ('status', '/index/status')):
        try:
            status, headers, raw = get(origin + path)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('body is not a JSON object')
            seen[name] = {'http': status, 'headers': headers, 'body': body}
        except (OSError, ValueError) as error:
            seen[name] = {'http': None, 'headers': {}, 'body': {}, 'error': f'{type(error).__name__}: {error}'}
    return seen


def identity_reasons(label, seen, expected):
    """Every response must acknowledge the same engine, build, format, binary and strategy."""
    reasons = []
    for name in ('ready', 'health', 'status'):
        reply = seen[name]
        if reply['http'] != 200:
            reasons.append(f"{label} /{name} answered {reply.get('error') or reply['http']}")
            continue
        if reply['headers'].get('x-typeahead-engine') != 'tantivy':
            reasons.append(f'{label} /{name} does not acknowledge the tantivy engine')
        for key, header in IDENTITY_HEADERS.items():
            if reply['headers'].get(header) != expected[key]:
                reasons.append(f"{label} /{name} {header} is {reply['headers'].get(header)!r}, expected {expected[key]!r}")
    status = seen['status']['body']
    for key in ('build_id', 'format', 'binary_sha256', 'retrieval_strategy'):
        if seen['status']['http'] == 200 and status.get(key) != expected[key]:
            reasons.append(f'{label} /index/status {key} is {status.get(key)!r}, expected {expected[key]!r}')
    if seen['status']['http'] == 200 and status.get('engine') != 'tantivy':
        reasons.append(f'{label} /index/status engine is not tantivy')
    return reasons


def status_acknowledges(seen, expected):
    """Whether /index/status alone answered as the slot `expected` names."""
    reply = seen['status']
    return (
        reply['http'] == 200
        and reply['headers'].get('x-typeahead-engine') == 'tantivy'
        and reply['body'].get('engine') == 'tantivy'
        and all(reply['headers'].get(header) == expected[key] and reply['body'].get(key) == expected[key] for key, header in IDENTITY_HEADERS.items())
    )


def serving_slot_reasons(seen, target):
    """Why the serving slot is not healthy: empty when it is ready as the slot the record names."""
    reasons = identity_reasons('serving slot', seen, target)
    if seen['ready']['http'] == 200 and seen['ready']['body'].get('ready') is not True:
        reasons.append('serving slot /ready is not true')
    return reasons


def is_count(value):
    return type(value) is int and value >= 0


def admit(candidate_origin, candidate, serving_target, serving, binary_sha256, now, serving_unhealthy=False):
    """Decide whether a store-fed slot may take the route from `serving_target`.

    Returns the candidate's route identity (everything but the admission
    digest) and the measured gates, or raises Refused with every reason.

    `serving_unhealthy` is the failover case: the serving slot is not required
    to be ready or to answer at all, and is refused if it is in fact healthy.
    Every gate on the candidate itself still applies. The comparisons with the
    serving slot are made when its /index/status still answers as the slot the
    record names, and skipped when it does not, which the gates record.
    """
    reasons = []
    if candidate_origin not in STORE_ORIGINS:
        raise Refused(f'{candidate_origin} is not a store slot origin')
    if not isinstance(binary_sha256, str) or not DIGEST.fullmatch(binary_sha256):
        raise Refused('the expected binary sha256 must be 64 lowercase hex characters')
    validate_target(serving_target)
    if candidate_origin == serving_target['origin']:
        raise Refused(f'{candidate_origin} is already routed')

    serving_reasons = serving_slot_reasons(serving, serving_target)
    compared = True
    if not serving_unhealthy:
        reasons += serving_reasons
    elif not serving_reasons:
        raise Refused('the serving slot answers ready as the slot the record names; --serving-slot-unhealthy is only for a serving slot that does not')
    else:
        compared = status_acknowledges(serving, serving_target)
    theirs = serving['status']['body'] if compared else {}

    ours = candidate['status']['body']
    build_id = ours.get('build_id')
    if not isinstance(build_id, str) or not BUILD.fullmatch(build_id):
        unanswered = candidate['status']
        if unanswered['http'] != 200:
            reasons.append(f"candidate /index/status answered {unanswered.get('error') or unanswered['http']}")
        raise Refused(reasons + ['candidate does not report a valid build id'])
    identity = {
        'engine': 'tantivy',
        'origin': candidate_origin,
        'build_id': build_id,
        'format': FORMAT,
        'binary_sha256': binary_sha256,
        'retrieval_strategy': serving_target['retrieval_strategy'],
    }
    reasons += identity_reasons('candidate', candidate, identity)

    if candidate['ready']['body'].get('ready') is not True:
        reasons.append('candidate /ready is not true')
    if candidate['health']['body'].get('ready') is not True:
        reasons.append('candidate /health is not ready')
    if ours.get('ready') is not True:
        reasons.append('candidate /index/status is not ready')

    for key in ('normalize_version', 'plan_version', 'pds_scope_version', 'pds_scope_policy'):
        if not ours.get(key):
            reasons.append(f'candidate does not report {key}')
        elif compared and ours.get(key) != theirs.get(key):
            reasons.append(f'{key} is {ours.get(key)!r} on the candidate and {theirs.get(key)!r} on the serving slot')

    config = ours.get('query_config') or {}
    if config.get('allow_partial') is not False or config.get('allow_stale') is not False:
        reasons.append('candidate answers from a partial or stale index')
    if type(config.get('max_sync_lag_s')) is not int or not 1 <= config['max_sync_lag_s'] <= MAX_SYNC_LAG_S:
        reasons.append(f'candidate does not bound its own sync lag at {MAX_SYNC_LAG_S} s or less')
    for key in ('full_source', 'import_complete', 'expiry_coverage_ready'):
        if ours.get(key) is not True:
            reasons.append(f'candidate {key} is not true')

    watermark = ours.get('watermark')
    lag = now - watermark if is_count(watermark) else None
    if lag is None or not 0 <= lag <= MAX_SYNC_LAG_S:
        reasons.append(f'candidate sync lag is {lag!r} s; the bound is {MAX_SYNC_LAG_S} s')

    cursor = ours.get('source_cursor')
    if not isinstance(cursor, dict):
        cursor = {}
    if cursor.get('source_policy') != STORE_POLICY or cursor.get('schema_version') != 1:
        reasons.append(f"candidate source policy is {cursor.get('source_policy')!r}, expected {STORE_POLICY!r}")
    if cursor.get('cycle') is not None:
        reasons.append('candidate is in the middle of a source cycle')
    if cursor.get('build_id') != build_id or cursor.get('snapshot_watermark') != ours.get('snapshot_watermark') or cursor.get('through') != watermark:
        reasons.append('candidate source cursor does not describe the index it serves')
    if not is_count(ours.get('source_observation_revision')) or ours['source_observation_revision'] < 1:
        reasons.append('candidate has applied no source observations')

    round_state = cursor.get('reconciliation')
    if not isinstance(round_state, dict):
        round_state = {}
    end, start = round_state.get('last_completed_at'), round_state.get('last_completed_started_at')
    floor = ours.get('snapshot_watermark')
    round_age = round_duration = None
    # The bridge reads every entry of the store's change log and starts a round
    # itself when it falls behind the log or finds the index diverged
    # (services/src/bridge/Bridge.zig), and runs none on a timer by default. So
    # the gate is that the index was reconciled against the store after the
    # snapshot it started from, and that no round is running now.
    if round_state.get('schema_version') != 1 or not is_count(end) or not is_count(start) or not is_count(floor) or not floor < start <= end <= now:
        reasons.append('candidate has not completed a reconciliation round against its store since its bootstrap')
    else:
        round_age, round_duration = now - end, end - start
    if round_state.get('phase') != 'idle':
        reasons.append(f"candidate is in the middle of a reconciliation round (phase {round_state.get('phase')!r})")

    policy = ours.get('moderation')
    if not isinstance(policy, dict):
        policy = {}
    their_policy = theirs.get('moderation')
    if not isinstance(their_policy, dict):
        their_policy = {}
    observed = policy.get('source_observed_at')
    policy_age = now - observed if is_count(observed) else None
    if any(policy.get(key) is not True for key in ('enabled', 'complete', 'fresh')) or policy_age is None or not 0 <= policy_age <= MAX_POLICY_AGE_S:
        reasons.append(f'candidate moderation policy is missing or stale (age {policy_age!r} s, bound {MAX_POLICY_AGE_S} s)')
    rules = policy.get('canonical_rules_source_sha256')
    if not isinstance(rules, str) or not DIGEST.fullmatch(rules):
        reasons.append('candidate does not report a moderation rules hash')
    elif compared and rules != their_policy.get('canonical_rules_source_sha256'):
        reasons.append('moderation rules differ between the candidate and the serving slot')
    # A stale serving slot holds an old policy; only a fresh one is a fair comparison.
    policy_compared = compared and their_policy.get('fresh') is True
    if not isinstance(policy.get('sha256'), str) or not DIGEST.fullmatch(policy['sha256']):
        reasons.append('candidate does not report a moderation policy hash')
    elif policy_compared and policy['sha256'] != their_policy.get('sha256'):
        reasons.append('moderation policy differs between the candidate and the fresh serving slot')

    actors, their_actors = ours.get('actor_count'), theirs.get('actor_count')
    if not is_count(actors) or actors < 1:
        reasons.append(f'candidate holds {actors!r} actors')
    elif compared and (not is_count(their_actors) or abs(actors - their_actors) * 100 > their_actors * ACTOR_TOLERANCE_PERCENT):
        reasons.append(f'candidate holds {actors!r} actors against {their_actors!r} on the serving slot; the tolerance is {ACTOR_TOLERANCE_PERCENT}% either way')

    if reasons:
        raise Refused(reasons)
    gates = {
        'sync_lag_s': lag,
        'policy_age_s': policy_age,
        'policy_sha256': policy['sha256'],
        'policy_compared_with_serving': policy_compared,
        'canonical_rules_source_sha256': rules,
        'reconciliation_age_s': round_age,
        'reconciliation_duration_s': round_duration,
        'source_policy': STORE_POLICY,
        'source_id': cursor.get('source_id'),
        'source_cursor_sha256': digest_json(cursor),
        'source_observation_revision': ours['source_observation_revision'],
        'normalize_version': ours['normalize_version'],
        'plan_version': ours['plan_version'],
        'actor_count': actors,
        'serving_actor_count': their_actors,
        'serving_origin': serving_target['origin'],
        'serving_healthy': not serving_reasons,
        'serving_unhealthy_reasons': serving_reasons,
        'compared_with_serving': compared,
        'machine_id': candidate['status']['headers'].get('x-typeahead-machine-id'),
    }
    return identity, gates


def plan_cutover(record, candidate_origin, candidate, serving, binary_sha256, now, serving_unhealthy=False):
    """The admission document and the record that would replace `record`."""
    validate_record(record)
    identity, gates = admit(candidate_origin, candidate, record['current'], serving, binary_sha256, now, serving_unhealthy)
    admission = {
        'schema_version': 1,
        'kind': 'store-slot-route-admission',
        'admitted_at': now,
        'target': identity,
        'gates': gates,
        'prior_route': record,
    }
    target = {**identity, 'admission_sha256': digest_json(admission)}
    return admission, {'version': 1, 'current': validate_target(target), 'previous': record['current']}


def plan_rollback(record, admission, prior_slot):
    """The record that was routed before the cutover, if its slot still answers as it did."""
    validate_record(record)
    if (
        not isinstance(admission, dict)
        or admission.get('kind') != 'store-slot-route-admission'
        or digest_json(admission) != record['current']['admission_sha256']
    ):
        raise Refused('the stored admission does not match the routed store slot')
    prior = validate_record(admission.get('prior_route'))
    if prior['current'] != record.get('previous'):
        raise Refused('the stored prior route is not the one this record replaced')
    reasons = identity_reasons('prior slot', prior_slot, prior['current'])
    if prior_slot['ready']['body'].get('ready') is not True:
        reasons.append('prior slot /ready is not true')
    if reasons:
        raise Refused(reasons)
    return prior


def encode_record(record):
    return json.dumps(record, separators=(',', ':')).encode()


class R2:
    def __init__(self, environ):
        import boto3

        missing = [name for name in ('INDEX_R2_ENDPOINT', 'INDEX_R2_BUCKET', 'INDEX_R2_ACCESS_KEY_ID', 'INDEX_R2_SECRET_ACCESS_KEY') if not environ.get(name)]
        if missing:
            raise Refused(f"unset: {', '.join(missing)}")
        self.bucket = environ['INDEX_R2_BUCKET']
        self.client = boto3.client(
            's3',
            endpoint_url=environ['INDEX_R2_ENDPOINT'],
            region_name='auto',
            aws_access_key_id=environ['INDEX_R2_ACCESS_KEY_ID'],
            aws_secret_access_key=environ['INDEX_R2_SECRET_ACCESS_KEY'],
        )

    def get(self, key):
        from botocore.exceptions import ClientError

        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response['Error']['Code'] in ('NoSuchKey', '404'):
                return None
            raise
        try:
            raw = result['Body'].read(MAX_BODY + 1)
        finally:
            result['Body'].close()
        if len(raw) > MAX_BODY:
            raise ValueError(f'{key} exceeds {MAX_BODY} bytes')
        return raw, result['ETag']

    def put(self, key, body, *, if_match=None, if_none_match=False):
        from botocore.exceptions import ClientError

        condition = {'IfNoneMatch': '*'} if if_none_match else {'IfMatch': if_match}
        try:
            result = self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType='application/json', **condition)
        except ClientError as error:
            if error.response['Error']['Code'] in ('PreconditionFailed', '412'):
                raise Conflict(key) from None
            raise
        return result['ETag']


def read_record(store):
    found = store.get(ROUTE_KEY)
    if found is None:
        raise Refused('there is no routing record')
    raw, etag = found
    return json.loads(raw), etag


def routed(target, get=http_get):
    """'yes' once the transport names `target` and a search through it acknowledges
    that identity, 'rejected' if the transport cannot use the record at all."""
    try:
        status, _, raw = get(TRANSPORT + '/route')
        # /route reads R2 on every request, so this is the transport's verdict on
        # the record just written: it does not parse it, or cannot read it.
        if status == 503:
            return 'rejected'
        if status != 200 or json.loads(raw) != target:
            return 'pending'
        status, headers, _ = get(TRANSPORT + '/search?q=zzstoatzz.io&limit=1')
    except (OSError, ValueError):
        return 'pending'
    followed = (
        status == 200
        and headers.get('x-typeahead-engine') == 'tantivy'
        and all(headers.get(header) == target[key] for key, header in IDENTITY_HEADERS.items())
    )
    return 'yes' if followed else 'pending'


def switch(store, etag, before, after, *, get=http_get, sleep=time.sleep, clock=time.monotonic):
    """Write `after` over `before`, then put `before` back if the transport does not follow."""
    try:
        written = store.put(ROUTE_KEY, encode_record(after), if_match=etag)
    except Conflict:
        raise Refused('the routing record changed while this was being decided; nothing was written') from None
    deadline = clock() + VERIFY_SECONDS
    while (verdict := routed(after['current'], get)) != 'yes':
        if verdict == 'rejected' or clock() >= deadline:
            try:
                store.put(ROUTE_KEY, encode_record(before), if_match=written)
            except Conflict:
                raise Refused('the transport did not follow the new record, and the record changed again before it could be restored') from None
            raise Refused(
                'the transport cannot use the new record (is the deployed transport older than the store origin?); the prior record was restored'
                if verdict == 'rejected'
                else 'the transport did not follow the new record; the prior record was restored'
            )
        sleep(1)
    return written


def cutover(store, candidate_origin, binary_sha256, *, write, serving_unhealthy=False, now=None, get=http_get, **timing):
    record, etag = read_record(store)
    validate_record(record)
    now = int(time.time()) if now is None else now
    admission, after = plan_cutover(
        record, candidate_origin, observe(candidate_origin, get), observe(record['current']['origin'], get), binary_sha256, now, serving_unhealthy
    )
    report = {'operation': 'cutover', 'written': False, 'expected_etag': etag, 'admission': admission, 'route': after}
    if not write:
        return report
    try:
        store.put(ADMISSION_PREFIX + after['current']['admission_sha256'] + '.json', canonical(admission), if_none_match=True)
    except Conflict:
        raise Refused('this exact admission was already recorded; run it again for a fresh one') from None
    report['etag'] = switch(store, etag, record, after, get=get, **timing)
    report['written'] = True
    return report


def rollback(store, *, write, get=http_get, **timing):
    record, etag = read_record(store)
    validate_record(record)
    found = store.get(ADMISSION_PREFIX + record['current']['admission_sha256'] + '.json')
    if found is None:
        raise Refused('no admission is stored for the routed store slot')
    admission = json.loads(found[0])
    if record.get('previous') is None:
        raise Refused('the routing record has no previous slot to return to')
    prior = plan_rollback(record, admission, observe(record['previous']['origin'], get))
    report = {'operation': 'rollback', 'written': False, 'expected_etag': etag, 'route': prior}
    if not write:
        return report
    report['etag'] = switch(store, etag, record, prior, get=get, **timing)
    report['written'] = True
    return report


def serving_route(get):
    status, _, raw = get(TRANSPORT + '/route')
    if status != 200:
        raise Refused(f'the transport answered {status} for /route')
    return validate_target(json.loads(raw))


def standby(binary_sha256=None, *, now=None, get=http_get):
    """Whether each store slot that is not serving could take the route right now.

    It runs the cutover's admission, as a failover when the serving slot is
    not healthy. Without `binary_sha256` a standby is expected to run the
    binary the serving slot's record names, which an operator read from that
    machine when it was admitted.
    """
    serving_target = serving_route(get)
    now = int(time.time()) if now is None else now
    serving = observe(serving_target['origin'], get)
    serving_reasons = serving_slot_reasons(serving, serving_target)
    slots = []
    for origin in STORE_ORIGINS:
        if origin == serving_target['origin']:
            continue
        expected, source = binary_sha256, 'operator'
        if expected is None:
            expected, source = serving_target['binary_sha256'], 'serving route'
        try:
            identity, gates = admit(origin, observe(origin, get), serving_target, serving, expected, now, bool(serving_reasons))
            slots.append({'origin': origin, 'fit': True, 'cutover_needs_serving_slot_unhealthy': bool(serving_reasons),
                          'binary_sha256_expected_from': source, 'target': identity, 'gates': gates})
        except Refused as refusal:
            slots.append({'origin': origin, 'fit': False, 'refused': refusal.reasons})
    return {
        'operation': 'standby', 'written': False, 'fit': any(slot['fit'] for slot in slots), 'serving': serving_target,
        'serving_healthy': not serving_reasons, 'serving_unhealthy_reasons': serving_reasons, 'standby': slots,
    }


def check(candidate_origin, binary_sha256, *, serving_unhealthy=False, now=None, get=http_get):
    serving_target = serving_route(get)
    now = int(time.time()) if now is None else now
    identity, gates = admit(candidate_origin, observe(candidate_origin, get), serving_target, observe(serving_target['origin'], get), binary_sha256, now, serving_unhealthy)
    return {'operation': 'check', 'written': False, 'serving': serving_target, 'target': identity, 'gates': gates}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'cutover'):
        command = commands.add_parser(name)
        command.add_argument('--origin', required=True, choices=STORE_ORIGINS, help='the store slot that would take the route')
        command.add_argument('--binary-sha256', required=True, help="the reader's sha256, read from the file on that slot's machine")
        command.add_argument(
            '--serving-slot-unhealthy', action='store_true',
            help='failover: admit the candidate although the serving slot is not ready or does not answer; refused when it is healthy',
        )
        if name == 'cutover':
            command.add_argument('--write', action='store_true')
    commands.add_parser('rollback').add_argument('--write', action='store_true')
    commands.add_parser('standby').add_argument(
        '--binary-sha256', help="the reader's sha256 a standby must run; by default the one the serving slot's record names"
    )
    return parser


def main(argv=None, get=http_get, now=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'check':
            report = check(args.origin, args.binary_sha256, serving_unhealthy=args.serving_slot_unhealthy, now=now, get=get)
        elif args.command == 'standby':
            report = standby(args.binary_sha256, now=now, get=get)
        elif args.command == 'cutover':
            report = cutover(R2(os.environ), args.origin, args.binary_sha256, write=args.write, serving_unhealthy=args.serving_slot_unhealthy)
        else:
            report = rollback(R2(os.environ), write=args.write)
    except Refused as refusal:
        print(json.dumps({'operation': args.command, 'written': False, 'refused': refusal.reasons}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report.get('fit', True) else 1


if __name__ == '__main__':
    sys.exit(main())
