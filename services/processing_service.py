"""
Runs download -> extract -> analyse for the queued tenders, one at a time, in a background thread. A tender
whose criteria were saved by an earlier run is checked against them instead, unless it is queued to regenerate them.
Each result is saved in the tender's folder and read back when the app starts, so it survives a restart.
"""
import copy
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ext import COMPANY_DETAILS_FILE, TENDERTIGER_BLOCKED
from services.analysis_service import (CRITERIA_FILE, RESULT_FILE, process_tender_files, recheck_saved_criteria,
                                       tender_files)
from services.download_service import DownloadError, download_tender_documents
from services.tender_service import has_documents, prequalification_url
from utils.files import safe_name

log = logging.getLogger(__name__)

IDLE_PROGRESS = {
    'job_id': None, 'status': 'idle', 'total': 0, 'completed': 0, 'failed': 0, 'cancelled': 0, 'processed': 0,
    'percent': 0, 'current': None, 'started_at': None, 'finished_at': None, 'stop_requested': False, 'items': [],
}

# The part of a result the tender table needs: everything except the full details and file list.
# rechecked: checked against the saved criteria instead of the documents (for a queued tender: will be).
# criteria_file: the saved criteria file after the run, or None.
SUMMARY_KEYS = ('status', 'verdict', 'score', 'error', 'error_title', 'error_action', 'checks', 'processed_at',
                'rechecked', 'criteria_file')


class NoJobRunning(Exception):
    """There is no running job to stop."""


