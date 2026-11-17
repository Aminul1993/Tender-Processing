"""Tests of the watchlist (services/watchlist_service.py and its API), with TenderTiger faked."""
import json
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import psycopg
from fastapi.testclient import TestClient

from app import create_app
from config import Config
from services import tender_service, watchlist_service
from services.company_service import CompanyStore
from services.watchlist_service import (Watchlist, WatchlistError, WatchlistMissing, WatchlistRunner, read_rules,
                                        save_rules)
from tests.test_database import scratch_database

TODAY = date.today()


def day(offset):
    """A closing date as the list writes it, offset days from today."""
    return f'{TODAY + timedelta(days=offset):%d-%m-%Y}'


def tender(tcno, org, state, closes=5, brief='Civil work', value=None):
    return {'tcno': str(tcno), 'companyname': org, 'statename': state, 'tendersbrief': brief,
            'closingdate': day(closes), 'tendervalue': value, 'isdocument': '1', 'tenderprocid': f'p{tcno}'}


RULES = {
    'filters': {'closing_from': 'today', 'bid_doc': True},
    'state_groups': {'East': ['West Bengal', 'Odisha', 'Bihar']},
    'departments': [
        {'sl': 1, 'name': 'ER', 'full_name': 'Eastern Railway', 'sector': 'Railway Zonal',
         'organisations': ['^eastern railways?\\b'], 'search': ['eastern railway'], 'states': ['West Bengal', 'Bihar']},
        {'sl': 2, 'name': 'IOCL', 'organisations': ['^indian oil corporation'],
         'search': ['indian oil corporation limited'], 'states': 'any'},
        {'sl': 3, 'name': 'NHAI (O&M)', 'organisations': ['^national highways? authority of india'],
         'search': ['national highway authority of india'], 'states': ['East'], 'brief': '\\bo\\s?&\\s?m\\b|maintenance'},
        {'sl': 4, 'name': 'NHAI', 'organisations': ['^national highways? authority of india'],
         'search': ['national highway authority of india'], 'states': ['Odisha', 'Bihar']},
        {'sl': 5, 'name': 'PWD Odisha', 'organisations': ['^(?!central\\b).*\\bpublic works\\b'],
         'search': ['public works department'], 'states': ['Odisha']},
    ],
}


class FakeTenderTiger:
    """tender_list() over a few organisations' tenders: an exact organisation, closing from a date, sorted by closing
    date, served at most `cap` at a time; past the end it answers with another search's tenders, as TenderTiger does."""

    def __init__(self, tenders, cap=10_000):
        self.tenders, self.cap, self.calls = tenders, cap, []

    def __call__(self, fields, page=1, sort=None, page_size=20):
        self.calls.append((fields.get('worgname'), fields.get('wtenderclosingdate'), page))
        start = fields['wtenderclosingdate'].split("'")[0]
        found = sorted((t for t in self.tenders if t['companyname'].lower() == fields['worgname'].lower()
                        and tender_service.closing_date(t).isoformat() >= start),
                       key=tender_service.closing_date)
        offset = (page - 1) * page_size
        if offset >= min(len(found), self.cap):
            return {'TenderList': [tender(999, 'national highways', None)], 'categoryflag': '2'}
        return {'TenderList': found[offset:min(offset + page_size, self.cap)], 'categoryflag': '1'}


