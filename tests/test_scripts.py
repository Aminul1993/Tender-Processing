"""Tests of the scheduled scripts in scripts/, against the test database, a fake TenderTiger and a fake Pinecone."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from psycopg.types.json import Json

from config import Config
from tests.pinecone_fake import FakePinecone
from scripts.process_watchlist import process_watchlist
from scripts.remove_closed_tenders import remove_closed_tenders
from services import tender_service
from services.company_service import CompanyStore
from services.decision_service import DecisionStore
from services.job_state import JobState
from services.pinecone_service import Document, PineconeService
from services.processing_service import result_brief
from services.watchlist_service import load_list, save_rules
from tests.test_database import scratch_database
from tests.test_watchlist_service import RULES, FakeTenderTiger, tender
from utils.tendertiger import share_pacing

COMPANY = '# Company Profile\n\n| Item | Value |\n| --- | --- |\n| Company Name | ABC Ltd |'


def fake_pinecone():
    return PineconeService(api_key='test-key', client=FakePinecone(), dimension=64, chunk_size=400, chunk_overlap=50,
                           freshness_timeout=1, settle_seconds=0)


class ScriptTestCase(unittest.TestCase):
    """A scratch database and download folder, and the Config a script is run with."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = db = scratch_database(self)
        CompanyStore(db).save(COMPANY)

        class TestConfig(Config):
            TARGET_DIRECTORY = Path(tmp.name) / 'downloads'
            LOG_DIRECTORY = Path(tmp.name) / 'logs'
            DATABASE_URL, DATABASE_SCHEMA = db.url, db.schema

        self.config = TestConfig
        self.config.TARGET_DIRECTORY.mkdir()
        self.index = fake_pinecone()
        self.state = JobState(db)
        self.addCleanup(share_pacing, None)  # the process script paces TenderTiger's requests in this database
        tender_service._tenders.clear()
        self.addCleanup(tender_service._tenders.clear)


class ProcessWatchlistTest(ScriptTestCase):
    def setUp(self):
        super().setUp()
        save_rules(self.db, RULES)
        self.listed = [
            tender(1, 'Eastern Railway', 'West Bengal', closes=9, value='2000000'),
            tender(2, 'Eastern Railway', 'Gujarat', closes=3),  # not in the department's states
            tender(3, 'Eastern Railway', 'Bihar', closes=1, value='9000000'),
            tender(4, 'Indian Oil Corporation Limited', 'Multi State', closes=20),
            tender(7, 'Public Works Department', 'Orissa (Odisha)', closes=2),
        ]
        self.tendertiger = FakeTenderTiger(self.listed)
        self.checked = []
        for patch in (mock.patch('services.tender_service.tender_list', side_effect=self.tendertiger),
                      mock.patch('services.processing_service.download_tender_documents', side_effect=self.download),
                      mock.patch('services.processing_service.process_tender_files', side_effect=self.analyse),
                      mock.patch.dict(os.environ, {'GROQ_API_KEY': 'key', 'TENDERTIGER_EMAIL': 'bid@example.test',
                                                   'TENDERTIGER_PASSWORD': 'secret'})):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def download(tender, folder, **options):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'NIT.pdf').write_bytes(b'%PDF')
        return [folder / 'NIT.pdf']

    def analyse(self, folder, tcno=None, **options):
        self.checked.append(tcno)
        return {'verdict': 'Eligible', 'details': '**Overall verdict:** Eligible', 'score': 100, 'criteria': [],
                'unreadable_files': [], 'jv': None, 'comparison': []}

    def run_script(self, threads=1):
        with self.assertLogs('process_watchlist', 'INFO') as logs:
            status = process_watchlist(self.config, threads=threads, vector_store=self.index)
        return status, logs.output

    def test_the_watchlist_is_searched_and_its_tenders_never_checked_are_checked(self):
        # Tender 3 was checked before (and failed): it is left alone
        failed = {'tcno': '3', 'name': 'Old check', 'status': 'failed', 'verdict': None, 'error': 'Download failed'}
        self.state.put_result(failed, result_brief(failed))

        status, logs = self.run_script(threads=2)

        self.assertEqual(status, 0)
        # Searched as Search again does, and saved for the page
        self.assertEqual(sorted(t['tcno'] for t, _ in load_list(self.db)[0]), ['1', '3', '4', '7'])
        self.assertEqual(sorted(self.checked), ['1', '4', '7'])
        self.assertEqual({tcno: self.state.result(tcno)['status'] for tcno in ('1', '3', '4', '7')},
                         {'1': 'completed', '3': 'failed', '4': 'completed', '7': 'completed'})
        self.assertIn('INFO:process_watchlist:The watchlist holds 4 tenders, 3 of them never checked', logs)
        self.assertIn('INFO:process_watchlist:Checked 3 new tender(s), 0 failed', logs)

        # Run again: every tender has a result, so nothing is checked
        self.checked.clear()
        self.assertEqual(self.run_script()[0], 0)
        self.assertEqual(self.checked, [])

    def test_a_failed_search_checks_nothing(self):
        with mock.patch('services.tender_service.tender_list', side_effect=RuntimeError('firewall')), \
                self.assertLogs('services.watchlist_service', 'ERROR'), \
                self.assertLogs('process_watchlist', 'ERROR') as logs:
            status = process_watchlist(self.config, vector_store=self.index)
        self.assertEqual((status, self.checked), (1, []))
        self.assertIn('The watchlist search failed, so nothing is checked: firewall', logs.output[0])

    def test_incomplete_settings_search_nothing(self):
        with mock.patch.dict(os.environ, {'GROQ_API_KEY': ''}), \
                self.assertLogs('process_watchlist', 'ERROR') as logs:
            status = process_watchlist(self.config, vector_store=PineconeService(api_key=None))
        self.assertEqual((status, self.tendertiger.calls), (1, []))
        self.assertIn('Set GROQ_API_KEY, PINECONE_API_KEY in .env.', logs.output[0])