class TenderProcessor:
    """
    Owns the processing job and the latest result of every processed tender.

    One job runs at a time; tenders queued while it runs are added to it. State is kept in memory, so the
    app must run as a single process (any number of threads is fine); results are also saved in the tender
    folders and loaded from there on creation. Every read goes through progress() / result() / summaries(),
    which return copies taken under the lock.
    """

    def __init__(self, target_directory, download_timeout=120, max_download_bytes=500 * 1024 * 1024,
                 company_file=COMPANY_DETAILS_FILE):
        self.target_directory = Path(target_directory)
        self.download_timeout = download_timeout
        self.max_download_bytes = max_download_bytes
        self.company_file = company_file
        self._lock = threading.Lock()
        self._job = None
        # (raw tender, regenerate) of the job, in the order of its items; the tenders hold the TenderTiger IDs
        self._queue = []
        self._results = self._load_results()  # tcno -> result dict (see _process)

    def criteria_file(self, tcno):
        """Where the tender's eligibility criteria are saved: CRITERIA_FILE in its folder."""
        return self.target_directory / safe_name(tcno) / CRITERIA_FILE

    def _load_results(self):
        """The results saved in the tender folders by earlier runs. A file that cannot be read is skipped."""
        results = {}
        for path in sorted(self.target_directory.glob(f'*/{RESULT_FILE}')):
            try:
                result = read_result(path)
            except (OSError, ValueError):
                log.warning('Could not read the saved result %s; skipping it', path, exc_info=True)
                continue
            # Where the folder is now, and whether its criteria are still there
            criteria_file = path.parent / CRITERIA_FILE
            result.update(folder=str(path.parent), criteria_file=str(criteria_file) if criteria_file.is_file() else None)
            results[str(result['tcno'])] = result
        if results:
            log.info('Loaded %d saved result(s) from %s', len(results), self.target_directory)
        return results

    def start(self, tenders, regenerate=False):
        """
        Queues the given raw tenders in order and returns the job's progress. Starts a new job when none is
        running, otherwise adds them to the running one. Tenders already queued or processing are skipped.
        A tender with saved criteria is checked against them, unless regenerate is true: then its documents
        are read again and its criteria saved anew.
        """
        with self._lock:
            new_job = not self._running()
            if new_job:
                self._job = {
                    'job_id': uuid.uuid4().hex,
                    'status': 'running',
                    'total': 0,
                    'completed': 0,
                    'failed': 0,
                    'cancelled': 0,
                    'current': None,
                    'started_at': _now(),
                    'finished_at': None,
                    'stop_requested': False,
                    'items': [],
                }
                self._queue = []
            waiting = {item['tcno'] for item in self._job['items'] if item['status'] in ('queued', 'processing')}
            added = 0
            for tender in tenders:
                tcno = str(tender['tcno'])
                if tcno in waiting:
                    continue
                waiting.add(tcno)
                self._job['items'].append({**dict.fromkeys(SUMMARY_KEYS), 'tcno': tcno, 'status': 'queued',
                                           'rechecked': not regenerate and self.criteria_file(tcno).is_file()})
                self._queue.append((tender, regenerate))
                added += 1
            self._job['total'] = len(self._job['items'])
            if added:
                self._job['stop_requested'] = False  # new work overrides an earlier stop
            job_id = self._job['job_id']

        if new_job:
            log.info('Job %s started for %d tender(s)', job_id, added)
            threading.Thread(target=self._run, name=f'job-{job_id[:8]}', daemon=True).start()
        else:
            log.info('Job %s: %d tender(s) added', job_id, added)
        return self.progress()

    def stop(self):
        """
        Cancels the tenders still queued; the one being processed finishes normally.
        Returns the job's progress. Raises NoJobRunning when no job is running.
        """
        with self._lock:
            if not self._running():
                raise NoJobRunning()
            for item in self._job['items']:
                if item['status'] == 'queued':
                    item['status'] = 'cancelled'
                    self._job['cancelled'] += 1
            self._job['stop_requested'] = True
            log.info('Job %s: stop requested', self._job['job_id'])
        return self.progress()

    def progress(self):
        """
        Returns a snapshot of the current (or last) job, with processed count, percentage, and the average
        duration of the tenders completed so far, for time estimates: average_seconds for those whose documents
        were read, average_recheck_seconds for those checked against saved criteria (None before the first one).
        """
        with self._lock:
            job = copy.deepcopy(self._job if self._job is not None else IDLE_PROGRESS)
            completed = [r for r in self._results.values() if r['status'] == 'completed']
        current = job['current']
        if current:
            current['elapsed_seconds'] = round(time.monotonic() - current.pop('started'))
        job['processed'] = job['completed'] + job['failed']
        done = job['processed'] + job['cancelled']
        job['percent'] = round(100 * done / job['total']) if job['total'] else 100
        job['average_seconds'] = _average([r['duration_seconds'] for r in completed if not r.get('rechecked')])
        job['average_recheck_seconds'] = _average([r['duration_seconds'] for r in completed if r.get('rechecked')])
        return job

    def result(self, tcno):
        """Returns the full result (including details) of the last run of this tender, or None."""
        with self._lock:
            result = self._results.get(str(tcno))
            return copy.deepcopy(result) if result else None

    def summaries(self):
        """Returns {tcno: summary} for every processed tender, where a summary holds the SUMMARY_KEYS fields."""
        with self._lock:
            return {tcno: _summary(result) for tcno, result in self._results.items()}

    def _running(self):
        """True while a job is running. The caller holds the lock."""
        return bool(self._job and self._job['status'] == 'running')

    def _run(self):
        """Thread body: processes queued tenders one by one, including ones added meanwhile, then finishes."""
        try:
            while self._process_next():
                pass
        except Exception:
            # Only a bug can get here (_process catches everything); still finish the job so the UI stops waiting
            log.exception('Job stopped unexpectedly')
            with self._lock:
                for item in self._job['items']:
                    if item['status'] in ('queued', 'processing'):
                        item.update(status='failed', error='Job stopped unexpectedly',
                                    error_title='Processing stopped unexpectedly',
                                    error_action='Retry. If it happens again, see logs/app.log.')
                        self._job['failed'] += 1
                self._finish()

    def _process_next(self):
        """Processes the next queued tender. Returns False, after finishing the job, when none is left."""
        with self._lock:
            index = next((i for i, item in enumerate(self._job['items']) if item['status'] == 'queued'), None)
            if index is None:
                # Finish under the same lock as the check, so start() cannot add to a job that is ending
                self._finish()
                return False
            tender, regenerate = self._queue[index]
            self._job['items'][index]['status'] = 'processing'
            self._job['current'] = {'tcno': str(tender['tcno']), 'name': _name(tender), 'stage': 'Starting',
                                    'step': None, 'steps': None, 'started': time.monotonic()}

        result = self._process(tender, regenerate)
        with self._lock:
            self._results[result['tcno']] = result
            self._job['items'][index].update(_summary(result))
            self._job['completed' if result['status'] == 'completed' else 'failed'] += 1
            if result['error'] == TENDERTIGER_BLOCKED:
                self._cancel_tendertiger_work()
        return True

    def _cancel_tendertiger_work(self):
        """
        Cancels the queued tenders that need TenderTiger once its firewall blocks the app: their requests would only
        be refused, and keep the block going. Re-checks against saved criteria go on. The caller holds the lock.
        """
        cancelled = 0
        for item in self._job['items']:
            if item['status'] == 'queued' and not item['rechecked']:
                item['status'] = 'cancelled'
                cancelled += 1
        self._job['cancelled'] += cancelled
        if cancelled:
            log.warning('Job %s: TenderTiger is blocking automated visits, so %d queued tender(s) were not started',
                        self._job['job_id'], cancelled)

    def _finish(self):
        """Marks the job completed. The caller holds the lock."""
        self._job.update(status='completed', current=None, finished_at=_now())
        log.info('Job %s finished: %d completed, %d failed, %d cancelled', self._job['job_id'],
                 self._job['completed'], self._job['failed'], self._job['cancelled'])

    def _process(self, tender, regenerate=False):
        """
        Downloads, extracts and analyses one tender, or checks its saved criteria when it has them and
        regenerate is false. Never raises: failures become a 'failed' result.
        """
        tcno = str(tender['tcno'])
        folder = self.target_directory / safe_name(tcno)
        criteria_file = self.criteria_file(tcno)

        def set_stage(stage, step=None, steps=None):
            with self._lock:
                self._job['current'].update(stage=stage, step=step, steps=steps)

        log.info('Processing tender %s', tcno)
        started = time.monotonic()
        result = {
            'tcno': tcno, 'name': _name(tender), 'status': 'completed', 'verdict': None, 'details': None,
            'score': None, 'criteria': [], 'unreadable_files': [], 'jv': None, 'comparison': [], 'checks': None,
            'error': None,
            'error_title': None, 'error_action': None, 'files': [], 'folder': str(folder),
            'rechecked': not regenerate and criteria_file.is_file(), 'criteria_file': None,
        }
        try:
            if result['rechecked']:
                log.info('Tender %s: checking its saved criteria in %s', tcno, criteria_file)
                result['files'] = [path.relative_to(folder).as_posix() for path in tender_files(folder)]
                analysis = recheck_saved_criteria(folder, on_progress=set_stage, company_file=self.company_file)
            else:
                analysis = self._analyse_documents(tender, folder, result, set_stage)
            criteria = analysis.get('criteria') or []
            unreadable = analysis.get('unreadable_files') or []
            jv = analysis.get('jv')
            comparison = analysis.get('comparison') or []
            result.update(verdict=analysis['verdict'], details=analysis['details'], score=analysis['score'],
                          criteria=criteria, unreadable_files=unreadable, jv=jv, comparison=comparison,
                          checks=_checks(criteria, unreadable, jv, comparison))
            log.info('Tender %s: %s (score %s)', tcno, result['verdict'], result['score'])
        except Exception as exc:
            log.exception('Tender %s failed', tcno)
            title, action = explain_error(exc)
            result.update(status='failed', error=str(exc) or exc.__class__.__name__,
                          error_title=title, error_action=action)
        if criteria_file.is_file():
            result['criteria_file'] = str(criteria_file)
        result['duration_seconds'] = round(time.monotonic() - started, 1)
        result['processed_at'] = _now()
        try:
            save_result(folder, result)
        except OSError:
            log.exception('Could not save the result of tender %s; it is lost on restart', tcno)
        return result

    def _analyse_documents(self, tender, folder, result, set_stage):
        """Downloads the tender's documents into folder (emptied first) and analyses them; sets result['files']."""
        tcno = str(tender['tcno'])
        prequalification_link = prequalification_url(tender)
        if has_documents(tender) or not prequalification_link:
            files = download_tender_documents(
                tender, folder,
                timeout=self.download_timeout, max_bytes=self.max_download_bytes, on_progress=set_stage,
            )
        else:
            # No documents: check the pre-qualification criteria alone, in an emptied folder so files left
            # by an earlier run are not checked as this tender's
            log.info('Tender %s has no documents: checking its pre-qualification criteria only', tcno)
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir(parents=True)
            files = []
        result['files'] = [path.relative_to(folder).as_posix() for path in files]
        return process_tender_files(folder, on_progress=set_stage, company_file=self.company_file,
                                    prequalification_link=prequalification_link)


