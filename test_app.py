import copy
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import create_app
from config import Config
from ext import TENDERTIGER_BLOCKED, TenderTigerBlocked, company_details, save_company_details
from services import tender_service
from services.analysis_service import (CRITERIA_FILE, RESULT_FILE, compare_routes, jv_verdict, parse_criteria,
                                       parse_jv, parse_score, parse_verdict, plain_maths, process_tender_files,
                                       read_criteria, recheck_saved_criteria, tender_files)
from services.company_service import company_profile, parse_profile, render_profile, unnamed_part
from services.download_service import DownloadError, download_tender_documents
from services.processing_service import explain_error

SAMPLE_TENDERS = {
    'KeywordList': [{'id': 1, 'keyword': 'india'}],
    'TenderList': [
        {'tcno': '100091540', 'tenderprocid': '102487311', 'enctcno': 'enc-tc-1', 'enctenderprocid': 'enc-proc-1',
         'tendersbrief': 'Providing Project\nGeneral Management Services',
         'tenderrefno': 'NWR-SC-PGMS-HQ-2026-27', 'companyname': 'North Western Railway',
         'cityname': 'Multi Location', 'statename': 'Multi State', 'publicationdate': '01-09-2026',
         'closingdate': '18-11-2026', 'tendervalue': '5117971516.64', 'currcode': 'inr', 'isdocument': '1'},
        {'tcno': '102309372', 'tenderprocid': '104705634', 'enctcno': 'enc-tc-2', 'enctenderprocid': 'enc-proc-2',
         'tendersbrief': 'Construction of ROB', 'companyname': 'East Central Railway',
         'publicationdate': '31-10-2026', 'closingdate': '08-01-2027', 'isdocument': '1'},
    ],
}
PREQUALIFICATION_LINK = ('https://www.tendertiger.com/TenderDetail/GetPreQualification'
                         '?hdnTcNo=100091540&hdntenderprocId=102487311')

ASSESSMENT = """| Criterion | Requirement | Company | Status | Source |
|---|---|---|---|---|
| Turnover | 10 Cr | 171 Cr | **Met** | NIT.pdf |
| Similar work | 1 work of 40 Cr | 225 Cr | ✅ Met | NIT.pdf |
| Bid capacity | 2x cost | n/a | Cannot determine | NIT.pdf |
| Net worth | positive | 116 Cr | Not met | NIT.pdf |

**Overall verdict:** Not eligible, because the net worth criterion is not met."""

COMPANY = """# Company Eligibility Assessment (Sample Template)

| Item | Value |
|---|---|
| Company Name | ABC Infrastructure Ltd. |"""


def zip_bytes(entries):
    """Builds a zip in memory from {name: bytes}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zfile:
        for name, data in entries.items():
            zfile.writestr(name, data)
    return buffer.getvalue()


class FakeResponse:
    """Just enough of requests.Response for download_service."""

    def __init__(self, body, headers=None, url='https://example.test/api/DownloadAllTenderDocuments'):
        self.body = body
        self.headers = headers or {}
        self.url = url
        self.text = body.decode('latin-1')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.body)

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]


class ParseTest(unittest.TestCase):
    def test_verdict(self):
        self.assertEqual(parse_verdict(ASSESSMENT), 'Not Eligible')
        self.assertEqual(parse_verdict('## Overall Verdict\n\n**Eligible** - all criteria met'), 'Eligible')
        self.assertEqual(parse_verdict('Overall verdict: Cannot determine (bid capacity missing)'), 'Requires Review')
        self.assertEqual(parse_verdict('No conclusion given'), 'Requires Review')

    def test_score_counts_met_rows_of_the_status_column(self):
        self.assertEqual(parse_score(ASSESSMENT), 50)
        self.assertIsNone(parse_score('No table here'))

    def test_criteria_rows_with_plain_text_and_status(self):
        criteria = parse_criteria(ASSESSMENT.replace('| 171 Cr |', '| **171 Cr**<br>(FY26) |'))

        self.assertEqual([(c['criterion'], c['status']) for c in criteria], [
            ('Turnover', 'met'), ('Similar work', 'met'), ('Bid capacity', 'unknown'), ('Net worth', 'not_met')])
        self.assertEqual(criteria[0], {'criterion': 'Turnover', 'requirement': '10 Cr', 'company': '171 Cr (FY26)',
                                       'status': 'met', 'source': 'NIT.pdf'})
        self.assertEqual(parse_criteria('| a | b |\n|---|---|\n| 1 | 2 |'), [])

    def test_a_revised_table_replaces_the_rows_it_corrects(self):
        assessment = ('| Criterion | Requirement | Company | Status |\n|---|---|---|---|\n'
                      '| Turnover | 15,000 Cr | 95,933 Cr | Met |\n| Net worth | 5,000 Cr | 11,600 Cr | Met |\n'
                      '| Similar work | 1 work | 2 works | Met |\n| Similar work | 2 works | 2 works | Met |\n\n'
                      'Wait, 95,933,333,333 rupees is 9,593 crore.\n\n**Revised Table:**\n'
                      '| Criterion | Requirement | Company | Status |\n|---|---|---|---|\n'
                      '| Turnover | 15,000 Cr | 9,593 Cr | Not met |\n| Net worth | 5,000 Cr | 11,600 Cr | Met |')

        rows = parse_criteria(assessment)

        self.assertEqual([(row['criterion'], row['company'], row['status']) for row in rows], [
            ('Turnover', '9,593 Cr', 'not_met'), ('Net worth', '11,600 Cr', 'met'),
            ('Similar work', '2 works', 'met'), ('Similar work', '2 works', 'met'),  # two rows of one table stay
        ])

    def test_joint_venture_terms_and_criteria(self):
        assessment = ASSESSMENT.replace('\n\n**Overall verdict:**', """

## Joint venture

**JV allowed:** Yes (RFP clause 2.1.13)
**Maximum JV members:** 3, lead member included (RFP clause 2.1.14)

| Criterion | Lead member | Other members | JV combined | Company as lead member | Source |
|---|---|---|---|---|---|
| Turnover | 60% of 10 Cr = 6 Cr | 20% each | 10 Cr | **Met** (171 Cr) | NIT.pdf |
| Net worth | 26% | 10% | positive | Not met | NIT.pdf |

**Other JV conditions:**
- Lead member must hold at least 51% share.
- A JV agreement must be submitted with the bid.

