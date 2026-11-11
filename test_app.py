import copy
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import docx
import openpyxl
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from PIL import Image
from pptx import Presentation

from app import create_app
from config import Config
from ext import (NOT_RELEVANT, TENDERTIGER_BLOCKED, AnswerCutOff, TenderTigerBlocked, _whole_reply, company_details,
                 save_company_details)
from routes.api import BODY_ERRORS
from services import tender_service
from pinecone_fake import FakePinecone
from services.analysis_service import (RESULT_FILE, TENDER_DATA_SOURCE, TokenUsage, compare_routes, jv_verdict,
                                       parse_criteria, parse_jv, parse_score, parse_verdict, plain_maths,
                                       process_tender_files, recheck_saved_criteria, tender_data_text, tender_files)
from services.company_service import (company_profile, parse_profile, profile_for_tender, record_mark, render_profile,
                                      unnamed_part)
from services import deep_analysis_service as deep
from services.deep_analysis_service import (DEEP_ANALYSIS_FILE, LLM_AGENTS, MAX_ROUNDS, DeepAnalysisError,
                                            parse_amount, parse_financial_year, parse_json_reply, run_deep_analysis)
from services.download_service import DownloadError, download_tender_documents
from services.pinecone_service import Document, NamespaceIncomplete, PineconeService
from services.processing_service import explain_error, save_result

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


def counting_model(*replies):
    """A chat model answering each call with the next (content, input tokens, output tokens), reported as ChatGroq does."""
    return GenericFakeChatModel(messages=iter([
        AIMessage(content=content, response_metadata={'model_name': 'openai/gpt-oss-20b'},
                  usage_metadata={'input_tokens': sent, 'output_tokens': received, 'total_tokens': sent + received})
        for content, sent, received in replies
    ]))


def spend_tokens(usage, **steps):
    """Makes one LLM call for each step through usage, a TokenUsage, using the (input, output) tokens given."""
    for step, (sent, received) in steps.items():
        usage.track(counting_model(('reply', sent, received)), step).invoke('prompt')


class ParseTest(unittest.TestCase):
    def test_line_breaks_in_tender_text_never_reach_the_page_as_tags(self):
        row = tender_service.to_row({**SAMPLE_TENDERS['TenderList'][0], 'tenderrefno': 'gem/2026/b/8104997 <br> gem/2026/r/767291',
                                     'tendersbrief': 'Supply of\n lifts<BR/>and  pumps'})

        self.assertEqual(row['reference'], 'gem/2026/b/8104997, gem/2026/r/767291')
        self.assertEqual(row['name'], 'Supply of lifts and pumps')

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
        self.assertEqual(explain_error(RuntimeError('GROQ_API_KEY is not set.'))[0], 'The LLM API key is missing')
        title, action = explain_error(RuntimeError(TENDERTIGER_BLOCKED))
        self.assertEqual(title, 'TenderTiger is blocking automated visits for now')
        title, action = explain_error(RuntimeError('TenderTiger login failed: Invalid username or password.'))
        self.assertEqual(title, 'The TenderTiger login failed')
        self.assertIn('TENDERTIGER_PASSWORD', action)
        # Groq's 413 and 429, as the groq SDK words them
        title, action = explain_error(RuntimeError(
            "Error code: 413 - {'error': {'message': 'Request too large for model `openai/gpt-oss-120b` ... on tokens "
            "per minute (TPM): Limit 8000, Requested 14717', 'code': 'rate_limit_exceeded'}}"))
        self.assertEqual(title, 'The request is too large for the Groq plan')
        self.assertIn('Upgrade the Groq plan', action)
        title, action = explain_error(RuntimeError(
            "Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` ... on tokens "
            "per minute (TPM): Limit 8000, Used 6379, Requested 2636.', 'code': 'rate_limit_exceeded'}}"))
        self.assertEqual(title, "Groq's rate limit was reached")
        title, action = explain_error(AnswerCutOff("The model's reply was cut off at 16,384 tokens "
                                                   '(LLM_MAX_OUTPUT_TOKENS).'))
        self.assertEqual(title, "The model's answer was cut off")
        self.assertIn('LLM_MAX_OUTPUT_TOKENS', action)
        self.assertEqual(explain_error(ValueError('boom'))[0], 'The analysis failed')

    def test_a_reply_cut_off_at_the_token_limit_is_refused(self):
        whole = AIMessage(content='| Criterion | Status |', response_metadata={'finish_reason': 'stop'})
        self.assertIs(_whole_reply(whole), whole)
        with self.assertRaises(AnswerCutOff):
            _whole_reply(AIMessage(content='| Criterion | Sta', response_metadata={'finish_reason': 'length'}))

    def test_empty_company_profile_fails_before_any_llm_call(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'GROQ_API_KEY': 'key'}), \
                mock.patch('services.analysis_service.make_model') as make_model:
            with self.assertRaises(RuntimeError) as raised:
                process_tender_files(tmp, index=pinecone_service(), company_file=Path(tmp) / 'missing.md')
        make_model.assert_not_called()
        self.assertEqual(explain_error(raised.exception)[0], 'The company profile is empty')


def pinecone_service(client=None):
    """A PineconeService over an in-memory FakePinecone, with small chunks so short test texts still split."""
    return PineconeService(api_key='test-key', client=client or FakePinecone(), dimension=64, chunk_size=400,
                           chunk_overlap=50, freshness_timeout=1, settle_seconds=0)


class AnalysisTestCase(unittest.TestCase):
    """A tender folder, a company profile, a fake LLM and a fake Pinecone for process_tender_files()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / '100091540'
        self.folder.mkdir()
        self.company_file = Path(self.tmp.name) / 'company_details.md'
        save_company_details(COMPANY, self.company_file)
        self.llm = mock.Mock()
        self.llm.with_config.return_value = self.llm  # TokenUsage.track() binds the token counter to it
        self.pinecone = FakePinecone()
        self.index = pinecone_service(self.pinecone)
        for patch in (mock.patch.dict(os.environ, {'GROQ_API_KEY': 'key'}),
                      mock.patch('services.analysis_service.make_model', return_value=self.llm)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def process(self, **options):
        return process_tender_files(self.folder, index=self.index, company_file=self.company_file, **options)

    def recheck(self, **options):
        return recheck_saved_criteria('100091540', index=self.index, company_file=self.company_file, **options)

    def indexed(self):
        """{source: its text} of everything in the tender's index, in the order it was indexed."""
        return {section.split('\n', 1)[0][3:]: section.split('\n', 1)[1]
                for section in self.index.knowledge('100091540').sections()}

    def sources_sent(self):
        """The system message of the last assessment: the excerpts retrieved from the index."""
        return self.llm.invoke.call_args[0][0][0]['content']

    def task_sent(self):
        """The user message of the last assessment: the task, the tender's details and the company profile."""
        return self.llm.invoke.call_args[0][0][1]['content']


