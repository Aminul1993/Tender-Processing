import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ext import (TENDERTIGER_REQUEST_GAP, TenderTigerBlocked, _PacedAdapter, assess_eligibility, company_details,
                 extract_zip_recursive, prequalification_criteria, save_company_details, split_text,
                 summarize_for_goal, tender_list, tendertiger_session)


def zip_bytes(entries):
    """Builds a zip in memory from {name: bytes}; a name ending in '/' is a folder entry."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zfile:
        for name, data in entries.items():
            zfile.writestr(name, data)
    return buffer.getvalue()


class FakeLLM:
    """Returns the given replies in order, one per chunk."""
    def __init__(self, replies):
        self.replies = iter(replies)
        self.invoked = []

    def batch(self, messages, config=None):
        return [SimpleNamespace(content=next(self.replies)) for _ in messages]

    def invoke(self, messages):
        self.invoked.append(messages)
        return SimpleNamespace(content=next(self.replies))


class ExtractZipRecursiveTest(unittest.TestCase):
    def test_slip_folders_inner_zips_and_same_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / 'tender.zip'
            archive.write_bytes(zip_bytes({
                '../escaped.txt': b'x',
                'docs/': b'',
                'docs/a.pdf': b'a',
                'NIT.pdf': b'outer',
                'a.zip': zip_bytes({'NIT.pdf': b'from a'}),
                'b.zip': zip_bytes({'NIT.pdf': b'from b'}),
            }))
            out = tmp / 'out'

            files = extract_zip_recursive(archive, out)

            self.assertFalse((tmp / 'escaped.txt').exists())
            names = sorted(p.relative_to(out.resolve()).as_posix() for p in files)
            self.assertEqual(names, ['NIT.pdf', 'a/NIT.pdf', 'b/NIT.pdf', 'docs/a.pdf', 'escaped.txt'])
            self.assertEqual((out / 'a' / 'NIT.pdf').read_bytes(), b'from a')
            self.assertEqual((out / 'NIT.pdf').read_bytes(), b'outer')
            self.assertEqual(list(out.rglob('*.zip')), [])


class SummarizeForGoalTest(unittest.TestCase):
    def test_keeps_rule_followed_by_not_relevant_and_drops_bold_not_relevant(self):
        document = 'a' * 10 + '\n\n' + 'b' * 10
        llm = FakeLLM(['Minimum turnover ₹10 Cr.\nRest: NOT_RELEVANT', '**NOT_RELEVANT**'])

        summary = summarize_for_goal(llm, document, 'goal', max_chars=15)

        self.assertEqual(summary, 'Minimum turnover ₹10 Cr.\nRest: NOT_RELEVANT')


class CompanyDetailsTest(unittest.TestCase):
    def test_saved_profile_is_loaded_and_sent_to_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'company_details.md'
            self.assertEqual(company_details(path), '')

            save_company_details('\n# ACME {Ltd}\n| Turnover | ₹10 Cr |\n\n', path)
            llm = FakeLLM(['assessment'])
            assess_eligibility(llm, ['## NIT.pdf\nTurnover at least ₹5 Cr'], company_details(path))

            self.assertEqual(company_details(path), '# ACME {Ltd}\n| Turnover | ₹10 Cr |')
            self.assertTrue(llm.invoked[0][1]['content'].endswith('Company details:\n# ACME {Ltd}\n| Turnover | ₹10 Cr |'))

    def test_profile_saved_with_a_bom_reads_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'company_details.md'
            path.write_text('# ACME\n', encoding='utf-8-sig')
            self.assertEqual(company_details(path), '# ACME')


def fake_response(text='', body=None, status=200):
    """Just enough of requests.Response for the TenderTiger helpers."""
    content = text.encode() if body is None else json.dumps(body).encode()
    return SimpleNamespace(status_code=status, text=text, content=content, raise_for_status=lambda: None,
                           json=lambda: json.loads(content))


class TenderTigerTest(unittest.TestCase):
    LOGIN_PAGE = '<form><input name="__RequestVerificationToken" type="hidden" value="tok-1" /></form>'

    def setUp(self):
        for patch in (mock.patch.dict(os.environ, {'TENDERTIGER_EMAIL': 'bid@example.test',
                                                   'TENDERTIGER_PASSWORD': 'secret'}),
                      mock.patch('ext._tendertiger', None)):
            patch.start()
            self.addCleanup(patch.stop)

    def login(self, answer):
        session = mock.Mock(headers={})
        session.get.return_value = fake_response(self.LOGIN_PAGE)
        # The login answer is a JSON string holding the JSON object
        session.post.return_value = fake_response(body=json.dumps(answer))
        with mock.patch('ext.requests.Session', return_value=session):
            return session, tendertiger_session()

    def test_login_posts_the_form_token_and_keeps_the_session(self):
        session, logged_in = self.login({'SuccessCode': 3, 'Token': 'jwt', 'SuccessMessage': 'You have logged in'})

        self.assertIs(logged_in, session)
        self.assertIs(tendertiger_session(), session)  # reused, not logged in again
        form = session.post.call_args.kwargs['data']
        self.assertEqual((form['Email'], form['Password'], form['__RequestVerificationToken']),
                         ('bid@example.test', 'secret', 'tok-1'))
        self.assertIn('Mozilla', session.headers['User-Agent'])
        prefix, adapter = session.mount.call_args.args
        self.assertEqual((prefix, type(adapter)), ('https://www.tendertiger.com', _PacedAdapter))

    def test_requests_to_the_site_are_spaced_out(self):
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch('ext._next_request', 0.0), mock.patch('ext.time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch('ext.time.sleep', side_effect=sleep) as slept, \
                mock.patch('requests.adapters.HTTPAdapter.send', return_value='response') as send:
            adapter = _PacedAdapter()
            self.assertEqual(adapter.send('login page'), 'response')  # the first request does not wait
            clock[0] += 1
            adapter.send('login')
            clock[0] += TENDERTIGER_REQUEST_GAP + 5
            adapter.send('tender list')  # long after the previous one, so it does not wait either

        self.assertEqual([call.args for call in slept.call_args_list], [(TENDERTIGER_REQUEST_GAP - 1,)])
        self.assertEqual([call.args[0] for call in send.call_args_list], ['login page', 'login', 'tender list'])

    def test_refused_login_names_the_reason(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid username or password'):
            self.login({'SuccessCode': 0, 'Token': None, 'SuccessMessage': 'Invalid username or password.'})

    def test_firewall_browser_check_is_reported_as_such(self):
        session = mock.Mock(headers={})
        session.get.return_value = fake_response('<html><title>You are being redirected...</title><script>'
                                                 "var s={},sucuri_cloudproxy_js='',S='dj0n';</script></html>")
        with mock.patch('ext.requests.Session', return_value=session), \
                self.assertRaisesRegex(TenderTigerBlocked, "TenderTiger's firewall is showing a browser check"):
            tendertiger_session()
        session.post.assert_not_called()

    def test_tender_list_logs_in_again_when_the_session_has_ended(self):
        ended, current = mock.Mock(), mock.Mock()
        ended.post.return_value = fake_response(status=204)
        current.post.return_value = fake_response(body={'TenderList': [{'tcno': '1'}]})

        with mock.patch('ext.tendertiger_session', side_effect=[ended, current]) as login:
            result = tender_list()

        self.assertEqual(result['TenderList'], [{'tcno': '1'}])
        self.assertEqual([call.args for call in login.call_args_list], [(False,), (True,)])
        fields = current.post.call_args.kwargs['files']
        self.assertEqual((fields['searchkeyword'], fields['tender_typewise']), ((None, 'india'), (None, 'live')))

    def test_tender_list_pages_by_the_offset_of_the_first_tender(self):
        session = mock.Mock()
        session.post.return_value = fake_response(body={'TenderList': []})
        paging = ('total', 'fromdata', 'rescount', 'last_rescount')

        with mock.patch('ext.tendertiger_session', return_value=session):
            tender_list({'wlocation': 'kolkata'})
            first = session.post.call_args.kwargs['files']
            tender_list({'wlocation': 'kolkata'}, page=3)
            third = session.post.call_args.kwargs['files']

        self.assertEqual([first[name][1] for name in paging], ['20', '0', '0', '20'])
        self.assertEqual([third[name][1] for name in paging], ['20', '40', '40', '60'])
        self.assertEqual(third['wlocation'], (None, 'kolkata'))
        self.assertNotIn('sortcolumnname', third)  # TenderTiger's own order

    def test_tender_list_is_sorted_by_tendertiger(self):
        session = mock.Mock()
        session.post.return_value = fake_response(body={'TenderList': []})

        with mock.patch('ext.tendertiger_session', return_value=session):
            tender_list(page=2, sort=('tendervalue', 'desc'))

        fields = session.post.call_args.kwargs['files']
        self.assertEqual((fields['sortcolumnname'], fields['sortingby'], fields['fromdata']),
                         ((None, 'tendervalue'), (None, 'desc'), (None, '20')))

    def test_tender_list_pages_of_more_tenders_ask_for_that_many(self):
        session = mock.Mock()
        session.post.return_value = fake_response(body={'TenderList': []})

        with mock.patch('ext.tendertiger_session', return_value=session):
            tender_list(page=3, page_size=100)

        fields = session.post.call_args.kwargs['files']
        self.assertEqual([fields[name][1] for name in ('total', 'fromdata', 'rescount', 'last_rescount')],
                         ['100', '200', '200', '300'])

    def fetch_prequalification(self, html):
        session = mock.Mock()
        session.get.return_value = fake_response(html)
        with mock.patch('ext.tendertiger_session', return_value=session):
            return prequalification_criteria('https://www.tendertiger.com/TenderDetail/GetPreQualification?hdnTcNo=1')

    def test_pre_qualification_is_read_from_the_display_text_script(self):
        html = ('<input type="hidden" id="hidPreQualificationCount" value="1" />\n<script>\n'
                '            //displayText($(".contentPreQua"),"FINANCIAL CAPACITY:\\n\\u2022 Net Worth of 5% '
                '(the \\u0022Financial Capacity\\u0022) \\u0026 more.\\n");\n'
                '    await displayText(contentElement, text);\n</script>')

        self.assertEqual(self.fetch_prequalification(html),
                         'FINANCIAL CAPACITY:\n• Net Worth of 5% (the "Financial Capacity") & more.')

    def test_no_pre_qualification_is_empty(self):
        html = ('<input type="hidden" id="hidPreQualificationCount" value="0" />\n'
                '<script>await displayText(contentElement, text);</script>')
        self.assertEqual(self.fetch_prequalification(html), '')


class SplitTextTest(unittest.TestCase):
    def test_falls_back_to_line_break_inside_table(self):
        text = '| x | 12,50,000 |\n| y | 3,00,000 |'

        chunks = split_text(text, max_chars=25)

        self.assertEqual(chunks, ['| x | 12,50,000 |', '| y | 3,00,000 |'])


if __name__ == '__main__':
    unittest.main()
