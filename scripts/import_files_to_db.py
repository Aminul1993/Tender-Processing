"""
One-off import of what versions before the database kept in files, into the database of DATABASE_URL:

  TARGET_DIRECTORY/<tcno>/eligibility_result.json      each tender's last result
  TARGET_DIRECTORY/<tcno>/deep_analysis.json           each tender's last deep analysis
  company_details.md (or COMPANY_DETAILS_FILE)         the company profile; the criteria the bid team marked by
                                                       hand, kept in its "Criteria marked by the bid team" table,
                                                       move to the criterion_marks table (and so do those of a
                                                       profile already in the database)
  watchlist.json (or WATCHLIST_FILE)                   the watchlist's departments and rules
  TARGET_DIRECTORY/decisions.json                      the team's shortlisted / dropped decisions
  TARGET_DIRECTORY/watchlist_tenders.json              the watchlist's tenders, as the last refresh found them
  TARGET_DIRECTORY/watchlist_search.json               when the last watchlist refresh started
  TARGET_DIRECTORY/cache.sqlite3                       the cache of file texts, page OCR and chunk vectors

What the database already holds is kept: a result, deep analysis, mark, decision or cached row already there is
not replaced, and the company profile, the watchlist's rules and its tenders are imported only when the database has
none. So it can be
run again. The files are left where they are; delete them once the app shows what they held.

    python scripts/import_files_to_db.py
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env')

from psycopg.types.json import Json  # noqa: E402

from config import Config  # noqa: E402
from services.analysis_service import LEGACY_DEEP_ANALYSIS_FILE, LEGACY_RESULT_FILE  # noqa: E402
from services.company_service import (MARK_LABELS, MARKED_COLUMNS, MARKED_HEADING, CompanyStore,  # noqa: E402
                                      parse_profile, render_profile)
from services.database import Database  # noqa: E402
from services.decision_service import DECISIONS  # noqa: E402
from services.processing_service import result_brief  # noqa: E402
from services.watchlist_service import (SEARCHED, Watchlist, WatchlistError, load_list, read_rules,  # noqa: E402
                                        save_list, save_rules)

log = logging.getLogger('import_files_to_db')

RESULT_STATUSES = ('completed', 'failed', 'awaiting_approval')


def import_results(db, directory):
    """Imports every TCNO folder's saved result; returns how many were new."""
    imported = 0
    for path in sorted(Path(directory).glob(f'*/{LEGACY_RESULT_FILE}')):
        try:
            result = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            log.warning('%s: could not be read, so it is not imported: %s', path, exc)
            continue
        if not isinstance(result, dict) or result.get('status') not in RESULT_STATUSES or 'tcno' not in result:
            log.warning('%s: holds no tender result, so it is not imported', path)
            continue
        # Results saved before Pinecone named a criteria file or an index of their own
        result.pop('criteria_file', None)
        result.pop('index_name', None)
        result['folder'] = str(path.parent)
        with db.connection() as connection:
            inserted = connection.execute(
                'INSERT INTO results (tcno, result, brief) VALUES (%s, %s, %s) ON CONFLICT (tcno) DO NOTHING',
                (str(result['tcno']), Json(result), Json(result_brief(result)))).rowcount
        imported += inserted
    return imported


def import_deep_analyses(db, directory):
    """Imports every TCNO folder's saved deep analysis; returns how many were new."""
    imported = 0
    for path in sorted(Path(directory).glob(f'*/{LEGACY_DEEP_ANALYSIS_FILE}')):
        try:
            report = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            log.warning('%s: could not be read, so it is not imported: %s', path, exc)
            continue
        if not isinstance(report, dict) or report.get('status') != 'completed':
            log.warning('%s: holds no deep analysis, so it is not imported', path)
            continue
        with db.connection() as connection:
            imported += connection.execute(
                'INSERT INTO deep_analyses (tcno, report) VALUES (%s, %s) ON CONFLICT (tcno) DO NOTHING',
                (str(report.get('tcno') or path.parent.name), Json(report))).rowcount
    return imported


def import_company(db, path):
    """Imports the company profile file when the database has no profile; returns whether it did."""
    store = CompanyStore(db)
    if store.read() or not Path(path).is_file():
        return False
    store.save(Path(path).read_text(encoding='utf-8-sig'))
    return True


def import_marks(db):
    """
    Moves the criteria statuses the bid team marked by hand out of the company profile's MARKED_HEADING table, where
    versions before the criterion_marks table kept them, into that table; returns how many.
    """
    store = CompanyStore(db)
    statuses = {label.lower(): status for status, label in MARK_LABELS.items()}
    with db.transaction():
        markdown, rows = split_marks(store.read())
        moved = 0
        for criterion, requirement, label, tcno, marked_on in rows:
            status = statuses.get(label.strip().lower())
            if not status:
                log.warning('Marked criterion %r of tender %s: unknown status %r, so it is dropped', criterion, tcno,
                            label)
                continue
            with db.connection() as connection:
                moved += connection.execute(
                    'INSERT INTO criterion_marks (tcno, criterion_key, criterion, requirement, status, marked_on) '
                    'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tcno, criterion_key) DO NOTHING',
                    (tcno.strip(), _key(criterion), criterion, requirement, status, marked_on)).rowcount
        if rows:
            store.save(markdown)
    return moved