class PrequalificationTest(AnalysisTestCase):
    LINK = PREQUALIFICATION_LINK

    def analyse(self, prequalification):
        with mock.patch('services.analysis_service.prequalification_criteria', side_effect=prequalification):
            return self.process(prequalification_link=self.LINK)

    def test_pre_qualification_is_checked_as_written_beside_the_files(self):
        self.llm.invoke.return_value = mock.Mock(content=ASSESSMENT)

        result = self.analyse(['FINANCIAL CAPACITY:\n• Net worth of 5%'])

        self.assertIn('## TenderTiger Pre-Qualification\nFINANCIAL CAPACITY:\n• Net worth of 5%', self.sources_sent())
        self.assertEqual((result['verdict'], result['unreadable_files']), ('Not Eligible', []))
        self.assertEqual(list(self.indexed()), ['TenderTiger Pre-Qualification'])

    def test_pre_qualification_that_cannot_be_fetched_makes_the_check_incomplete(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')

        with self.assertLogs('services.analysis_service', 'WARNING'):
            result = self.analyse(ConnectionError('offline'))

        self.assertEqual(result['verdict'], 'Requires Review')
        self.assertEqual(result['unreadable_files'], ['TenderTiger Pre-Qualification'])

    def test_a_firewall_block_fails_the_tender_instead_of_skipping_the_pre_qualification(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')

        with self.assertRaises(TenderTigerBlocked):
            self.analyse(TenderTigerBlocked(TENDERTIGER_BLOCKED))
        self.llm.invoke.assert_not_called()
        self.assertEqual(self.pinecone.namespaces(), {})

    def test_no_files_and_no_pre_qualification_is_nothing_to_check(self):
        with self.assertRaises(RuntimeError) as raised:
            self.analyse([''])
        self.assertEqual(explain_error(raised.exception)[0], 'Nothing to check')

        with self.assertRaises(RuntimeError) as raised, self.assertLogs('services.analysis_service', 'WARNING'):
            self.analyse(ConnectionError('offline'))
        self.assertEqual(explain_error(raised.exception)[0], 'The pre-qualification criteria could not be fetched')
        self.llm.invoke.assert_not_called()


class IndexedCriteriaTest(AnalysisTestCase):
    """The tender's own Pinecone namespace, built from all its sources, and re-checks against it."""

    def test_every_source_is_indexed_and_a_recheck_retrieves_them_without_reading_the_documents(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        (self.folder / 'scan.txt').write_text('', encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')
        tender = SAMPLE_TENDERS['TenderList'][0]
        with mock.patch('services.analysis_service.prequalification_criteria', return_value='Net worth of 5%'):
            result = self.process(prequalification_link=PREQUALIFICATION_LINK, tender=tender)

        # One namespace for the tender, tcno-100091540, in the shared index, holding every source with its metadata
        self.assertEqual(list(self.pinecone.indexes), ['tender-processing'])
        self.assertEqual(list(self.pinecone.namespaces()), ['tcno-100091540'])
        self.assertEqual(list(self.indexed()), ['TenderTiger Tender Data', 'TenderTiger Pre-Qualification', 'NIT.txt'])
        self.assertIn('Tender value as listed on TenderTiger (not from the tender documents): INR 5,117,971,516.64',
                      self.indexed()['TenderTiger Tender Data'])
        chunk = next(meta for meta in self.pinecone.chunks('tcno-100091540').values() if meta['source_file'] == 'NIT.txt')
        self.assertEqual({key: chunk[key] for key in ('tcno', 'source_file', 'document_type')},
                         {'tcno': '100091540', 'source_file': 'NIT.txt', 'document_type': 'nit'})
        self.assertTrue(chunk['chunk_id'] and chunk['created_at'])
        self.assertEqual(result['index']['namespace'], 'tcno-100091540')
        self.assertEqual(result['index']['index'], 'tender-processing')
        self.assertEqual(result['unreadable_files'], ['scan.txt'])
        self.assertIn('## NIT.txt\nTurnover at least 5 Cr', self.sources_sent())

        # Re-checked: nothing is read or downloaded, the criteria come from the index, the unreadable source counts
        self.llm.reset_mock()
        with mock.patch('services.analysis_service.read_documents') as read, \
                mock.patch('services.analysis_service.prequalification_criteria') as prequalification:
            result = self.recheck()

        read.assert_not_called()
        prequalification.assert_not_called()
        self.llm.invoke.assert_called_once()
        sources = self.sources_sent()
        self.assertIn('## TenderTiger Pre-Qualification\nNet worth of 5%', sources)
        self.assertIn('## NIT.txt\nTurnover at least 5 Cr', sources)
        self.assertEqual((result['verdict'], result['unreadable_files']), ('Requires Review', ['scan.txt']))

    def test_the_tender_list_row_goes_to_the_assessment_in_place_of_its_indexed_copy(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 30% of the estimated cost', encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')
        tender = SAMPLE_TENDERS['TenderList'][0]
        self.process(tender=tender)

        task = self.task_sent()
        self.assertIn('Tender details, from the TenderTiger list:', task)
        for line in ('Organisation: North Western Railway', 'Closing date: 18-Nov-2026',
                     'Tender value as listed on TenderTiger (not from the tender documents): INR 5,117,971,516.64'):
            self.assertIn(line, task)
        self.assertLess(task.index('Tender details'), task.index('Company details:'))
        self.assertNotIn('## TenderTiger Tender Data', self.sources_sent())
        self.assertIn('## NIT.txt', self.sources_sent())

        # A re-check sends the list row as it is now (closing date moved, value gone), not the copy indexed earlier
        self.recheck(tender={**tender, 'closingdate': '02-12-2026', 'tendervalue': None})
        task = self.task_sent()
        self.assertIn('Closing date: 02-Dec-2026', task)
        self.assertIn('Tender value as listed on TenderTiger (not from the tender documents): not listed', task)
        self.assertNotIn('18-Nov-2026', task + self.sources_sent())

        # Without the list row, the copy indexed with the documents is still sent, as an excerpt
        self.recheck()
        self.assertIn('## TenderTiger Tender Data', self.sources_sent())
        self.assertNotIn('Tender details, from the TenderTiger list', self.task_sent())

    def test_result_and_deep_analysis_files_are_not_read_as_documents(self):
        (self.folder / 'NIT.txt').write_text('x', encoding='utf-8')
        (self.folder / RESULT_FILE).write_text('{}', encoding='utf-8')
        (self.folder / DEEP_ANALYSIS_FILE).write_text('{}', encoding='utf-8')
        self.assertEqual([path.name for path in tender_files(self.folder)], ['NIT.txt'])

    def test_sources_with_no_text_make_an_empty_index_and_no_criteria(self):
        (self.folder / 'scan.txt').write_text('', encoding='utf-8')
        result = self.process()
        self.assertEqual((result['verdict'], result['criteria'], result['unreadable_files']),
                         ('Requires Review', [], ['scan.txt']))
        self.assertIsNotNone(self.index.manifest('100091540'))

        result = self.recheck()
        self.assertEqual((result['verdict'], result['criteria']), ('Requires Review', []))
        self.llm.invoke.assert_not_called()

    def test_a_recheck_needs_a_complete_index(self):
        with self.assertRaises(NamespaceIncomplete) as raised:
            self.recheck()
        self.assertEqual(explain_error(raised.exception)[0], "The tender's Pinecone namespace is missing")
        self.llm.invoke.assert_not_called()

    def test_the_index_is_kept_when_the_final_check_fails(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        self.llm.invoke.side_effect = ConnectionError('timed out')
        with self.assertRaises(ConnectionError):
            self.process()
        self.assertIsNotNone(self.index.manifest('100091540'))

    def test_without_a_pinecone_key_the_check_fails_before_reading_anything(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        self.index = PineconeService(api_key=None)
        with mock.patch('services.analysis_service.read_documents') as read, self.assertRaises(RuntimeError) as raised:
            self.process()
        read.assert_not_called()
        self.assertEqual(explain_error(raised.exception)[0], 'The Pinecone API key is missing')

    def test_the_index_limit_is_explained(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        self.pinecone.max_namespaces = 0
        with mock.patch('services.analysis_service.read_documents') as read, self.assertRaises(RuntimeError) as raised:
            self.process()
        read.assert_not_called()  # before any file is read, so no OCR is paid for
        self.assertEqual(explain_error(raised.exception)[0], 'The Pinecone namespace limit is reached')


class FileSelectionTest(AnalysisTestCase):
    """The tender files that are not indexed: they hold no criteria, or are too long."""

    def test_a_tender_is_checked_without_the_statuses_marked_on_its_own_criteria(self):
        net_worth = {'criterion': 'Net worth', 'requirement': '20 Cr'}
        profile = record_mark(COMPANY, '100091540', net_worth, 'met', '2026-11-11')
        save_company_details(record_mark(profile, '102309372', net_worth, 'not_met', '2026-11-11'), self.company_file)
        (self.folder / 'NIT.txt').write_text('Net worth at least 20 Cr', encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')

        for check in (self.process, self.recheck):
            check()
            company = self.llm.invoke.call_args[0][0][1]['content'].split('Company details:\n', 1)[1]
            self.assertIn('| Net worth | 20 Cr | Not met | 102309372 | 2026-11-11 |', company)
            self.assertNotIn('100091540', company)

    def test_drawings_and_item_lists_are_not_read(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        (self.folder / 'Drawings1.txt').write_text('Plan of the bridge', encoding='utf-8')
        (self.folder / 'items.csv').write_text('Item Number,Item Title\n1,Salt', encoding='utf-8')
        (self.folder / 'BOQ_301578.txt').write_text('Estimated cost: 98 Cr', encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')

        result = self.process()

        self.assertEqual(self.indexed(), {'BOQ_301578.txt': 'Estimated cost: 98 Cr', 'NIT.txt': 'Turnover at least 5 Cr'})
        # Not read on purpose, so the check is still complete
        self.assertEqual((result['verdict'], result['unreadable_files']), ('Eligible', []))

    def test_word_powerpoint_and_excel_files_are_read(self):
        document = docx.Document()
        document.add_paragraph('Positive net worth')
        document.save(self.folder / 'NIT.docx')
        presentation = Presentation()
        presentation.slides.add_slide(presentation.slide_layouts[0]).shapes.title.text = 'Turnover at least 5 Cr'
        presentation.save(self.folder / 'Briefing.pptx')
        workbook = openpyxl.Workbook()
        workbook.active.append(['Estimated cost', '98 Cr'])
        workbook.save(self.folder / 'BOQ.xlsx')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')

        result = self.process(workers=3)

        indexed = self.indexed()
        self.assertEqual(list(indexed), ['BOQ.xlsx', 'Briefing.pptx', 'NIT.docx'])  # in file order, read in parallel
        for text, expected in zip(indexed.values(), ['Estimated cost', 'Turnover at least 5 Cr', 'Positive net worth']):
            self.assertIn(expected, text)
        self.assertEqual(result['unreadable_files'], [])

    def test_a_file_over_the_length_limit_is_listed_as_not_checked(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        (self.folder / 'DCA.txt').write_text('x' * 2001, encoding='utf-8')
        self.llm.invoke.return_value = mock.Mock(content='**Overall verdict:** Eligible')

        with self.assertLogs('services.analysis_service', 'WARNING'):
            result = self.process(max_file_chars=2000)

        self.assertEqual(list(self.indexed()), ['NIT.txt'])
        self.assertEqual(result['verdict'], 'Requires Review')
        self.assertEqual(result['unreadable_files'],
                         ['DCA.txt (too long: 2,001 characters; the MAX_FILE_CHARS limit is 2,000)'])
        self.assertEqual(self.index.manifest('100091540')['unreadable'], result['unreadable_files'])


class ScannedPdfTest(AnalysisTestCase):
    """A PDF that MarkItDown finds no text in, such as a scan, is read by OCR_MODEL with markitdown-ocr."""

    def setUp(self):
        super().setUp()
        # Two pages that are only images, with no text layer
        pages = [Image.new('RGB', (850, 1100), 'white') for _ in range(2)]
        pages[0].save(self.folder / 'scan.pdf', save_all=True, append_images=pages[1:])

    def analyse(self, *page_texts):
        """Checks the tender with the OCR of each page answered by page_texts in turn; an exception fails it."""
        replies = iter(page_texts)

        def invoke(messages):
            if isinstance(messages[0]['content'], list):  # a page image to read
                reply = next(replies)
                if isinstance(reply, Exception):
                    raise reply
                return mock.Mock(content=reply)
            return mock.Mock(content='**Overall verdict:** Eligible')

        self.llm.invoke.side_effect = invoke
        result = self.process()
        return result, list(self.indexed().values())

    def test_a_scanned_pdf_is_read_with_ocr(self):
        result, read = self.analyse('Turnover at least 5 Cr', 'Positive net worth')

        self.assertEqual(len(read), 1)
        self.assertIn('Turnover at least 5 Cr', read[0])
        self.assertIn('Positive net worth', read[0])
        self.assertEqual((result['verdict'], result['unreadable_files']), ('Eligible', []))
        image = self.llm.invoke.call_args_list[0].args[0][0]['content'][1]['image_url']['url']
        self.assertTrue(image.startswith('data:image/'))

    def test_a_scan_with_a_page_ocr_could_not_read_is_listed_as_not_checked(self):
        with self.assertLogs('services.analysis_service', 'WARNING') as logs:
            result, read = self.analyse('Turnover at least 5 Cr', ConnectionError('timed out'))

        self.assertIn('Could not OCR every page of scan.pdf: timed out', logs.output[0])
        self.assertEqual((read, result['unreadable_files']), ([], ['scan.pdf']))

    def test_a_scan_ocr_finds_no_text_in_is_listed_as_not_checked(self):
        result, read = self.analyse('', '')
        self.assertEqual((read, result['unreadable_files']), ([], ['scan.pdf']))


class ModelChoiceTest(AnalysisTestCase):
    """The final check runs on ASSESSMENT_MODEL, for a new index and for a re-check; nothing summarises any more."""

    def test_new_checks_and_rechecks_use_the_assessment_model(self):
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        models = {'summary-model': mock.Mock(), 'assessment-model': mock.Mock()}
        for llm in models.values():
            llm.with_config.return_value = llm
            llm.invoke.return_value = mock.Mock(content=ASSESSMENT)

        # Named here, so the test does not depend on the models set in .env
        with mock.patch.multiple('services.analysis_service', ASSESSMENT_MODEL='assessment-model',
                                 make_model=mock.Mock(side_effect=models.get)):
            self.process()
            models['assessment-model'].invoke.assert_called_once()
            self.recheck()
            self.assertEqual(models['assessment-model'].invoke.call_count, 2)
        models['summary-model'].invoke.assert_not_called()


class TokenUsageTest(AnalysisTestCase):
    """The LLM tokens of a tender, counted for reading its documents (OCR) and for its final check."""

    def test_tokens_are_counted_by_step(self):
        (self.folder / 'BOQ.txt').write_text('Item rates', encoding='utf-8')
        (self.folder / 'NIT.txt').write_text('Turnover at least 5 Cr', encoding='utf-8')
        usage = TokenUsage()

        with mock.patch('services.analysis_service.make_model', return_value=counting_model((ASSESSMENT, 500, 200))):
            result = self.process(usage=usage)

        self.assertEqual(result['verdict'], 'Not Eligible')
        # Text files are indexed, not summarised, so reading them costs no LLM tokens
        self.assertEqual(usage.counts(), {
            'criteria': {'input': 0, 'output': 0, 'total': 0},
            'assessment': {'input': 500, 'output': 200, 'total': 700},
            'total': {'input': 500, 'output': 200, 'total': 700},
        })

        usage = TokenUsage()
        with mock.patch('services.analysis_service.make_model', return_value=counting_model((ASSESSMENT, 300, 150))):
            self.recheck(usage=usage)
        self.assertEqual(usage.counts()['total'], {'input': 300, 'output': 150, 'total': 450})

    def test_tokens_spent_before_a_failure_are_still_counted(self):
        pages = [Image.new('RGB', (850, 1100), 'white')]
        pages[0].save(self.folder / 'scan.pdf')
        usage = TokenUsage()

        with mock.patch('services.analysis_service.make_model', return_value=counting_model(('Turnover 5 Cr', 1000, 40))), \
                mock.patch('services.analysis_service.assess_eligibility', side_effect=ConnectionError('timed out')), \
                self.assertRaises(ConnectionError):
            self.process(usage=usage)

        self.assertEqual(usage.counts()['criteria'], {'input': 1000, 'output': 40, 'total': 1040})
        self.assertEqual(usage.counts()['assessment']['total'], 0)


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

    def test_statuses_marked_by_hand_are_recorded_in_their_own_section(self):
        net_worth = {'criterion': 'Net worth', 'requirement': '20 Cr'}
        gst = {'criterion': 'GST | registration', 'requirement': ''}

        markdown = record_mark(COMPANY, '100091540', net_worth, 'met', '2026-11-11')
        markdown = record_mark(markdown, '102309372', gst, 'unknown', '2026-11-11')

        self.assertEqual(parse_profile(markdown), parse_profile(COMPANY) + [
            {'type': 'rule'},
            {'type': 'heading', 'level': 2, 'text': 'Criteria marked by the bid team'},
            {'type': 'table', 'columns': ['Criterion', 'Requirement', 'Status', 'TCNO', 'Marked on'], 'rows': [
                ['Net worth', '20 Cr', 'Met', '100091540', '2026-11-11'],
                ['GST | registration', '', 'Cannot determine', '102309372', '2026-11-11']]},
        ])
        # Marked again, under a renumbered heading: the row is replaced where it is
        markdown = record_mark(markdown.replace('## Criteria', '## 2. Criteria'), '100091540', net_worth, 'not_met',
                               '2026-11-12')
        self.assertEqual(parse_profile(markdown)[-1]['rows'],
                         [['Net worth', '20 Cr', 'Not met', '100091540', '2026-11-12'],
                          ['GST | registration', '', 'Cannot determine', '102309372', '2026-11-11']])
        # Each tender is checked without its own marks; a profile with none is left exactly as it is
        self.assertEqual(parse_profile(profile_for_tender(markdown, '100091540'))[-1]['rows'],
                         [['GST | registration', '', 'Cannot determine', '102309372', '2026-11-11']])
        self.assertEqual(profile_for_tender(COMPANY, '100091540'), COMPANY)

        # Back to the AI's status: the row goes, and the section with its last row
        markdown = record_mark(markdown, '100091540', net_worth, None, '2026-11-12')
        markdown = record_mark(markdown, '102309372', gst, None, '2026-11-12')
        self.assertEqual(parse_profile(markdown), parse_profile(COMPANY))
        self.assertEqual(record_mark(COMPANY, '100091540', net_worth, None, '2026-11-12'), COMPANY)

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
            DEFAULT_FILTERS_FILE = Path(self.tmp.name) / 'default_filters.json'

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
        self.pinecone = FakePinecone()
        self.index = pinecone_service(self.pinecone)
        self.client = TestClient(create_app(TestConfig, vector_store=self.index))

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

    def fake_analysis(self, folder, index=None, tcno=None, on_progress=None, company_file=None,
                      prequalification_link=None, tender=None, usage=None, max_file_chars=None, workers=None):
        self.company_file = company_file
        self.prequalification_link = prequalification_link
        self.max_file_chars = max_file_chars
        self.analysed_with = (index, tcno, tender['tcno'] if tender else None)
        time.sleep(self.analysis_delay)
        # As process_tender_files() does: fills the index the processor made empty before downloading
        index.ingest(tcno, [Document('NIT.pdf', 'Turnover 10 Cr', 'nit')], ['scan.pdf'])
        spend_tokens(usage, criteria=(1000, 40), assessment=(500, 200))
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

    def fake_recheck(self, tcno, index=None, on_progress=None, company_file=None, usage=None, tender=None):
        if index.manifest(tcno) is None:
            raise NamespaceIncomplete(f'Tender {tcno} has no complete Pinecone namespace')
        # A re-check is given the tender's row from the TenderTiger list, for its value, organisation and closing date
        if str((tender or {}).get('tcno')) != tcno:
            raise AssertionError(f'Re-check of {tcno} was not given its TenderTiger list row')
        self.rechecked.append(tcno)
        spend_tokens(usage, assessment=(300, 150))
        return {'verdict': 'Not Eligible', 'details': '## Result\nNet worth not met.', 'score': 0,
                'criteria': [{'criterion': 'Net worth', 'requirement': '20 Cr', 'company': '5 Cr',
                              'status': 'not_met', 'source': 'NIT.pdf'}],
                'unreadable_files': []}

    def index_tender(self, tcno):
        """Indexes a document for the tender, as an earlier run would have."""
        self.index.rebuild_namespace(tcno, [Document('NIT.pdf', 'Net worth 20 Cr', 'nit')])

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
        self.assertEqual(result['tokens'], {
            'criteria': {'input': 1000, 'output': 40, 'total': 1040},
            'assessment': {'input': 500, 'output': 200, 'total': 700},
            'total': {'input': 1500, 'output': 240, 'total': 1740},
        })
        failed = self.client.get('/api/result/102309372').json()
        self.assertEqual(failed['error'], 'Download failed: 404')
        self.assertEqual(failed['error_title'], 'The documents could not be downloaded')
        self.assertEqual(failed['tokens']['total'], {'input': 0, 'output': 0, 'total': 0})  # failed before any LLM call

        listed = self.client.get('/api/tenders').json()['tenders'][0]
        self.assertEqual(listed['result']['verdict'], 'Eligible')
        self.assertEqual(listed['result']['checks'], {
            'total': 2, 'met': 1, 'not_met': [], 'unknown': ['Net worth'], 'user_marked': 0, 'unreadable_files': 1,
            'unreadable_checked': 0,
            'jv': {'allowed': 'yes', 'max_members': 3, 'verdict': 'Requires Review',
                   'lead': {'total': 2, 'met': 1, 'not_met': [], 'unknown': ['Net worth']}}})
        self.assertEqual(result['jv']['conditions'], ['Lead member holds at least 51%'])
        self.assertEqual([row['lead_status'] for row in result['comparison']], ['met', 'unknown'])
        self.assertNotIn('details', listed['result'])
        self.assertIsNotNone(self.client.get('/api/progress').json()['average_seconds'])
        self.assertEqual(self.company_file, self.config.COMPANY_DETAILS_FILE)
        self.assertEqual(self.max_file_chars, self.config.MAX_FILE_CHARS)
        self.assertEqual(self.prequalification_link, PREQUALIFICATION_LINK)
        self.assertEqual(self.analysed_with, (self.index, '100091540', '100091540'))

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

    def test_indexed_tenders_are_rechecked_unless_regenerated(self):
        folder = self.config.TARGET_DIRECTORY / '100091540'
        folder.mkdir(parents=True)
        (folder / 'NIT.pdf').write_bytes(b'kept from the first run')
        self.index_tender('100091540')
        rows = self.client.get('/api/tenders').json()['tenders']
        self.assertEqual([row['has_indexed_criteria'] for row in rows], [True, False])
        self.assertNotIn('has_saved_criteria', rows[0])

        queued = self.client.post('/api/process-tenders', json={'tcnos': ['100091540']}).json()
        self.assertTrue(queued['items'][0]['rechecked'])
        progress = self.wait_for_job()

        result = self.client.get('/api/result/100091540').json()
        self.assertEqual(self.rechecked, ['100091540'])
        self.assertEqual((result['verdict'], result['rechecked'], result['files']), ('Not Eligible', True, ['NIT.pdf']))
        self.assertEqual(result['namespace'], 'tcno-100091540')
        self.assertNotIn('criteria_file', result)
        self.assertEqual((result['tokens']['criteria']['total'], result['tokens']['assessment']['total']), (0, 450))
        self.assertIsNotNone(progress['average_recheck_seconds'])
        self.assertIsNone(progress['average_seconds'])  # a quick re-check must not shorten full-run estimates

        self.client.post('/api/process-tenders', json={'tcnos': ['100091540'], 'regenerate': True})
        self.wait_for_job()

        result = self.client.get('/api/result/100091540').json()
        self.assertEqual(self.rechecked, ['100091540'])  # not re-checked again: the documents were analysed
        self.assertEqual((result['verdict'], result['rechecked']), ('Eligible', False))
        self.assertEqual(self.prequalification_link, PREQUALIFICATION_LINK)
        # Regenerating deleted the namespace before downloading again, then built it anew
        self.assertIn(('delete_namespace', 'tcno-100091540'), self.pinecone.calls)
        self.assertEqual(result['namespace'], 'tcno-100091540')
        self.assertEqual(list(self.pinecone.indexes), ['tender-processing'])  # one shared index throughout

    def test_a_failed_build_leaves_no_namespace_behind(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['102309372']})  # its download fails
        self.wait_for_job()

        self.assertIn(('upsert', 'tcno-102309372', 1), self.pinecone.calls)  # made before the download
        self.assertEqual(self.pinecone.namespaces(), {})
        result = self.client.get('/api/result/102309372').json()
        self.assertEqual((result['status'], result['namespace']), ('failed', None))

    def test_a_full_pinecone_plan_fails_the_tender_before_the_download(self):
        self.pinecone.max_namespaces = 0
        with mock.patch('services.processing_service.download_tender_documents') as download:
            self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
            self.wait_for_job()
        download.assert_not_called()
        result = self.client.get('/api/result/100091540').json()
        self.assertEqual(result['error_title'], 'The Pinecone namespace limit is reached')

    def test_many_tenders_share_one_index(self):
        tcnos = [str(number) for number in range(200, 230)]
        for tcno in tcnos:
            self.index_tender(tcno)
        self.assertEqual(list(self.pinecone.indexes), ['tender-processing'])
        self.assertEqual(len(self.pinecone.namespaces()), 30)

    def test_a_half_built_namespace_is_rebuilt_from_the_documents(self):
        self.index.reset_namespace('100091540')  # an earlier run failed before writing the complete manifest

        with self.assertLogs('services.processing_service', 'WARNING') as logs:
            self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
            self.wait_for_job()

        self.assertIn('left half-built', logs.output[0])
        result = self.client.get('/api/result/100091540').json()
        self.assertEqual((result['status'], result['verdict'], result['rechecked']), ('completed', 'Eligible', False))
        self.assertEqual(self.rechecked, [])

    def test_the_namespace_of_a_tender_can_be_inspected_and_deleted(self):
        self.assertEqual(self.client.get('/api/namespace/100091540').json()['exists'], False)
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        self.index_tender('100091540')
        self.index_tender('102309372')

        status = self.client.get('/api/namespace/100091540').json()
        self.assertEqual((status['index'], status['namespace'], status['exists'], status['complete']),
                         ('tender-processing', 'tcno-100091540', True, True))
        self.assertEqual([source['name'] for source in status['sources']], ['NIT.pdf'])

        deleted = self.client.delete('/api/namespace/100091540').json()
        self.assertEqual(deleted, {'tcno': '100091540', 'namespace': 'tcno-100091540', 'deleted': True})
        self.assertFalse(self.client.get('/api/namespace/100091540').json()['exists'])
        self.assertEqual([row['has_indexed_criteria'] for row in self.client.get('/api/tenders').json()['tenders']],
                         [False, True])  # the other tender's namespace is untouched
        self.assertIsNone(self.client.get('/api/result/100091540').json()['namespace'])
        self.assertFalse(self.client.delete('/api/namespace/100091540').json()['deleted'])

    def test_without_pinecone_the_list_loads_and_each_check_fails_clearly(self):
        client = TestClient(create_app(self.config, vector_store=PineconeService(api_key=None)))
        rows = client.get('/api/tenders').json()['tenders']
        self.assertEqual([row['has_indexed_criteria'] for row in rows], [False, False])

        client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        deadline = time.monotonic() + 5
        while client.get('/api/progress').json()['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.02)
        result = client.get('/api/result/100091540').json()
        self.assertEqual((result['status'], result['error_title']), ('failed', 'The Pinecone API key is missing'))
        self.assertEqual(client.get('/api/namespace/100091540').status_code, 503)

    def test_results_saved_with_a_criteria_file_load_without_it(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        path = self.config.TARGET_DIRECTORY / '100091540' / RESULT_FILE
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved.pop('namespace')
        saved['criteria_file'] = str(path.parent / 'eligibility_criteria.md')
        saved['index_name'] = 'tcno-100091540'  # saved by the version with an index per tender
        path.write_text(json.dumps(saved), encoding='utf-8')
        self.index_tender('100091540')

        restarted = TestClient(create_app(self.config, vector_store=self.index))
        result = restarted.get('/api/result/100091540').json()
        self.assertNotIn('criteria_file', result)
        self.assertNotIn('index_name', result)
        self.assertEqual(result['namespace'], 'tcno-100091540')

    def test_a_criterion_status_can_be_marked_by_hand_and_back(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()

        response = self.client.put('/api/result/100091540/criteria/1', json={'status': 'not_met'})

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['criteria'][1], {'criterion': 'Net worth', 'requirement': '20 Cr', 'company': '',
                                                 'status': 'not_met', 'source': 'NIT.pdf', 'ai_status': 'unknown',
                                                 'marked_by_user': True})
        # The verdict follows the statuses now; the model's is kept
        self.assertEqual((result['verdict'], result['ai_verdict'], result['score']), ('Not Eligible', 'Eligible', 50))
        self.assertEqual((result['checks']['not_met'], result['checks']['user_marked']), (['Net worth'], 1))
        self.assertEqual(result['comparison'][1]['alone_status'], 'not_met')
        listed = self.client.get('/api/tenders').json()['tenders'][0]['result']
        self.assertEqual((listed['verdict'], listed['checks']['user_marked']), ('Not Eligible', 1))
        self.assertEqual(self.client.get('/api/progress').json()['items'][0]['verdict'], 'Not Eligible')
        restarted = TestClient(create_app(self.config, vector_store=self.index))
        self.assertEqual(restarted.get('/api/result/100091540').json(), result)
        # Recorded in the company profile too, for the tenders checked from now on
        self.assertEqual(parse_profile(company_details(self.config.COMPANY_DETAILS_FILE))[-1]['rows'],
                         [['Net worth', '20 Cr', 'Not met', '100091540', date.today().isoformat()]])
        self.assertIn('Not met', self.client.get('/api/company').json()['details'])

        result = self.client.put('/api/result/100091540/criteria/1', json={'status': None}).json()

        self.assertEqual((result['criteria'][1]['status'], result['verdict']), ('unknown', 'Eligible'))
        self.assertNotIn('marked_by_user', result['criteria'][1])
        self.assertNotIn('ai_verdict', result)
        self.assertNotIn('marked by the bid team', company_details(self.config.COMPANY_DETAILS_FILE))
        self.assertEqual(self.client.put('/api/result/100091540/criteria/2', json={'status': 'met'}).status_code, 404)
        self.assertEqual(self.client.put('/api/result/999/criteria/0', json={'status': 'met'}).status_code, 404)
        refused = self.client.put('/api/result/100091540/criteria/0', json={'status': 'maybe'})
        self.assertEqual((refused.status_code, refused.json()['error']), (400, BODY_ERRORS['mark_criterion']))

    def test_tender_files_open_in_the_browser_or_download(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        folder = self.config.TARGET_DIRECTORY / '100091540'
        (folder / 'BOQ part').mkdir()
        (folder / 'BOQ part' / 'BOQ 1.xls').write_bytes(b'sheet')
        (folder / 'page.html').write_bytes(b'<script>alert(1)</script>')

        pdf = self.client.get('/api/files/100091540/NIT.pdf')
        self.assertEqual((pdf.status_code, pdf.content), (200, b'x'))
        self.assertEqual(pdf.headers['content-type'], 'application/pdf')
        self.assertTrue(pdf.headers['content-disposition'].startswith('inline'))
        sheet = self.client.get('/api/files/100091540/BOQ%20part/BOQ%201.xls')
        self.assertEqual(sheet.content, b'sheet')
        self.assertTrue(sheet.headers['content-disposition'].startswith('attachment'))
        # A web page among the files is downloaded, never shown as part of the app
        self.assertTrue(self.client.get('/api/files/100091540/page.html').headers['content-disposition'].startswith('attachment'))
        for name in (RESULT_FILE, 'missing.pdf', '..%2F..%2Fcompany_details.md'):
            self.assertEqual(self.client.get(f'/api/files/100091540/{name}').status_code, 404, name)

    def test_files_the_ai_could_not_read_can_be_checked_by_hand(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        result = self.client.put('/api/result/100091540/criteria/1', json={'status': 'met'}).json()
        # Every criterion is met, but scan.pdf was never read
        self.assertEqual((result['score'], result['verdict']), (100, 'Requires Review'))

        result = self.client.put('/api/result/100091540/unreadable-checked', json={'checked': True}).json()

        self.assertEqual((result['verdict'], result['unreadable_checked']), ('Eligible', ['scan.pdf']))
        self.assertEqual((result['checks']['unreadable_files'], result['checks']['unreadable_checked']), (1, 1))
        self.assertEqual(result['jv']['verdict'], 'Requires Review')  # the lead member's net worth is still undetermined

        # Regenerated, scan.pdf is unreadable again: the check stands, like the mark
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540'], 'regenerate': True})
        self.wait_for_job()
        result = self.client.get('/api/result/100091540').json()
        self.assertEqual((result['verdict'], result['unreadable_checked']), ('Eligible', ['scan.pdf']))

        result = self.client.put('/api/result/100091540/unreadable-checked', json={'checked': False}).json()
        self.assertEqual(result['verdict'], 'Requires Review')
        self.assertNotIn('unreadable_checked', result)
        refused = self.client.put('/api/result/100091540/unreadable-checked', json={})
        self.assertEqual((refused.status_code, refused.json()['error']), (400, BODY_ERRORS['check_unreadable']))

    def test_marked_statuses_are_kept_by_a_recheck_and_locked_while_it_runs(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        self.client.put('/api/result/100091540/criteria/1', json={'status': 'met'})
        self.index_tender('100091540')

        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()

        # The re-check says Net worth is not met; the mark by hand stands, and decides the verdict
        result = self.client.get('/api/result/100091540').json()
        self.assertEqual(result['criteria'], [{'criterion': 'Net worth', 'requirement': '20 Cr', 'company': '5 Cr',
                                               'status': 'met', 'source': 'NIT.pdf', 'ai_status': 'not_met',
                                               'marked_by_user': True}])
        self.assertEqual((result['verdict'], result['ai_verdict'], result['score']), ('Eligible', 'Not Eligible', 100))

        self.analysis_delay = 0.5
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540'], 'regenerate': True})
        self.wait_for(lambda progress: progress['items'][0]['status'] == 'processing')
        self.assertEqual(self.client.put('/api/result/100091540/criteria/0', json={'status': None}).status_code, 409)
        self.wait_for_job()

    def test_results_are_saved_in_the_tender_folder_and_survive_a_restart(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
        self.wait_for_job()
        before = {tcno: self.client.get(f'/api/result/{tcno}').json() for tcno in ('100091540', '102309372')}

        for tcno, result in before.items():
            saved = (self.config.TARGET_DIRECTORY / tcno / RESULT_FILE).read_text(encoding='utf-8')
            self.assertEqual(json.loads(saved), result)
        self.assertEqual(before['100091540']['tokens']['total']['total'], 1740)  # saved with the result
        broken = self.config.TARGET_DIRECTORY / '999' / RESULT_FILE
        broken.parent.mkdir()
        broken.write_text('{"tcno": "999"', encoding='utf-8')

        with self.assertLogs('services.processing_service', 'WARNING') as logs:
            restarted = TestClient(create_app(self.config, vector_store=self.index))

        self.assertIn(str(broken), logs.output[0])
        for tcno, result in before.items():
            self.assertEqual(restarted.get(f'/api/result/{tcno}').json(), result)
        rows = restarted.get('/api/tenders').json()['tenders']
        self.assertEqual([row['result']['status'] for row in rows], ['completed', 'failed'])
        self.assertEqual(rows[0]['result']['checks']['unknown'], ['Net worth'])
        self.assertIsNotNone(restarted.get('/api/progress').json()['average_seconds'])

    def closed_downloads(self, today, client=None):
        """GET /api/closed-downloads as if today were the given date."""
        with mock.patch('services.processing_service.date', wraps=date) as fake_date:
            fake_date.today.return_value = today
            return (client or self.client).get('/api/closed-downloads').json()

    def saved_result(self, tcno):
        return json.loads((self.config.TARGET_DIRECTORY / tcno / RESULT_FILE).read_text(encoding='utf-8'))

    def test_closed_tenders_whose_documents_are_still_downloaded_are_listed(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
        self.wait_for_job()
        self.assertEqual(self.saved_result('100091540')['closing_date'], '2026-11-18')
        downloads = self.config.TARGET_DIRECTORY
        # Documents with no result, so no closing date on record
        (downloads / '555').mkdir()
        (downloads / '555' / 'old.pdf').write_bytes(b'12345')
        # A download's staging folder and a tender folder holding only its result are not listed
        (downloads / 'tmp1x2y').mkdir()
        (downloads / 'tmp1x2y' / 'all.zip').write_bytes(b'x')
        (downloads / '777').mkdir()
        (downloads / '777' / RESULT_FILE).write_text('{}', encoding='utf-8')

        data = self.closed_downloads(date(2026, 11, 20))
        self.assertEqual(data['directory'], str(downloads))
        self.assertEqual(data['closed'], [{
            'tcno': '100091540', 'name': 'Providing Project General Management Services',
            'closing_date': '18-Nov-2026', 'days_closed': 2, 'files': 1, 'bytes': 1,
        }])
        self.assertEqual(data['unknown'], [{'tcno': '555', 'name': '', 'closing_date': None, 'days_closed': None,
                                            'files': 1, 'bytes': 5}])
        # Still open on its closing date
        self.assertEqual(self.closed_downloads(date(2026, 11, 18))['closed'], [])

    def test_a_listed_tender_keeps_its_closing_date_in_its_result(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        moved = copy.deepcopy(SAMPLE_TENDERS)
        moved['TenderList'][0]['closingdate'] = '25-11-2026'
        with mock.patch('services.tender_service.tender_list', return_value=moved):
            self.client.get('/api/tenders')

        self.assertEqual(self.saved_result('100091540')['closing_date'], '2026-11-25')
        self.assertEqual(self.client.get('/api/result/100091540').json()['closing_date'], '2026-11-25')
        self.assertEqual(self.closed_downloads(date(2026, 11, 20))['closed'], [])

    def test_a_closing_date_missing_from_an_old_result_is_read_from_pinecone(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        # A result saved before closing dates were, of a tender whose TenderTiger data is indexed
        result = self.saved_result('100091540')
        del result['closing_date']
        save_result(self.config.TARGET_DIRECTORY / '100091540', result)
        tender = SAMPLE_TENDERS['TenderList'][0]
        self.index.rebuild_namespace('100091540', [Document(TENDER_DATA_SOURCE, tender_data_text(tender), 'tender_data')])
        restarted = TestClient(create_app(self.config, vector_store=self.index))

        closed = self.closed_downloads(date(2026, 11, 20), restarted)['closed']
        self.assertEqual([(row['tcno'], row['closing_date']) for row in closed], [('100091540', '18-Nov-2026')])
        self.assertEqual(self.saved_result('100091540')['closing_date'], '2026-11-18')

    def test_everything_kept_for_a_closed_tender_can_be_deleted_but_its_profile_marks(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        self.client.put('/api/result/100091540/criteria/1', json={'status': 'met'})
        self.client.put('/api/decision/100091540', json={'decision': 'shortlisted'})
        self.index_tender('102309372')
        folder = self.config.TARGET_DIRECTORY / '100091540'
        (folder / 'unpacked').mkdir()
        (folder / 'unpacked' / 'BOQ.xlsx').write_bytes(b'xyz')
        (folder / DEEP_ANALYSIS_FILE).write_text('{}', encoding='utf-8')
        profile = self.config.COMPANY_DETAILS_FILE.read_text(encoding='utf-8')
        self.assertIn('100091540', profile)  # the mark is recorded in the company profile

        deleted = self.client.delete('/api/downloads/100091540')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {'tcno': '100091540', 'files': 2, 'bytes': 4, 'namespace_deleted': True})
        self.assertFalse(folder.exists())
        self.assertEqual(self.client.get('/api/result/100091540').status_code, 404)
        self.assertEqual(self.client.get('/api/deep-analysis/100091540').json()['status'], 'none')
        self.assertFalse(self.client.get('/api/namespace/100091540').json()['exists'])
        self.assertTrue(self.client.get('/api/namespace/102309372').json()['exists'])  # another tender's is untouched
        row = self.client.get('/api/tenders').json()['tenders'][0]
        self.assertEqual((row['tcno'], row['result'], row['decision'], row['has_indexed_criteria']),
                         ('100091540', None, None, False))
        # The last job no longer shows its verdict after a reload
        self.assertEqual([item['status'] for item in self.client.get('/api/progress').json()['items']], ['deleted'])
        self.assertEqual(self.config.COMPANY_DETAILS_FILE.read_text(encoding='utf-8'), profile)
        restarted = TestClient(create_app(self.config, vector_store=self.index))
        self.assertEqual(restarted.get('/api/result/100091540').status_code, 404)
        self.assertIsNone(restarted.get('/api/tenders').json()['tenders'][0]['decision'])
        self.assertEqual(self.client.delete('/api/downloads/100091540').status_code, 404)

        # Documents with no result
        (self.config.TARGET_DIRECTORY / '555').mkdir()
        (self.config.TARGET_DIRECTORY / '555' / 'old.pdf').write_bytes(b'12345')
        self.assertEqual(self.client.delete('/api/downloads/555').json()['files'], 1)
        self.assertFalse((self.config.TARGET_DIRECTORY / '555').exists())

    def test_a_tender_cannot_be_deleted_while_it_is_checked_or_a_file_is_open(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        self.analysis_delay = 0.5
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540'], 'regenerate': True})
        self.wait_for(lambda progress: progress['items'][0]['status'] == 'processing')
        self.assertEqual(self.client.delete('/api/downloads/100091540').status_code, 409)
        self.wait_for_job()
        runs = self.client.app.state.deep_analyses._runs
        runs['100091540'] = {'status': 'running'}
        self.assertIn('deep analysis', self.client.delete('/api/downloads/100091540').json()['error'])
        runs.clear()

        with mock.patch('shutil.rmtree', side_effect=PermissionError('NIT.pdf is in use')):
            locked = self.client.delete('/api/downloads/100091540')
        self.assertEqual(locked.status_code, 409)
        self.assertIn('NIT.pdf is in use', locked.json()['error'])
        self.assertEqual(self.client.get('/api/result/100091540').status_code, 200)

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

            # A tender with a Pinecone namespace is re-checked without TenderTiger, so it still runs
            self.index_tender('102309372')
            self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
            progress = self.wait_for_job()

        self.assertEqual([item['status'] for item in progress['items']], ['failed', 'completed'])
        self.assertEqual(self.rechecked, ['102309372'])

    def test_decisions_are_saved_and_survive_a_restart(self):
        response = self.client.put('/api/decision/100091540', json={'decision': 'dropped'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['decision']['decision'], 'dropped')

        restarted = TestClient(create_app(self.config, vector_store=self.index))
        rows = restarted.get('/api/tenders').json()['tenders']
        self.assertEqual(rows[0]['decision']['decision'], 'dropped')
        self.assertIsNone(rows[1]['decision'])

        self.assertIsNone(restarted.put('/api/decision/100091540', json={'decision': None}).json()['decision'])
        self.assertIsNone(restarted.get('/api/tenders').json()['tenders'][0]['decision'])
        self.assertEqual(restarted.put('/api/decision/100091540', json={'decision': 'maybe'}).status_code, 400)
        self.assertEqual(restarted.put('/api/decision/100091540', json={}).status_code, 400)
        self.assertEqual(restarted.put('/api/decision/999', json={'decision': 'dropped'}).status_code, 404)

    def test_the_profile_gap_report_counts_this_month_s_cannot_determine_reasons(self):
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()

        report = self.client.get('/api/profile-gaps').json()
        self.assertEqual((report['month'], report['tenders'], report['criteria'], report['cannot_determine']),
                         (f'{datetime.now(timezone.utc):%Y-%m}', 1, 2, 1))
        self.assertEqual([(reason['key'], reason['criteria'], reason['profile'], reason['examples'])
                          for reason in report['reasons']], [('financial', 1, 'missing', ['Net worth'])])

        response = self.client.get('/api/profile-gaps/file')
        self.assertTrue(response.headers['content-type'].startswith('text/markdown'))
        self.assertIn(f'filename="profile-gaps-{report["month"]}.md"', response.headers['content-disposition'])
        self.assertIn('| 1 | Turnover, net worth, profit, solvency and tax returns | 1 (100%) | 1 |', response.text)

        self.assertEqual(self.client.get('/api/profile-gaps', params={'month': '2020-01'}).json()['tenders'], 0)
        self.assertEqual(self.client.get('/api/profile-gaps', params={'month': '2026-13'}).status_code, 400)

    def test_default_filters_come_from_the_json_file_as_query_parameters(self):
        path = self.config.DEFAULT_FILTERS_FILE
        response = self.client.get('/api/default-filters')
        self.assertEqual(response.status_code, 404)
        self.assertIn('default_filters.json', response.json()['error'])

        path.write_text(json.dumps({'word': 'civil', 'closing_from': 'today', 'published_from': 'today-7',
                                    'value': 5000000, 'emd_op': 'lt', 'bid_doc': True, 'msme': False,
                                    'gem': 'non_gem'}), encoding='utf-8')
        filters = self.client.get('/api/default-filters').json()
        # Strings, as the form sends them; an unset flag and a comparison without its amount are left out, and an
        # amount always comes with its comparison
        self.assertEqual(filters, {'word': 'civil', 'closing_from': date.today().isoformat(),
                                   'published_from': (date.today() - timedelta(days=7)).isoformat(),
                                   'value': '5000000', 'value_op': 'gt', 'bid_doc': 'true', 'gem': 'non_gem'})
        # What it returns is what the tender list takes
        self.assertEqual(self.client.get('/api/tenders', params=filters).status_code, 200)

        for content, problem in (('{"wrod": "civil"}', 'wrod'), ('{"closing_from": "soon"}', 'closing_from'),
                                 ('["civil"]', 'one JSON object'), ('{"word": ', 'Could not read')):
            path.write_text(content, encoding='utf-8')
            response = self.client.get('/api/default-filters')
            self.assertEqual(response.status_code, 500)
            self.assertIn(problem, response.json()['error'])

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

    # ---------- deep analysis ----------

    def deep_agents(self, **replies):
        """Answers the deep analysis agents with DEEP_REPLIES, or the replies given, for the rest of the test."""
        agents = ScriptedAgents(**{**DEEP_REPLIES, **replies})
        for patch in (mock.patch('services.deep_analysis_service.make_model', return_value=agents),
                      mock.patch.dict(os.environ, {'GROQ_API_KEY': 'key'})):
            patch.start()
            self.addCleanup(patch.stop)
        return agents

    def wait_for_deep(self, tcno, status='completed'):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.client.get(f'/api/deep-analysis/{tcno}').json()
            if state['status'] == status:
                return state
            time.sleep(0.02)
        self.fail(f'the deep analysis did not reach {status}')

    def test_a_deep_analysis_reviews_the_result_and_is_saved_beside_it(self):
        agents = self.deep_agents()
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        self.client.put('/api/result/100091540/criteria/1', json={'status': 'met'})  # Net worth: the AI said unknown
        self.client.put('/api/decision/100091540', json={'decision': 'shortlisted'})
        self.assertEqual(self.client.get('/api/deep-analysis/100091540').json()['status'], 'none')

        response = self.client.post('/api/deep-analysis/100091540')

        self.assertEqual(response.status_code, 202)
        self.assertIn(response.json()['status'], ('queued', 'running', 'completed'))
        state = self.wait_for_deep('100091540')
        report = state['analysis']
        self.assertFalse(state['stale'])
        self.assertEqual(agents.agents(), ['profiler', 'audit', 'status_review', 'supervisor'])
        # The audit judges afresh: it is never shown the statuses, the marks or the verdict
        audit_prompt = agents.calls[1][1][1]['content']
        self.assertNotIn('marked by user', audit_prompt.lower())
        self.assertNotIn('Criteria marked by the bid team', audit_prompt)  # nor in the company profile
        self.assertIn('[1] Net worth', audit_prompt)
        # The reviewer sees the mark, the AI's status and the decision
        review_prompt = agents.calls[2][1][1]['content']
        self.assertIn('Met, marked by user; the AI said Cannot determine', review_prompt)
        self.assertIn("The bid team's decision: shortlisted", review_prompt)

        self.assertEqual(report['final']['determination'], 'Requires Review')
        self.assertEqual((report['final']['current_verdict'], report['final']['agrees_with_current']),
                         ('Requires Review', True))
        net_worth = report['criteria'][1]
        self.assertEqual((net_worth['current_status'], net_worth['ai_status'], net_worth['marked_by_user'],
                          net_worth['audit_result'], net_worth['final_status'], net_worth['conflict'],
                          net_worth['bias_risk']),
                         ('met', 'unknown', True, 'insufficient_data', 'unknown', True, 'high'))
        self.assertEqual(report['inputs']['unreadable'], ['scan.pdf'])
        self.assertEqual(report['inputs']['decision'], 'shortlisted')
        self.assertEqual([entry['agent'] for entry in report['audit_log']], list(deep.AGENTS))

        folder = self.config.TARGET_DIRECTORY / '100091540'
        self.assertEqual(json.loads((folder / DEEP_ANALYSIS_FILE).read_text(encoding='utf-8')), report)
        # Saved beside the result, which it leaves as it was, and never listed as one of the tender's documents
        self.assertEqual(self.client.get('/api/result/100091540').json()['criteria'][1]['status'], 'met')
        self.assertEqual(self.client.get(f'/api/files/100091540/{DEEP_ANALYSIS_FILE}').status_code, 404)
        download = self.client.get('/api/deep-analysis/100091540/file')
        self.assertEqual(download.json(), report)
        self.assertTrue(download.headers['content-disposition'].startswith('attachment'))

        # It survives a restart, and goes stale once the result it reviewed changes
        restarted = TestClient(create_app(self.config, vector_store=self.index))
        self.assertEqual(restarted.get('/api/deep-analysis/100091540').json()['analysis'], report)
        self.client.put('/api/result/100091540/criteria/1', json={'status': None})
        self.assertTrue(self.client.get('/api/deep-analysis/100091540').json()['stale'])

    def test_a_deep_analysis_needs_a_completed_result_and_runs_once_at_a_time(self):
        agents = self.deep_agents()
        self.assertEqual(self.client.post('/api/deep-analysis/100091540').status_code, 404)
        self.assertEqual(self.client.get('/api/deep-analysis/100091540/file').status_code, 404)
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540', '102309372']})
        self.wait_for_job()
        self.assertEqual(self.client.post('/api/deep-analysis/102309372').status_code, 404)  # its check failed

        agents.delay = 0.3
        self.client.post('/api/deep-analysis/100091540')
        refused = self.client.post('/api/deep-analysis/100091540')
        self.assertEqual((refused.status_code, refused.json()['error']),
                         (409, 'A deep analysis of tender 100091540 is already running.'))
        running = self.wait_for_deep('100091540', 'running')
        self.assertEqual(running['progress']['stage'][:len('Company Profiler agent: ')], 'Company Profiler agent: ')
        self.wait_for_deep('100091540')

        self.analysis_delay = 0.5
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540'], 'regenerate': True})
        self.wait_for(lambda progress: progress['items'][0]['status'] == 'processing')
        self.assertEqual(self.client.post('/api/deep-analysis/100091540').status_code, 409)
        self.wait_for_job()

    def test_a_failed_deep_analysis_keeps_the_last_report(self):
        agents = self.deep_agents()
        self.client.post('/api/process-tenders', json={'tcnos': ['100091540']})
        self.wait_for_job()
        self.client.post('/api/deep-analysis/100091540')
        report = self.wait_for_deep('100091540')['analysis']

        agents.replies['supervisor'] = [ConnectionError('api.groq.com timed out')]
        with self.assertLogs('services.deep_analysis_service', 'ERROR'):
            self.client.post('/api/deep-analysis/100091540')
            state = self.wait_for_deep('100091540', 'failed')

        self.assertEqual((state['error'], state['error_title']), ('api.groq.com timed out', 'The deep analysis failed'))
        self.assertEqual(state['analysis'], report)


# The deep analysis agents' answers for ApiTest's tender: Turnover met, Net worth undetermined
DEEP_REPLIES = {
    'profiler': [{'flags': [{'field': 'Net worth', 'issue': 'missing', 'detail': 'The profile gives no net worth.',
                             'needed_for': 'Net worth', 'severity': 'high', 'verify_with': 'Audited balance sheet'}],
                  'reliable_fields': ['Company Name'], 'summary': 'Net worth is missing.'}],
    'audit': [{'rules': [{'ref': 0, 'criterion': 'Turnover', 'result': 'pass', 'reason': '171 Cr ≥ 10 Cr'},
                         {'ref': 1, 'criterion': 'Net worth', 'result': 'insufficient_data', 'reason': 'Not stated.',
                          'missing': 'Net worth figure'}],
               'summary': 'Net worth decides it.'}],
    'status_review': [{'reviews': [{'ref': 1, 'criterion': 'Net worth', 'agreement': 'disagrees', 'bias_risk': 'high',
                                    'finding': 'No figure supports Met.', 'possible_reasons': ['Knows the figure'],
                                    'recommendation': 'verify'}],
                       'overrides': [], 'summary': 'One mark lacks evidence.'}],
    'supervisor': [{'criteria': [{'ref': 0, 'criterion': 'Turnover', 'final_status': 'met', 'confidence': 0.95},
                                 {'ref': 1, 'criterion': 'Net worth', 'final_status': 'unknown', 'confidence': 0.8,
                                  'resolution': 'The mark has no figure behind it.'}],
                    'determination': 'Requires Review', 'confidence': 0.8, 'summary': 'Net worth is not stated.',
                    'deciding_criteria': ['Net worth'],
                    'data_gaps': [{'gap': 'Net worth', 'needed_for': 'Net worth', 'how_to_close': 'Add it',
                                   'severity': 'high'}],
                    'recheck': []}],
}


class ScriptedAgents:
    """
    A chat model answering each deep analysis agent, told apart by its system prompt, with the next of its replies:
    a dict is sent as JSON, a string as it is, an exception is raised. An agent's last reply repeats.
    """
    TITLES = {'Company Profiler': 'profiler', 'Eligibility Audit': 'audit',
              'Discrepancy & Status Review': 'status_review', 'Supervisor / Judge': 'supervisor'}

    def __init__(self, **replies):
        self.replies = {agent: list(answers) for agent, answers in replies.items()}
        self.calls = []  # (agent, messages)
        self.delay = 0

    def with_config(self, **kwargs):
        return self

    def invoke(self, messages):
        agent = next(key for title, key in self.TITLES.items() if f'You are the {title} agent' in messages[0]['content'])
        self.calls.append((agent, messages))
        time.sleep(self.delay)
        answers = self.replies[agent]
        reply = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(reply, Exception):
            raise reply
        return AIMessage(content=reply if isinstance(reply, str) else json.dumps(reply))

    def agents(self):
        return [agent for agent, _ in self.calls]


class DeepAnalysisTest(unittest.TestCase):
    """The five agents of a deep analysis, run over a result with a scripted model."""

    RESULT = {
        'tcno': '100091540', 'name': 'ROB', 'status': 'completed', 'verdict': 'Eligible', 'ai_verdict': 'Not Eligible',
        'processed_at': '2026-11-05T10:00:00+00:00', 'unreadable_files': [], 'jv': None,
        'criteria': [
            {'criterion': 'Turnover', 'requirement': '10 Cr', 'company': '171 Cr', 'status': 'met', 'source': 'NIT.pdf'},
            {'criterion': 'Similar work', 'requirement': '1 work of 40 Cr', 'company': '', 'status': 'met',
             'source': 'NIT.pdf', 'ai_status': 'not_met', 'marked_by_user': True},
        ],
    }
    COMPANY = COMPANY + '\n\n## Finance\n\n| Year | Turnover |\n|---|---|\n| FY26 | 1710000000 |'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.index = pinecone_service()
        self.index.rebuild_namespace('100091540',
                                     [Document('NIT.pdf', 'Turnover 10 Cr; one similar work of 40 Cr.', 'nit')])
        patch = mock.patch.dict(os.environ, {'GROQ_API_KEY': 'key'})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_agents(self, result=None, **replies):
        agents = ScriptedAgents(**{**self.replies(), **replies})
        with mock.patch('services.deep_analysis_service.make_model', return_value=agents):
            result = result or self.RESULT
            report = run_deep_analysis(result, self.knowledge(result), self.COMPANY,
                                       today=tender_service.date(2026, 11, 6))
        return report, agents

    def knowledge(self, result):
        return deep.load_knowledge(self.index, '100091540', result)

    @staticmethod
    def replies():
        return {
            'profiler': [{'flags': [], 'summary': 'Complete.'}],
            'audit': [{'rules': [{'ref': 0, 'criterion': 'Turnover', 'result': 'pass', 'reason': '171 Cr ≥ 10 Cr'},
                                 {'ref': 1, 'criterion': 'Similar work', 'result': 'fail',
                                  'reason': 'No work of 40 Cr is listed.'}],
                       'summary': 'Similar work fails.'}],
            'status_review': [{'reviews': [{'ref': 1, 'agreement': 'disagrees', 'bias_risk': 'high',
                                            'finding': 'Nothing in the profile supports Met.',
                                            'possible_reasons': ['A work outside the profile'],
                                            'recommendation': 'verify'}],
                               'summary': 'The Similar work mark is unsupported.'}],
            'supervisor': [{'criteria': [{'ref': 0, 'final_status': 'met', 'confidence': 0.9},
                                         {'ref': 1, 'final_status': 'not_met', 'confidence': '70%'}],
                            'determination': 'Not eligible', 'confidence': 0.7, 'summary': 'Similar work fails.',
                            'recheck': []}],
        }

    def test_the_agents_share_one_state_and_the_supervisor_decides(self):
        report, agents = self.run_agents()

        self.assertEqual(agents.agents(), ['profiler', 'audit', 'status_review', 'supervisor'])
        # The criteria come from the tender's Pinecone namespace, by kind of requirement and as evidence per criterion
        audit_prompt = agents.calls[1][1][1]['content']
        self.assertIn("## The tender's eligibility criteria (from Pinecone namespace tcno-100091540)", audit_prompt)
        self.assertIn('[E1] NIT.pdf (part 1)\nTurnover 10 Cr; one similar work of 40 Cr.', audit_prompt)
        self.assertIn('- Financial Requirements: E1', audit_prompt)
        self.assertIn('  - [1] Similar work: E1', audit_prompt)
        self.assertEqual((report['inputs']['criteria_source'], report['inputs']['excerpts']),
                         ('Pinecone namespace tcno-100091540', 1))
        # The ingestion agent's normalised figures reach the agents after it
        self.assertIn('FY26 > Turnover (FY 2025-26, 2025-04-01 to 2026-03-31): 1710000000 = ₹171.00 Cr',
                      agents.calls[0][1][1]['content'])
        judge_prompt = agents.calls[3][1][1]['content']
        self.assertIn('[1] Similar work (disagreement): the AI: Not met; marked by user: Met; the audit: Not met',
                      judge_prompt)
        self.assertEqual(report['final']['determination'], 'Not Eligible')
        self.assertEqual((report['final']['confidence'], report['final']['agrees_with_current']), (0.7, False))
        self.assertEqual([(row['final_status'], row['confidence'], row['conflict']) for row in report['criteria']],
                         [('met', 0.9, False), ('not_met', 0.7, True)])
        self.assertEqual(report['rounds'], 0)
        self.assertEqual(report['agents']['ingestion']['normalised_facts'][0]['rupees'], 1710000000)

    def test_the_supervisor_sends_disputed_criteria_back_to_the_audit_for_a_limited_number_of_rounds(self):
        recheck = {**self.replies()['supervisor'][0], 'recheck': [{'ref': 1, 'criterion': 'Similar work',
                                                                   'question': 'Does ROB Package-1 count?'}]}
        reexamined = {'rules': [{'ref': 1, 'criterion': 'Similar work', 'result': 'insufficient_data',
                                 'reason': 'Its value is not stated.'}]}

        report, agents = self.run_agents(supervisor=[recheck], audit=[self.replies()['audit'][0], reexamined])

        self.assertEqual(agents.agents(), ['profiler', 'audit', 'status_review']
                         + ['supervisor', 'audit'] * MAX_ROUNDS + ['supervisor'])
        self.assertIn('Re-examine only these criteria (Supervisor / Judge agent, round 1)', agents.calls[4][1][1]['content'])
        self.assertIn('Does ROB Package-1 count?', agents.calls[4][1][1]['content'])
        self.assertIn('No re-examination rounds are left', agents.calls[-1][1][0]['content'])
        self.assertEqual(report['rounds'], MAX_ROUNDS)
        # The re-examined rule replaces the first one, which the report keeps no more
        self.assertEqual([rule['result'] for rule in report['agents']['audit']['rules']], ['pass', 'insufficient_data'])
        self.assertEqual(report['criteria'][1]['audit_result'], 'insufficient_data')
        # The status review, made before the re-examination, is shown beside the audit result that stands
        self.assertEqual(report['status_review']['reviews'][0]['audit_result'], 'insufficient_data')
        self.assertIn('has re-examined disputed criteria 1 time.', agents.calls[5][1][1]['content'])

    def test_a_determination_the_statuses_do_not_bear_out_is_requires_review(self):
        supervisor = {**self.replies()['supervisor'][0], 'determination': 'Eligible'}

        report, _ = self.run_agents(supervisor=[supervisor])

        self.assertEqual((report['final']['determination'], report['final']['supervisor_determination']),
                         ('Requires Review', 'Eligible'))
        self.assertIn('the final statuses give Not Eligible', report['audit_log'][-1]['event'])

    def test_a_criterion_the_supervisor_left_out_keeps_the_audit_result(self):
        supervisor = {**self.replies()['supervisor'][0], 'criteria': [], 'determination': None, 'confidence': None}

        report, _ = self.run_agents(supervisor=[supervisor])

        self.assertEqual([row['final_status'] for row in report['criteria']], ['met', 'not_met'])
        self.assertEqual(report['final']['determination'], 'Not Eligible')
        self.assertIsNone(report['final']['confidence'])

    def test_nothing_marked_and_nothing_disputed_skips_the_status_review(self):
        result = copy.deepcopy(self.RESULT)
        result['criteria'][1] = {'criterion': 'Similar work', 'requirement': '1 work of 40 Cr', 'company': '',
                                 'status': 'not_met', 'source': 'NIT.pdf'}

        report, agents = self.run_agents(result)

        self.assertEqual(agents.agents(), ['profiler', 'audit', 'supervisor'])
        self.assertTrue(report['status_review']['skipped'])
        self.assertIsNone(report['audit_log'][3]['model'])  # it ran on no model

    def test_each_agent_runs_on_its_own_model(self):
        models = {'profiler': 'profiler-model', 'audit': 'audit-model', 'status_review': 'review-model',
                  'supervisor': 'judge-model'}
        agents = ScriptedAgents(**self.replies())

        # Named here, so the test does not depend on the models set in .env
        with mock.patch.dict(deep.AGENT_MODELS, models), \
                mock.patch('services.deep_analysis_service.make_model', return_value=agents) as make_model:
            report = run_deep_analysis(self.RESULT, self.knowledge(self.RESULT), self.COMPANY)

        self.assertEqual([call.args[0] for call in make_model.call_args_list],
                         ['profiler-model', 'audit-model', 'review-model', 'judge-model'])
        self.assertEqual(report['models'], models)
        self.assertEqual([entry['model'] for entry in report['audit_log']],
                         [None, 'profiler-model', 'audit-model', 'review-model', 'judge-model'])

    def test_an_answer_that_is_not_json_is_asked_for_again_once(self):
        audit = self.replies()['audit'][0]
        report, agents = self.run_agents(audit=['Here is the matrix: {"rules": [', audit])
        self.assertEqual(agents.agents().count('audit'), 2)
        self.assertIn('not one valid JSON object', agents.calls[2][1][-1]['content'])
        self.assertEqual(report['criteria'][1]['audit_result'], 'fail')

        with self.assertRaises(DeepAnalysisError), self.assertLogs('services.deep_analysis_service', 'WARNING'):
            self.run_agents(audit=['no JSON here'])

    def test_tokens_are_counted_by_agent(self):
        usage = TokenUsage(LLM_AGENTS)
        result = copy.deepcopy(self.RESULT)
        del result['criteria'][1]
        model = counting_model((json.dumps({'flags': []}), 400, 20),
                               ('```json\n{"rules": [{"ref": 0, "result": "pass"}]}\n```', 900, 60),
                               (json.dumps({'criteria': [{'ref': 0, 'final_status': 'met'}]}), 700, 50))
        with mock.patch('services.deep_analysis_service.make_model', return_value=model):
            report = run_deep_analysis(result, self.knowledge(result), self.COMPANY, usage=usage)

        self.assertEqual(report['tokens']['audit'], {'input': 900, 'output': 60, 'total': 960})
        self.assertEqual(report['tokens']['status_review']['total'], 0)  # nothing to review
        self.assertEqual(report['tokens']['total'], {'input': 2000, 'output': 130, 'total': 2130})

    def test_without_an_index_the_audit_works_from_the_result(self):
        self.index.delete_namespace('100091540')
        report, agents = self.run_agents()
        self.assertEqual(report['inputs']['criteria_source'], 'result')
        self.assertIn('The Pinecone namespace tcno-100091540 is missing or incomplete, so the audit works from '
                      'the '
                      'requirements the result recorded.', report['inputs']['warnings'])
        self.assertIn('- Similar work: 1 work of 40 Cr (source: NIT.pdf)', agents.calls[1][1][1]['content'])

    def test_nothing_to_review_and_no_api_key_fail_early(self):
        empty = {**self.RESULT, 'criteria': []}
        self.index.delete_namespace('100091540')
        with self.assertRaises(DeepAnalysisError):
            self.run_agents(empty)
        with mock.patch.dict(os.environ, {'GROQ_API_KEY': ''}), self.assertRaises(RuntimeError):
            self.run_agents()


class DeepAnalysisInputTest(unittest.TestCase):
    """What the ingestion agent reads from the company profile without the LLM."""

    def test_amounts_are_normalised_to_rupees(self):
        for text, rupees in [('₹ 12.5 Cr', 125_000_000), ('Rs. 1,25,00,000/-', 12_500_000), ('3 lakh', 300_000),
                             ('INR 5 crore', 50_000_000), ('2.5 Years', None), ('76 m', None), ('62011', None)]:
            self.assertEqual(parse_amount(text), rupees, text)
        self.assertEqual(parse_amount('94500000000', money_name=True), 94_500_000_000)

    def test_financial_years_run_april_to_march(self):
        for text in ('FY24', 'FY 2023-24', '2023-24', 'F.Y. 2023-2024', "FY'24"):
            year = parse_financial_year(text)
            self.assertEqual((year['label'], year['start'].isoformat(), year['end'].isoformat()),
                             ('FY 2023-24', '2023-04-01', '2024-03-31'), text)
        self.assertIsNone(parse_financial_year('2024-03'))
        self.assertIsNone(parse_financial_year('Turnover'))

    def test_a_bare_number_is_money_only_in_a_money_field(self):
        facts = [deep.normalise_fact(fact) for fact in deep.profile_facts(
            '# Profile\n\n## 1. Basic\n\n| Item | Value |\n|---|---|\n| NIC code | 62011 |\n'
            '| Total Existing Commitments | 4300000000 |\n\n## 2. Certificates\n\n'
            '| Certificate | Valid until |\n|---|---|\n| ISO 9001 | 31-03-2026 |\n\n'
            '## 3. Finance\n\n| Year | Turnover |\n|---|---|\n| FY24 | 5 Cr |')]

        self.assertEqual([fact.get('rupees') for fact in facts], [None, 4_300_000_000, None, 50_000_000])
        self.assertEqual(facts[2]['section'], 'Certificates')
        flags = deep.date_checks(facts, tender_service.date(2026, 11, 6))
        self.assertEqual([(flag['field'], flag['issue'], flag['severity']) for flag in flags],
                         [('Certificates > ISO 9001 > Valid until', 'expired', 'high'),
                          ('Finance > Turnover', 'outdated', 'medium')])

    def test_json_is_found_in_a_fence_or_a_sentence(self):
        self.assertEqual(parse_json_reply('<think>hmm</think>```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(parse_json_reply('Here it is: {"a": [1, 2]} as asked.'), {'a': [1, 2]})
        for text in ('no object', '[1, 2]', '{"a": '):
            with self.assertRaises(ValueError):
                parse_json_reply(text)


if __name__ == '__main__':
    unittest.main()
