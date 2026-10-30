"""Shared durable source accounting; incomplete HTTP requests retain their reservation."""
import fcntl
import json
import os
from pathlib import Path
import time

def accounted_source(sync, url, token, ledger, build_id, max_bytes, *, allow_test_loopback=False, window_seconds=None, poll_reserve_bytes=None, min_request_interval=0.5, http=None):
    ledger = Path(ledger)
    if not callable(getattr(sync.Turso, '_before_request', None)) or not callable(getattr(sync.Turso, '_after_request', None)):
        raise ValueError('Source client lacks durable request accounting hooks')
    if window_seconds is not None and (type(window_seconds) is not int or window_seconds < 1):
        raise ValueError('Invalid source budget window')
    if not isinstance(build_id, str) or not build_id or (not 0 <= min_request_interval <= 5):
        raise ValueError('Invalid source budget identity/pacing')

    class AccountedTurso(sync.Turso):

        def __init__(self):
            self.recovered_reservation_bytes = 0
            self.pending = False
            self.window_started_at = int(time.time()) + 1
            self.next_request = 0
            super().__init__(url, token, max_bytes=max_bytes, allow_test_loopback=allow_test_loopback, poll_reserve_bytes=poll_reserve_bytes, http=http)
            if any((path.is_symlink() for path in [ledger, ledger.with_suffix('.lock'), ledger.with_suffix('.partial')])):
                raise ValueError('Source budget paths cannot be symlinks')
            self.lock = ledger.with_suffix('.lock').open('a+b')
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.restore()
                self.persist(self.accounting(), pending=False)
            except BaseException:
                self.lock.close()
                raise

        def restore(self):
            if not ledger.exists():
                return
            with ledger.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('Source accounting ledger too large')
            state = json.loads(raw)
            if state.get('schema_version') not in (1, 2) or state.get('build_id') != build_id or state.get('max_source_bytes') != max_bytes or (state.get('poll_reserve_bytes') != self.poll_reserve_bytes):
                raise ValueError('Source accounting identity changed')
            if state.get('window_seconds') != window_seconds:
                raise ValueError('Source accounting window changed')
            for key in ('source_bytes', 'poll_bytes', 'sweep_bytes', 'requests'):
                if type(state.get(key)) is not int or state[key] < 0:
                    raise ValueError('Invalid source accounting')
            if state['source_bytes'] != state['poll_bytes'] + state['sweep_bytes'] or state['source_bytes'] > max_bytes or state['sweep_bytes'] > max_bytes - self.poll_reserve_bytes:
                raise ValueError('Invalid source accounting sum')
            recovered = state.get('recovered_reservation_bytes', 0)
            reserved = state.get('reservation_bytes', 0)
            if type(recovered) is not int or recovered < 0 or type(reserved) is not int or (not 0 <= reserved <= state['source_bytes']) or (type(state.get('pending_reservation')) is not bool):
                raise ValueError('Invalid source reservation')
            self.recovered_reservation_bytes = recovered + (reserved if state['pending_reservation'] else 0)
            self.bytes, self.poll_bytes, self.sweep_bytes, self.requests = (state[key] for key in ('source_bytes', 'poll_bytes', 'sweep_bytes', 'requests'))
            if window_seconds is not None:
                start = state.get('window_started_at')
                if type(start) is not int or start < 0 or state.get('window_ends_at') != start + window_seconds:
                    raise ValueError('Invalid source accounting clock')
                self.window_started_at = start
            self.rollover()

        def rollover(self):
            if window_seconds is not None and (not self.pending) and (time.time() >= self.window_started_at + window_seconds):
                self.window_started_at = int(time.time()) + 1
                self.bytes = 0
                self.poll_bytes = 0
                self.sweep_bytes = 0
                self.requests = 0
                self.recovered_reservation_bytes = 0
                self.persist(self.accounting(), pending=False)

        def remaining_bytes(self):
            self.rollover()
            return super().remaining_bytes()

        def accounting(self):
            return {**super().accounting(), 'accounting_mode': 'conservative-fixed-window' if window_seconds is not None else 'conservative-offline', 'recovered_reservation_bytes': self.recovered_reservation_bytes, 'window_seconds': window_seconds, 'window_started_at': self.window_started_at if window_seconds is not None else None, 'window_ends_at': self.window_started_at + window_seconds if window_seconds is not None else None}

        def persist(self, accounting, *, pending, reservation_bytes=0):
            state = {**accounting, 'schema_version': 2, 'build_id': build_id, 'requests': self.requests, 'pending_reservation': pending, 'reservation_bytes': reservation_bytes}
            state['remaining_source_bytes'] = max(0, max_bytes - state['source_bytes'])
            state['remaining_sweep_bytes'] = max(0, min(state['remaining_source_bytes'], max_bytes - self.poll_reserve_bytes - state['sweep_bytes']))
            temporary = ledger.with_suffix('.partial')
            with temporary.open('wb') as stream:
                stream.write(json.dumps(state, sort_keys=True).encode() + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(ledger)
            descriptor = os.open(ledger.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        def _before_request(self, max_response_bytes):
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + min_request_interval
            reservation = min(self.remaining_bytes(), max_response_bytes)
            charged = self.accounting()
            charged['source_bytes'] += reservation
            charged[self.budget_kind + '_bytes'] += reservation
            self.persist(charged, pending=True, reservation_bytes=reservation)
            self.pending = True

        def _after_request(self):
            self.persist(self.accounting(), pending=False)
            self.pending = False

        def close(self):
            self.lock.close()
    return AccountedTurso()