class RemoveClosedTendersTest(ScriptTestCase):
    TODAY = date(2026, 11, 20)

    def setUp(self):
        super().setUp()
        for tcno, days_closed in (('100', 10), ('200', 7), ('300', 6), ('400', None)):
            closing = self.TODAY - timedelta(days=days_closed) if days_closed is not None else None
            result = {'tcno': tcno, 'name': f'Tender {tcno}', 'status': 'completed', 'verdict': 'Eligible',
                      'closing_date': closing.isoformat() if closing else None}
            self.state.put_result(result, result_brief(result))
            folder = self.config.TARGET_DIRECTORY / tcno
            folder.mkdir()
            (folder / 'NIT.pdf').write_bytes(b'12345')
            self.index.rebuild_namespace(tcno, [Document('NIT.pdf', 'Turnover of Rs 10 crore', 'nit')])
            DecisionStore(self.db).mark(tcno, 'shortlisted')
            with self.db.connection() as connection:
                connection.execute('INSERT INTO deep_analyses (tcno, report) VALUES (%s, %s)',
                                   (tcno, Json({'tcno': tcno, 'status': 'completed'})))
        CompanyStore(self.db).mark('100', {'criterion': 'Net worth', 'requirement': '20 Cr'}, 'met', '2026-11-01')

    def kept(self):
        """The tenders with any record left: result, folder, namespace, decision or deep analysis."""
        with self.db.connection() as connection:
            reports = {row[0] for row in connection.execute('SELECT tcno FROM deep_analyses').fetchall()}
        return {tcno: (self.state.result(tcno) is not None, (self.config.TARGET_DIRECTORY / tcno).exists(),
                       self.index.has_namespace(tcno), tcno in DecisionStore(self.db).all(), tcno in reports)
                for tcno in ('100', '200', '300', '400')}

    def test_tenders_closed_seven_days_ago_or_more_are_removed(self):
        everything = (True, True, True, True, True)
        with self.assertLogs('remove_closed_tenders', 'INFO') as logs:
            self.assertEqual(remove_closed_tenders(self.config, dry_run=True, vector_store=self.index,
                                                   today=self.TODAY), 0)
        self.assertEqual(set(self.kept().values()), {everything})  # a dry run removes nothing
        self.assertIn('INFO:remove_closed_tenders:Tender 200: closed 13-Nov-2026, 7 days ago (Tender 200)',
                      logs.output)

        with self.assertLogs('remove_closed_tenders', 'INFO') as logs:
            self.assertEqual(remove_closed_tenders(self.config, vector_store=self.index, today=self.TODAY), 0)

        nothing = (False, False, False, False, False)
        self.assertEqual(self.kept(), {'100': nothing, '200': nothing, '300': everything, '400': everything})
        self.assertIn('INFO:remove_closed_tenders:Removed 2 tender(s), 0 could not be', logs.output)
        # The bid team's marks stay, for the tenders checked later
        self.assertEqual([row[3] for row in CompanyStore(self.db).marks()], ['100'])

    def test_without_pinecone_nothing_is_removed(self):
        with self.assertLogs('remove_closed_tenders', 'ERROR'):
            status = remove_closed_tenders(self.config, vector_store=PineconeService(api_key=None), today=self.TODAY)
        self.assertEqual(status, 1)
        self.assertTrue(all(all(records) for tcno, records in self.kept().items() if tcno != '400'))


if __name__ == '__main__':
    unittest.main()