def save_result(folder, result):
    """Writes a tender's result to folder/RESULT_FILE as JSON, which read_result() reads back. Returns its path."""
    path = Path(folder) / RESULT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and then moved, so a crash mid-write cannot leave a result that cannot be read back
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)
    log.info('Saved the result of tender %s in %s', result['tcno'], path)
    return path


def read_result(path):
    """Reads back the result saved by save_result(). Raises OSError, or ValueError when it holds no result."""
    result = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(result, dict) or result.get('status') not in ('completed', 'failed') \
            or 'tcno' not in result or 'duration_seconds' not in result:
        raise ValueError(f'{path} holds no tender result')
    return result


def explain_error(exc):
    """Returns (title, action): what failed and what to do next, in the bid team's terms."""
    text = str(exc)
    lower = text.lower()
    if "tendertiger's firewall" in lower:
        return ('TenderTiger is blocking automated visits for now',
                'Wait a while, then retry. Re-checks against saved criteria do not need TenderTiger.')
    if 'TENDERTIGER_' in text or 'tendertiger login' in lower or 'did not accept the login' in lower:
        return ('The TenderTiger login failed',
                'Check TENDERTIGER_EMAIL and TENDERTIGER_PASSWORD in .env, restart the app and retry.')
    if isinstance(exc, DownloadError):
        if 'no documents' in lower:
            return ('No documents', 'TenderTiger has no documents for this tender. Check it on TenderTiger.')
        if re.search(r'\b(401|403)\b', text) or 'forbidden' in lower or 'unauthorized' in lower:
            return ('Document download refused', 'TenderTiger refused the download. Retry in a few minutes.')
        if 'expected a document' in lower:
            return ('The document download returned a web page',
                    'TenderTiger sent a page instead of the documents. Retry in a few minutes.')
        if 'timed out' in lower or 'timeout' in lower:
            return ('The document server did not respond', 'Retry in a few minutes.')
        if 'larger than' in lower:
            return ('The documents are too large',
                    'The download is over the MAX_DOWNLOAD_MB limit. Raise the limit in .env, restart and retry.')
        if 'could not extract' in lower:
            return ('The ZIP file could not be opened',
                    'The archive may be damaged or encrypted. Retry, or check the documents on TenderTiger.')
        return ('The documents could not be downloaded', 'Retry. If it fails again, check the tender on TenderTiger.')
    if 'OLLAMA_API_KEY' in text:
        return ('The LLM API key is missing', 'Add OLLAMA_API_KEY to .env, restart the app and retry.')
    if 'no tender files' in lower:
        if 'could not be fetched' in lower:
            return ('The pre-qualification criteria could not be fetched',
                    'The tender has no documents, so there is nothing else to check. Retry in a few minutes.')
        return ('Nothing to check', 'TenderTiger has no documents or pre-qualification criteria for this tender. '
                'Check it on TenderTiger.')
    if 'company profile' in lower:
        return ('The company profile is empty', 'Add the company details with Edit profile at the top of the page, '
                'then retry.')
    return ('The analysis failed', 'Retry. If it fails again, see logs/app.log.')


