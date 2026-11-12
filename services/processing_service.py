"""
Runs download -> extract -> index in Pinecone -> retrieve -> assess for the queued tenders, on as many workers as
run: threads of the web app and worker processes (worker.py), sharing the job's state in Redis. A tender whose documents an earlier run indexed (its Pinecone namespace tcno-<tcno> exists) is
re-checked against that namespace instead, unless it is queued to regenerate: then its namespace is deleted, the
documents downloaded again and the namespace rebuilt. Each result is saved in the tender's folder and read back when the app
starts, so it survives a restart.
"""
import copy
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from ext import COMPANY_DETAILS_FILE, TENDERTIGER_BLOCKED, update_company_details
from services.analysis_service import (RESULT_FILE, TENDER_DATA_SOURCE, OcrBudgetExceeded, TokenUsage, criterion_key,
                                       jv_verdict, ocr_estimate, over_budget, process_tender_files,
                                       recheck_saved_criteria, statuses_verdict, tender_files)
from services.cache_service import Cache
from services.company_service import record_mark
from services.job_state import LEASE_SECONDS, JobState
from services.ocr_service import OCR_PAGE_COST
from services.download_service import DownloadError, download_tender_documents
from services.pinecone_service import NamespaceIncomplete, PineconeService, QuotaExceeded, VectorStoreError
from services.tender_service import closing_date, has_documents, prequalification_url
from utils.files import safe_name

log = logging.getLogger(__name__)

IDLE_PROGRESS = {
    'job_id': None, 'status': 'idle', 'total': 0, 'completed': 0, 'failed': 0, 'awaiting_approval': 0, 'cancelled': 0,
    'processed': 0, 'percent': 0, 'current': None, 'active': [], 'started_at': None, 'finished_at': None,
    'stop_requested': False, 'items': [],
}

# The part of a result the tender table needs: everything except the full details and file list.
# rechecked: checked against the tender's Pinecone namespace instead of the documents (for a queued tender: will be).
# namespace: the tender's Pinecone namespace after the run (such as tcno-100091540), or None when it has none.
# status: completed, failed, or awaiting_approval (stopped before any OCR, its scans being over the OCR budget).
# ocr_estimate: for a tender awaiting approval, the OCR it needs (see analysis_service.ocr_estimate()).
SUMMARY_KEYS = ('status', 'verdict', 'score', 'error', 'error_title', 'error_action', 'checks', 'processed_at',
                'rechecked', 'namespace', 'ocr_estimate')
# A result's brief in the state (see JobState.briefs()): its summary, and what the run estimates and the folder scan
# need
BRIEF_KEYS = SUMMARY_KEYS + ('tcno', 'name', 'closing_date', 'folder', 'duration_seconds')


class NoJobRunning(Exception):
    """There is no running job to stop."""


class TenderBusy(Exception):
    """The tender is queued or being processed, so its result is about to be replaced."""


