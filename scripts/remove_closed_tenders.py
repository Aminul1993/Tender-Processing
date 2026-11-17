"""
Removes every checked tender whose bid submission (closing) date was KEEP_DAYS (7) days ago or more: its Pinecone
namespace, its downloaded documents, its result, deep analysis and decision in the database, as Delete does in Closed
tenders on the page. For a scheduled task, daily say:

    python scripts/remove_closed_tenders.py             # remove them
    python scripts/remove_closed_tenders.py --dry-run   # only list them

It uses the app's .env (the database, PINECONE_API_KEY, TARGET_DIRECTORY). The closing dates are the ones saved with
the results: a tender with none on record is not removed (Closed tenders on the page lists those). The statuses the bid
team marked by hand stay, with the bid team's marks, for the tenders checked later. A tender being checked is left for
the next run. Without PINECONE_API_KEY nothing is removed, so no tender leaves its records in Pinecone. Logs to the
console and to LOG_DIRECTORY/remove-closed-tenders.log. Exit status 1 when a tender could not be removed (a document
open in another program, Pinecone out of reach), 0 otherwise.
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env')

from config import Config  # noqa: E402
from services.database import Database, DatabaseError  # noqa: E402
from services.decision_service import DecisionStore  # noqa: E402
from services.deep_analysis_service import DeepAnalysisRunner  # noqa: E402
from services.pinecone_service import VectorStoreError, VectorStoreNotConfigured  # noqa: E402
from services.processing_service import TenderBusy, TenderProcessor  # noqa: E402
from utils.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger('remove_closed_tenders')

# Days after its closing date a tender is removed
KEEP_DAYS = 7


def remove_closed_tenders(config, dry_run=False, vector_store=None, today=None):
    """
    Removes the tenders closed KEEP_DAYS days or more before today (date.today() by default), or with dry_run only
    lists them, with the settings of config (see config.Config); vector_store, if given, in place of the Pinecone index
    config describes. Returns the exit status.
    """
    db = Database.connect(config.DATABASE_URL, config.DATABASE_SCHEMA)
    processor = TenderProcessor.from_config(config, db, index=vector_store, job_workers=0)
    closed = processor.closed_downloads(today)
    due = [tender for tender in closed['closed'] if tender['days_closed'] >= KEEP_DAYS]
    log.info('%d checked tender(s) closed %d days ago or more%s; %d with no closing date on record are kept',
             len(due), KEEP_DAYS, ', not removed (dry run)' if dry_run else '', len(closed['unknown']))
    if dry_run:
        for tender in due:
            log.info('Tender %s: closed %s, %d days ago (%s)', tender['tcno'], tender['closing_date'],
                     tender['days_closed'], tender['name'])
        return 0
    if not due:
        return 0
    try:
        processor.index.require()
    except VectorStoreNotConfigured as exc:
        log.error('Nothing removed: %s', exc)
        return 1

    decisions, deep_analyses = DecisionStore(db), DeepAnalysisRunner(processor, db)
    removed = failed = 0
    for tender in due:
        tcno = tender['tcno']
        try:
            deleted = processor.delete_tender(tcno)
        except TenderBusy:
            log.warning('Tender %s is being checked, so it is left for the next run', tcno)
            continue
        except LookupError:  # removed meanwhile, on the page say
            continue
        except (OSError, VectorStoreError) as exc:
            log.error('Tender %s could not be removed: %s', tcno, exc)
            failed += 1
            continue
        deep_analyses.forget(tcno)
        decisions.forget(tcno)
        removed += 1
        log.info('Removed tender %s, closed %s (%d days ago): %d document(s), %.1f MB%s (%s)', tcno,
                 tender['closing_date'], tender['days_closed'], deleted['files'], deleted['bytes'] / 1024 ** 2,
                 ', and its Pinecone namespace' if deleted['namespace_deleted'] else '', tender['name'])
    log.info('Removed %d tender(s), %d could not be', removed, failed)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--dry-run', action='store_true', help='list the tenders, remove nothing')
    args = parser.parse_args(argv)
    configure_logging(Config.LOG_LEVEL, Config.LOG_DIRECTORY, 'remove-closed-tenders.log')
    try:
        return remove_closed_tenders(Config, dry_run=args.dry_run)
    except DatabaseError as exc:
        log.error('%s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