def _checks(criteria, unreadable_files, jv=None, comparison=()):
    """
    Counts and names that let the tender table explain a verdict without opening the analysis. jv holds whether a
    JV is allowed, its maximum members, the verdict for the company as JV lead member and, under lead, the same
    counts for it, from the comparison rows.
    """
    def counts(rows):
        return {
            'total': len(rows),
            'met': sum(1 for row in rows if row['status'] == 'met'),
            'not_met': [row['criterion'] for row in rows if row['status'] == 'not_met'],
            'unknown': [row['criterion'] for row in rows if row['status'] == 'unknown'],
        }

    jv = jv or {'allowed': 'unknown', 'max_members': None, 'verdict': None}
    lead = [{'criterion': row['criterion'], 'status': row['lead_status']} for row in comparison if row['lead_status']]
    return {
        **counts(criteria),
        'unreadable_files': len(unreadable_files),
        'jv': {'allowed': jv['allowed'], 'max_members': jv['max_members'], 'verdict': jv.get('verdict'),
               'lead': counts(lead)},
    }


def _summary(result):
    """The SUMMARY_KEYS fields of a result."""
    return {key: result.get(key) for key in SUMMARY_KEYS}


def _average(seconds):
    """The average of a list of durations, rounded, or None for an empty list."""
    return round(sum(seconds) / len(seconds), 1) if seconds else None


def _name(tender):
    """The tender's description on one line."""
    return ' '.join(str(tender.get('tendersbrief') or '').split())


def _now():
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
