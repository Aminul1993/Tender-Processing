"""
One-off migration from the per-tender eligibility_criteria.md files to one Pinecone namespace per tender.

For each tender folder under TARGET_DIRECTORY (or only the TCNOs given), it reads the documents already downloaded
there, plus the TenderTiger pre-qualification text kept in the folder's old eligibility_criteria.md (so TenderTiger
is not asked again), indexes them all in the tender's own namespace tcno-<tcno> of the shared index
PINECONE_INDEX_NAME (made on first use), and then deletes eligibility_criteria.md. Saved results
(eligibility_result.json) are left as they are: the app drops their old criteria_file field and finds the namespace
when it starts.

Run it from the project folder, with the app stopped:

    python scripts/migrate_to_pinecone.py --dry-run          # what would be done
    python scripts/migrate_to_pinecone.py                    # every tender folder
    python scripts/migrate_to_pinecone.py 100091540 102309372
    python scripts/migrate_to_pinecone.py --force            # rebuild namespaces that are already complete
    python scripts/migrate_to_pinecone.py --keep-criteria-files

A tender folder with no documents and no pre-qualification text is skipped: the app downloads its documents the
next time it is checked. Scanned PDFs are read with OCR again (PaddleOCR, and OCR_MODEL with GROQ_API_KEY for pages
with tables or that PaddleOCR is unsure of), as the documents were never kept as
text. The run stops at the Pinecone plan's namespace limit and says how many tenders are left.
"""
import argparse
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env')

from config import Config  # noqa: E402
from services.analysis_service import (PREQUALIFICATION_SOURCE, TokenUsage, _NO_CRITERIA, read_documents,  # noqa: E402
                                       tender_files)
from services.cache_service import Cache  # noqa: E402
from services.pinecone_service import Document, PineconeService, QuotaExceeded  # noqa: E402

# The criteria file of the app before Pinecone; read here for the last time, then deleted
LEGACY_CRITERIA_FILE = 'eligibility_criteria.md'

log = logging.getLogger('migrate_to_pinecone')


def legacy_prequalification(path):
    """The '## TenderTiger Pre-Qualification' section of an old criteria file, as written, or ''."""
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return ''
    match = re.search(rf'(?ms)^## {re.escape(PREQUALIFICATION_SOURCE)}\s*\n(.*?)(?=^## |\Z)', text)
    return match.group(1).strip() if match else ''


def migrate(folder, index, force=False, keep_criteria_files=False, dry_run=False):
    """Indexes one tender folder. Returns 'indexed', 'skipped' or 'up to date'."""
    tcno = folder.name
    legacy = folder / LEGACY_CRITERIA_FILE
    if not force and index.manifest(tcno) is not None:
        if legacy.exists() and not keep_criteria_files and not dry_run:
            legacy.unlink()
        return 'up to date'
    files = [path for path in tender_files(folder) if path != legacy and not _NO_CRITERIA.search(path.name)]
    prequalification = legacy_prequalification(legacy)
    if not files and not prequalification:
        log.info('%s: no documents and no pre-qualification text; the app will download them on the next check', tcno)
        return 'skipped'
    if dry_run:
        log.info('%s: would index %d file(s)%s into %s', tcno, len(files),
                 ' and the pre-qualification text' if prequalification else '', index.namespace(tcno))
        return 'indexed'
    documents = [Document(PREQUALIFICATION_SOURCE, prequalification, 'prequalification')] if prequalification else []
    read, unreadable = read_documents(folder, files, TokenUsage(), max_file_chars=Config.MAX_FILE_CHARS,
                                      workers=Config.INGEST_WORKERS, cache=index.cache)
    try:
        built = index.rebuild_namespace(tcno, documents + read, unreadable,
                                        on_progress=lambda message: log.debug(message))
    except Exception:
        discard_incomplete(index, tcno)
        raise
    log.info('%s: indexed %d source(s), %d chunk(s) into namespace %s; %d unreadable', tcno, built['sources'],
             built['chunks'], built['namespace'], len(unreadable))
    if legacy.exists() and not keep_criteria_files:
        if built['chunks']:
            legacy.unlink()
        else:
            # Nothing could be read (scans OCR could not read, say): the old file is the only record of the criteria
            log.warning('%s: no text could be indexed, so %s is kept', tcno, LEGACY_CRITERIA_FILE)
    return 'indexed'


def discard_incomplete(index, tcno):
    """Deletes the tender's namespace if a failure left it without a complete manifest, freeing its place."""
    try:
        if index.has_namespace(tcno, strict=True) and index.manifest(tcno) is None:
            index.delete_namespace(tcno)
    except Exception:
        log.warning('%s: could not delete its incomplete namespace; the app rebuilds it on the next check', tcno)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('tcnos', nargs='*', help='only these tenders (default: every folder in TARGET_DIRECTORY)')
    parser.add_argument('--force', action='store_true', help='rebuild namespaces that are already complete')
    parser.add_argument('--keep-criteria-files', action='store_true', help=f'do not delete {LEGACY_CRITERIA_FILE}')
    parser.add_argument('--dry-run', action='store_true', help='say what would be done, change nothing')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-7s %(message)s')
    for noisy in ('httpx', 'pinecone'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # The app's cache, so a run stopped at the namespace limit and run again reads and embeds nothing twice
    index = PineconeService.from_config(
        Config, cache=Cache(Config.CACHE_FILE or Config.TARGET_DIRECTORY / 'cache.sqlite3'))
    index.require()
    folders = sorted(path for path in Config.TARGET_DIRECTORY.iterdir() if path.is_dir()) \
        if not args.tcnos else [Config.TARGET_DIRECTORY / tcno for tcno in args.tcnos]
    counts = {'indexed': 0, 'skipped': 0, 'up to date': 0, 'failed': 0}
    for number, folder in enumerate(folders):
        if not folder.is_dir():
            log.warning('%s: no such tender folder', folder)
            counts['failed'] += 1
            continue
        try:
            counts[migrate(folder, index, args.force, args.keep_criteria_files, args.dry_run)] += 1
        except QuotaExceeded as exc:
            log.error('%s: %s', folder.name, exc)
            log.error('Stopped: %d tender folder(s) not migrated. Delete namespaces no longer needed or raise the '
                      'plan, then run this again: complete namespaces are skipped.', len(folders) - number)
            counts['failed'] += len(folders) - number
            break
        except Exception:  # one tender failing (Pinecone, OCR without GROQ_API_KEY, a bad file) is not the end
            log.exception('%s: could not be indexed; it is left as it was', folder.name)
            counts['failed'] += 1
    log.info('Done: %s', ', '.join(f'{count} {name}' for name, count in counts.items()))
    return 1 if counts['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
