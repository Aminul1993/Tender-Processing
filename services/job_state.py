"""
The processing job's state in PostgreSQL (services/database.py), shared by the web app and any number of worker
processes (worker.py), so several workers can process the tenders of one job at the same time, and the job survives
a restart.

Tables:
  job        the one job: job_id, status (running / completed), started_at, finished_at, stop_requested
  job_items  the job's items, in the order they were queued (by id, which a new job does not start again): the item
             ({'tcno', 'name', 'status', 'rechecked', ...SUMMARY_KEYS}; its 'id' is the row's), the payload a worker
             processes it from ({'tender', 'regenerate', 'approve_ocr'}), whether it waits to be taken (queued), the
             stage of one being processed ({'stage', 'step', 'steps', 'started'}) and its lease: until when
             (lease_until, LEASE_SECONDS after the worker processing it last renewed it). A lease run out, the worker
             is taken as dead and the item failed (see TenderProcessor.progress())
  results    TCNO -> the tender's last full result, and its brief: the fields of it the tender table and the run
             estimates need

Every change is made under lock(): one transaction holding an advisory lock, for milliseconds, never while a tender is
processed. Reads are not, and see each table as of one moment.
"""
import time
from contextlib import contextmanager

from psycopg import errors, sql
from psycopg.types.json import Json

LEASE_SECONDS = 90
# The job lock is waited for up to LOCK_WAIT_SECONDS; it goes with its holder's connection, should the holder die
LOCK_WAIT_SECONDS = 120
_JOB_LOCK = 7_260_002  # the job lock's advisory lock key
# How often an idle worker looks for a tender queued
POLL_SECONDS = 0.5


