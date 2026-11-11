"""
Runs download -> extract -> index in Pinecone -> retrieve -> assess for the queued tenders, one at a time, in a
background thread. A tender whose documents an earlier run indexed (its Pinecone namespace tcno-<tcno> exists) is
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
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from ext import COMPANY_DETAILS_FILE, TENDERTIGER_BLOCKED, update_company_details
from services.analysis_service import (RESULT_FILE, TENDER_DATA_SOURCE, TokenUsage, criterion_key, jv_verdict,
                                       process_tender_files, recheck_saved_criteria, statuses_verdict, tender_files)
from services.company_service import record_mark
from services.download_service import DownloadError, download_tender_documents
from services.pinecone_service import NamespaceIncomplete, PineconeService, QuotaExceeded, VectorStoreError
from services.tender_service import closing_date, has_documents, prequalification_url
from utils.files import safe_name

log = logging.getLogger(__name__)

IDLE_PROGRESS = {
    'job_id': None, 'status': 'idle', 'total': 0, 'completed': 0, 'failed': 0, 'cancelled': 0, 'processed': 0,
    'percent': 0, 'current': None, 'started_at': None, 'finished_at': None, 'stop_requested': False, 'items': [],
}

# The part of a result the tender table needs: everything except the full details and file list.
# rechecked: checked against the tender's Pinecone namespace instead of the documents (for a queued tender: will be).
# namespace: the tender's Pinecone namespace after the run (such as tcno-100091540), or None when it has none.
SUMMARY_KEYS = ('status', 'verdict', 'score', 'error', 'error_title', 'error_action', 'checks', 'processed_at',
                'rechecked', 'namespace')


class NoJobRunning(Exception):
    """There is no running job to stop."""


class TenderBusy(Exception):
    """The tender is queued or being processed, so its result is about to be replaced."""


class TenderProcessor:
    """
    Owns the processing job and the latest result of every processed tender.

    One job runs at a time; tenders queued while it runs are added to it. State is kept in memory, so the
    app must run as a single process (any number of threads is fine); results are also saved in the tender
    folders and loaded from there on creation. Every read goes through progress() / result() / summaries(),
    which return copies taken under the lock.
    """

    def __init__(self, target_directory, download_timeout=120, max_download_bytes=500 * 1024 * 1024,
                 max_file_chars=300_000, company_file=COMPANY_DETAILS_FILE, index=None, workers=4):
        self.target_directory = Path(target_directory)
        self.download_timeout = download_timeout
        self.max_download_bytes = max_download_bytes
        self.max_file_chars = max_file_chars
        self.company_file = company_file
        # The Pinecone index of the tenders' namespaces; unconfigured (no API key), every analysis fails clearly
        self.index = index or PineconeService()
        # Files read at the same time
        self.workers = workers
        self._lock = threading.Lock()
        self._job = None
        # (raw tender, regenerate) of the job, in the order of its items; the tenders hold the TenderTiger IDs
        self._queue = []
        self._results = self._load_results()  # tcno -> result dict (see _process)

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
        """The results saved in the tender folders by earlier runs. A file that cannot be read is skipped."""
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
        if results:
            log.info('Loaded %d saved result(s) from %s', len(results), self.target_directory)
        return results

    def start(self, tenders, regenerate=False):
        """
        Queues the given raw tenders in order and returns the job's progress. Starts a new job when none is
        running, otherwise adds them to the running one. Tenders already queued or processing are skipped.
        A tender with a Pinecone namespace is checked against it, unless regenerate is true: then its namespace is
        deleted, its documents downloaded and read again, and the namespace rebuilt.
        """
        # Asked before taking the lock: it may call Pinecone (once per cache period, not once per tender)
        indexed = {} if regenerate else {str(tender['tcno']): self.index.has_namespace(tender['tcno'])
                                         for tender in tenders}
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
                                           'rechecked': indexed.get(tcno, False)})
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
        were read, average_recheck_seconds for those checked against their namespace (None before the first one).
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

    def saved_results(self):
        """Returns the full result of the last run of every processed tender, as copies."""
        with self._lock:
            return copy.deepcopy(list(self._results.values()))

    def summaries(self):
        """Returns {tcno: summary} for every processed tender, where a summary holds the SUMMARY_KEYS fields."""
        with self._lock:
            return {tcno: _summary(result) for tcno, result in self._results.items()}

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
        with self._lock:
            result = self._results.get(tcno)
            if result and result.get('namespace'):
                result['namespace'] = None
                try:
                    save_result(result['folder'], result)
                except OSError:
                    log.exception('Could not save the result of tender %s', tcno)
        return deleted

    def _change(self, tcno, change):
        """
        Applies change, a function editing a copy of the tender's completed result by hand and returning what it
        did, works out again what follows from it (_restatus()), saves the result and returns it.
        """
        tcno = str(tcno)
        with self._lock:
            result = self._results.get(tcno)
            if not result or result['status'] != 'completed':
                raise LookupError(f'No completed result for TCNO {tcno}.')
            items = self._job['items'] if self._job else []
            if self._busy(tcno):
                raise TenderBusy()
            updated = copy.deepcopy(result)
            done = change(updated)
            _restatus(updated)
            save_result(updated['folder'], updated)
            self._results[tcno] = updated
            # The job's item of this tender is what the page reads after a reload, so it shows the change too
            for item in items:
                if item['tcno'] == tcno and item['status'] == 'completed':
                    item.update(_summary(updated))
            log.info('Tender %s: %s by the user (now %s)', tcno, done, updated['verdict'])
            return copy.deepcopy(updated)

    def _carry_marks(self, tcno, result):
        """
        Marks again, on a new result, what the bid team marked by hand on the tender's last one: the statuses of
        criteria with the same name, and the unreadable sources it checked that are still unreadable. Returns
        True when anything was marked.
        """
        with self._lock:
            last = self._results.get(tcno) or {}
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
        for tender in tenders:
            closing = closing_date(tender)
            if closing:
                self._save_closing_date(str(tender.get('tcno')), closing.isoformat())

    def _save_closing_date(self, tcno, closing):
        """Saves closing (YYYY-MM-DD) in the tender's result when it has one with another date and is not busy."""
        with self._lock:
            result = self._results.get(tcno)
            if not result or result.get('closing_date') == closing or self._busy(tcno):
                return
            updated = {**result, 'closing_date': closing}
            try:
                save_result(updated['folder'], updated)
            except OSError:
                log.exception('Could not save the closing date of tender %s', tcno)
                return
            self._results[tcno] = updated

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
        with self._lock:
            results = {Path(result['folder']).name: {key: result.get(key) for key in ('tcno', 'name', 'closing_date')}
                       for result in self._results.values()}
        folders = []  # (tcno, saved result fields or None, documents)
        for folder in sorted(path for path in self.target_directory.iterdir() if path.is_dir()):
            result = results.get(folder.name)
            # A folder that is no tender's, such as a download's staging folder (TCNOs are numbers)
            if result is None and not folder.name.isdigit():
                continue
            tcno = str(result['tcno']) if result else folder.name
            files = tender_files(folder)
            if files and not self.busy(tcno):
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
        folder (the downloaded documents, the saved result and deep analysis) and its result, so it reads as never
        checked. The statuses the team marked by hand stay recorded in the company profile; the team's decision and
        the deep analysis's memory are the caller's (see routes.api.delete_downloads()). The namespace goes first, so
        a Pinecone failure leaves the rest for a retry.
        Returns {'tcno', 'files', 'bytes', 'namespace_deleted'}: how many documents went (the result and deep
        analysis not counted), their size, and whether there was a namespace. Raises LookupError when nothing is
        kept for the tender, TenderBusy while it is queued or being checked, VectorStoreError when Pinecone fails,
        and OSError when a file cannot be deleted (one open in another program, say).
        """
        tcno = str(tcno)
        folder = self.target_directory / safe_name(tcno)
        with self._lock:
            known = tcno in self._results
        if not known and not folder.is_dir():
            raise LookupError(f'Nothing is kept for tender {tcno}.')
        namespace_deleted = self.delete_namespace(tcno) if self.index.configured else False
        with self._lock:
            # Under the lock, so the tender cannot be queued and downloaded again while its folder goes
            if self._busy(tcno):
                raise TenderBusy()
            files = tender_files(folder) if folder.is_dir() else []
            size = sum(path.stat().st_size for path in files)
            if folder.is_dir():
                shutil.rmtree(folder)
            self._results.pop(tcno, None)
            # The last job's item of the tender is what the page reads after a reload, so it stops showing the verdict
            for item in self._job['items'] if self._job else []:
                if item['tcno'] == tcno:
                    item.update({**dict.fromkeys(SUMMARY_KEYS), 'status': 'deleted'})
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
        with self._lock:
            return self._busy(str(tcno))

    def _busy(self, tcno):
        """busy() for the caller holding the lock."""
        items = self._job['items'] if self._job else []
        return any(item['tcno'] == tcno and item['status'] in ('queued', 'processing') for item in items)

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
        be refused, and keep the block going. Re-checks against a Pinecone namespace go on. The caller holds the
        lock.
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
        Downloads, extracts, indexes and analyses one tender, or re-checks it against its Pinecone namespace when it
        has one and regenerate is false. Regenerating deletes the namespace first, so a failed download leaves none
        and the next check reads the documents again. A namespace left half-built by an earlier failure is rebuilt
        from the documents. Never raises: failures become a 'failed' result. Its 'tokens' are the LLM tokens spent,
        failed or not: see TokenUsage.counts().
        """
        tcno = str(tender['tcno'])
        folder = self.target_directory / safe_name(tcno)
        usage = TokenUsage()

        def set_stage(stage, step=None, steps=None):
            with self._lock:
                self._job['current'].update(stage=stage, step=step, steps=steps)

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
                analysis = self._analyse_documents(tender, folder, result, set_stage, usage)
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
            log.exception('Tender %s failed', tcno)
            title, action = explain_error(exc)
            result.update(status='failed', error=str(exc) or exc.__class__.__name__,
                          error_title=title, error_action=action)
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

    def _analyse_documents(self, tender, folder, result, set_stage, usage):
        """
        Downloads the tender's documents into folder (emptied first), indexes them in the tender's Pinecone
        namespace (rebuilt) and analyses them, counting the tokens in usage; sets result['files'].
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
                                    workers=self.workers)


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


def _average(seconds):
    """The average of a list of durations, rounded, or None for an empty list."""
    return round(sum(seconds) / len(seconds), 1) if seconds else None


def _name(tender):
    """The tender's description on one line."""
    return ' '.join(str(tender.get('tendersbrief') or '').split())


def _now():
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
