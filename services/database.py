"""
The app's PostgreSQL database (DATABASE_URL), shared by the web app and its worker processes (worker.py):

  job, job_items          the processing job and its tenders (services/job_state.py)
  results                 every tender's last result, and the brief of it the lists need (services/job_state.py)
  deep_analyses           every tender's last deep analysis report (services/deep_analysis_service.py)
  decisions               the team's shortlisted / dropped decisions (services/decision_service.py)
  watchlist_settings      the watchlist: the filters its refresh searches with and the least tender value
  watchlist_state_groups  names that stand for several states
  watchlist_departments   its departments, in claiming order; each the parent of its organisation name patterns
  watchlist_organisations (child) and of the exact names a refresh searches TenderTiger for
  watchlist_searches      (child) (services/watchlist_service.py)
  watchlist_tenders       the watchlist's tenders, as its last search kept them, each with its department
  company_profile_blocks  the company profile, one row per block in order: its title, headings, tables and text
  company_profile_rows    the rows of the profile's tables, each the child of its table's block
                          (services/company_service.py)
  app_state               small JSON documents by name: when the watchlist was last refreshed, and last searched
  criterion_marks         the statuses the bid team marked by hand on tenders' criteria (services/company_service.py)
  pacing                  when the next request to TenderTiger may go, whichever process sends it (Database.pace())
  texts, pages, vectors   the cache of what is slow or paid for to work out (services/cache_service.py)

The tables are made on first connect, in DATABASE_SCHEMA (made too). The database itself must exist. The downloaded
documents stay in TARGET_DIRECTORY. JSON is kept as json, not jsonb, so a result reads back with its keys in the
order they were written.
"""
import threading
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

TABLES = '''
CREATE TABLE IF NOT EXISTS job (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    job_id text NOT NULL, status text NOT NULL, started_at text NOT NULL, finished_at text,
    stop_requested boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS job_items (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, item json NOT NULL, payload json NOT NULL,
    queued boolean NOT NULL DEFAULT true, stage json, lease_worker text, lease_until timestamptz);
CREATE TABLE IF NOT EXISTS results (
    tcno text PRIMARY KEY, result json NOT NULL, brief json NOT NULL, saved_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS deep_analyses (
    tcno text PRIMARY KEY, report json NOT NULL, saved_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS company_profile_blocks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, position integer NOT NULL UNIQUE,
    type text NOT NULL CHECK (type IN ('heading', 'table', 'text', 'rule')),
    level smallint, text text, columns text[]);
CREATE TABLE IF NOT EXISTS company_profile_rows (
    block_id bigint NOT NULL REFERENCES company_profile_blocks (id) ON DELETE CASCADE, position integer NOT NULL,
    cells text[] NOT NULL, PRIMARY KEY (block_id, position));
CREATE TABLE IF NOT EXISTS criterion_marks (
    tcno text, criterion_key text, criterion text NOT NULL, requirement text NOT NULL, status text NOT NULL,
    marked_on text NOT NULL, seq bigint GENERATED ALWAYS AS IDENTITY, PRIMARY KEY (tcno, criterion_key));
CREATE TABLE IF NOT EXISTS decisions (tcno text PRIMARY KEY, decision text NOT NULL, decided_at text NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist_settings (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row), about text, filters json NOT NULL DEFAULT '{}',
    min_value_cr double precision);
CREATE TABLE IF NOT EXISTS watchlist_state_groups (name text PRIMARY KEY, states text[] NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist_departments (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, position integer NOT NULL UNIQUE, sl integer, sector text,
    name text NOT NULL, full_name text, states text[], brief text, min_value_cr double precision);
CREATE TABLE IF NOT EXISTS watchlist_organisations (
    department_id bigint NOT NULL REFERENCES watchlist_departments (id) ON DELETE CASCADE, position integer NOT NULL,
    pattern text NOT NULL, PRIMARY KEY (department_id, position));
CREATE TABLE IF NOT EXISTS watchlist_searches (
    department_id bigint NOT NULL REFERENCES watchlist_departments (id) ON DELETE CASCADE, position integer NOT NULL,
    name text NOT NULL, PRIMARY KEY (department_id, position));
CREATE TABLE IF NOT EXISTS watchlist_tenders (tcno text PRIMARY KEY, tender json NOT NULL, department json);
ALTER TABLE watchlist_tenders ADD COLUMN IF NOT EXISTS department json;
CREATE TABLE IF NOT EXISTS app_state (
    name text PRIMARY KEY, value json NOT NULL, saved_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS pacing (name text PRIMARY KEY, next_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS texts (sha256 text, reader text, text text NOT NULL, PRIMARY KEY (sha256, reader));
CREATE TABLE IF NOT EXISTS pages (sha256 text, reader text, text text NOT NULL, PRIMARY KEY (sha256, reader));
CREATE TABLE IF NOT EXISTS vectors (model text, sha256 text, vector bytea NOT NULL, PRIMARY KEY (model, sha256));
'''
# The most connections a process keeps open; each is held for one call, or for the milliseconds of the job lock
POOL_SIZE = 10
# An advisory lock, so two processes starting at once do not both make the tables
_TABLES_LOCK = 7_260_001