class JobState:
    """The job, its items and the tenders' results, in a services.database.Database."""

    def __init__(self, db):
        self.db = db

    @contextmanager
    def lock(self):
        """
        The lock to change the state under (see the module docstring), and the transaction the changes are made in:
        all of them, or none on an exception. Raises TimeoutError when it cannot be taken within LOCK_WAIT_SECONDS.
        """
        with self.db.transaction() as connection:
            connection.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT_SECONDS}s'")
            try:
                connection.execute('SELECT pg_advisory_xact_lock(%s)', (_JOB_LOCK,))
            except errors.LockNotAvailable:
                raise TimeoutError(f'Could not take the job lock in {LOCK_WAIT_SECONDS} seconds') from None
            yield

    def _all(self, query, params=()):
        with self.db.connection() as connection:
            return connection.execute(query, params).fetchall()

    def _one(self, query, params=()):
        rows = self._all(query, params)
        return rows[0] if rows else None

    def _run(self, query, params=()):
        with self.db.connection() as connection:
            connection.execute(query, params)

    # ---------- the job ----------

    def job(self):
        """The job as {'job_id', 'status', 'started_at', 'finished_at', 'stop_requested'}, or None before any."""
        row = self._one('SELECT job_id, status, started_at, finished_at, stop_requested FROM job')
        if row is None:
            return None
        return dict(zip(('job_id', 'status', 'started_at', 'finished_at', 'stop_requested'), row))

    def new_job(self, job_id, started_at):
        """Starts job_id, empty, in place of the last job."""
        with self.db.connection() as connection:
            connection.execute('DELETE FROM job_items')
            connection.execute('DELETE FROM job')
            connection.execute("INSERT INTO job (job_id, status, started_at) VALUES (%s, 'running', %s)",
                               (job_id, started_at))

    def clear_job(self):
        """Forgets the job, as if none had run; the results stay."""
        with self.db.connection() as connection:
            connection.execute('DELETE FROM job_items')
            connection.execute('DELETE FROM job')

    def update_job(self, **fields):
        """Sets fields of the job: status, finished_at, stop_requested."""
        assignments = sql.SQL(', ').join(sql.SQL('{} = %s').format(sql.Identifier(name)) for name in fields)
        self._run(sql.SQL('UPDATE job SET {}').format(assignments), list(fields.values()))

    # ---------- items ----------

    def items(self):
        """The job's items, in order."""
        return [{**item, 'id': str(item_id)} for item_id, item in self._all('SELECT id, item FROM job_items ORDER BY id')]

    def item(self, item_id):
        row = self._one('SELECT item FROM job_items WHERE id = %s', (int(item_id),))
        return {**row[0], 'id': str(item_id)} if row else None

    def put_item(self, item):
        self._run('UPDATE job_items SET item = %s WHERE id = %s',
                  (Json({key: value for key, value in item.items() if key != 'id'}), int(item['id'])))

    def add_item(self, item, payload):
        """Adds item to the job and queues it, with the payload a worker processes it from; returns its ID."""
        row = self._one('INSERT INTO job_items (item, payload) VALUES (%s, %s) RETURNING id',
                        (Json({key: value for key, value in item.items() if key != 'id'}), Json(payload)))
        return str(row[0])

    def payload(self, item_id):
        row = self._one('SELECT payload FROM job_items WHERE id = %s', (int(item_id),))
        return row[0] if row else None

    def next_queued(self):
        """Takes the ID of the oldest item waiting off the queue, or None."""
        row = self._one('UPDATE job_items SET queued = false WHERE id = (SELECT id FROM job_items WHERE queued '
                        'ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id')
        return str(row[0]) if row else None

    def unqueue(self, item_ids):
        """Takes these items off the queue (cancelled)."""
        self._run('UPDATE job_items SET queued = false WHERE id = ANY(%s)', ([int(i) for i in item_ids],))

    def remove_items(self, item_ids):
        """Takes these items out of the job altogether, with their payloads, stages and leases."""
        self._run('DELETE FROM job_items WHERE id = ANY(%s)', ([int(i) for i in item_ids],))

    def wait(self, timeout):
        """Returns once an item is queued, or after timeout seconds."""
        deadline = time.monotonic() + timeout
        while not self._one('SELECT EXISTS (SELECT 1 FROM job_items WHERE queued)')[0]:
            if time.monotonic() >= deadline:
                return
            time.sleep(POLL_SECONDS)

    # ---------- items being processed ----------

    def set_stage(self, item_id, stage, step=None, steps=None, started=None):
        self._run('UPDATE job_items SET stage = %s WHERE id = %s',
                  (Json({'stage': stage, 'step': step, 'steps': steps, 'started': started or time.time()}),
                   int(item_id)))

    def stages(self):
        return {str(item_id): stage
                for item_id, stage in self._all('SELECT id, stage FROM job_items WHERE stage IS NOT NULL')}

    def renew(self, item_id, worker):
        """Keeps the item leased to worker for LEASE_SECONDS more."""
        self._run('UPDATE job_items SET lease_worker = %s, lease_until = now() + make_interval(secs => %s) '
                  'WHERE id = %s', (worker, LEASE_SECONDS, int(item_id)))

    def leased(self, item_ids):
        """Those of item_ids whose lease is alive."""
        rows = self._all('SELECT id FROM job_items WHERE id = ANY(%s) AND lease_until > now()',
                         ([int(i) for i in item_ids],))
        return {str(item_id) for item_id, in rows}

    def release(self, item_id):
        """Forgets the stage and lease of an item no longer processed."""
        self._run('UPDATE job_items SET stage = NULL, lease_worker = NULL, lease_until = NULL WHERE id = %s',
                  (int(item_id),))

    # ---------- results ----------

    def result(self, tcno):
        row = self._one('SELECT result FROM results WHERE tcno = %s', (str(tcno),))
        return row[0] if row else None

    def results(self):
        return [result for result, in self._all('SELECT result FROM results ORDER BY tcno')]

    def briefs(self):
        """{TCNO: brief} of every result (see put_result())."""
        return dict(self._all('SELECT tcno, brief FROM results ORDER BY tcno'))

    def put_result(self, result, brief):
        self._run('INSERT INTO results (tcno, result, brief) VALUES (%s, %s, %s) ON CONFLICT (tcno) DO UPDATE '
                  'SET result = excluded.result, brief = excluded.brief, saved_at = now()',
                  (str(result['tcno']), Json(result), Json(brief)))

    def delete_result(self, tcno):
        self._run('DELETE FROM results WHERE tcno = %s', (str(tcno),))