class TenderProcessor:
    """
    Owns the processing job and the latest result of every processed tender, in Redis (see services/job_state.py),
    so several workers can process the tenders of a job at the same time: up to job_workers threads of this process,
    started as tenders are queued and gone once none is left, and any number of worker processes (worker.py, see
    work()) sharing the same Redis and the same target_directory. A tender is processed by one worker at a time.
    Results are also saved in the tender folders, the record they are loaded from on creation (load_results).
    """

    def __init__(self, target_directory, download_timeout=120, max_download_bytes=500 * 1024 * 1024,
                 max_file_chars=300_000, company_file=COMPANY_DETAILS_FILE, index=None, workers=4, cache=None,
                 ocr_budget=None, state=None, job_workers=1, load_results=True):
        self.target_directory = Path(target_directory)
        # The most OCR a tender may need before the team is asked, {'pages', 'usd'} (0 or None: no limit)
        self.ocr_budget = ocr_budget
        # The texts of files read before (a cache_service.Cache), so regenerating reads only the files that changed
        self.cache = cache
        self.download_timeout = download_timeout
        self.max_download_bytes = max_download_bytes
        self.max_file_chars = max_file_chars
        self.company_file = company_file
        # The Pinecone index of the tenders' namespaces; unconfigured (no API key), every analysis fails clearly
        self.index = index or PineconeService()
        # Files read at the same time
        self.workers = workers
        # The job and the results (a JobState); an in-process Redis without one
        self.state = state or JobState.connect()
        # Tenders this process works on at the same time; 0 leaves them to worker processes
        self.job_workers = job_workers
        self.worker_name = f'{socket.gethostname()}:{os.getpid()}'
        self._threads = []  # this process's worker threads
        self._threads_lock = threading.Lock()
        if load_results:
            self._load_results()

    @classmethod
    def from_config(cls, config, index=None, load_results=True, job_workers=None):
        """
        The processor config (see config.Config) describes: its Redis (REDIS_URL), cache, Pinecone index (index, if
        given, in place of the one config describes) and OCR budget. job_workers defaults to JOB_WORKERS.
        """
        cache = Cache(config.CACHE_FILE or config.TARGET_DIRECTORY / 'cache.sqlite3')
        return cls(config.TARGET_DIRECTORY, download_timeout=config.DOWNLOAD_TIMEOUT,
                   max_download_bytes=config.MAX_DOWNLOAD_MB * 1024 * 1024, max_file_chars=config.MAX_FILE_CHARS,
                   company_file=config.COMPANY_DETAILS_FILE,
                   index=index or PineconeService.from_config(config, cache=cache), workers=config.INGEST_WORKERS,
                   cache=cache, ocr_budget={'pages': config.OCR_BUDGET_PAGES, 'usd': config.OCR_BUDGET_USD},
                   state=JobState.connect(config.REDIS_URL, config.REDIS_PREFIX),
                   job_workers=config.JOB_WORKERS if job_workers is None else job_workers, load_results=load_results)

    def namespace(self, tcno):
        """The tender's Pinecone namespace when it has one, else None. Never raises (see has_namespace())."""
        try:
            return self.index.namespace(tcno) if self.index.has_namespace(tcno) else None
        except VectorStoreError:
            return None

    def tender_file(self, tcno, name):
        """
        The tender's document called name, its path in the tender folder as result['files'] lists it, or None.
        Only a listed document is found, so no name can reach outside the folder or the saved result and analysis.
        """
        folder = self.target_directory / safe_name(tcno)
        return next((path for path in tender_files(folder) if path.relative_to(folder).as_posix() == name), None)

    def _load_results(self):
        """
        Loads the results saved in the tender folders by earlier runs into the state, in place of those there: the
        folders are the record. A file that cannot be read is skipped.
        """
        with self.state.lock():
            results = {}
            for path in sorted(self.target_directory.glob(f'*/{RESULT_FILE}')):
                try:
                    result = read_result(path)
                except (OSError, ValueError):
                    log.warning('Could not read the saved result %s; skipping it', path, exc_info=True)
                    continue
                # Where the folder is now, and whether its namespace is still there; results saved before Pinecone
                # named a criteria file instead
                result.pop('criteria_file', None)
                result.pop('index_name', None)
                result.update(folder=str(path.parent), namespace=self.namespace(result['tcno']))
                results[str(result['tcno'])] = result
            self.state.replace_results(results, {tcno: _brief(result) for tcno, result in results.items()})
        if results:
            log.info('Loaded %d saved result(s) from %s', len(results), self.target_directory)

    def start(self, tenders, regenerate=False, approve_ocr=False):
        """
        Queues the given raw tenders in order and returns the job's progress. Starts a new job when none is
        running, otherwise adds them to the running one. Tenders already queued or processing are skipped.
        A tender with a Pinecone namespace is checked against it, unless regenerate is true: then its namespace is
        deleted, its documents downloaded and read again, and the namespace rebuilt. A tender whose documents need
        more OCR than the OCR budget stops before any is done, as awaiting_approval with its 'ocr_estimate', unless
        approve_ocr is true. Up to job_workers threads of this process start on them; worker processes take the rest.
        """
        # Asked before taking the lock: it may call Pinecone (once per cache period, not once per tender)
        indexed = {} if regenerate else {str(tender['tcno']): self.index.has_namespace(tender['tcno'])
                                         for tender in tenders}
        with self.state.lock():
            job = self.state.job()
            new_job = not (job and job['status'] == 'running')
            if new_job:
                job = {'job_id': uuid.uuid4().hex}
                self.state.new_job(job['job_id'], _now())
            waiting = {item['tcno'] for item in self.state.items() if item['status'] in ('queued', 'processing')}
            added = 0
            for tender in tenders:
                tcno = str(tender['tcno'])
                if tcno in waiting:
                    continue
                waiting.add(tcno)
                self.state.add_item({**dict.fromkeys(SUMMARY_KEYS), 'tcno': tcno, 'name': _name(tender),
                                     'status': 'queued', 'rechecked': indexed.get(tcno, False)},
                                    {'tender': tender, 'regenerate': regenerate, 'approve_ocr': approve_ocr})
                added += 1
            if added:
                self.state.update_job(stop_requested=False)  # new work overrides an earlier stop
            self._finish_if_done()
        log.info('Job %s: %d tender(s) %s', job['job_id'], added, 'queued in a new job' if new_job else 'added')
        self._start_threads()
        return self.progress()

    def stop(self):
        """
        Cancels the tenders still queued; those being processed finish normally.
        Returns the job's progress. Raises NoJobRunning when no job is running.
        """
        with self.state.lock():
            job = self.state.job()
            if not job or job['status'] != 'running':
                raise NoJobRunning()
            queued = [item for item in self.state.items() if item['status'] == 'queued']
            for item in queued:
                item['status'] = 'cancelled'
                self.state.put_item(item)
            self.state.unqueue([item['id'] for item in queued])
            self.state.update_job(stop_requested=True)
            self._finish_if_done()
        log.info('Job %s: stop requested', job['job_id'])
        return self.progress()

    def progress(self):
        """
        Returns a snapshot of the current (or last) job, with processed count, percentage, and the average
        duration of the tenders completed so far, for time estimates: average_seconds for those whose documents
        were read, average_recheck_seconds for those checked against their namespace (None before the first one).
        active lists the tenders being processed, each {'tcno', 'name', 'stage', 'step', 'steps',
        'elapsed_seconds'}, and current is the first of them (or None). A tender whose worker stopped is failed here.
        """
        self._reap()
        job = self.state.job()
        if job is None:
            progress = copy.deepcopy(IDLE_PROGRESS)
        else:
            items = self.state.items()
            stages = self.state.stages()
            counts = Counter(item['status'] for item in items)
            active = []
            for item in items:
                if item['status'] == 'processing':
                    stage = stages.get(item['id']) or {}
                    active.append({'tcno': item['tcno'], 'name': item.get('name') or '',
                                   'stage': stage.get('stage') or 'Starting', 'step': stage.get('step'),
                                   'steps': stage.get('steps'),
                                   'elapsed_seconds': round(time.time() - stage.get('started', time.time()))})
            progress = {**job, 'total': len(items), 'completed': counts['completed'], 'failed': counts['failed'],
                        'awaiting_approval': counts['awaiting_approval'], 'cancelled': counts['cancelled'],
                        'current': active[0] if active else None, 'active': active,
                        'items': [{key: value for key, value in item.items() if key != 'id'} for item in items]}
        progress['processed'] = progress['completed'] + progress['failed'] + progress['awaiting_approval']
        done = progress['processed'] + progress['cancelled']
        progress['percent'] = round(100 * done / progress['total']) if progress['total'] else 100
        completed = [brief for brief in self.state.briefs().values() if brief.get('status') == 'completed']
        progress['average_seconds'] = _average(
            [brief['duration_seconds'] for brief in completed if not brief.get('rechecked')])
        progress['average_recheck_seconds'] = _average(
            [brief['duration_seconds'] for brief in completed if brief.get('rechecked')])
        return progress

    def result(self, tcno):
        """Returns the full result (including details) of the last run of this tender, or None."""
        return self.state.result(tcno)

    def saved_results(self):
        """Returns the full result of the last run of every processed tender."""
        return self.state.results()

    def summaries(self):
        """Returns {tcno: summary} for every processed tender, where a summary holds the SUMMARY_KEYS fields."""
        return {tcno: {key: brief.get(key) for key in SUMMARY_KEYS} for tcno, brief in self.state.briefs().items()}

    def mark_criterion(self, tcno, index, status):
        """
        Sets the status of the criterion at index in the tender's result by hand: 'met', 'not_met' or 'unknown',
        or None for the model's status again. Saves the result and returns it, with the score, counts and verdict
        worked out again (see _restatus()). A re-check keeps the marks: see _carry_marks(). The mark is also
        recorded in the company profile, or removed from it with the mark (see _record_mark()).
        Raises LookupError without a completed result or such a criterion, TenderBusy while it is queued or
        being processed, and OSError when the result cannot be saved (then nothing changes).
        """
        def change(result):
            if not 0 <= index < len(result['criteria']):
                raise LookupError(f'Tender {tcno} has no criterion {index}.')
            _mark(result['criteria'][index], status)
            return f"criterion {result['criteria'][index]['criterion']!r} marked {status or 'as the model had it'}"
        result = self._change(tcno, change)
        self._record_mark(tcno, result['criteria'][index])
        return result

    def _record_mark(self, tcno, criterion):
        """
        Records in the company profile the status the bid team marked by hand on the tender's criterion, or removes
        it once the criterion is back to the model's status, so the other tenders checked from now on know it. A
        profile that cannot be saved is only logged: the mark is saved in the result all the same.
        """
        status = criterion['status'] if criterion.get('marked_by_user') else None
        try:
            update_company_details(lambda text: record_mark(text, tcno, criterion, status, date.today().isoformat()),
                                   self.company_file)
        except OSError:
            log.exception('Tender %s: could not record the mark of criterion %r in the company profile %s', tcno,
                          criterion['criterion'], self.company_file)

    def check_unreadable(self, tcno, checked):
        """
        Records that the bid team checked the sources the model could not read (checked true), or takes that back.
        Checked sources no longer keep a verdict worked out from the statuses from Eligible. Saves the result and
        returns it; raises as mark_criterion() does, and LookupError when no source was unreadable.
        """
        def change(result):
            unreadable = result.get('unreadable_files') or []
            if not unreadable:
                raise LookupError(f'Tender {tcno} has no unreadable files.')
            if checked:
                result['unreadable_checked'] = list(unreadable)
            else:
                result.pop('unreadable_checked', None)
            return f"{len(unreadable)} unreadable source(s) {'checked' if checked else 'no longer checked'}"
        return self._change(tcno, change)

    def delete_namespace(self, tcno):
        """
        Deletes the tender's Pinecone namespace, to free a place under the Pinecone plan's namespace limit; the next
        check of the tender downloads and indexes its documents again. Its last result is kept, with namespace None.
        Returns whether there was an index. Raises TenderBusy while the tender is queued or being processed, and
        VectorStoreError when Pinecone fails.
        """
        tcno = str(tcno)
        if self.busy(tcno):
            raise TenderBusy()
        deleted = self.index.delete_namespace(tcno)
        with self.state.lock():
            result = self.state.result(tcno)
            if result and result.get('namespace'):
                result['namespace'] = None
                try:
                    save_result(result['folder'], result)
                except OSError:
                    log.exception('Could not save the result of tender %s', tcno)
                self.state.put_result(result, _brief(result))  # gone from Pinecone, saved or not
        return deleted

    def _change(self, tcno, change):
        """
        Applies change, a function editing a copy of the tender's completed result by hand and returning what it
        did, works out again what follows from it (_restatus()), saves the result and returns it.
        """
        tcno = str(tcno)
        with self.state.lock():
            updated = self.state.result(tcno)
            if not updated or updated['status'] != 'completed':
                raise LookupError(f'No completed result for TCNO {tcno}.')
            if self.busy(tcno):
                raise TenderBusy()
            done = change(updated)
            _restatus(updated)
            save_result(updated['folder'], updated)
            self.state.put_result(updated, _brief(updated))
            # The job's item of this tender is what the page reads after a reload, so it shows the change too
            for item in self.state.items():
                if item['tcno'] == tcno and item['status'] == 'completed':
                    item.update(_summary(updated))
                    self.state.put_item(item)
            log.info('Tender %s: %s by the user (now %s)', tcno, done, updated['verdict'])
            return updated

    def _carry_marks(self, tcno, result):
        """
        Marks again, on a new result, what the bid team marked by hand on the tender's last one: the statuses of
        criteria with the same name, and the unreadable sources it checked that are still unreadable. Returns
        True when anything was marked.
        """
        last = self.state.result(tcno) or {}
        marks = {criterion_key(criterion['criterion']): criterion['status']
                 for criterion in last.get('criteria') or [] if criterion.get('marked_by_user')}
        matched = 0
        for criterion in result['criteria']:
            status = marks.get(criterion_key(criterion['criterion']))
            if status:
                _mark(criterion, status)
                matched += 1
        if len(marks) > matched:
            log.info('Tender %s: %d status(es) marked by the user match no criterion of the new result',
                     tcno, len(marks) - matched)
        checked = [source for source in last.get('unreadable_checked') or [] if source in result['unreadable_files']]
        if checked:
            result['unreadable_checked'] = checked
        return bool(checked) or any(criterion.get('marked_by_user') for criterion in result['criteria'])

    def remember_closing_dates(self, tenders):
        """
        Saves the closing date of each listed tender (raw TenderTiger rows) in its saved result, if it has one, so
        the date is still known once TenderTiger no longer lists the tender (see closed_downloads()). TenderTiger
        may move a closing date, so the listed one replaces the saved one.
        """
        # Compared with the briefs first, in one read: only a date that changed costs a write
        briefs = self.state.briefs()
        for tender in tenders:
            closing = closing_date(tender)
            brief = briefs.get(str(tender.get('tcno')))
            if closing and brief and brief.get('closing_date') != closing.isoformat():
                self._save_closing_date(str(tender.get('tcno')), closing.isoformat())

    def _save_closing_date(self, tcno, closing):
        """Saves closing (YYYY-MM-DD) in the tender's result when it has one with another date and is not busy."""
        with self.state.lock():
            result = self.state.result(tcno)
            if not result or result.get('closing_date') == closing or self.busy(tcno):
                return
            updated = {**result, 'closing_date': closing}
            try:
                save_result(updated['folder'], updated)
            except OSError:
                log.exception('Could not save the closing date of tender %s', tcno)
                return
            self.state.put_result(updated, _brief(updated))

    def ocr_estimates(self, tcnos):
        """
        What regenerating these tenders would cost in OCR, from the documents already downloaded, before anything is
        queued: {'tenders', 'pages', 'cost', 'over_budget', 'budget', 'page_cost'}. Each of tenders is the tender's
        ocr_estimate() with 'tcno', 'downloaded' (False when its folder has no documents yet: they are estimated
        after the download) and 'over_budget' (the run will stop there and ask). Files read before cost nothing: their
        text is in the cache. Documents changed since the download are not foreseen.
        """
        tenders = []
        for tcno in dict.fromkeys(str(tcno) for tcno in tcnos):
            folder = self.target_directory / safe_name(tcno)
            if not (folder.is_dir() and tender_files(folder)):
                tenders.append({'tcno': tcno, 'downloaded': False, 'pages': 0, 'cost': 0.0, 'files': [],
                                'over_budget': False})
                continue
            estimate = ocr_estimate(folder, self.cache, self.max_file_chars)
            tenders.append({'tcno': tcno, 'downloaded': True, **estimate,
                            'over_budget': over_budget(estimate, self.ocr_budget)})
        return {'tenders': tenders, 'pages': sum(tender['pages'] for tender in tenders),
                'cost': round(sum(tender['cost'] for tender in tenders), 4),
                'over_budget': sum(1 for tender in tenders if tender['over_budget']),
                'budget': dict(self.ocr_budget or {}), 'page_cost': OCR_PAGE_COST}

    def closed_downloads(self, today=None):
        """
        The tenders whose documents are still in the download folder although they have closed, to tidy up:
        {'directory', 'closed', 'unknown'}. closed holds those whose closing date is before today, longest closed
        first; unknown those with documents but no closing date on record (results saved before closing dates were,
        of tenders no longer listed, and not indexed in Pinecone either). Each is {'tcno', 'name', 'closing_date'
        (21-Nov-2026, or None), 'days_closed', 'files', 'bytes'}. A closing date a result lacks is read from the
        TenderTiger data indexed in the tender's Pinecone namespace, and saved.
        """
        today = today or date.today()
        results = {Path(brief['folder']).name: {key: brief.get(key) for key in ('tcno', 'name', 'closing_date')}
                   for brief in self.state.briefs().values()}
        # Read once, not once per folder (see busy()): each read is a round trip to Redis
        busy = {item['tcno'] for item in self.state.items() if item['status'] in ('queued', 'processing')}
        folders = []  # (tcno, saved result fields or None, documents)
        for folder in sorted(path for path in self.target_directory.iterdir() if path.is_dir()):
            result = results.get(folder.name)
            # A folder that is no tender's, such as a download's staging folder (TCNOs are numbers)
            if result is None and not folder.name.isdigit():
                continue
            tcno = str(result['tcno']) if result else folder.name
            files = tender_files(folder)
            if files and tcno not in busy:
                folders.append((tcno, result or {}, files))

        missing = [tcno for tcno, result, _ in folders if not result.get('closing_date')]
        found = {}
        if missing and self.index.configured:
            # Fetched by ID, in parallel; a date found is saved in the result, so it is fetched once per tender
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                found = dict(zip(missing, pool.map(self._indexed_closing_date, missing)))

        closed, unknown = [], []
        for tcno, result, files in folders:
            closing = result.get('closing_date')
            if not closing and found.get(tcno):
                closing = found[tcno]
                self._save_closing_date(tcno, closing)
            closing = date.fromisoformat(closing) if closing else None
            if closing and closing >= today:
                continue
            (closed if closing else unknown).append({
                'tcno': tcno, 'name': result.get('name') or '',
                'closing_date': closing.strftime('%d-%b-%Y') if closing else None,
                'days_closed': (today - closing).days if closing else None,
                'files': len(files), 'bytes': sum(path.stat().st_size for path in files),
            })
        closed.sort(key=lambda tender: -tender['days_closed'])
        return {'directory': str(self.target_directory), 'closed': closed, 'unknown': unknown}

    def delete_tender(self, tcno):
        """
        Deletes everything this processor keeps for the tender, once it has closed: its Pinecone namespace, then its
        folder (the downloaded documents, the saved result and deep analysis) and all Redis holds of it (its result
        and brief, and its items in the last job, with the job itself if it had no other tender), so it reads as
        never checked. The statuses the team marked by hand stay recorded in the company profile; the team's decision and
        the deep analysis's memory are the caller's (see routes.api.delete_downloads()). The namespace goes first, so
        a Pinecone failure leaves the rest for a retry.
        Returns {'tcno', 'files', 'bytes', 'namespace_deleted'}: how many documents went (the result and deep
        analysis not counted), their size, and whether there was a namespace. Raises LookupError when nothing is
        kept for the tender, TenderBusy while it is queued or being checked, VectorStoreError when Pinecone fails,
        and OSError when a file cannot be deleted (one open in another program, say).
        """
        tcno = str(tcno)
        folder = self.target_directory / safe_name(tcno)
        known = self.state.result(tcno) is not None
        if not known and not folder.is_dir():
            raise LookupError(f'Nothing is kept for tender {tcno}.')
        namespace_deleted = self.delete_namespace(tcno) if self.index.configured else False
        with self.state.lock():
            # Under the lock, so the tender cannot be queued and downloaded again while its folder goes
            if self.busy(tcno):
                raise TenderBusy()
            files = tender_files(folder) if folder.is_dir() else []
            size = sum(path.stat().st_size for path in files)
            if folder.is_dir():
                shutil.rmtree(folder)
            # Nothing of the tender stays in Redis: its result and brief, and its items in the last job (which the page
            # reads after a reload); a job left with no tenders is forgotten, as if it had not run
            self.state.delete_result(tcno)
            self.state.remove_items([item['id'] for item in self.state.items() if item['tcno'] == tcno])
            if not self.state.items():
                self.state.clear_job()
        log.info('Tender %s: deleted its folder (%d downloaded file(s), %d bytes) and result%s', tcno, len(files), size,
                 ', and its Pinecone namespace' if namespace_deleted else '')
        return {'tcno': tcno, 'files': len(files), 'bytes': size, 'namespace_deleted': namespace_deleted}

    def _indexed_closing_date(self, tcno):
        """
        The closing date in the TenderTiger data indexed with the tender's documents (tender_data_text()), as
        YYYY-MM-DD; None without a namespace or such a line, or when Pinecone fails (logged).
        """
        if not self.index.has_namespace(tcno):
            return None
        try:
            text = self.index.first_chunk(tcno, TENDER_DATA_SOURCE) or ''
        except VectorStoreError:
            log.warning('Tender %s: could not read its closing date from Pinecone', tcno, exc_info=True)
            return None
        match = re.search(r'^Closing date: (\d{2}-[A-Za-z]{3}-\d{4})$', text, re.MULTILINE)
        try:
            return datetime.strptime(match.group(1), '%d-%b-%Y').date().isoformat() if match else None
        except ValueError:
            return None

    def busy(self, tcno):
        """True while the tender is queued or being processed, so its result is about to be replaced."""
        tcno = str(tcno)
        return any(item['tcno'] == tcno and item['status'] in ('queued', 'processing') for item in self.state.items())

    # ---------- workers ----------

    def work(self, threads=1, stop=None):
        """
        A worker process's loop (worker.py): processes the queued tenders of every job, threads at a time, until stop
        (a threading.Event) is set, waiting for more when none is queued. Returns the threads, already started; each
        finishes the tender it is on once stop is set.
        """
        stop = stop or threading.Event()
        pool = [threading.Thread(target=self._work, kwargs={'stop': stop}, name=f'worker-{number}', daemon=True)
                for number in range(1, threads + 1)]
        for thread in pool:
            thread.start()
        return pool

    def _start_threads(self):
        """Starts worker threads of this process on the queued tenders, up to job_workers in all."""
        with self._threads_lock:
            for number in range(len(self._threads), self.job_workers):
                thread = threading.Thread(target=self._work, kwargs={'until_empty': True},
                                          name=f'job-worker-{number + 1}', daemon=True)
                self._threads.append(thread)
                thread.start()

    def _work(self, until_empty=False, stop=None):
        """
        Thread body: takes queued tenders one at a time and processes them. until_empty: returns once none is queued
        (this process's threads, started as tenders are queued); otherwise waits for more until stop is set.
        """
        while not (stop and stop.is_set()):
            try:
                self._reap()
                claimed = self._claim()
                if not claimed and until_empty:
                    # Looked at again under the lock _start_threads() counts threads under, so a tender queued as this
                    # thread leaves still gets one
                    with self._threads_lock:
                        claimed = self._claim()
                        if not claimed:
                            self._threads.remove(threading.current_thread())
                            return
                if claimed:
                    self._work_on(*claimed)
                else:
                    self.state.wait(timeout=5)
            except Exception:
                # Redis out of reach, say; the tender a worker had is failed once its lease runs out (see _reap())
                log.exception('Worker %s could not take or finish a tender', self.worker_name)
                if until_empty:
                    with self._threads_lock:
                        self._threads.remove(threading.current_thread())
                    return
                (stop or threading.Event()).wait(5)

    def _claim(self):
        """Takes the oldest queued tender for this worker: (item, payload, started), or None when none is queued."""
        with self.state.lock():
            while True:
                item_id = self.state.next_queued()
                if item_id is None:
                    return None
                item = self.state.item(item_id)
                if item and item['status'] == 'queued':  # not cancelled meanwhile
                    break
            item['status'] = 'processing'
            self.state.put_item(item)
            started = time.time()
            self.state.set_stage(item_id, 'Starting', started=started)
            self.state.renew(item_id, self.worker_name)
        return item, self.state.payload(item_id), started

    def _work_on(self, item, payload, started):
        """Processes a claimed tender, keeping its lease alive meanwhile, and records its result."""
        item_id = item['id']
        done = threading.Event()

        def keep_lease():
            while not done.wait(LEASE_SECONDS / 4):
                try:
                    self.state.renew(item_id, self.worker_name)
                except Exception:
                    log.warning('Could not renew the lease on tender %s', item['tcno'], exc_info=True)

        def set_stage(stage, step=None, steps=None):
            try:
                self.state.set_stage(item_id, stage, step, steps, started)
            except Exception:
                log.warning('Could not record the stage of tender %s', item['tcno'], exc_info=True)

        threading.Thread(target=keep_lease, name=f'lease-{item["tcno"]}', daemon=True).start()
        try:
            result = self._process(payload['tender'], payload['regenerate'], payload['approve_ocr'], set_stage)
        finally:
            done.set()
        with self.state.lock():
            self.state.put_result(result, _brief(result))
            current = self.state.item(item_id)
            if current:  # gone only if a new job replaced this one
                current.update(_summary(result))
                self.state.put_item(current)
            self.state.release(item_id)
            if result['error'] == TENDERTIGER_BLOCKED:
                self._cancel_tendertiger_work()
            self._finish_if_done()

    def _reap(self):
        """Fails the tenders being processed whose worker stopped (its lease ran out), and ends a job left with none."""
        processing = [item['id'] for item in self.state.items() if item['status'] == 'processing']
        if not processing or len(self.state.leased(processing)) == len(processing):
            return
        with self.state.lock():
            for item in self.state.items():
                if item['status'] != 'processing' or self.state.leased([item['id']]):
                    continue
                item.update(status='failed', error='The worker processing the tender stopped',
                            error_title='Processing stopped unexpectedly',
                            error_action="Retry. If it happens again, see the worker's log.")
                self.state.put_item(item)
                self.state.release(item['id'])
                log.warning('Tender %s failed: the worker processing it stopped', item['tcno'])
            self._finish_if_done()

    def _cancel_tendertiger_work(self):
        """
        Cancels the queued tenders that need TenderTiger once its firewall blocks the app: their requests would only
        be refused, and keep the block going. Re-checks against a Pinecone namespace go on. The caller holds the
        lock.
        """
        cancelled = [item for item in self.state.items() if item['status'] == 'queued' and not item['rechecked']]
        for item in cancelled:
            item['status'] = 'cancelled'
            self.state.put_item(item)
        self.state.unqueue([item['id'] for item in cancelled])
        if cancelled:
            log.warning('TenderTiger is blocking automated visits, so %d queued tender(s) were not started',
                        len(cancelled))

    def _finish_if_done(self):
        """Marks the running job completed once no tender is queued or being processed. The caller holds the lock."""
        job = self.state.job()
        if not job or job['status'] != 'running':
            return
        items = self.state.items()
        if any(item['status'] in ('queued', 'processing') for item in items):
            return
        self.state.update_job(status='completed', finished_at=_now())
        counts = Counter(item['status'] for item in items)
        log.info('Job %s finished: %d completed, %d failed, %d awaiting approval, %d cancelled', job['job_id'],
                 counts['completed'], counts['failed'], counts['awaiting_approval'], counts['cancelled'])

    def _process(self, tender, regenerate=False, approve_ocr=False, set_stage=None):
        """
        Downloads, extracts, indexes and analyses one tender, or re-checks it against its Pinecone namespace when it
        has one and regenerate is false. Regenerating deletes the namespace first, so a failed download leaves none
        and the next check reads the documents again. A namespace left half-built by an earlier failure is rebuilt
        from the documents. Never raises: failures become a 'failed' result, and a tender whose scans need more OCR
        than the budget an 'awaiting_approval' one. Its 'tokens' are the LLM tokens spent,
        failed or not: see TokenUsage.counts(). approve_ocr lifts the OCR budget. set_stage(stage, step=None,
        steps=None), if given, is told what it is doing.
        """
        tcno = str(tender['tcno'])
        folder = self.target_directory / safe_name(tcno)
        usage = TokenUsage()
        set_stage = set_stage or (lambda stage, step=None, steps=None: None)

        log.info('Processing tender %s', tcno)
        started = time.monotonic()
        closing = closing_date(tender)
        result = {
            'tcno': tcno, 'name': _name(tender), 'closing_date': closing.isoformat() if closing else None,
            'status': 'completed', 'verdict': None, 'details': None,
            'score': None, 'criteria': [], 'unreadable_files': [], 'jv': None, 'comparison': [], 'checks': None,
            'error': None,
            'error_title': None, 'error_action': None, 'files': [], 'folder': str(folder),
            'rechecked': False, 'namespace': None, 'index': None,
        }
        try:
            # Strict: a Pinecone that cannot be reached fails the tender now, before any download
            result['rechecked'] = not regenerate and self.index.has_namespace(tcno, strict=True)
            analysis = None
            if result['rechecked']:
                log.info('Tender %s: re-checking it against its Pinecone namespace %s', tcno,
                         self.index.namespace(tcno))
                result['files'] = [path.relative_to(folder).as_posix() for path in tender_files(folder)]
                try:
                    analysis = recheck_saved_criteria(tcno, index=self.index, on_progress=set_stage,
                                                      company_file=self.company_file, usage=usage, tender=tender)
                except NamespaceIncomplete:
                    log.warning('Tender %s: its Pinecone namespace was left half-built; reading the documents again',
                                tcno)
                    result['rechecked'] = False
            elif regenerate:
                set_stage(f'Deleting the Pinecone namespace {self.index.namespace(tcno)}')
                self.index.delete_namespace(tcno)
            if analysis is None:
                analysis = self._analyse_documents(tender, folder, result, set_stage, usage, approve_ocr)
            criteria = analysis.get('criteria') or []
            unreadable = analysis.get('unreadable_files') or []
            jv = analysis.get('jv')
            comparison = analysis.get('comparison') or []
            result.update(verdict=analysis['verdict'], details=analysis['details'], score=analysis['score'],
                          criteria=criteria, unreadable_files=unreadable, jv=jv, comparison=comparison,
                          checks=_checks(criteria, unreadable, jv, comparison), index=analysis.get('index'))
            if self._carry_marks(tcno, result):
                _restatus(result)
            log.info('Tender %s: %s (score %s)', tcno, result['verdict'], result['score'])
        except Exception as exc:
            title, action = explain_error(exc)
            result.update(status='failed', error=str(exc) or exc.__class__.__name__,
                          error_title=title, error_action=action)
            if isinstance(exc, OcrBudgetExceeded):
                # Not a failure: nothing went wrong, the team decides whether to spend on the OCR
                log.info('Tender %s is awaiting approval of its OCR: %s', tcno, exc)
                result.update(status='awaiting_approval', ocr_estimate=exc.estimate)
            else:
                log.exception('Tender %s failed', tcno)
            if not result['rechecked']:
                self._discard_incomplete_namespace(tcno)
        result['namespace'] = self.namespace(tcno)
        result['tokens'] = usage.counts()
        log.info('Tender %s used %d LLM tokens: %d reading the documents, %d for the result', tcno,
                 result['tokens']['total']['total'], result['tokens']['criteria']['total'],
                 result['tokens']['assessment']['total'])
        result['duration_seconds'] = round(time.monotonic() - started, 1)
        result['processed_at'] = _now()
        try:
            save_result(folder, result)
        except OSError:
            log.exception('Could not save the result of tender %s; it is lost on restart', tcno)
        return result

    def _discard_incomplete_namespace(self, tcno):
        """
        After a failed build: deletes the tender's namespace when it has no complete manifest (empty, or left
        half-built), so it neither counts as indexed nor holds a place under the Pinecone plan's namespace limit. A
        complete namespace (the failure came after it was built, in the final check) is kept for a re-check.
        """
        if not self.index.configured:
            return
        try:
            if self.index.has_namespace(tcno, strict=True) and self.index.manifest(tcno) is None:
                self.index.delete_namespace(tcno)
                log.info('Tender %s: deleted its incomplete Pinecone namespace', tcno)
        except VectorStoreError:
            log.warning('Tender %s: could not tidy up its incomplete Pinecone namespace; the next check rebuilds it',
                        tcno, exc_info=True)

    def _analyse_documents(self, tender, folder, result, set_stage, usage, approve_ocr=False):
        """
        Downloads the tender's documents into folder (emptied first), indexes them in the tender's Pinecone
        namespace (rebuilt) and analyses them, counting the tokens in usage; sets result['files']. The OCR budget
        applies unless approve_ocr is true.
        """
        tcno = str(tender['tcno'])
        prequalification_link = prequalification_url(tender)
        # Made (empty) before the download, so a Pinecone plan with no room for another namespace fails the tender
        # before TenderTiger is asked for anything or any OCR is paid for
        set_stage(f'Preparing the Pinecone namespace {self.index.namespace(tcno)}')
        self.index.reset_namespace(tcno)
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
        return process_tender_files(folder, index=self.index, tcno=tcno, on_progress=set_stage,
                                    company_file=self.company_file, prequalification_link=prequalification_link,
                                    tender=tender, usage=usage, max_file_chars=self.max_file_chars,
                                    workers=self.workers, cache=self.cache,
                                    ocr_budget=None if approve_ocr else self.ocr_budget)


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
    if not isinstance(result, dict) or result.get('status') not in ('completed', 'failed', 'awaiting_approval') \
            or 'tcno' not in result or 'duration_seconds' not in result:
        raise ValueError(f'{path} holds no tender result')
    return result


