"""Fenced identity refreshes; the cloud runner owns scheduling and budgets."""
import time
import urllib.parse

from pds_cloud_pilot import source
from pds_coverage import pds_from_document


def refresh_batch(execute, ledger, queue, claims, resolve):
    if not 1 <= len(claims) <= 100:
        raise ValueError('One to 100 claims required')
    results, prepared = [], []
    for claim in claims:
        did, token = claim['did'], claim['token']
        try:
            current = source(execute, did)
            document = resolve(did)
            observed_at = int(time.time())
            pds = pds_from_document(did, document)
            parsed = urllib.parse.urlsplit(pds)
            if parsed.scheme != 'https' or parsed.port or parsed.path not in ('', '/'):
                raise ValueError('Unsupported PDS origin')
            if current['pds'].rstrip('/') == pds:
                if source(execute, did)['pds'].rstrip('/') != pds:
                    raise ValueError('Source hosting changed during resolution')
                queue.verified(did, token, pds, observed_at, interval=30 * 86400)
                results.append({'did': did, 'outcome': 'unchanged', 'verified_at': observed_at})
                continue
            previous = ledger.current(did)
            if previous is not None and previous['state'] not in ('confirmed', 'conflict'):
                raise ValueError('Prior uncertain write requires reconciliation')
            prepared.append((claim, current, pds, observed_at, previous))
        except Exception as error:
            queue.failed(did, token, type(error).__name__)
            results.append({'did': did, 'outcome': 'retry', 'error': type(error).__name__})
    writes = []
    for claim, current, pds, observed_at, previous in prepared:
        did, token = claim['did'], claim['token']
        if time.time() - observed_at > 60:
            queue.failed(did, token, 'authority observation expired')
            results.append({'did': did, 'outcome': 'retry', 'error': 'stale authority'})
            continue
        queue.begin_write(did, token)
        if previous is None:
            attempt = ledger.prepare(did, current['updated_at'], pds, observed_pds=current['pds'])
        else:
            attempt = ledger.retry(previous['id'], current['updated_at'], pds, observed_pds=current['pds'])
        writes.append((claim, current, pds, observed_at, attempt))
    if not writes:
        return results
    try:
        ledger.send_batch([item[4] for item in writes], execute)
    except Exception as error:
        for claim, *_ in writes:
            queue.failed(claim['did'], claim['token'], type(error).__name__)
        raise
    for claim, current, pds, observed_at, attempt in writes:
        did, token = claim['did'], claim['token']
        receipt = ledger.get(attempt)
        if receipt['state'] == 'conflict':
            queue.conflict(did, token)
            results.append({'did': did, 'outcome': 'conflict'})
            continue
        try:
            after = source(execute, did)
            if after['pds'] != pds:
                raise ValueError('Source changed after confirmed identity write')
            queue.verified(did, token, pds, observed_at, interval=30 * 86400)
        except Exception as error:
            queue.failed(did, token, type(error).__name__)
            raise
        results.append({'did': did, 'outcome': 'repaired' if current['pds'] == '' else 'migrated', 'verified_at': observed_at, 'receipt': receipt})
    return results


def refresh_one(execute, ledger, queue, claim, resolve):
    return refresh_batch(execute, ledger, queue, [claim], resolve)[0]
