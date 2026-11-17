"""
The TenderTiger website as the app uses it: the logged-in session (TENDERTIGER_EMAIL / TENDERTIGER_PASSWORD), its
requests spaced out so the site's firewall does not block them, the tender list search, and a tender's
pre-qualification criteria.
"""
import json
import logging
import os
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

TENDERTIGER_URL = 'https://www.tendertiger.com'
# The site's firewall refuses the default python-requests user agent
TENDERTIGER_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/130.0 Safari/537.36')
_tendertiger_lock = threading.Lock()
_tendertiger = None  # the logged-in requests.Session, shared by every request to the site
TENDERTIGER_BLOCKED = ("TenderTiger's firewall is showing a browser check instead of the site, as it does after many "
                       'automated visits in a short time. Wait a while, then try again.')
# Seconds between two requests to the site: its firewall answers bursts of requests with the browser check
TENDERTIGER_REQUEST_GAP = float(os.getenv('TENDERTIGER_REQUEST_GAP', '3'))
_pace_lock = threading.Lock()
_next_request = 0.0  # the time.monotonic() before which no request to the site is sent
_shared_turn = None  # see share_pacing()


class TenderTigerBlocked(RuntimeError):
    """The site's firewall answered with its browser check instead of the page."""


def share_pacing(take_turn):
    """
    Spaces out the requests to the site of every process together (the web app and its workers), not of this one
    alone: take_turn(gap) takes the next turn, gap seconds after the last one any process took, and returns the
    seconds to wait for it (services.database.Database.pace()). None paces this process alone again.
    """
    global _shared_turn
    _shared_turn = take_turn


class _PacedAdapter(HTTPAdapter):
    """
    Sends each request at least TENDERTIGER_REQUEST_GAP seconds after the previous one, from any thread, and from any
    process once share_pacing() is set up; this process alone when the turn cannot be taken (the database out of
    reach, say).
    """

    def send(self, request, **kwargs):
        global _next_request
        wait = None
        if _shared_turn is not None:
            try:
                wait = _shared_turn(TENDERTIGER_REQUEST_GAP)
            except Exception:
                log.warning('Could not take a turn to call TenderTiger in the database; spacing out the requests of '
                            'this process alone', exc_info=True)
        if wait is not None:
            time.sleep(wait)
        else:
            with _pace_lock:
                wait = _next_request - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _next_request = time.monotonic() + TENDERTIGER_REQUEST_GAP
        return super().send(request, **kwargs)


def tendertiger_checked(response):
    """
    Returns response, or raises TenderTigerBlocked(TENDERTIGER_BLOCKED) when the site's firewall (Sucuri) answered
    with its JavaScript browser check instead. Only for pages and JSON: it reads the body.
    """
    if 'sucuri_cloudproxy' in response.text[:5000]:
        raise TenderTigerBlocked(TENDERTIGER_BLOCKED)
    return response


def tendertiger_session(refresh=False):
    """
    Returns a requests.Session logged in to TenderTiger with TENDERTIGER_EMAIL / TENDERTIGER_PASSWORD. Logs in on
    first use, and again when refresh is true (the site has ended the session).
    Raises RuntimeError when the credentials are missing or refused, and on a network error or a non-2xx status.
    """
    global _tendertiger
    with _tendertiger_lock:
        if _tendertiger is None or refresh:
            _tendertiger = _tendertiger_login()
        return _tendertiger


def _tendertiger_login():
    """Posts the login form and returns the session holding the login cookies."""
    email, password = os.getenv('TENDERTIGER_EMAIL'), os.getenv('TENDERTIGER_PASSWORD')
    if not email or not password:
        raise RuntimeError('TENDERTIGER_EMAIL and TENDERTIGER_PASSWORD are not set. Add them to .env and restart the app.')
    session = requests.Session()
    session.mount(TENDERTIGER_URL, _PacedAdapter())
    session.headers['User-Agent'] = TENDERTIGER_USER_AGENT
    # The form's anti-forgery token is only accepted together with the cookie the login page sets
    page = tendertiger_checked(session.get(f'{TENDERTIGER_URL}/User/Login', timeout=60))
    page.raise_for_status()
    token = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]*)"', page.text)
    if not token:
        raise RuntimeError('TenderTiger login failed: the login page has no __RequestVerificationToken')
    response = session.post(f'{TENDERTIGER_URL}/User/Login', timeout=60, data={
        'Email': email, 'Password': password, '__RequestVerificationToken': token.group(1),
        'ReturnUrl': '', 'redirect_uri': '', 'code_challenge': '', 'state': '',
    })
    response.raise_for_status()
    # The answer is a JSON string that holds the JSON object
    result = response.json()
    if isinstance(result, str):
        result = json.loads(result)
    if not result.get('Token'):
        raise RuntimeError(f"TenderTiger login failed: {result.get('SuccessMessage') or 'no session was returned'}")
    return session