_shared = {}
_shared_lock = threading.Lock()


class DatabaseError(Exception):
    """The database cannot be reached or set up."""


class Database:
    """A pool of connections to the database at url, its tables in schema. Thread-safe."""

    def __init__(self, url, schema='public', pool_size=POOL_SIZE):
        self.url, self.schema = url, schema
        options = {'options': f'-c search_path={schema}'}
        try:
            # Connected once on its own first, so a wrong password or a missing database says so (the pool would only
            # time out)
            with psycopg.connect(url, connect_timeout=10, **options) as connection:
                connection.execute('SELECT pg_advisory_xact_lock(%s)', (_TABLES_LOCK,))
                connection.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
                connection.execute(TABLES)
        except psycopg.Error as exc:
            raise DatabaseError(f'Could not open the database of DATABASE_URL: {exc}') from None
        self.pool = ConnectionPool(url, min_size=1, max_size=pool_size, kwargs=options, open=True,
                                   check=ConnectionPool.check_connection, name=f'tender-processing:{schema}')
        self._held = threading.local()

    @classmethod
    def connect(cls, url, schema='public'):
        """This process's Database of url and schema: made on first use, then shared."""
        if not url:
            raise DatabaseError('DATABASE_URL is not set: give the PostgreSQL database the app keeps its data in, '
                                'such as postgresql://postgres:<password>@localhost:5433/tender_processing')
        with _shared_lock:
            database = _shared.get((url, schema))
            if database is None:
                database = _shared[(url, schema)] = cls(url, schema)
            return database

    def close(self):
        with _shared_lock:
            if _shared.get((self.url, self.schema)) is self:
                del _shared[(self.url, self.schema)]
        self.pool.close()

    @contextmanager
    def connection(self):
        """A connection in a transaction, committed on leaving and rolled back on an exception; inside transaction(),
        the one it holds."""
        held = getattr(self._held, 'connection', None)
        if held is not None:
            yield held
            return
        with self.pool.connection() as connection:
            yield connection

    @contextmanager
    def transaction(self):
        """One transaction for this thread: every connection() inside it is the same, committed together."""
        if getattr(self._held, 'connection', None) is not None:
            yield self._held.connection
            return
        with self.pool.connection() as connection:
            self._held.connection = connection
            try:
                yield connection
            finally:
                self._held.connection = None

    def pace(self, name, seconds):
        """
        Takes the next turn of name, the turns of every process using this database at least seconds apart, by the
        database's clock: returns the seconds to wait before it (0 when it is now). Concurrent callers queue on the row,
        so each gets a turn of its own.
        """
        with self.connection() as connection:
            wait = connection.execute(
                'INSERT INTO pacing (name, next_at) VALUES (%(name)s, clock_timestamp() + make_interval(secs => %(gap)s)) '
                'ON CONFLICT (name) DO UPDATE SET '
                'next_at = GREATEST(pacing.next_at, clock_timestamp()) + make_interval(secs => %(gap)s) '
                'RETURNING extract(epoch FROM next_at - clock_timestamp())::float8 - %(gap)s',
                {'name': name, 'gap': float(seconds)}).fetchone()[0]
        return max(0.0, wait)

    def value(self, name):
        """The JSON document saved under name in app_state, or None."""
        with self.connection() as connection:
            row = connection.execute('SELECT value FROM app_state WHERE name = %s', (name,)).fetchone()
        return row[0] if row else None

    def set_value(self, name, value):
        with self.connection() as connection:
            connection.execute('INSERT INTO app_state (name, value) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE '
                               'SET value = excluded.value, saved_at = now()', (name, Json(value)))
