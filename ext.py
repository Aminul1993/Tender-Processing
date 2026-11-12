import json
import os
import re
import threading
import time
import zipfile
from pathlib import Path
from langchain_core.runnables import RunnableLambda
from langchain_groq import ChatGroq
import requests
from requests.adapters import HTTPAdapter

def extract_zip_recursive(zip_path, target_dir):
    """
    Extracts a zip and every zip inside it. Each inner zip goes into its own folder
    (a.zip -> a/). Returns the paths of all non-zip files.
    """
    files = []
    with zipfile.ZipFile(zip_path) as zfile:
        for member in zfile.infolist():
            if member.is_dir():
                continue
            # extract() strips '..' and drive letters, so entries cannot escape target_dir
            path = Path(zfile.extract(member, target_dir))
            if path.suffix.lower() == '.zip':
                files += extract_zip_recursive(path, path.with_suffix(''))
                path.unlink()
            else:
                files.append(path.resolve())
    return files

NOT_RELEVANT = 'NOT_RELEVANT'

def split_text(text, max_chars=8000):
    """
    Splits text into chunks of at most max_chars, breaking on paragraph boundaries where possible.
    """
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            split_at = text.rfind('\n\n', start, end)
            if split_at <= start:
                # No paragraph break (e.g. a long table): fall back to a line break
                split_at = text.rfind('\n', start, end)
            if split_at > start:
                end = split_at
        chunks.append(text[start:end].strip())
        start = end
    return [chunk for chunk in chunks if chunk]