# Tenders TenderTiger returns for one search request, a page of the list, unless asked for more (up to 500 at
# once, tried). It does not say how many there are.
TENDER_PAGE_SIZE = 20

# The search the tender list shows, as the TenderTiger search page posts it (multipart form). The page also sends
# about 50 empty or "undefined" filter fields; leaving them out returns the same tenders.
TENDER_SEARCH = {
    'subscribetype': 'all', 'searchkeyword': 'india', 'seosearchlocation': 'india', 'tabindex': '1',
    'maintabindex': '0', 'tender_typewise': 'live', 'categoryflag': '1',
}


def tender_list(filters=None, page=1, sort=None, page_size=TENDER_PAGE_SIZE):
    """
    Fetches the live tenders TENDER_SEARCH finds, with the logged-in TenderTiger session. filters, if given, adds
    more fields of the same form, the filters of the search page such as {'wlocation': 'kolkata'}. page (from 1)
    picks which page_size tenders: as the search page asks for more, by how many (total), the offset of the first
    (fromdata) and the counts before and after them (rescount, last_rescount). sort, if given, is (column,
    direction) to order the whole list by before it is paged, such as ('closingdate', 'asc') or ('tendervalue',
    'desc') (sortcolumnname, sortingby). Returns the parsed JSON response; the tenders are in
    json_response['TenderList'], empty past the last page.
    Raises on a network error, a non-2xx status or a failed login.
    """
    offset = (page - 1) * page_size
    form = {**TENDER_SEARCH, **(filters or {}), 'total': str(page_size),
            'fromdata': str(offset), 'rescount': str(offset), 'last_rescount': str(offset + page_size)}
    if sort:
        form['sortcolumnname'], form['sortingby'] = sort
    for refresh in (False, True):
        response = tendertiger_checked(tendertiger_session(refresh).post(
            f'{TENDERTIGER_URL}/Tender/GetTendersList', timeout=60,
            files={name: (None, value) for name, value in form.items()},
        ))
        response.raise_for_status()
        # A session the site has ended gets an empty 204 instead of the list
        if response.content:
            return response.json()
    raise RuntimeError('TenderTiger sent no tender list, even after logging in again')


# GetPreQualification writes the criteria into its script as displayText($(".contentPreQua"), "<JSON string>")
_PREQUALIFICATION_TEXT = re.compile(r'displayText\(\s*\$\("\.contentPreQua"\)\s*,\s*("(?:[^"\\]|\\.)*")\s*\)')


def prequalification_criteria(url, timeout=60):
    """
    Fetches a tender's GetPreQualification link with the TenderTiger session and returns its pre-qualification
    criteria as text, or an empty string if it has none. (The page needs no login: it was served in full to a
    session that had none, so an ended session is not logged in again for it.)
    Raises on a network error, a non-2xx status or a failed login, and RuntimeError when the answer is not the
    pre-qualification page, so it is not taken for a tender with no criteria.
    """
    response = tendertiger_checked(tendertiger_session().get(url, timeout=timeout))
    response.raise_for_status()
    # Every pre-qualification page holds it, with the number of criteria (0 for none)
    if 'hidPreQualificationCount' not in response.text:
        raise RuntimeError(f'TenderTiger sent another page instead of the pre-qualification criteria: '
                           f'{response.text[:200]}')
    texts = [json.loads(literal).strip() for literal in _PREQUALIFICATION_TEXT.findall(response.text)]
    return '\n\n'.join(text for text in texts if text)