def explain_error(exc):
    """Returns (title, action): what failed and what to do next, in the bid team's terms."""
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, OcrBudgetExceeded):
        # The pages and cost are in the error itself and in the result's ocr_estimate
        return ('Waiting for approval of the OCR',
                'The scanned documents need more OCR than the budget allows, so nothing was read yet. Approve the OCR '
                'to go on, or raise OCR_BUDGET_PAGES / OCR_BUDGET_USD in .env.')
    if "tendertiger's firewall" in lower:
        return ('TenderTiger is blocking automated visits for now',
                'Wait a while, then retry. Re-checks against a Pinecone namespace do not need TenderTiger.')
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
    if isinstance(exc, QuotaExceeded):
        return ('The Pinecone namespace limit is reached',
                'Each tender needs its own Pinecone namespace, and the Pinecone plan allows only so many in an index. '
                'Delete the namespaces of tenders no longer needed (DELETE /api/namespace/<tcno>), or move to a '
                'larger plan, then retry.')
    if 'PINECONE_API_KEY' in text:
        return ('The Pinecone API key is missing', 'Add PINECONE_API_KEY to .env, restart the app and retry.')
    if isinstance(exc, VectorStoreError):
        if 'refused the api key' in lower:
            return ('Pinecone refused the API key', 'Check PINECONE_API_KEY in .env, restart the app and retry.')
        if isinstance(exc, NamespaceIncomplete):
            return ("The tender's Pinecone namespace is missing",
                    'Regenerate the criteria to download the documents and index them again.')
        return ("The tender's Pinecone namespace could not be built or searched",
                'Retry in a few minutes. If it fails again, see logs/app.log.')
    if 'GROQ_API_KEY' in text:
        return ('The LLM API key is missing', 'Add GROQ_API_KEY to .env, restart the app and retry.')
    if 'request too large' in lower and 'tokens per minute' in lower:
        # Groq's 413: one request over the plan's tokens-per-minute limit can never pass, however often it is retried
        return ('The request is too large for the Groq plan',
                "The check needs more tokens than the Groq plan's per-minute limit for the model (8,000 on the free "
                'tier); a full eligibility check can need 12,000, prompt and answer together. Upgrade the Groq plan '
                '(Dev tier) and retry.')
    if 'LLM_MAX_OUTPUT_TOKENS' in text:
        return ("The model's answer was cut off",
                'The answer needed more tokens than LLM_MAX_OUTPUT_TOKENS allows. Raise it in .env, restart the app '
                'and retry.')
    if 'rate_limit_exceeded' in lower or 'rate limit reached' in lower:
        return ("Groq's rate limit was reached",
                'Too many tokens were sent to Groq in the last minute. Wait a minute and retry, or upgrade the '
                'Groq plan.')
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