**Overall verdict:**""")

        jv = parse_jv(assessment)

        self.assertEqual((jv['allowed'], jv['max_members']), ('yes', 3))
        self.assertEqual(jv['criteria'][0], {
            'criterion': 'Turnover', 'lead_member': '60% of 10 Cr = 6 Cr', 'other_members': '20% each',
            'combined': '10 Cr', 'company': 'Met (171 Cr)', 'status': 'met', 'source': 'NIT.pdf'})
        self.assertEqual([c['status'] for c in jv['criteria']], ['met', 'not_met'])
        self.assertEqual(jv['conditions'], ['Lead member must hold at least 51% share.',
                                            'A JV agreement must be submitted with the bid.'])
        # The JV section changes neither the bidding-alone criteria nor the verdict
        self.assertEqual(parse_criteria(assessment), parse_criteria(ASSESSMENT))
        self.assertEqual(parse_verdict(assessment), 'Not Eligible')

    def test_standalone_and_jv_criteria_are_compared_side_by_side(self):
        criteria = [
            {'criterion': 'Annual Turnover', 'requirement': '100 Cr', 'company': '80 Cr', 'status': 'not_met', 'source': 'NIT.pdf'},
            {'criterion': 'Net worth', 'requirement': 'positive', 'company': '116 Cr', 'status': 'met', 'source': 'NIT.pdf'},
            {'criterion': 'Similar work', 'requirement': '1 work of 40 Cr', 'company': '45 Cr', 'status': 'met', 'source': 'NIT.pdf'},
        ]
        jv_row = lambda name, status: {'criterion': name, 'lead_member': f'{name} (lead)', 'other_members': 'x',
                                       'combined': 'y', 'company': status.title(), 'status': status, 'source': 'RFP.pdf'}
        jv = {'allowed': 'yes', 'max_members': 3, 'conditions': [],
              'criteria': [jv_row('Net worth', 'met'), jv_row('annual turnover', 'met'), jv_row('Lead member share', 'met')]}

        rows = compare_routes(criteria, jv)

        self.assertEqual([(r['criterion'], r['alone_status'], r['lead_status']) for r in rows], [
            ('Annual Turnover', 'not_met', 'met'),   # not met alone, met as lead member
            ('Net worth', 'met', 'met'),
            ('Similar work', 'met', 'unknown'),      # the JV table left it out
            ('Lead member share', None, 'met'),      # only a JV has it
        ])
        self.assertEqual((rows[0]['lead_member'], rows[0]['company']), ('annual turnover (lead)', '80 Cr'))
        self.assertEqual(jv_verdict(rows, jv, skipped=[]), 'Requires Review')
        rows[2]['lead_status'] = 'met'
        self.assertEqual(jv_verdict(rows, jv, skipped=[]), 'Eligible')
        self.assertEqual(jv_verdict(rows, jv, skipped=['scan.pdf']), 'Requires Review')
        rows[3]['lead_status'] = 'not_met'
        self.assertEqual(jv_verdict(rows, jv, skipped=[]), 'Not Eligible')

    def test_without_a_jv_only_the_standalone_route_is_compared(self):
        criteria = [{'criterion': 'Turnover', 'requirement': '10 Cr', 'company': '171 Cr', 'status': 'met', 'source': ''}]
        jv = {'allowed': 'no', 'max_members': None, 'conditions': [], 'criteria': []}

        rows = compare_routes(criteria, jv)

        self.assertEqual((rows[0]['alone_status'], rows[0]['lead_status'], rows[0]['lead_member']), ('met', None, None))
        self.assertIsNone(jv_verdict(rows, jv, skipped=[]))

    def test_latex_maths_becomes_plain_text(self):
        self.assertEqual(plain_maths(r'hold $\ge 26\%$ equity; value $\le 2 \times$ cost'),
                         'hold ≥ 26% equity; value ≤ 2 × cost')
        self.assertEqual(plain_maths('between $5 million and $10 million'), 'between $5 million and $10 million')

    def test_joint_venture_not_allowed_or_not_stated(self):
        self.assertEqual(parse_jv('## Joint venture\nJV allowed: No\nMaximum JV members: 2\n\nOverall verdict: Eligible'),
                         {'allowed': 'no', 'max_members': None, 'criteria': [], 'conditions': []})
        jv = parse_jv('**Joint venture / consortium**\n- JV allowed: Not stated\n- Maximum JV members: two\n')
        self.assertEqual((jv['allowed'], jv['max_members'], jv['conditions']), ('unknown', 2, []))
        self.assertEqual(parse_jv(ASSESSMENT)['allowed'], 'unknown')
        # A table with a Status column inside the JV section is not read as a bidding-alone criterion
        table = '| Criterion | Status |\n|---|---|\n| JV share | Met |'
        self.assertEqual(parse_criteria(f'## Joint venture\nJV allowed: Yes\n\n{table}'), [])

    def test_errors_are_explained_with_a_next_step(self):
        title, action = explain_error(DownloadError('Download failed: 403 Client Error: Forbidden for url'))
        self.assertEqual(title, 'Document download refused')
        self.assertIn('Retry in a few minutes', action)
        self.assertEqual(explain_error(RuntimeError('OLLAMA_API_KEY is not set.'))[0], 'The LLM API key is missing')
        title, action = explain_error(RuntimeError(TENDERTIGER_BLOCKED))
        self.assertEqual(title, 'TenderTiger is blocking automated visits for now')
        title, action = explain_error(RuntimeError('TenderTiger login failed: Invalid username or password.'))
        self.assertEqual(title, 'The TenderTiger login failed')
        self.assertIn('TENDERTIGER_PASSWORD', action)
        self.assertEqual(explain_error(ValueError('boom'))[0], 'The analysis failed')

    def test_empty_company_profile_fails_before_any_llm_call(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'OLLAMA_API_KEY': 'key'}), \
                mock.patch('services.analysis_service.make_model') as make_model:
            with self.assertRaises(RuntimeError) as raised:
                process_tender_files(tmp, company_file=Path(tmp) / 'missing.md')
        make_model.assert_not_called()
        self.assertEqual(explain_error(raised.exception)[0], 'The company profile is empty')


class AnalysisTestCase(unittest.TestCase):
    """A tender folder, a company profile and a fake LLM for process_tender_files()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / '100091540'
        self.folder.mkdir()
        self.company_file = Path(self.tmp.name) / 'company_details.md'
        save_company_details(COMPANY, self.company_file)
        self.llm = mock.Mock()
        for patch in (mock.patch.dict(os.environ, {'OLLAMA_API_KEY': 'key'}),
                      mock.patch('services.analysis_service.make_model', return_value=self.llm)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()


class PrequalificationTest(AnalysisTestCase):
    LINK = PREQUALIFICATION_LINK

    def analyse(self, prequalification):
        with mock.patch('services.analysis_service.prequalification_criteria', side_effect=prequalification):
            return process_tender_files(self.folder, company_file=self.company_file, prequalification_link=self.LINK)

    def test_pre_qualification_is_checked_as_written_beside_the_files(self):
        self.llm.invoke.return_value = mock.Mock(content=ASSESSMENT)

        result = self.analyse(['FINANCIAL CAPACITY:\n• Net worth of 5%'])

        sources = self.llm.invoke.call_args[0][0][0]['content']
        self.assertIn('## TenderTiger Pre-Qualification\nFINANCIAL CAPACITY:\n• Net worth of 5%', sources)
        self.assertEqual((result['verdict'], result['unreadable_files']), ('Not Eligible', []))

    def test_pre_qualification_that_cannot_be_fetched_makes_the_check_incomplete(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')

        with mock.patch('services.analysis_service.summarize_for_goal', return_value='Turnover at least 5 Cr'), \
                self.assertLogs('services.analysis_service', 'WARNING'):
            result = self.analyse(ConnectionError('offline'))

        self.assertEqual(result['verdict'], 'Requires Review')
        self.assertEqual(result['unreadable_files'], ['TenderTiger Pre-Qualification'])

    def test_a_firewall_block_fails_the_tender_instead_of_skipping_the_pre_qualification(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')

        with self.assertRaises(TenderTigerBlocked):
            self.analyse(TenderTigerBlocked(TENDERTIGER_BLOCKED))
        self.llm.invoke.assert_not_called()

    def test_no_files_and_no_pre_qualification_is_nothing_to_check(self):
        with self.assertRaises(RuntimeError) as raised:
            self.analyse([''])
        self.assertEqual(explain_error(raised.exception)[0], 'Nothing to check')

        with self.assertRaises(RuntimeError) as raised, self.assertLogs('services.analysis_service', 'WARNING'):
            self.analyse(ConnectionError('offline'))
        self.assertEqual(explain_error(raised.exception)[0], 'The pre-qualification criteria could not be fetched')
        self.llm.invoke.assert_not_called()


class SavedCriteriaTest(AnalysisTestCase):
    """The criteria file written after the documents are read, and re-checks against it."""

    def test_criteria_are_saved_and_a_recheck_uses_them_without_reading_the_documents(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        (self.folder / 'scan.txt').write_text('', encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')
        with mock.patch('services.analysis_service.summarize_for_goal', return_value='Turnover at least 5 Cr'), \
                mock.patch('services.analysis_service.prequalification_criteria', return_value='Net worth of 5%'):
            process_tender_files(self.folder, company_file=self.company_file, prequalification_link=PREQUALIFICATION_LINK)

        saved = (self.folder / CRITERIA_FILE).read_text(encoding='utf-8')
        self.assertTrue(saved.startswith('# Eligibility criteria\n\n> Taken from the tender documents on '))
        self.assertIn('## TenderTiger Pre-Qualification\nNet worth of 5%\n\n## NIT.txt\nTurnover at least 5 Cr', saved)
        self.assertTrue(saved.endswith('## Sources that could not be read\n\nThese were not checked, so any criteria '
                                       'they hold are missing above:\n\n- scan.txt\n'))

        # Edited by hand, then re-checked: only the edited file is sent, and the unreadable source still counts
        (self.folder / CRITERIA_FILE).write_text(saved.replace('5 Cr', '8 Cr'), encoding='utf-8')
        self.llm.reset_mock()
        with mock.patch('services.analysis_service.summarize_for_goal') as summarize:
            result = recheck_saved_criteria(self.folder, company_file=self.company_file)

        summarize.assert_not_called()
        self.llm.invoke.assert_called_once()
        sources = self.llm.invoke.call_args[0][0][0]['content']
        self.assertTrue(sources.endswith('## TenderTiger Pre-Qualification\nNet worth of 5%\n\n## NIT.txt\nTurnover at least 8 Cr'))
        self.assertNotIn('Taken from', sources)
        self.assertEqual((result['verdict'], result['unreadable_files']), ('Requires Review', ['scan.txt']))

    def test_criteria_and_result_files_are_not_read_as_documents(self):
        (self.folder / 'NIT.txt').write_text('x', encoding='utf-8')
        (self.folder / CRITERIA_FILE).write_text('# Eligibility criteria\n', encoding='utf-8')
        (self.folder / RESULT_FILE).write_text('{}', encoding='utf-8')
        self.assertEqual([path.name for path in tender_files(self.folder)], ['NIT.txt'])

    def test_text_added_above_the_sections_is_kept_and_an_empty_file_has_no_criteria(self):
        path = self.folder / CRITERIA_FILE
        path.write_text('# Eligibility criteria\n\n> note\n\nBid capacity: 2x cost\n\n## NIT.pdf\nTurnover\n',
                        encoding='utf-8')
        self.assertEqual(read_criteria(path), ('Bid capacity: 2x cost\n\n## NIT.pdf\nTurnover', []))

        path.write_text('# Eligibility criteria\n\n> No eligibility criteria were found in the documents.\n',
                        encoding='utf-8')
        result = recheck_saved_criteria(self.folder, company_file=self.company_file)
        self.assertEqual((result['verdict'], result['criteria']), ('Requires Review', []))
        self.llm.invoke.assert_not_called()


class CompanyProfileTest(unittest.TestCase):
    MARKDOWN = """# Profile (Sample)

## 1. Basic Information

| Item | Value |
|:-------|-------:|
| Company Name | A \\| B Ltd. |
| PAN |
| GST | 19AAA | extra |

Figures in INR.
- Audited

---

### Turnover

| Financial Year | Turnover |
|---|---|"""

    def test_markdown_becomes_form_blocks(self):
        self.assertEqual(parse_profile(self.MARKDOWN), [
            {'type': 'heading', 'level': 1, 'text': 'Profile (Sample)'},
            {'type': 'heading', 'level': 2, 'text': '1. Basic Information'},
            {'type': 'table', 'columns': ['Item', 'Value'],
             'rows': [['Company Name', 'A | B Ltd.'], ['PAN', ''], ['GST', '19AAA']]},
            {'type': 'text', 'text': 'Figures in INR.\n- Audited'},
            {'type': 'rule'},
            {'type': 'heading', 'level': 3, 'text': 'Turnover'},
            {'type': 'table', 'columns': ['Financial Year', 'Turnover'], 'rows': []},
        ])

    def test_edited_blocks_become_markdown_that_reads_back_the_same(self):
        blocks = parse_profile(self.MARKDOWN)
        blocks[2]['rows'][1][1] = 'AAAAA\n1234A'
        blocks[6]['rows'] = [['FY26', '1710000000'], [' ', '']]

        markdown = render_profile(blocks)

        self.assertIn('| Company Name | A \\| B Ltd. |\n| PAN | AAAAA 1234A |', markdown)
        self.assertTrue(markdown.endswith('### Turnover\n\n| Financial Year | Turnover |\n| --- | --- |\n| FY26 | 1710000000 |'))
        blocks[2]['rows'][1][1] = 'AAAAA 1234A'
        blocks[6]['rows'].pop()
        self.assertEqual(parse_profile(markdown), blocks)

    def test_rules_left_with_nothing_to_separate_are_dropped(self):
        blocks = [{'type': 'rule'}, {'type': 'heading', 'level': 2, 'text': '1. Basic'}, {'type': 'rule'},
                  {'type': 'heading', 'level': 2, 'text': ' '}, {'type': 'rule'}, {'type': 'rule'}]
        self.assertEqual(render_profile(blocks), '## 1. Basic')

    def test_headings_and_columns_need_a_name(self):
        heading = lambda level, text: {'type': 'heading', 'level': level, 'text': text}
        table = lambda *columns: {'type': 'table', 'columns': list(columns), 'rows': []}
        self.assertIsNone(unnamed_part([heading(1, ''), heading(2, '9. Key Personnel'), heading(3, '2024'), table('Item', 'Value')]))
        self.assertIn('heading', unnamed_part([heading(2, '1. Basic'), heading(2, '9. ')]))
        self.assertIn('heading', unnamed_part([heading(3, '  ')]))
        self.assertIn('column', unnamed_part([table('Item', ' ')]))

    def test_company_name(self):
        self.assertEqual(company_profile(COMPANY), {'name': 'ABC Infrastructure Ltd.'})
        self.assertEqual(company_profile('# Profile\n\nNo table yet'), {'name': 'Company profile'})
        self.assertEqual(company_profile(''), {'name': 'No company profile'})


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / 'downloads' / '100091540'

    def tearDown(self):
        self.tmp.cleanup()

    def download(self, response, checks=(b'{"successCode":1,"fileName":"DocumentAll.zip"}',)):
        """Downloads SAMPLE_TENDERS' first tender; checks are the DownloadAllFiles answers before the zip."""
        checks = iter(checks)
        self.calls = []

        def get(url, params=None, **kwargs):
            self.calls.append((url.rsplit('/', 1)[-1], params))
            if url.endswith('/Tenderinformation'):
                return FakeResponse(b'<html>tender page</html>')
            return FakeResponse(next(checks)) if params['mode'] == '' else response

        session = mock.Mock(get=get)
        tender = SAMPLE_TENDERS['TenderList'][0]
        with mock.patch('services.download_service.tendertiger_session', return_value=session) as login:
            files = download_tender_documents(tender, self.folder)
        self.logins = [call.args for call in login.call_args_list]
        return files

    def test_documents_are_fetched_like_the_tender_page_does(self):
        self.download(FakeResponse(b'%PDF-1.7', {'Content-Type': 'application/pdf'}))

        base = {'hdntenderprocId': '102487311', 'hdnfreetender': '0', 'hdnwhatsapp': '0'}
        self.assertEqual(self.calls, [
            ('Tenderinformation', {'tenderNo': 'enc-proc-1', 'tcNo': 'enc-tc-1', 'tendertype': 'Live'}),
            ('DownloadAllFiles', {**base, 'hdnTcNo': '100091540', 'mode': ''}),
            ('DownloadAllFiles', {**base, 'hdnTcNo': 'undefined', 'mode': 'download'}),
        ])

    def test_an_ended_session_logs_in_again(self):
        files = self.download(FakeResponse(b'%PDF-1.7', {'Content-Type': 'application/pdf'}),
                              checks=[b'{"isLogin":0}', b'{"successCode":1}'])

        self.assertEqual(self.logins, [(False,), (True,)])
        self.assertEqual(len(files), 1)

    def test_no_documents_on_tendertiger_is_explained(self):
        with self.assertRaises(DownloadError) as raised:
            self.download(None, checks=[b'{"successCode":2,"fileName":"","isSubscribedUser":7}'])
        self.assertEqual(explain_error(raised.exception)[0], 'No documents')

    def test_nested_zip_is_extracted_into_the_tcno_folder(self):
        self.folder.mkdir(parents=True)
        (self.folder / 'stale.pdf').write_bytes(b'from an earlier run')
        body = zip_bytes({'NIT.pdf': b'nit', 'annex.zip': zip_bytes({'BOQ.xlsx': b'boq', 'deep.zip': zip_bytes({'GCC.pdf': b'gcc'})})})

        files = self.download(FakeResponse(body, {'Content-Type': 'application/octet-stream'}))

        names = sorted(path.relative_to(self.folder).as_posix() for path in files)
        self.assertEqual(names, ['NIT.pdf', 'annex/BOQ.xlsx', 'annex/deep/GCC.pdf'])
        self.assertEqual(list(self.folder.rglob('*.zip')), [])
        # The staging folder is removed, so only the tcno folder is left
        self.assertEqual([p.name for p in self.folder.parent.iterdir()], ['100091540'])

    def test_single_document_keeps_its_name(self):
        response = FakeResponse(b'%PDF-1.7', {'Content-Type': 'application/pdf',
                                              'Content-Disposition': 'attachment; filename="NIT 2026.pdf"'})

        files = self.download(response)

        self.assertEqual([path.name for path in files], ['NIT_2026.pdf'])

    def test_html_error_page_is_a_failure(self):
        with self.assertRaises(DownloadError):
            self.download(FakeResponse(b'<html>Session expired</html>', {'Content-Type': 'text/html; charset=utf-8'}))


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

        class TestConfig(Config):
            TARGET_DIRECTORY = Path(self.tmp.name) / 'downloads'
            LOG_DIRECTORY = Path(self.tmp.name) / 'logs'
            LOG_LEVEL = 'WARNING'
            COMPANY_DETAILS_FILE = Path(self.tmp.name) / 'company_details.md'

        save_company_details(COMPANY, TestConfig.COMPANY_DETAILS_FILE)

        patches = [
            mock.patch('services.tender_service.tender_list', return_value=SAMPLE_TENDERS),
            mock.patch('services.processing_service.download_tender_documents', side_effect=self.fake_download),
            mock.patch('services.processing_service.process_tender_files', side_effect=self.fake_analysis),
            mock.patch('services.processing_service.recheck_saved_criteria', side_effect=self.fake_recheck),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.rechecked = []
        tender_service._tenders.clear()
        self.config = TestConfig
        self.analysis_delay = 0
        self.client = TestClient(create_app(TestConfig))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def fake_download(tender, folder, **kwargs):
        if tender.get('isdocument') != '1':
            raise DownloadError('TenderTiger has no documents for this tender: {"successCode":2}')
        if tender['tcno'] == '102309372':
            raise DownloadError('Download failed: 404')
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'NIT.pdf').write_bytes(b'x')
        return [folder / 'NIT.pdf']

    def fake_analysis(self, folder, on_progress=None, company_file=None, prequalification_link=None):
        self.company_file = company_file
        self.prequalification_link = prequalification_link
        time.sleep(self.analysis_delay)
        return {
            'verdict': 'Eligible', 'details': '## Result\nAll criteria met.', 'score': 50,
            'criteria': [
                {'criterion': 'Turnover', 'requirement': '10 Cr', 'company': '171 Cr', 'status': 'met', 'source': 'NIT.pdf'},
                {'criterion': 'Net worth', 'requirement': '20 Cr', 'company': '', 'status': 'unknown', 'source': 'NIT.pdf'},
            ],
            'unreadable_files': ['scan.pdf'],
            'jv': {'allowed': 'yes', 'max_members': 3, 'verdict': 'Requires Review',
                   'conditions': ['Lead member holds at least 51%']},
            'comparison': [
                {'criterion': 'Turnover', 'company': '171 Cr', 'source': 'NIT.pdf', 'alone_requirement': '10 Cr',
                 'alone_status': 'met', 'lead_member': '6 Cr', 'lead_company': 'Met', 'lead_status': 'met',
                 'other_members': '2 Cr', 'combined': '10 Cr'},
                {'criterion': 'Net worth', 'company': '', 'source': 'NIT.pdf', 'alone_requirement': '20 Cr',
                 'alone_status': 'unknown', 'lead_member': '26 Cr', 'lead_company': 'Cannot determine',
                 'lead_status': 'unknown', 'other_members': '', 'combined': ''},
            ],
        }

    def fake_recheck(self, folder, on_progress=None, company_file=None):
        self.rechecked.append(folder.name)
        return {'verdict': 'Not Eligible', 'details': '## Result\nNet worth not met.', 'score': 0,
                'criteria': [{'criterion': 'Net worth', 'requirement': '20 Cr', 'company': '5 Cr',
                              'status': 'not_met', 'source': 'NIT.pdf'}],
                'unreadable_files': []}

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            progress = self.client.get('/api/progress').json()
            if condition(progress):
                return progress
            time.sleep(0.02)
        self.fail('condition not reached')

    def wait_for_job(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            progress = self.client.get('/api/progress').json()
            if progress['status'] != 'running':
                return progress
            time.sleep(0.05)
        self.fail('job did not finish')

    def test_page_renders(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Check eligibility', response.content)
        self.assertIn(b'ABC Infrastructure Ltd.', response.content)

    def test_tenders_are_listed_without_tendertiger_ids(self):
        today = tender_service.date(2026, 11, 2)
        with mock.patch('services.tender_service.date', wraps=tender_service.date) as fake_date:
            fake_date.today.return_value = today
            data = self.client.get('/api/tenders').json()

        self.assertEqual(data['count'], 2)
        first = data['tenders'][0]
        self.assertEqual(first['tcno'], '100091540')
        self.assertEqual(first['name'], 'Providing Project General Management Services')
        self.assertEqual(first['organisation'], 'North Western Railway')
        self.assertEqual((first['published_date'], first['closing_date'], first['days_left']),
                         ('01-Sep-2026', '18-Nov-2026', 16))
        self.assertEqual((first['tender_value'], first['currency']), (5117971516.64, 'INR'))
        self.assertTrue(first['has_document'])
        self.assertNotIn('tenderprocid', first)
        self.assertNotIn('enctcno', first)

    def test_process_reports_progress_and_results(self):
        response = self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
        self.assertEqual(response.status_code, 202)

        progress = self.wait_for_job()

        self.assertEqual((progress['total'], progress['completed'], progress['failed']), (2, 1, 1))
        self.assertEqual(progress['percent'], 100)
        self.assertEqual([item['status'] for item in progress['items']], ['completed', 'failed'])

        result = self.client.get('/api/result/100091540').json()
        self.assertEqual((result['verdict'], result['score'], result['files']), ('Eligible', 50, ['NIT.pdf']))
        self.assertEqual(result['unreadable_files'], ['scan.pdf'])
        failed = self.client.get('/api/result/102309372').json()
        self.assertEqual(failed['error'], 'Download failed: 404')
        self.assertEqual(failed['error_title'], 'The documents could not be downloaded')

        listed = self.client.get('/api/tenders').json()['tenders'][0]
        self.assertEqual(listed['result']['verdict'], 'Eligible')
        self.assertEqual(listed['result']['checks'], {
            'total': 2, 'met': 1, 'not_met': [], 'unknown': ['Net worth'], 'unreadable_files': 1,
            'jv': {'allowed': 'yes', 'max_members': 3, 'verdict': 'Requires Review',
                   'lead': {'total': 2, 'met': 1, 'not_met': [], 'unknown': ['Net worth']}}})
        self.assertEqual(result['jv']['conditions'], ['Lead member holds at least 51%'])
        self.assertEqual([row['lead_status'] for row in result['comparison']], ['met', 'unknown'])
        self.assertNotIn('details', listed['result'])
        self.assertIsNotNone(self.client.get('/api/progress').json()['average_seconds'])
        self.assertEqual(self.company_file, self.config.COMPANY_DETAILS_FILE)
        self.assertEqual(self.prequalification_link, PREQUALIFICATION_LINK)

    def test_tender_without_documents_is_checked_on_its_pre_qualification_alone(self):
        tenders = copy.deepcopy(SAMPLE_TENDERS)
        for tender in tenders['TenderList']:
            tender['isdocument'] = '0'
        del tenders['TenderList'][1]['tenderprocid']  # the first still has a pre-qualification link, the second has neither
        folder = self.config.TARGET_DIRECTORY / '100091540'
        folder.mkdir(parents=True)
        (folder / 'old.pdf').write_bytes(b'from an earlier run')

        with mock.patch('services.tender_service.tender_list', return_value=tenders):
            rows = self.client.get('/api/tenders').json()['tenders']
            self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
        self.wait_for_job()

        self.assertEqual([(row['has_document'], row['has_prequalification']) for row in rows],
                         [(False, True), (False, False)])
        result = self.client.get('/api/result/100091540').json()
        self.assertEqual((result['status'], result['verdict'], result['files']), ('completed', 'Eligible', []))
        self.assertEqual(self.prequalification_link, PREQUALIFICATION_LINK)
        self.assertEqual(list(folder.iterdir()), [folder / RESULT_FILE])  # old.pdf is gone
        self.assertEqual(self.client.get('/api/result/102309372').json()['error_title'], 'No documents')

    def test_saved_criteria_are_rechecked_unless_regenerated(self):
        folder = self.config.TARGET_DIRECTORY / '100091540'
        folder.mkdir(parents=True)
        (folder / 'NIT.pdf').write_bytes(b'kept from the first run')
        (folder / CRITERIA_FILE).write_text('# Eligibility criteria\n\n## NIT.pdf\nNet worth 20 Cr\n', encoding='utf-8')
        self.assertEqual([row['has_saved_criteria'] for row in self.client.get('/api/tenders').json()['tenders']],
                         [True, False])

        queued = self.client.post('/api/process-tenders', json={'tcnos': ['100091540']}).json()
        self.assertTrue(queued['items'][0]['rechecked'])
        progress = self.wait_for_job()

        result = self.client.get('/api/result/100091540').json()
        self.assertEqual(self.rechecked, ['100091540'])
        self.assertEqual((result['verdict'], result['rechecked'], result['files']), ('Not Eligible', True, ['NIT.pdf']))
        self.assertEqual(result['criteria_file'], str(folder / CRITERIA_FILE))
        self.assertIsNotNone(progress['average_recheck_seconds'])
        self.assertIsNone(progress['average_seconds'])  # a quick re-check must not shorten full-run estimates

        self.client.post('/api/process-tenders', json={'tcnos': ['100091540'], 'regenerate': True})
        self.wait_for_job()

        result = self.client.get('/api/result/100091540').json()
        self.assertEqual(self.rechecked, ['100091540'])  # not re-checked again: the documents were analysed
        self.assertEqual((result['verdict'], result['rechecked']), ('Eligible', False))
        self.assertEqual(self.prequalification_link, PREQUALIFICATION_LINK)

    def test_results_are_saved_in_the_tender_folder_and_survive_a_restart(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
        self.wait_for_job()
        before = {tcno: self.client.get(f'/api/result/{tcno}').json() for tcno in ('100091540', '102309372')}

        for tcno, result in before.items():
            saved = (self.config.TARGET_DIRECTORY / tcno / RESULT_FILE).read_text(encoding='utf-8')
            self.assertEqual(json.loads(saved), result)
        broken = self.config.TARGET_DIRECTORY / '999' / RESULT_FILE
        broken.parent.mkdir()
        broken.write_text('{"tcno": "999"', encoding='utf-8')

        with self.assertLogs('services.processing_service', 'WARNING') as logs:
            restarted = TestClient(create_app(self.config))

        self.assertIn(str(broken), logs.output[0])
        for tcno, result in before.items():
            self.assertEqual(restarted.get(f'/api/result/{tcno}').json(), result)
        rows = restarted.get('/api/tenders').json()['tenders']
        self.assertEqual([row['result']['status'] for row in rows], ['completed', 'failed'])
        self.assertEqual(rows[0]['result']['checks']['unknown'], ['Net worth'])
        self.assertIsNotNone(restarted.get('/api/progress').json()['average_seconds'])

    def test_bad_requests(self):
        self.assertEqual(self.client.post('/api/process-tenders', json={}).status_code, 400)
        self.assertEqual(self.client.post('/api/process-tenders', json={'tcnos': ['999']}).status_code, 400)
        self.assertEqual(self.client.get('/api/result/999').status_code, 404)
        self.assertEqual(self.client.post('/api/stop').status_code, 409)

    def test_errors_keep_the_error_shape(self):
        invalid = self.client.post('/api/process-tenders', content=b'not json', headers={'Content-Type': 'application/json'})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('{"tcnos": ["100091540"]}', invalid.json()['error'])
        self.assertEqual(self.client.post('/api/process-tenders', json={'tcnos': []}).status_code, 400)
        self.assertEqual(self.client.get('/api/result/999').json(), {'error': 'No result for TCNO 999 yet.'})
        self.assertEqual(self.client.get('/api/nothing-here').json(), {'error': 'Not Found'})

    def test_tenders_posted_during_a_run_join_it(self):
        self.analysis_delay = 0.3
        first = self.client.post('/api/process-tenders', json={'tcnos': ['100091540']}).json()
        self.wait_for(lambda p: p['items'][0]['status'] == 'processing')

        response = self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})

        self.assertEqual(response.status_code, 202)
        joined = response.json()
        self.assertEqual(joined['job_id'], first['job_id'])
        self.assertEqual([item['tcno'] for item in joined['items']], ['100091540', '102309372'])
        progress = self.wait_for_job()
        self.assertEqual([item['status'] for item in progress['items']], ['completed', 'failed'])

    def test_stop_cancels_queued_tenders_and_keeps_the_current_one(self):
        self.analysis_delay = 0.3
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
        self.wait_for(lambda p: p['items'][0]['status'] == 'processing')

        stopped = self.client.post('/api/stop')

        self.assertEqual(stopped.status_code, 202)
        self.assertTrue(stopped.json()['stop_requested'])
        progress = self.wait_for_job()
        self.assertEqual([item['status'] for item in progress['items']], ['completed', 'cancelled'])
        self.assertEqual((progress['completed'], progress['cancelled'], progress['percent']), (1, 1, 100))
        self.assertEqual(self.client.get('/api/result/102309372').status_code, 404)

    def test_a_firewall_block_cancels_the_queued_tenders_that_need_tendertiger(self):
        with mock.patch('services.processing_service.download_tender_documents',
                        side_effect=TenderTigerBlocked(TENDERTIGER_BLOCKED)) as download:
            self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
            progress = self.wait_for_job()

            self.assertEqual([item['status'] for item in progress['items']], ['failed', 'cancelled'])
            self.assertEqual((progress['failed'], progress['cancelled']), (1, 1))
            self.assertEqual(progress['items'][0]['error_title'], 'TenderTiger is blocking automated visits for now')
            self.assertEqual(download.call_count, 1)

            # A tender with saved criteria is re-checked without TenderTiger, so it still runs
            folder = self.config.TARGET_DIRECTORY / '102309372'
            folder.mkdir(parents=True)
            (folder / CRITERIA_FILE).write_text('# Eligibility criteria\n\n## NIT.pdf\nNet worth 20 Cr\n',
                                                encoding='utf-8')
            self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
            progress = self.wait_for_job()

        self.assertEqual([item['status'] for item in progress['items']], ['failed', 'completed'])
        self.assertEqual(self.rechecked, ['102309372'])

    def test_decisions_are_saved_and_survive_a_restart(self):
        response = self.client.put('/api/decision/100091540', json={'decision': 'dropped'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['decision']['decision'], 'dropped')

        restarted = TestClient(create_app(self.config))
        rows = restarted.get('/api/tenders').json()['tenders']
        self.assertEqual(rows[0]['decision']['decision'], 'dropped')
        self.assertIsNone(rows[1]['decision'])

        self.assertIsNone(restarted.put('/api/decision/100091540', json={'decision': None}).json()['decision'])
        self.assertIsNone(restarted.get('/api/tenders').json()['tenders'][0]['decision'])
        self.assertEqual(restarted.put('/api/decision/100091540', json={'decision': 'maybe'}).status_code, 400)
        self.assertEqual(restarted.put('/api/decision/100091540', json={}).status_code, 400)
        self.assertEqual(restarted.put('/api/decision/999', json={'decision': 'dropped'}).status_code, 404)

    def test_company_profile_is_edited_as_a_form_and_saved_as_markdown(self):
        profile = self.client.get('/api/company').json()
        self.assertEqual((profile['details'], profile['name']), (COMPANY, 'ABC Infrastructure Ltd.'))
        title, table = profile['blocks']
        self.assertEqual(title, {'type': 'heading', 'level': 1, 'text': 'Company Eligibility Assessment (Sample Template)'})
        self.assertEqual(table['rows'], [['Company Name', 'ABC Infrastructure Ltd.']])

        title['text'] = 'Company Eligibility Assessment'
        table['rows'][0][1] = ' XYZ Projects Pvt. Ltd. '
        table['rows'] += [['Turnover FY26', '1710000000'], ['', '']]
        response = self.client.put('/api/company', json={'blocks': profile['blocks']})

        self.assertEqual(response.status_code, 200)
        saved = ('# Company Eligibility Assessment\n\n| Item | Value |\n| --- | --- |\n'
                 '| Company Name | XYZ Projects Pvt. Ltd. |\n| Turnover FY26 | 1710000000 |')
        self.assertEqual(company_details(self.config.COMPANY_DETAILS_FILE), saved)
        self.assertEqual({key: response.json()[key] for key in ('details', 'name')},
                         {'details': saved, 'name': 'XYZ Projects Pvt. Ltd.'})
        self.assertEqual(len(response.json()['blocks'][1]['rows']), 2)
        self.assertIn(b'XYZ Projects Pvt. Ltd.', self.client.get('/').content)

        blank = {'blocks': [{'type': 'heading', 'level': 1, 'text': ' '}]}
        self.assertEqual(self.client.put('/api/company', json=blank).json(), {'error': 'The company details cannot be empty.'})
        self.assertEqual(self.client.put('/api/company', json={'details': '# Markdown'}).status_code, 400)
        self.assertEqual(self.client.put('/api/company', json={'blocks': [{'type': 'chart'}]}).status_code, 400)
        self.assertEqual(company_details(self.config.COMPANY_DETAILS_FILE), saved)

    def test_headings_and_sub_headings_are_added_and_renamed(self):
        blocks = self.client.get('/api/company').json()['blocks']
        blocks[0]['text'] = 'Company Profile'
        blocks += [
            {'type': 'rule'},
            {'type': 'heading', 'level': 2, 'text': '2. Key Personnel'},
            {'type': 'heading', 'level': 3, 'text': 'Site Engineers'},
            {'type': 'table', 'columns': ['Item', 'Value'], 'rows': [['Civil engineers', '12'], ['', '']]},
        ]

        unnamed = [*blocks[:-2], {'type': 'heading', 'level': 3, 'text': ''}, blocks[-1]]
        self.assertEqual(self.client.put('/api/company', json={'blocks': unnamed}).json(),
                         {'error': 'Every heading and sub-heading needs a name. Name it, or remove it.'})
        response = self.client.put('/api/company', json={'blocks': blocks})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(company_details(self.config.COMPANY_DETAILS_FILE), (
            '# Company Profile\n\n| Item | Value |\n| --- | --- |\n| Company Name | ABC Infrastructure Ltd. |\n\n---\n\n'
            '## 2. Key Personnel\n\n### Site Engineers\n\n| Item | Value |\n| --- | --- |\n| Civil engineers | 12 |'))

    def test_table_columns_are_renamed_added_and_removed(self):
        blocks = self.client.get('/api/company').json()['blocks']
        table = blocks[1]
        table['columns'] = ['Detail', 'Value', '']
        table['rows'] = [row + ['Verified'] for row in table['rows']]

        self.assertEqual(self.client.put('/api/company', json={'blocks': blocks}).json(),
                         {'error': 'Every table column needs a name. Name it, or remove the column.'})
        table['columns'][2] = 'Status'
        blocks.append({'type': 'table', 'columns': ['Year', 'Turnover'], 'rows': [['FY26', '1710000000']]})
        response = self.client.put('/api/company', json={'blocks': blocks})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(company_details(self.config.COMPANY_DETAILS_FILE), (
            '# Company Eligibility Assessment (Sample Template)\n\n'
            '| Detail | Value | Status |\n| --- | --- | --- |\n| Company Name | ABC Infrastructure Ltd. | Verified |\n\n'
            '| Year | Turnover |\n| --- | --- |\n| FY26 | 1710000000 |'))

    def test_empty_company_profile_opens_a_starter_form(self):
        self.config.COMPANY_DETAILS_FILE.unlink()
        profile = self.client.get('/api/company').json()
        self.assertEqual((profile['details'], profile['name']), ('', 'No company profile'))
        self.assertEqual(profile['blocks'][1]['rows'], [['Company Name', '']])

    def test_upstream_failure_is_a_bad_gateway(self):
        with mock.patch('services.tender_service.tender_list', side_effect=ConnectionError('offline')):
            response = self.client.get('/api/tenders')
        self.assertEqual(response.status_code, 502)
        self.assertIn('offline', response.json()['error'])

    def test_filters_are_sent_as_the_tendertiger_search_page_sends_them(self):
        listed = self.client.get('/api/tenders', params={
            'word': 'Test', 'organisation': 'demo', 'location': 'kolkata', 'tid': '123456', 'industry': 'ok',
            'published_from': '2025-11-04', 'published_to': '2026-11-04',
            'closing_from': '2025-09-30', 'closing_to': '2026-11-04',
            'qty': '10', 'value': '20', 'emd_op': 'gte', 'emd': '30', 'gem': 'non_gem', 'bidding': 'icb',
            'funding': 'non_funded', 'msme': 'true', 'bid_doc': 'true', 'startup': 'true',
        })

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(tender_service.tender_list.call_args.args[0], {
            'wwordsearch': 'test', 'worgname': 'demo', 'wlocation': 'kolkata', 'wtenderrefno': '123456',
            'wcompanysubindustry': 'ok', 'wtenderhostdate': "2025-11-04' and '2026-11-04",
            'wtenderclosingdate': "2025-09-30' and '2026-11-04", 'qtyType': 'GreaterThan', 'qtyValue': '10',
            'valType': 'GreaterThan', 'tenderValue': '20', 'emdType': 'GreaterThanEqual', 'emdValue': '30',
            'gemtender': '0', 'wbiddingtype': 'ICB', 'wfundingAgency': 'NonFunded', 'wismse': 'ismse',
            'isotherdocument': 'isotherdocument', 'isstartup': '1',
        })

        # A range, and a date range open at one end
        self.client.get('/api/tenders', params={'value_op': 'between', 'value': '1e7', 'value_to': '50000000.50',
                                                'closing_from': '2026-11-10'})
        self.assertEqual(tender_service.tender_list.call_args.args[0], {
            'valType': 'Between', 'fromVal': '10000000', 'toVal': '50000000.5',
            'wtenderclosingdate': "2026-11-10' and '2099-12-31",
        })
        self.client.get('/api/tenders')
        self.assertEqual(tender_service.tender_list.call_args.args[0], {})

    def test_invalid_filters_are_refused_before_asking_tendertiger(self):
        tender_service.tender_list.reset_mock()
        for params, error in [
            ({'value_op': 'between', 'value': '5'}, 'Give both ends of the tender value range.'),
            ({'emd_op': 'between', 'emd': '9', 'emd_to': '2'}, 'The EMD range ends before it starts.'),
            ({'published_from': '2026-11-05', 'published_to': '2026-11-01'},
             'The published date range ends before it starts.'),
            ({'qty': '-1'}, 'query.qty: Input should be greater than or equal to 0'),
            ({'gem': 'maybe'}, "query.gem: Input should be 'gem' or 'non_gem'"),
            ({'colour': 'red'}, 'query.colour: Extra inputs are not permitted'),
        ]:
            response = self.client.get('/api/tenders', params=params)
            self.assertEqual((response.status_code, response.json()['error']), (400, error), params)
        tender_service.tender_list.assert_not_called()

    def test_the_list_is_paged(self):
        listed = self.client.get('/api/tenders', params={'location': 'patna', 'page': '2'}).json()

        self.assertEqual(tender_service.tender_list.call_args.args[0], {'wlocation': 'patna'})
        self.assertEqual(tender_service.tender_list.call_args.kwargs['page'], 2)
        self.assertEqual((listed['page'], listed['page_size'], listed['count']), (2, 20, 2))
        self.assertFalse(listed['has_more'])  # 2 of 20: the last page

        full = {'TenderList': [dict(SAMPLE_TENDERS['TenderList'][1], tcno=str(n)) for n in range(20)]}
        with mock.patch('services.tender_service.tender_list', return_value=full):
            self.assertTrue(self.client.get('/api/tenders').json()['has_more'])

        response = self.client.get('/api/tenders', params={'page': '0'})
        self.assertEqual((response.status_code, response.json()['error']),
                         (400, 'query.page: Input should be greater than or equal to 1'))

    def test_a_page_may_hold_20_50_100_or_500_tenders(self):
        listed = self.client.get('/api/tenders', params={'page_size': '100', 'page': '3'}).json()
        self.assertEqual(tender_service.tender_list.call_args.kwargs['page_size'], 100)
        self.assertEqual((listed['page'], listed['page_size'], listed['has_more']), (3, 100, False))

        tender_service.tender_list.reset_mock()
        response = self.client.get('/api/tenders', params={'page_size': '30'})
        self.assertEqual((response.status_code, response.json()['error']),
                         (400, 'Show 20, 50, 100 or 500 tenders a page.'))
        tender_service.tender_list.assert_not_called()

    def test_the_list_is_sorted_by_tendertiger(self):
        listed = self.client.get('/api/tenders').json()
        self.assertEqual(tender_service.tender_list.call_args.kwargs['sort'], ('closingdate', 'asc'))
        self.assertEqual((listed['sort'], listed['order']), ('closing', 'asc'))  # soonest closing first

        listed = self.client.get('/api/tenders', params={'sort': 'value', 'order': 'desc', 'page': '2',
                                                         'location': 'patna'}).json()
        call = tender_service.tender_list.call_args
        self.assertEqual((call.args[0], call.kwargs),
                         ({'wlocation': 'patna'}, {'page': 2, 'sort': ('tendervalue', 'desc'), 'page_size': 20}))
        self.assertEqual((listed['sort'], listed['order']), ('value', 'desc'))

        for params, error in [({'sort': 'name'}, "query.sort: Input should be 'closing' or 'value'"),
                              ({'order': 'up'}, "query.order: Input should be 'asc' or 'desc'")]:
            response = self.client.get('/api/tenders', params=params)
            self.assertEqual((response.status_code, response.json()['error']), (400, error))

    def test_a_tender_listed_under_other_filters_can_still_be_processed(self):
        self.client.get('/api/tenders')
        only_second = {'TenderList': SAMPLE_TENDERS['TenderList'][1:]}
        with mock.patch('services.tender_service.tender_list', return_value=only_second):
            self.assertEqual(self.client.get('/api/tenders', params={'location': 'patna'}).json()['count'], 1)

        self.assertEqual(self.client.post('/api/process-tenders', json={'tcnos': ['100091540']}).status_code, 202)
        self.wait_for_job()


if __name__ == '__main__':
    unittest.main()
