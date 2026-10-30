"""One fenced identity refresh; the cloud runner owns scheduling and budgets."""
import time
import urllib.parse

from pds_cloud_pilot import source
from pds_coverage import pds_from_document


def refresh_one(execute, ledger, queue, claim, resolve):
    did, token = claim['did'], claim['token']
    writing = False
    try:
        current = source(execute, did)
        document = resolve(did)
        observed_at = int(time.time())
        pds = pds_from_document(did, document)
        parsed = urllib.parse.urlsplit(pds)
        if parsed.scheme != 'https' or parsed.port or parsed.path not in ('', '/'):
            raise ValueError('Unsupported PDS origin')
        if current['pds'].rstrip('/') == pds:
            queue.verified(did, token, pds, observed_at, interval=30 * 86400)
            return {'did': did, 'outcome': 'unchanged', 'verified_at': observed_at}
        previous = ledger.current(did)
        if previous is not None and previous['state'] not in ('confirmed', 'conflict'):
            raise ValueError('Prior uncertain write requires reconciliation')
        queue.begin_write(did, token)
        writing = True
        if previous is None:
            attempt = ledger.prepare(did, current['updated_at'], pds, observed_pds=current['pds'])
        else:
            attempt = ledger.retry(previous['id'], current['updated_at'], pds, observed_pds=current['pds'])
        outcome = ledger.send(attempt, execute)
        if outcome == 'conflict':
            queue.conflict(did, token)
            return {'did': did, 'outcome': 'conflict'}
        after = source(execute, did)
        if after['pds'] != pds:
            raise ValueError('Source changed after confirmed identity write')
        queue.verified(did, token, pds, observed_at, interval=30 * 86400)
        return {'did': did, 'outcome': 'repaired' if current['pds'] == '' else 'migrated', 'verified_at': observed_at, 'receipt': ledger.get(attempt)}
    except Exception as error:
        queue.failed(did, token, type(error).__name__)
        if writing:
            raise
        return {'did': did, 'outcome': 'retry', 'error': type(error).__name__}