def _checks(criteria, unreadable_files, jv=None, comparison=(), unreadable_checked=()):
    """
    Counts and names that let the tender table explain a verdict without opening the analysis. jv holds whether a
    JV is allowed, its maximum members, the verdict for the company as JV lead member and, under lead, the same
    counts for it, from the comparison rows. unreadable_checked are the unreadable sources the team checked.
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
        'user_marked': sum(1 for row in criteria if row.get('marked_by_user')),
        'unreadable_files': len(unreadable_files),
        'unreadable_checked': sum(1 for source in unreadable_files if source in unreadable_checked),
        'jv': {'allowed': jv['allowed'], 'max_members': jv['max_members'], 'verdict': jv.get('verdict'),
               'lead': counts(lead)},
    }


def _mark(criterion, status):
    """
    Gives criterion the status marked by hand, keeping the model's as ai_status, with marked_by_user set. None, or
    the model's own status, removes the mark.
    """
    ai_status = criterion.pop('ai_status', criterion['status'])
    criterion.pop('marked_by_user', None)
    if status is None or status == ai_status:
        criterion['status'] = ai_status
    else:
        criterion.update(status=status, ai_status=ai_status, marked_by_user=True)


def _restatus(result):
    """
    Works out again what follows from a completed result once the bid team marked criteria statuses or checked
    the unreadable sources by hand: the score, the bidding-alone status of each comparison row, the verdict as JV
    lead member, the checks, and the verdict. While anything is marked, the verdict follows the statuses
    (statuses_verdict()), where only unreadable sources nobody checked keep it from Eligible, and the model's is
    kept as ai_verdict; once nothing is, the model's verdict is back.
    """
    criteria = result['criteria']
    unreadable = result.get('unreadable_files') or []
    checked = result.get('unreadable_checked') or []
    unchecked = [source for source in unreadable if source not in checked]
    comparison = result.get('comparison') or []  # results saved before JV checking have none
    if checked or any(criterion.get('marked_by_user') for criterion in criteria):
        result.setdefault('ai_verdict', result['verdict'])
        result['verdict'] = statuses_verdict([criterion['status'] for criterion in criteria], unchecked)
    elif 'ai_verdict' in result:
        result['verdict'] = result.pop('ai_verdict')
    met = sum(1 for criterion in criteria if criterion['status'] == 'met')
    result['score'] = round(100 * met / len(criteria)) if criteria else None
    # compare_routes() lists the criteria first, in order
    for row, criterion in zip(comparison, criteria):
        if row['criterion'] == criterion['criterion']:
            row['alone_status'] = criterion['status']
    jv = result.get('jv')
    if jv:
        jv['verdict'] = jv_verdict(comparison, jv, unchecked)
    result['checks'] = _checks(criteria, unreadable, jv, comparison, checked)


def _summary(result):
    """The SUMMARY_KEYS fields of a result."""
    return {key: result.get(key) for key in SUMMARY_KEYS}


def _brief(result):
    """The fields of a result the state keeps beside it, so lists and estimates need not read whole results."""
    return {key: result.get(key) for key in BRIEF_KEYS}


def _average(seconds):
    """The average of a list of durations, rounded, or None for an empty list."""
    return round(sum(seconds) / len(seconds), 1) if seconds else None


def _name(tender):
    """The tender's description on one line."""
    return ' '.join(str(tender.get('tendersbrief') or '').split())


def _now():
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
