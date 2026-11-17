"""
Tests of the database's job state, pacing and company profile. Also gives every test that needs the database one of
its own: scratch_database().

The tests use TEST_DATABASE_URL, or DATABASE_URL's server with the database <name>_test (made if missing), never the
app's own database. Each test gets a schema of its own there, dropped once it is done.
"""
import os
import threading
import time
import unittest
import uuid

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

load_dotenv()

from services.company_service import CompanyStore  # noqa: E402
from services.database import Database  # noqa: E402
from services.job_state import JobState  # noqa: E402

_test_url = None


def database_url_for_tests():
    """The test database's URL, the database made on first use."""
    global _test_url
    if _test_url is None:
        url = os.getenv('TEST_DATABASE_URL')
        if not url:
            app_url = os.getenv('DATABASE_URL')
            if not app_url:
                raise RuntimeError('Set TEST_DATABASE_URL (or DATABASE_URL) to a PostgreSQL server to test against')
            url = make_conninfo(app_url, dbname=f"{conninfo_to_dict(app_url).get('dbname') or 'postgres'}_test")
        name = conninfo_to_dict(url)['dbname']
        with psycopg.connect(make_conninfo(url, dbname='postgres'), autocommit=True) as connection:
            if not connection.execute('SELECT 1 FROM pg_database WHERE datname = %s', (name,)).fetchone():
                connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        _test_url = url
    return _test_url




def scratch_database(test):
    """A Database in a schema of the test database of its own, dropped once test (a TestCase) is done. An app the test
    makes with DATABASE_URL = db.url and DATABASE_SCHEMA = db.schema shares it."""
    url, schema = database_url_for_tests(), f'test_{uuid.uuid4().hex[:12]}'
    db = Database.connect(url, schema)

    def drop():
        db.close()
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))

    test.addCleanup(drop)
    return db


