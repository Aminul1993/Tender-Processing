"""
Searches TenderTiger for the watchlist's tenders, as Search again does on the page, then checks every tender found
that was never checked (no result in the database), as Check eligibility does. For a scheduled task, daily say:

    python scripts/process_watchlist.py               # search, then check the new tenders one at a time
    python scripts/process_watchlist.py --threads 2   # two at a time

It uses the app's .env: the database, the TenderTiger login, GROQ_API_KEY and PINECONE_API_KEY. A tender with a
result of any kind is left alone: completed, failed (Retry on the page) or waiting for its OCR to be approved; one
whose scans need more OCR than the budget stops as waiting, as on the page. The tenders join the web app's job when
one is running, so the page shows them, and worker processes (worker.py) may take some; the script also takes the
tenders the team queued meanwhile, and exits once none is queued. It may run while the web app runs: the requests to
TenderTiger are spaced out across both, and the web app takes up the refreshed list. Logs to the console and to
LOG_DIRECTORY/process-watchlist.log. Exit status 0 when every new tender was checked (or is waiting for OCR approval),
1 when the settings are incomplete, the search failed or a tender failed.
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env')

from config import Config  # noqa: E402
from services.database import Database, DatabaseError  # noqa: E402
from services.processing_service import TenderProcessor  # noqa: E402
from services.watchlist_service import WatchlistError, WatchlistRunner, load_list  # noqa: E402
from utils.logging_setup import configure_logging  # noqa: E402
from utils.tendertiger import share_pacing  # noqa: E402

log = logging.getLogger('process_watchlist')

# Seconds between two looks at the new tenders other processes are still checking
WAIT_SECONDS = 5


def missing_settings(processor):
    """What a tender cannot be checked without, as a message, or None."""
    missing = [name for name in ('TENDERTIGER_EMAIL', 'TENDERTIGER_PASSWORD', 'GROQ_API_KEY') if not os.getenv(name)]
    if not processor.index.configured:
        missing.append('PINECONE_API_KEY')
    if missing:
        return f'Set {", ".join(missing)} in .env.'
    if not processor.company_store.read():
        return 'The company profile is empty: add it with Edit profile on the page.'
    return None


def process_watchlist(config, threads=1, vector_store=None):
    """
    Searches the watchlist's tenders and checks those never checked, threads at a time, with the settings of config
    (see config.Config); vector_store, if given, in place of the Pinecone index config describes. Returns the exit
    status.
    """
    db = Database.connect(config.DATABASE_URL, config.DATABASE_SCHEMA)
    share_pacing(lambda gap: db.pace('tendertiger', gap))
    processor = TenderProcessor.from_config(config, db, index=vector_store, job_workers=threads)
    problem = missing_settings(processor)
    if problem:
        log.error('Nothing searched or checked. %s', problem)
        return 1

    log.info("Searching TenderTiger for the watchlist's tenders")
    try:
        status = WatchlistRunner(db).start(wait=True)
    except WatchlistError as exc:  # no watchlist, or one that cannot be searched
        log.error('The watchlist could not be searched: %s', exc)
        return 1
    if status['status'] != 'completed':
        log.error('The watchlist search failed, so nothing is checked: %s', status['error'])
        return 1

    tenders = [tender for tender, _ in load_list(db)[0]]
    checked = set(processor.state.briefs())
    new = [tender for tender in tenders if str(tender['tcno']) not in checked]
    log.info('The watchlist holds %d tenders, %d of them never checked', len(tenders), len(new))
    if not new:
        return 0
    processor.start(new)
    processor.join_workers()
    tcnos = {str(tender['tcno']) for tender in new}
    while True:  # the new tenders a worker process or the web app took are not done yet
        items = [item for item in processor.progress()['items'] if item['tcno'] in tcnos]
        if not any(item['status'] in ('queued', 'processing') for item in items):
            break
        time.sleep(WAIT_SECONDS)

    failed = 0
    for item in items:
        if item['status'] == 'completed':
            outcome = item['verdict']
        elif item['status'] == 'awaiting_approval':
            outcome = 'waiting for its OCR to be approved'
        else:
            outcome = f"{item['status']}: {item.get('error_title') or item.get('error') or ''}".rstrip(': ')
            failed += item['status'] == 'failed'
        log.info('Tender %s: %s (%s)', item['tcno'], outcome, item.get('name') or '')
    log.info('Checked %d new tender(s), %d failed', len(items), failed)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--threads', type=int, default=1, help='tenders checked at the same time (default 1)')
    args = parser.parse_args(argv)
    configure_logging(Config.LOG_LEVEL, Config.LOG_DIRECTORY, 'process-watchlist.log')
    try:
        return process_watchlist(Config, max(1, args.threads))
    except DatabaseError as exc:
        log.error('%s', exc)
        return 1
    except KeyboardInterrupt:
        log.warning('Stopped. A tender being checked is failed after a minute or so (Retry on the page); those still '
                    'queued wait for worker.py, or for the next tenders queued on the page.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