def split_marks(markdown):
    """(the profile markdown without its MARKED_HEADING section, that section's MARKED_COLUMNS rows)."""
    blocks = parse_profile(markdown)
    for index, block in enumerate(blocks):
        name = block['text'].lstrip('0123456789. ') if block['type'] == 'heading' else ''
        below = blocks[index + 1] if index + 1 < len(blocks) else {}
        if _key(name) == _key(MARKED_HEADING) and [_key(c) for c in below.get('columns', [])] == \
                [_key(c) for c in MARKED_COLUMNS]:
            return render_profile(blocks[:index] + blocks[index + 2:]), below['rows']
    return markdown, []


def _key(text):
    return ' '.join(text.split()).lower()


def import_decisions(db, path):
    """Imports the saved decisions; returns how many were new."""
    try:
        decisions = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return 0
    imported = 0
    with db.connection() as connection:
        for tcno, entry in decisions.items():
            if isinstance(entry, dict) and entry.get('decision') in DECISIONS:
                imported += connection.execute(
                    'INSERT INTO decisions VALUES (%s, %s, %s) ON CONFLICT (tcno) DO NOTHING',
                    (str(tcno), entry['decision'], entry['decided_at'])).rowcount
    return imported


def import_watchlist_rules(db, path):
    """
    Imports the watchlist's rules from the JSON file older versions kept them in, when the database has none;
    returns how many departments. Raises WatchlistError when the file holds no valid watchlist.
    """
    if read_rules(db) is not None or not Path(path).is_file():
        return 0
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    Watchlist.parse(raw)  # checked first: nothing is saved from a broken file
    save_rules(db, raw)
    return len(raw['departments'])


def import_watchlist(db, tenders_path, search_path):
    """
    Imports the saved watchlist when the database has none, keeping the tenders a department of the rules in the
    database claims, each with that department (older versions saved every tender and applied the rules when showing
    them); returns how many tenders.
    """
    imported = 0
    if load_list(db)[1] is None and tenders_path.is_file():
        saved = json.loads(tenders_path.read_text(encoding='utf-8'))
        try:
            rules = Watchlist.load(db)
        except WatchlistError as exc:
            log.warning('The watchlist tenders are not imported: %s', exc)
            return 0
        rows = [(tender, department.label()) for tender in saved['tenders']
                if (department := rules.department(tender))]
        save_list(db, rows, saved['refreshed_at'])  # the rules it searched with are not known
        imported = len(rows)
    if db.value(SEARCHED) is None and search_path.is_file():
        db.set_value(SEARCHED, json.loads(search_path.read_text(encoding='utf-8')))
    return imported


def import_cache(db, path):
    """Imports the rows of the SQLite cache; returns {table: rows new}."""
    if not Path(path).is_file():
        return {}
    counts = {}
    keys = {'texts': '(sha256, reader)', 'pages': '(sha256, reader)', 'vectors': '(model, sha256)'}
    with closing(sqlite3.connect(path)) as source:
        for table, key in keys.items():
            try:
                rows = source.execute(f'SELECT * FROM {table}').fetchall()
            except sqlite3.OperationalError:  # a cache from before the table was
                continue
            with db.connection() as connection, connection.cursor() as cursor:
                before = connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                cursor.executemany(f'INSERT INTO {table} VALUES (%s, %s, %s) ON CONFLICT {key} DO NOTHING', rows)
                counts[table] = connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] - before
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-7s %(message)s')
    db = Database.connect(Config.DATABASE_URL, Config.DATABASE_SCHEMA)
    directory = Config.TARGET_DIRECTORY
    log.info('Results: %d imported', import_results(db, directory))
    log.info('Deep analyses: %d imported', import_deep_analyses(db, directory))
    company = Path(os.getenv('COMPANY_DETAILS_FILE') or ROOT / 'company_details.md')
    log.info('Company profile: %s', 'imported' if import_company(db, company) else 'not imported (none, or one is '
                                                                                   'already in the database)')
    log.info('Criteria marked by the bid team: %d moved out of the profile', import_marks(db))
    log.info('Decisions: %d imported', import_decisions(db, directory / 'decisions.json'))
    rules = Path(os.getenv('WATCHLIST_FILE') or ROOT / 'watchlist.json')
    log.info('Watchlist rules: %d departments imported', import_watchlist_rules(db, rules))
    log.info('Watchlist: %d tenders imported', import_watchlist(db, directory / 'watchlist_tenders.json',
                                                                directory / 'watchlist_search.json'))
    counts = import_cache(db, directory / 'cache.sqlite3')
    log.info('Cache: %s', ', '.join(f'{rows} {table}' for table, rows in counts.items()) or 'none')
    log.info('Done. The files are left in %s; delete them once the app shows what they held.', directory)
    return 0


if __name__ == '__main__':
    sys.exit(main())