class WatchlistRulesTest(unittest.TestCase):
    def load(self, rules):
        return Watchlist.parse(rules)

    def test_the_rules_are_kept_in_departments_with_their_names_as_children(self):
        db = scratch_database(self)
        with self.assertRaisesRegex(WatchlistMissing, 'add its departments with Edit watchlist'):
            Watchlist.load(db)
        rules = {**RULES, 'about': 'From the sheet', 'min_value_cr': 25,
                 'departments': [{**RULES['departments'][0], 'min_value_cr': 2}, *RULES['departments'][1:]]}
        save_rules(db, rules)

        self.assertEqual(read_rules(db), rules)
        with db.connection() as connection:
            self.assertEqual(connection.execute(
                'SELECT d.position, d.name, d.states, o.pattern FROM watchlist_departments d JOIN '
                'watchlist_organisations o ON o.department_id = d.id ORDER BY d.position LIMIT 2').fetchall(),
                [(1, 'ER', ['West Bengal', 'Bihar'], '^eastern railways?\\b'),
                 (2, 'IOCL', None, '^indian oil corporation')])  # NULL states: any state
        watchlist = Watchlist.load(db)
        self.assertEqual([d.name for d in watchlist.departments], ['ER', 'IOCL', 'NHAI (O&M)', 'NHAI', 'PWD Odisha'])
        # Saved again, the departments and their children are replaced
        save_rules(db, RULES)
        self.assertEqual(read_rules(db), RULES)
        self.assertNotEqual(Watchlist.load(db).fingerprint, watchlist.fingerprint)

    def test_a_department_claims_by_organisation_state_and_description(self):
        watchlist = self.load(RULES)
        claim = lambda org, state, brief='Civil work': getattr(  # noqa: E731
            watchlist.department({'companyname': org, 'statename': state, 'tendersbrief': brief}), 'name', None)
        self.assertEqual(claim('Eastern  Railway', 'west bengal'), 'ER')  # any case, any spacing
        self.assertIsNone(claim('South Eastern Railway', 'West Bengal'))
        self.assertIsNone(claim('Eastern Railway', 'Multi State'))
        self.assertIsNone(claim('Eastern Railway', 'Not Classified'))
        self.assertEqual(claim('Indian Oil Corporation Limited', 'Multi State'), 'IOCL')
        # O&M work only in its states; any work in the second row's
        self.assertEqual(claim('National Highway Authority of India', 'West Bengal', 'O & M of NH-12'), 'NHAI (O&M)')
        self.assertIsNone(claim('National Highway Authority of India', 'West Bengal', 'Four laning of NH-12'))
        self.assertEqual(claim('National Highway Authority of India', 'Bihar', 'Four laning of NH-31'), 'NHAI')
        self.assertEqual(claim('Public Works Department', 'Orissa (Odisha)'), 'PWD Odisha')
        self.assertIsNone(claim('Central Public Works Department', 'Orissa (Odisha)'))
        self.assertEqual(watchlist.search_names(), ['eastern railway', 'indian oil corporation limited',
                                                    'national highway authority of india', 'public works department'])

    def test_a_department_claims_only_tenders_at_or_above_its_minimum_value(self):
        watchlist = self.load({**RULES, 'min_value_cr': 25, 'departments': [
            {**RULES['departments'][0], 'min_value_cr': 2}, *RULES['departments'][1:]]})
        claim = lambda org, state, value, currency='inr': getattr(watchlist.department(  # noqa: E731
            {'companyname': org, 'statename': state, 'tendersbrief': 'Civil work', 'tendervalue': value,
             'currcode': currency}), 'name', None)
        self.assertEqual(claim('Eastern Railway', 'Bihar', '20000000'), 'ER')       # 2 Cr: its own minimum
        self.assertIsNone(claim('Eastern Railway', 'Bihar', '19999999.99'))
        self.assertEqual(claim('Indian Oil Corporation Limited', 'Kerala', '250000000'), 'IOCL')  # the file's 25 Cr
        self.assertIsNone(claim('Indian Oil Corporation Limited', 'Kerala', '249000000'))
        # No value given (TenderTiger sends 0), or one in another currency: not tracked under a minimum
        self.assertIsNone(claim('Indian Oil Corporation Limited', 'Kerala', '0'))
        self.assertIsNone(claim('Indian Oil Corporation Limited', 'Kerala', '2500000000', 'usd'))
        # Without a minimum, any value, or none
        self.assertEqual(self.load(RULES).department({'companyname': 'Eastern Railway', 'statename': 'Bihar',
                                                      'tendervalue': '0'}).name, 'ER')
        with self.assertRaisesRegex(WatchlistError, 'department 1 \\(ER\\), min_value_cr: give the least tender value'):
            self.load({**RULES, 'departments': [{**RULES['departments'][0], 'min_value_cr': '2 Cr'}]})

    def test_a_broken_watchlist_says_what_is_wrong(self):
        department = RULES['departments'][0]
        for change, problem in [
            ({'states': ['West Bengali']}, "'West Bengali' is neither a state nor a state group"),
            ({'organisations': ['^eastern (railway']}, 'is not a valid regular expression'),
            ({'search': ['south eastern railway']}, "the search name 'south eastern railway' matches none"),
            ({'search': ["eastern railway's office"]}, 'leave the apostrophe out of the search name'),
            ({'states': []}, 'must be "any" or a list'),
            ({'name': ''}, 'with a "name"'),
        ]:
            with self.assertRaises(WatchlistError) as raised:
                self.load({**RULES, 'departments': [{**department, **change}]})
            self.assertIn(problem, str(raised.exception))
            self.assertIn('the watchlist, department 1', str(raised.exception))
        with self.assertRaisesRegex(WatchlistError, 'the watchlist, filters, closing_from'):
            self.load({**RULES, 'filters': {'closing_from': 'soon'}})
        with self.assertRaisesRegex(WatchlistError, "Leave the apostrophe out of \"collector's office\""):
            self.load({**RULES, 'filters': {'word': "collector's office"}})


class WatchlistRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = scratch_database(self)
        save_rules(self.db, RULES)
        tender_service._tenders.clear()
        self.addCleanup(tender_service._tenders.clear)
        self.listed = [
            tender(1, 'Eastern Railway', 'West Bengal', closes=9, value='2000000'),
            tender(2, 'Eastern Railway', 'Gujarat', closes=3),  # not in the department's states
            tender(3, 'Eastern Railway', 'Bihar', closes=1, value='9000000'),
            tender(4, 'Indian Oil Corporation Limited', 'Multi State', closes=20),
            tender(5, 'National Highway Authority of India', 'West Bengal', brief='O&M of NH-12', closes=4),
            tender(6, 'National Highway Authority of India', 'West Bengal', brief='New bridge', closes=4),
            tender(7, 'Public Works Department', 'Orissa (Odisha)', closes=2),
            tender(8, 'Public Works Department', 'Uttar Pradesh', closes=2),
        ]
        self.tendertiger = FakeTenderTiger(self.listed)
        patch = mock.patch('services.tender_service.tender_list', side_effect=self.tendertiger)
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self):
        return WatchlistRunner(self.db)

    def refresh(self, runner):
        runner.start()
        deadline = time.monotonic() + 5
        while runner.status()['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.01)
        return runner.status()

    def test_a_refresh_searches_each_organisation_and_keeps_what_departments_claim(self):
        runner = self.runner()
        self.assertEqual(runner.status()['status'], 'idle')
        status = self.refresh(runner)

        self.assertEqual((status['status'], status['searched'], status['searches'], status['tenders']),
                         ('completed', 4, 4, 5))
        self.assertEqual(status['departments'], 4)
        # One search per organisation, with the watchlist's filters, by exact name
        self.assertEqual([call[0] for call in self.tendertiger.calls],
                         ['eastern railway', 'indian oil corporation limited', 'national highway authority of india',
                          'public works department'])
        self.assertEqual(self.tendertiger.calls[0][1], f"{TODAY:%Y-%m-%d}' and '2099-12-31")
        tenders, total = runner.page(page_size=20)
        self.assertEqual(([t['tcno'] for t in tenders], total), (['3', '7', '5', '1', '4'], 5))
        # Listed, so they can be processed
        self.assertIsNotNone(tender_service._tenders.get('7'))
        self.assertEqual(runner.departments(tenders)[0]['name'], 'ER')

        # Saved: a restart has them, without asking TenderTiger
        self.tendertiger.calls.clear()
        tender_service._tenders.clear()
        restarted = self.runner()
        self.assertEqual(restarted.page(page_size=20)[1], 5)
        self.assertIn('7', tender_service._tenders)
        self.assertEqual(self.tendertiger.calls, [])

    def test_the_list_is_the_saved_one_sorted_and_paged(self):
        runner = self.runner()
        self.refresh(runner)
        tenders, total = runner.page(page=1, page_size=20, sort='value', order='desc')
        self.assertEqual([t['tcno'] for t in tenders], ['3', '1', '7', '5', '4'])  # those without a value last
        # As saved: one that has closed since the search stays until the next
        with mock.patch.object(watchlist_service, 'date') as fake_date:
            fake_date.today.return_value = TODAY + timedelta(days=3)
            self.assertEqual(runner.page(page_size=20)[1], 5)
        with self.assertRaisesRegex(ValueError, 'Show 20, 50, 100 or 500'):
            runner.page(page_size=30)

    def test_by_value_tenders_with_no_value_or_another_currency_come_last(self):
        runner = self.runner()
        er = {'sl': 1, 'name': 'ER'}
        rows = [(dict(tender(1, 'Eastern Railway', 'Bihar', closes=1, value='0')), er),  # TenderTiger's "no value"
                (dict(tender(2, 'Eastern Railway', 'Bihar', closes=2, value='50000000'), currcode='usd'), er),
                (dict(tender(3, 'Eastern Railway', 'Bihar', closes=3, value='20000000'), currcode='inr'), er),
                (dict(tender(4, 'Eastern Railway', 'Bihar', closes=4, value='9000000')), er)]
        watchlist_service.save_list(self.db, rows, '2026-11-17T06:00:00+00:00')

        for order, expected in (('asc', ['4', '3', '1', '2']), ('desc', ['3', '4', '1', '2'])):
            tenders, _ = runner.page(page_size=20, sort='value', order=order)
            self.assertEqual([t['tcno'] for t in tenders], expected, order)

    def test_an_edited_watchlist_applies_at_the_next_search(self):
        runner = self.runner()
        self.refresh(runner)
        rules = json.loads(json.dumps(RULES))
        rules['departments'][0]['states'] = ['Bihar']
        save_rules(self.db, rules)
        # The list is the saved one until the next search, which the bar asks for
        self.assertEqual([t['tcno'] for t in runner.page(page_size=20)[0]], ['3', '7', '5', '1', '4'])
        self.assertTrue(runner.status()['changed'])
        status = self.refresh(runner)
        self.assertEqual([t['tcno'] for t in runner.page(page_size=20)[0]], ['3', '7', '5', '4'])
        self.assertFalse(status['changed'])

    def test_the_minimum_value_applies_when_searching(self):
        rules = json.loads(json.dumps(RULES))
        rules['min_value_cr'] = 0.5
        save_rules(self.db, rules)
        runner = self.runner()
        status = self.refresh(runner)
        # Tender 1 (0.2 Cr) is under the minimum; 3 (0.9 Cr) is over it; the rest give no value, so they are not kept
        self.assertEqual(([t['tcno'] for t in runner.page(page_size=20)[0]], status['tenders']), (['3'], 1))
        self.assertEqual(runner.departments(runner.page(page_size=20)[0])[0]['min_value_cr'], 0.5)

    def test_a_search_past_tendertigers_cap_goes_on_from_the_last_closing_date(self):
        self.tendertiger.tenders = [tender(n, 'Eastern Railway', 'West Bengal', closes=n) for n in range(1, 8)]
        self.tendertiger.cap = 4
        runner = self.runner()
        with mock.patch.object(watchlist_service, 'SEARCH_PAGE', 2), mock.patch.object(watchlist_service, 'SEARCH_CAP', 4):
            status = self.refresh(runner)
        self.assertEqual(status['tenders'], 7)
        er = [call for call in self.tendertiger.calls if call[0] == 'eastern railway']
        # Pages 1 and 2 serve the cap; the next search starts on the closing date of the last tender served
        closing = lambda days: f'{TODAY + timedelta(days=days):%Y-%m-%d}'  # noqa: E731
        self.assertEqual([(call[1][:10], call[2]) for call in er],
                         [(closing(0), 1), (closing(0), 2), (closing(4), 1), (closing(4), 2), (closing(7), 1)])

    def test_another_searchs_tenders_past_the_end_are_ignored(self):
        self.tendertiger.tenders = [tender(n, 'Indian Oil Corporation Limited', 'Kerala', closes=n) for n in range(1, 3)]
        runner = self.runner()
        with mock.patch.object(watchlist_service, 'SEARCH_PAGE', 2):
            status = self.refresh(runner)
        self.assertEqual(status['tenders'], 2)  # a full page, then TenderTiger's other search: not kept
        self.assertNotIn('999', tender_service._tenders)

    def test_a_failed_refresh_keeps_the_last_list(self):
        runner = self.runner()
        self.refresh(runner)
        with mock.patch('services.tender_service.tender_list', side_effect=RuntimeError('firewall')):
            status = self.refresh(runner)
        self.assertEqual((status['status'], status['error'], status['tenders']), ('failed', 'firewall', 5))

    def test_a_refresh_whose_list_cannot_be_saved_is_failed_not_completed(self):
        runner = self.runner()
        before = self.refresh(runner)
        rules = json.loads(json.dumps(RULES))
        rules['departments'][0]['states'] = ['Bihar']
        save_rules(self.db, rules)
        with mock.patch.object(watchlist_service, 'save_list', side_effect=psycopg.OperationalError('server closed')), \
                self.assertLogs('services.watchlist_service', 'ERROR'):
            status = self.refresh(runner)

        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['error'], 'The tenders found could not be saved in the database: server closed')
        # The list is the last one saved, and not taken as searched with the new rules nor as refreshed just now
        self.assertEqual((status['tenders'], status['refreshed_at'], status['changed']),
                         (5, before['refreshed_at'], True))

    def test_a_refresh_another_process_made_is_shown(self):
        app = self.runner()  # the web app
        before = self.refresh(app)['refreshed_at']
        time.sleep(1.1)  # so the next search finishes a second later
        script = self.runner()  # scripts/process_watchlist.py, searching meanwhile
        self.assertEqual(script.start(wait=True)['status'], 'completed')

        # The app shows when the script refreshed the list
        self.assertNotEqual(app.status()['refreshed_at'], before)
        self.assertEqual(app.status()['refreshed_at'], script.status()['refreshed_at'])
        self.assertFalse(hasattr(app, 'schedule'))  # the web app never searches on its own


class WatchlistApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = db = scratch_database(self)

        class TestConfig(Config):
            TARGET_DIRECTORY = Path(self.tmp.name) / 'downloads'
            LOG_DIRECTORY = Path(self.tmp.name) / 'logs'
            LOG_LEVEL = 'WARNING'
            DATABASE_URL, DATABASE_SCHEMA = db.url, db.schema
            JOB_WORKERS = 1

        CompanyStore(db).save('# Company\n\n| Item | Value |\n|---|---|\n| Company Name | ABC |')
        self.config = TestConfig
        self.listed = [tender(1, 'Eastern Railway', 'West Bengal'), tender(2, 'Eastern Railway', 'Gujarat'),
                       tender(3, 'Indian Oil Corporation Limited', 'Kerala')]
        self.tendertiger = FakeTenderTiger(self.listed)
        patch = mock.patch('services.tender_service.tender_list', side_effect=self.tendertiger)
        patch.start()
        self.addCleanup(patch.stop)
        tender_service._tenders.clear()
        self.addCleanup(tender_service._tenders.clear)
        self.client = TestClient(create_app(TestConfig), headers={'X-Requested-With': 'test'})  # as the page

    def wait_for_refresh(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.client.get('/api/watchlist/status').json()
            if status['status'] != 'running':
                return status
            time.sleep(0.01)
        self.fail('the refresh did not finish')

    def test_without_a_watchlist_the_list_still_loads(self):
        self.assertEqual(self.client.get('/api/watchlist').json()['total'], 0)
        self.assertIn('Edit watchlist', self.client.post('/api/watchlist/refresh').json()['error'])
        with mock.patch('services.tender_service.tender_list', return_value={'TenderList': self.listed}):
            rows = self.client.get('/api/tenders').json()['tenders']
        self.assertEqual([row['watchlist'] for row in rows], [None, None, None])

    def test_the_watchlist_is_edited_on_the_page(self):
        # Before any: the usual filters and no department, which cannot be saved
        rules = self.client.get('/api/watchlist/rules').json()
        self.assertEqual(rules, {'filters': {'closing_from': 'today', 'bid_doc': True}, 'state_groups': {},
                                 'departments': []})
        refused = self.client.put('/api/watchlist/rules', json=rules)
        self.assertEqual((refused.status_code, refused.json()['error']),
                         (400, 'The watchlist needs at least one department.'))

        saved = self.client.put('/api/watchlist/rules', json=RULES)
        self.assertEqual((saved.status_code, saved.json()), (200, RULES))
        self.assertEqual(self.client.get('/api/watchlist/rules').json(), RULES)

        # A broken watchlist is refused, naming the department, and the saved one stays
        broken = {**RULES, 'departments': [*RULES['departments'][:2], {**RULES['departments'][2], 'states': ['Bengal']}]}
        refused = self.client.put('/api/watchlist/rules', json=broken)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("department 3 (NHAI (O&M)): 'Bengal' is neither a state", refused.json()['error'])
        self.assertEqual(self.client.get('/api/watchlist/rules').json(), RULES)
        self.assertEqual(self.client.put('/api/watchlist/rules', json={'departments': [{'nme': 'ER'}]}).status_code, 400)

    def test_the_watchlist_is_refreshed_listed_and_named_on_the_tender_list(self):
        save_rules(self.db, RULES)
        response = self.client.post('/api/watchlist/refresh')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.wait_for_refresh()['status'], 'completed')

        listed = self.client.get('/api/watchlist', params={'page_size': 20}).json()
        self.assertEqual([row['tcno'] for row in listed['tenders']], ['1', '3'])
        self.assertEqual((listed['total'], listed['has_more'], listed['watchlist']['tenders']), (2, False, 2))
        self.assertEqual(listed['tenders'][0]['watchlist'],
                         {'sl': 1, 'name': 'ER', 'full_name': 'Eastern Railway', 'sector': 'Railway Zonal',
                          'min_value_cr': None})
        self.assertEqual(self.client.get('/api/watchlist', params={'colour': 'red'}).status_code, 400)

        with mock.patch('services.tender_service.tender_list', return_value={'TenderList': self.listed}):
            rows = self.client.get('/api/tenders').json()['tenders']
        self.assertEqual([row['watchlist'] and row['watchlist']['name'] for row in rows], ['ER', None, 'IOCL'])

        # Broken rules stop the next search, not the lists: they show what the last search saved
        with self.db.connection() as connection:
            connection.execute("UPDATE watchlist_organisations SET pattern = '^eastern (railway'")
        self.assertEqual(self.client.post('/api/watchlist/refresh').status_code, 500)
        self.assertEqual(self.client.get('/api/watchlist').json()['total'], 2)
        with mock.patch('services.tender_service.tender_list', return_value={'TenderList': self.listed}):
            rows = self.client.get('/api/tenders').json()['tenders']
        self.assertEqual([row['watchlist'] and row['watchlist']['name'] for row in rows], ['ER', None, 'IOCL'])


if __name__ == '__main__':
    unittest.main()