def summarize_for_goal(llm, document_markdown, goal, max_chars=8000, max_concurrency=4):
    """
    Summarizes a document chunk by chunk, keeping only what is relevant to the goal,
    then merges the chunk notes. Returns an empty string if nothing is relevant.
    Chunks are sent in parallel, up to max_concurrency at a time.
    """
    system_prompt = (
        f'Goal: {goal}\n\n'
        'Summarize only the parts of the following document text that help achieve the goal. '
        'Keep exact values, numbers, dates and conditions as written, and do not add anything that is not in the text. '
        f'If nothing is relevant, reply with exactly {NOT_RELEVANT}.'
    )
    responses = llm.batch(
        [
            [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': chunk}]
            for chunk in split_text(document_markdown, max_chars)
        ],
        config={'max_concurrency': max_concurrency},
    )
    notes = []
    for response in responses:
        note = response.content.strip()
        # Drop only replies that are NOT_RELEVANT as a whole, so a real rule followed by it is kept
        if note and note.strip(' .*') != NOT_RELEVANT:
            notes.append(note)

    if len(notes) <= 1:
        return notes[0] if notes else ''

    # Merge the per-chunk notes into one summary for the whole document
    response = llm.invoke([
        {'role': 'system', 'content': (
            f'Goal: {goal}\n\n'
            'Merge the following notes, taken from parts of the same document, into one summary for the goal. '
            'Remove duplicates, keep exact values and conditions, and do not add anything that is not in the notes.'
        )},
        {'role': 'user', 'content': '\n\n---\n\n'.join(notes)},
    ])
    return response.content.strip()

# The company profile every tender is checked against, as markdown. Edited on the web page or by hand.
COMPANY_DETAILS_FILE = Path(__file__).with_name('company_details.md')
_company_lock = threading.Lock()

def company_details(path=COMPANY_DETAILS_FILE):
    """
    Returns the company profile saved in path, as markdown, or an empty string if the file does not exist.
    """
    with _company_lock:
        return _read_company(path)

def save_company_details(text, path=COMPANY_DETAILS_FILE):
    """
    Saves the company profile to path. Writes a temporary file first, so a crash mid-write cannot leave
    half a profile.
    """
    with _company_lock:
        _write_company(text, path)

def update_company_details(change, path=COMPANY_DETAILS_FILE):
    """
    Changes the company profile saved in path: change is given the profile as markdown and returns it changed,
    saved unless it is the same. No other save can come between the read and the write.
    """
    with _company_lock:
        text = _read_company(path)
        changed = change(text)
        if changed.strip() != text:
            _write_company(changed, path)

def _read_company(path):
    try:
        # utf-8-sig, because Windows editors may save the file with a BOM
        return Path(path).read_text(encoding='utf-8-sig').strip()
    except FileNotFoundError:
        return ''

def _write_company(text, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text.strip() + '\n', encoding='utf-8')
    os.replace(temporary, path)

ELIGIBILITY_GOAL = (
    'Find the eligibility / qualification criteria a bidder must meet, such as similar work experience, '
    'turnover, financial capacity, bid capacity and technical requirements. '
    'Also keep the estimated cost or tender value and any formulas, because the criteria may depend on them. '
    'Keep every joint venture (JV) or consortium term as well: whether a JV is allowed, the maximum number of '
    'members, the lead member\'s minimum share, and what the lead member, the other members and the JV as a whole '
    'must each meet.'
)

# assess_eligibility() fills in {company_details} when it runs, so a saved profile applies to the next check
ELIGIBILITY_TASK = """Find every eligibility criterion in the document excerpts and check whether the company below meets it when bidding alone.

For each criterion:
1. State the requirement exactly as written, with its source file. If several sources state the same criterion, list it once and name them all.
2. If the required value depends on another figure (such as estimated cost), work it out and show the calculation. Bring both sides to the same unit before comparing (1 crore = 1,00,00,000 rupees = 10,000,000; 1 lakh = 1,00,000 rupees) and show the conversion.
3. Compare it with the company details and mark it Met, Not met, or Cannot determine. For similar work or experience, judge each of the company's projects by what it was (its name, client and kind of work) against this tender's definition of similar work. Ignore any "similar" label in the company details: what counts as similar differs from tender to tender.
4. Briefly explain, quoting the company figures you used.

Then check the joint venture (JV) or consortium terms:
5. State whether a JV or consortium is allowed (Yes, No, or Not stated) and the maximum number of members, lead member included (a number, or Not stated).
6. If a JV is allowed, take every criterion of the first table, with the same name and in the same order, and state what the lead member, each other member and the JV as a whole must meet, as written or worked out. Then add the requirements that apply only to a JV, such as the lead member's minimum share. Compare the company with what the lead member must meet and mark it Met, Not met, or Cannot determine.
7. List the other JV conditions, such as the lead member's minimum share, a JV agreement, or members not bidding separately.

Use only the company details given below. Mark each criterion by these rules, so that the same documents and company details always get the same result:
- Met: the company details state what the criterion needs, and it satisfies the requirement.
- Not met: the company details state it, and it falls short.
- Cannot determine: the company details do not state it, or the documents do not give the required value (such as a turnover "as indicated in the bid document"). Say what is missing. Do not infer a fact the company details do not state, such as the company's country from its address, or its MSE category from a Udyam number.
- An exemption or relaxation (for MSEs, startups and the like) is not a criterion of its own: apply it to the criterion it relaxes, and only when the company details state that the company qualifies for it.
- The table "Criteria marked by the bid team", if the company details have one, holds the statuses the bid team set by hand on criteria of earlier tenders. When a criterion here has the same requirement as a row, give it the row's status and say the bid team marked it so; otherwise do not use the row.
- Leave out terms that are not conditions for bidding, such as purchase preferences, price matching, and payment or delivery terms.

Write plain text, not LaTeX: use symbols such as ≥, ≤ and × directly.

Present the result in this order:
- The calculations, then a table with columns: Criterion | Requirement | Company | Status | Source. Give one table: if a calculation changes a status, write the table with the corrected status only.
- A section headed "## Joint venture" with the lines "JV allowed: <Yes, No or Not stated>" and "Maximum JV members: <number or Not stated>", with their sources. If a JV is allowed, follow them with a table with columns: Criterion | Lead member | Other members | JV combined | Company as lead member | Source, one row per criterion as in step 6, and the other JV conditions as a bullet list.
Finish with an overall verdict (Eligible, Not eligible, or Cannot determine) for the company bidding alone, and the criteria that decide it.

{tender_details}Company details:
{company_details}"""

# The tender's own data from the TenderTiger list, put before the company details when assess_eligibility() has it
TENDER_DETAILS = """Tender details, from the TenderTiger list:
{tender}
When a requirement depends on the estimated cost or tender value and the excerpts do not state it, use the tender value above and say so; a figure in the excerpts comes first. Count periods such as "the last three financial years" back from the closing date, and check that certificates and registrations are still valid on it, unless the excerpts give another date.

"""

# The Groq models (https://console.groq.com/docs/models). gpt-oss-120b checks the company against the criteria
# retrieved from the tender's Pinecone index. SUMMARY_MODEL is used only by summarize_for_goal(), which the tender
# pipeline no longer calls: the documents are indexed in Pinecone instead of summarised.
SUMMARY_MODEL = os.getenv('SUMMARY_MODEL', 'openai/gpt-oss-20b')
ASSESSMENT_MODEL = os.getenv('ASSESSMENT_MODEL', 'openai/gpt-oss-120b')
# The Groq model that reads the pages of a scanned PDF that PaddleOCR is unsure of or that hold a table
# (services/ocr_service.py); it must accept images ('vision')
OCR_MODEL = os.getenv('OCR_MODEL', 'qwen/qwen3.8-27b')
# The models of the LLM agents of a deep analysis (services/deep_analysis_service.py), each ASSESSMENT_MODEL unless
# set: the profiler checks the company data, the audit matches it against every criterion, the status review
# questions the statuses marked by user, and the supervisor settles their disagreements
DEEP_PROFILER_MODEL = os.getenv('DEEP_PROFILER_MODEL', ASSESSMENT_MODEL)
DEEP_AUDIT_MODEL = os.getenv('DEEP_AUDIT_MODEL', ASSESSMENT_MODEL)
DEEP_STATUS_REVIEW_MODEL = os.getenv('DEEP_STATUS_REVIEW_MODEL', ASSESSMENT_MODEL)
DEEP_SUPERVISOR_MODEL = os.getenv('DEEP_SUPERVISOR_MODEL', ASSESSMENT_MODEL)
# Most tokens a model may write in one reply, reasoning included. Sent with every call: without it Groq stops a reply
# at 3,072 tokens, which cut eligibility checks off mid-table (one needs 6,831), and adds those 3,072 to the prompt
# when it checks a request against the free tier's 8,000 tokens per minute (error 413)
LLM_MAX_OUTPUT_TOKENS = int(os.getenv('LLM_MAX_OUTPUT_TOKENS', '16384'))


class AnswerCutOff(RuntimeError):
    """The model reached LLM_MAX_OUTPUT_TOKENS before it finished its reply."""


def _whole_reply(message):
    """message, unless the model was stopped before the end of it: a cut-off check must not pass for a result."""
    if (getattr(message, 'response_metadata', None) or {}).get('finish_reason') == 'length':
        raise AnswerCutOff(f"The model's reply was cut off at {LLM_MAX_OUTPUT_TOKENS:,} tokens "
                           '(LLM_MAX_OUTPUT_TOKENS).')
    return message


def make_model(model=SUMMARY_MODEL):
    """
    Returns the Groq chat model named model, with retries. The API key is read from GROQ_API_KEY.
    A reply cut off at LLM_MAX_OUTPUT_TOKENS raises AnswerCutOff (not retried: it would be cut off again).
    """
    # Qwen models think before answering unless told not to; hiding the thinking (reasoning_format='hidden') still
    # spends and bills its tokens. gpt-oss models refuse the option and keep their reasoning out of the reply anyway.
    reasoning = {'reasoning_effort': 'none'} if model.startswith('qwen/') else {}
    llm = ChatGroq(model=model, temperature=0, timeout=120, max_tokens=LLM_MAX_OUTPUT_TOKENS, **reasoning)
    return llm.with_retry(stop_after_attempt=3) | RunnableLambda(_whole_reply)

def assess_eligibility(llm, summaries, company=None, tender=None):
    """
    Checks the company against the eligibility criteria found in summaries: the tender's document excerpts, one
    '## <source file>' section each, as retrieved from its Pinecone index.
    company is the company profile as markdown; by default it is read from COMPANY_DETAILS_FILE.
    tender, if given, is the tender's data from the TenderTiger list as text (organisation, closing date, tender
    value and so on), sent with how to use its value and closing date (TENDER_DETAILS).
    Returns the model's assessment as markdown.
    """
    if company is None:
        company = company_details()
    response = llm.invoke([
        {'role': 'system', 'content': (
            'You check tender eligibility. The document excerpts below, retrieved from the tender\'s documents, are '
            'the only source of eligibility criteria. "[…]" marks text left out between two excerpts. '
            'Name the source file for each point.\n\n' + '\n\n'.join(summaries)
        )},
        {'role': 'user', 'content': ELIGIBILITY_TASK.format(
            company_details=company, tender_details=TENDER_DETAILS.format(tender=tender) if tender else '')},
    ])
    return response.content

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

class TenderTigerBlocked(RuntimeError):
    """The site's firewall answered with its browser check instead of the page."""

class _PacedAdapter(HTTPAdapter):
    """Sends each request at least TENDERTIGER_REQUEST_GAP seconds after the previous one, from any thread."""

    def send(self, request, **kwargs):
        global _next_request
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
    Fetches a tender's GetPreQualification link with the logged-in TenderTiger session and returns its
    pre-qualification criteria as text, or an empty string if it has none.
    Raises on a network error, a non-2xx status or a failed login.
    """
    response = tendertiger_checked(tendertiger_session().get(url, timeout=timeout))
    response.raise_for_status()
    texts = [json.loads(literal).strip() for literal in _PREQUALIFICATION_TEXT.findall(response.text)]
    return '\n\n'.join(text for text in texts if text)