class JobStateTest(unittest.TestCase):
    def setUp(self):
        self.state = JobState(scratch_database(self))

    def test_a_wait_ends_once_a_tender_is_queued_or_after_its_timeout(self):
        started = time.monotonic()
        self.state.wait(0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

        self.state.add_item({'tcno': '1', 'status': 'queued'}, {})
        started = time.monotonic()
        self.state.wait(5)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_items_are_taken_in_order_and_a_lease_runs_out(self):
        self.state.new_job('job-1', '2026-11-16T10:00:00+00:00')
        first = self.state.add_item({'tcno': '1', 'status': 'queued'}, {'tender': {'tcno': '1'}})
        second = self.state.add_item({'tcno': '2', 'status': 'queued'}, {})
        self.state.unqueue([second])
        self.assertEqual((self.state.next_queued(), self.state.next_queued()), (first, None))
        self.assertEqual(self.state.payload(first), {'tender': {'tcno': '1'}})

        self.state.renew(first, 'host:1')
        self.assertEqual(self.state.leased([first, second]), {first})
        with self.state.db.connection() as connection:  # as if its worker stopped 90 seconds ago
            connection.execute("UPDATE job_items SET lease_until = now() - interval '1 second'")
        self.assertEqual(self.state.leased([first]), set())

        self.state.update_job(status='completed', finished_at='2026-11-16T10:05:00+00:00', stop_requested=True)
        self.assertEqual(self.state.job(), {'job_id': 'job-1', 'status': 'completed',
                                            'started_at': '2026-11-16T10:00:00+00:00',
                                            'finished_at': '2026-11-16T10:05:00+00:00', 'stop_requested': True})

    def test_a_change_under_the_lock_is_all_or_nothing(self):
        self.state.put_result({'tcno': '1', 'verdict': 'Eligible'}, {'tcno': '1'})
        with self.assertRaises(LookupError), self.state.lock():
            self.state.put_result({'tcno': '1', 'verdict': 'Not Eligible'}, {'tcno': '1'})
            raise LookupError('the change failed half-way')
        self.assertEqual(self.state.result('1'), {'tcno': '1', 'verdict': 'Eligible'})


class PacingTest(unittest.TestCase):
    def test_turns_are_spaced_out_across_processes(self):
        db = scratch_database(self)
        other = Database(db.url, db.schema)  # another process's connections to the same database
        self.addCleanup(other.pool.close)
        turns, lock = [], threading.Lock()

        def take_turn(database):
            wait = database.pace('tendertiger', 0.5)
            with lock:
                turns.append(time.monotonic() + wait)  # when its turn comes

        threads = [threading.Thread(target=take_turn, args=(database,)) for database in (db, other, db, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Asked for at once, each is given a turn of its own, half a second after the one before
        turns.sort()
        for before, after in zip(turns, turns[1:]):
            self.assertAlmostEqual(after - before, 0.5, delta=0.05)
        # Once the last turn is past, the next is now
        with db.connection() as connection:
            connection.execute("UPDATE pacing SET next_at = now() - interval '1 second'")
        self.assertEqual(other.pace('tendertiger', 0.5), 0)


class CompanyStoreTest(unittest.TestCase):
    def test_marks_are_kept_apart_from_the_profile_and_given_to_the_other_tenders(self):
        store = CompanyStore(scratch_database(self))
        store.save('# ACME')
        net_worth = {'criterion': 'Net worth', 'requirement': '20 Cr'}
        store.mark('100', net_worth, 'met', '2026-11-11')
        store.mark('200', {'criterion': 'GST', 'requirement': ''}, 'unknown', '2026-11-11')
        store.mark('100', {'criterion': 'Net Worth', 'requirement': '25 Cr'}, 'not_met', '2026-11-12')  # again

        self.assertEqual(store.marks(), [['Net Worth', '25 Cr', 'Not met', '100', '2026-11-12'],
                                         ['GST', '', 'Cannot determine', '200', '2026-11-11']])
        self.assertEqual(store.read(), '# ACME')
        # A tender is given the others' marks, never its own
        self.assertIn('| GST |  | Cannot determine | 200 | 2026-11-11 |', store.for_tender('100'))
        self.assertNotIn('| 100 |', store.for_tender('100'))
        store.mark('200', {'criterion': 'GST'}, None, '2026-11-12')
        self.assertEqual(store.for_tender('100'), '# ACME')

    def test_the_profile_is_kept_as_blocks_with_their_table_rows_as_children(self):
        db = scratch_database(self)
        store = CompanyStore(db)
        self.assertEqual(store.read(), '')
        profile = ('# ACME {Ltd}\n\n## 1. Finance\n\n| Year | Turnover |\n| --- | --- |\n| FY25 | ₹10 Cr |\n'
                   '| FY26 | ₹12 \\| 13 Cr |\n\nAudited.\n\n---\n\n## 2. Staff\n\n| Item | Value |\n| --- | --- |')
        store.save('\n' + profile + '\n\n')

        with db.connection() as connection:
            blocks = connection.execute('SELECT position, type, level, text, columns FROM company_profile_blocks '
                                        'ORDER BY position').fetchall()
            rows = connection.execute('SELECT b.position, r.position, r.cells FROM company_profile_rows r '
                                      'JOIN company_profile_blocks b ON b.id = r.block_id ORDER BY 1, 2').fetchall()
        self.assertEqual(blocks, [(0, 'heading', 1, 'ACME {Ltd}', None), (1, 'heading', 2, '1. Finance', None),
                                  (2, 'table', None, None, ['Year', 'Turnover']), (3, 'text', None, 'Audited.', None),
                                  (4, 'rule', None, None, None), (5, 'heading', 2, '2. Staff', None),
                                  (6, 'table', None, None, ['Item', 'Value'])])
        self.assertEqual(rows, [(2, 0, ['FY25', '₹10 Cr']), (2, 1, ['FY26', '₹12 | 13 Cr'])])
        self.assertEqual(store.read(), profile)

        # Saved again, the blocks and their rows are replaced
        store.save('# ACME')
        self.assertEqual(store.blocks(), [{'type': 'heading', 'level': 1, 'text': 'ACME'}])
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM company_profile_rows').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
