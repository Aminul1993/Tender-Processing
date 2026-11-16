"""JSON API used by the tender page. Every error response has the shape {"error": "<message>"}."""
import json
import logging
import mimetypes
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from services import tender_service
from services.company_service import (EMPTY_PROFILE, CompanyStore, company_profile, parse_profile, render_profile,
                                      unnamed_part)
from services.decision_service import DecisionNotAllowed, DecisionStore
from services.deep_analysis_service import DeepAnalysisRunner, DeepAnalysisRunning
from services.pinecone_service import VectorStoreError, VectorStoreNotConfigured
from services.processing_service import NoJobRunning, TenderBusy, TenderProcessor
from services.profile_gap_service import profile_gap_report, report_markdown
from services.tender_service import TenderFilters, TenderServiceError
from services.watchlist_service import WatchlistError, WatchlistMissing, WatchlistRunner
from utils.files import safe_name

log = logging.getLogger(__name__)

api = APIRouter(prefix='/api')

# What to tell the caller when a request body fails validation, by endpoint name
BODY_ERRORS = {
    'process_tenders': ('Send a JSON body like {"tcnos": ["100091540"]} with at least one TCNO, and optionally '
                        '"regenerate": true.'),
    'set_decision': 'Send a JSON body like {"decision": "reviewed"}; use "reviewed", "shortlisted" or "dropped".',
    'mark_criterion': 'Send a JSON body like {"status": "met"}; use "met", "not_met", "unknown" or null.',
    'check_unreadable': 'Send a JSON body like {"checked": true}.',
    'save_company': 'Send a JSON body like {"blocks": [...]}, with the blocks GET /api/company returns.',
}


def get_processor(request: Request) -> TenderProcessor:
    """The app's single TenderProcessor, created in create_app()."""
    return request.app.state.tender_processor


def get_decisions(request: Request) -> DecisionStore:
    """The app's DecisionStore, created in create_app()."""
    return request.app.state.decisions


def get_company_store(request: Request) -> CompanyStore:
    """The company profile's store, the tender processor's."""
    return request.app.state.tender_processor.company_store


def get_deep_analyses(request: Request) -> DeepAnalysisRunner:
    """The app's DeepAnalysisRunner, created in create_app()."""
    return request.app.state.deep_analyses


def get_watchlist(request: Request) -> WatchlistRunner:
    """The app's WatchlistRunner, created in create_app()."""
    return request.app.state.watchlist


# Tender files of these types open in the browser. Any other is downloaded, so a web page or script among a
# tender's files never runs as part of the app
INLINE_TYPES = {'application/pdf', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'text/plain'}

Processor = Annotated[TenderProcessor, Depends(get_processor)]
# A month of the profile-gap report, YYYY-MM; the current month when left out
Month = Annotated[str | None, Query(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')]
Decisions = Annotated[DecisionStore, Depends(get_decisions)]
Company = Annotated[CompanyStore, Depends(get_company_store)]
DeepAnalyses = Annotated[DeepAnalysisRunner, Depends(get_deep_analyses)]
Watchlists = Annotated[WatchlistRunner, Depends(get_watchlist)]


class PageQuery(BaseModel):
    """
    How a list is sorted (by closing date or tender value, asc or desc), and which page of it (from 1) with how many
    tenders (one of tender_service.PAGE_SIZES, checked there: a Literal of numbers would refuse the query string's
    "50").
    """
    model_config = ConfigDict(extra='forbid')

    sort: Literal['closing', 'value'] = 'closing'
    order: Literal['asc', 'desc'] = 'asc'
    page: int = Field(1, ge=1)
    page_size: int = tender_service.TENDER_PAGE_SIZE


class TenderQuery(TenderFilters, PageQuery):
    """TenderTiger's search filters, as query parameters, and which page of what they find; TenderTiger sorts it."""


class ProcessRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)  # TCNOs may be sent as numbers

    tcnos: list[str] = Field(min_length=1)
    # Delete the tender's Pinecone namespace, download and read the documents again and rebuild the namespace,
    # instead of re-checking against the namespace an earlier run built
    regenerate: bool = False
    # Read every scanned page even when the OCR is over the tender's budget (OCR_BUDGET_PAGES / OCR_BUDGET_USD),
    # instead of stopping before it to ask
    approve_ocr: bool = False


class EstimateRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    tcnos: list[str] = Field(min_length=1)


class DecisionRequest(BaseModel):
    decision: Literal['reviewed', 'shortlisted', 'dropped']


class StatusRequest(BaseModel):
    status: Literal['met', 'not_met', 'unknown'] | None  # required; null goes back to the model's status


class CheckedRequest(BaseModel):
    checked: bool


# The company profile as the editor's form blocks; see company_service.parse_profile()

class HeadingBlock(BaseModel):
    type: Literal['heading']
    level: int = Field(ge=1, le=6)
    text: str


class TableBlock(BaseModel):
    type: Literal['table']
    columns: list[str] = Field(min_length=1)
    rows: list[list[str]]


class TextBlock(BaseModel):
    type: Literal['text']
    text: str


class RuleBlock(BaseModel):
    type: Literal['rule']


ProfileBlock = Annotated[HeadingBlock | TableBlock | TextBlock | RuleBlock, Field(discriminator='type')]


class CompanyRequest(BaseModel):
    blocks: list[ProfileBlock] = Field(min_length=1)


# Endpoints are plain functions, not coroutines: they call blocking code (the tender API, file I/O), so
# FastAPI runs them in its thread pool.

@api.get('/tenders')
def list_tenders(processor: Processor, decisions: Decisions, watchlists: Watchlists,
                 query: Annotated[TenderQuery, Query()]):
    """
    Returns one page (?page=, from 1, of ?page_size= 20, 50, 100 or 500 tenders) of the tenders TenderTiger finds
    with the filters given as query parameters (none by default), sorted by TenderTiger (?sort=closing or value,
    ?order=asc or desc; closing date, soonest first, by default), each as a table row with the result of any
    already processed, the team's decision and the watchlist department that claims it (null for none). TenderTiger
    gives no total, so has_more only says the page was full: the next one may still be empty.
    """
    try:
        tenders = tender_service.load_tenders(query.model_dump(exclude={'page', 'page_size', 'sort', 'order'}),
                                              page=query.page, sort=query.sort, order=query.order,
                                              page_size=query.page_size)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    rows = _rows(tenders, processor, decisions, watchlists)
    return {'count': len(rows), 'tenders': rows, 'page': query.page, 'page_size': query.page_size,
            'has_more': len(tenders) >= query.page_size, 'sort': query.sort, 'order': query.order}


def _rows(tenders, processor, decisions, watchlists):
    """The raw tenders as table rows, each with its result, the team's decision and its watchlist department."""
    # Kept with the saved results, so the date is still known once TenderTiger stops listing a closed tender
    processor.remember_closing_dates(tenders)
    results = processor.summaries()
    recorded = decisions.all()
    rows = []
    for tender, department in zip(tenders, watchlists.departments(tenders)):
        row = tender_service.to_row(tender)
        row['result'] = results.get(row['tcno'])
        row['decision'] = recorded.get(row['tcno'])
        # Its documents are indexed, so a check re-uses them: its last result names its Pinecone namespace. From the
        # database, not Pinecone, so a list never waits on Pinecone; the check itself asks Pinecone
        row['has_indexed_criteria'] = bool((row['result'] or {}).get('namespace'))
        row['watchlist'] = department
        rows.append(row)
    return rows


@api.get('/watchlist')
def watchlist(processor: Processor, decisions: Decisions, watchlists: Watchlists,
              query: Annotated[PageQuery, Query()]):
    """
    One page of the watchlist: the tenders its last search saved (the watchlist_tenders table), as they are,
    sorted here (?sort=closing or value, ?order=asc or desc), as table rows like /api/tenders. total is how many
    there are; watchlist is the refresh state, as GET /api/watchlist/status gives it. 500 when the database cannot
    be read.
    """
    try:
        tenders, total = watchlists.page(query.page, query.page_size, query.sort, query.order)
    except WatchlistMissing as exc:
        raise HTTPException(404, str(exc)) from None
    except WatchlistError as exc:
        raise HTTPException(500, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    rows = _rows(tenders, processor, decisions, watchlists)
    return {'count': len(rows), 'tenders': rows, 'page': query.page, 'page_size': query.page_size,
            'has_more': query.page * query.page_size < total, 'total': total, 'sort': query.sort,
            'order': query.order, 'watchlist': watchlists.status()}


@api.get('/watchlist/status')
def watchlist_status(watchlists: Watchlists):
    """
    Whether the watchlist is being refreshed and how far it got, when it was last refreshed, and how many live
    tenders and departments it holds: see WatchlistRunner.status().
    """
    return watchlists.status()


class DepartmentRule(BaseModel):
    """A department of the watchlist, as GET /api/watchlist/rules gives it."""
    model_config = ConfigDict(extra='forbid')
    sl: int | None = None
    sector: str | None = None
    name: str = ''
    full_name: str | None = None
    organisations: list[str] = []
    search: list[str] = []
    states: list[str] | Literal['any'] = 'any'
    brief: str | None = None
    min_value_cr: float | None = None


class WatchlistRules(BaseModel):
    """The watchlist's rules, as GET /api/watchlist/rules gives them."""
    model_config = ConfigDict(extra='forbid')
    about: str | None = None
    filters: dict[str, str | bool | int | float] = {}
    min_value_cr: float | None = None
    state_groups: dict[str, list[str]] = {}
    departments: list[DepartmentRule] = []


@api.get('/watchlist/rules')
def watchlist_rules(watchlists: Watchlists):
    """
    The watchlist's rules, for its editor: {"about", "filters", "min_value_cr", "state_groups", "departments":
    [{"sl", "sector", "name", "full_name", "organisations", "search", "states", "brief", "min_value_cr"}]}, a
    value left out when there is none (see watchlist_service.read_rules()). Before any, the usual filters and no
    department.
    """
    return watchlists.rules()


@api.put('/watchlist/rules')
def save_watchlist_rules(payload: WatchlistRules, watchlists: Watchlists):
    """
    Saves the watchlist's rules edited on the page, in the shape GET gives, in place of the last. A change applies
    to the list at once; a department or state added is found by the next search. Returns the rules as saved. 400,
    naming the department, when they are not a valid watchlist (a bad pattern, an unknown state, a search name its
    patterns do not match, no department): then nothing changes.
    """
    try:
        return watchlists.save_rules(payload.model_dump(exclude_none=True))
    except WatchlistError as exc:
        raise HTTPException(400, str(exc)) from None


@api.post('/watchlist/refresh', status_code=202)
def refresh_watchlist(watchlists: Watchlists):
    """
    Searches TenderTiger again, in the background, for every organisation the watchlist names; one refresh at a
    time (asking while one runs changes nothing). Returns 202 with the status. 404 without a watchlist, 500 when it
    is not valid.
    """
    try:
        return watchlists.start()
    except WatchlistMissing as exc:
        raise HTTPException(404, str(exc)) from None
    except WatchlistError as exc:
        raise HTTPException(500, str(exc)) from None


@api.post('/process-tenders', status_code=202)
def process_tenders(payload: ProcessRequest, processor: Processor):
    """
    Queues the tenders posted as {"tcnos": ["...", ...], "regenerate": false, "approve_ocr": false}, in that order.
    Starts a job, or adds them to the running one. A tender with a Pinecone namespace is re-checked against it unless
    regenerate is true (then the documents are read again and the namespace brought up to date, only the sources that
    changed being upserted again). A tender whose scans need more
    OCR than the budget stops before it, unless approve_ocr is true. Returns 202 with the job's progress.
    """
    tcnos = list(dict.fromkeys(payload.tcnos))  # drop duplicates, keep order
    tenders = [tender_service.get_tender(tcno) for tcno in tcnos]
    unknown = [tcno for tcno, tender in zip(tcnos, tenders) if tender is None]
    if unknown:
        raise HTTPException(400, f"Unknown TCNO(s): {', '.join(unknown)}. Reload the tender list and try again.")
    return processor.start(tenders, regenerate=payload.regenerate, approve_ocr=payload.approve_ocr)


@api.post('/ocr-estimate')
def ocr_estimate(payload: EstimateRequest, processor: Processor):
    """
    What regenerating the posted tenders, {"tcnos": [...]}, would cost in OCR, from the documents already
    downloaded: see TenderProcessor.ocr_estimates(). Nothing is queued or read.
    """
    return processor.ocr_estimates(payload.tcnos)


@api.post('/stop', status_code=202)
def stop(processor: Processor):
    """Cancels the tenders still queued; the one being processed finishes. Returns 202 with the job's progress."""
    try:
        return processor.stop()
    except NoJobRunning:
        raise HTTPException(409, 'No tenders are being processed.') from None


@api.get('/progress')
def progress(processor: Processor):
    """Returns the state of the current or last processing job (status is idle, running or completed)."""
    return processor.progress()


@api.get('/result/{tcno}')
def result(tcno: str, processor: Processor):
    """Returns the full result of a processed tender, including the details shown in the modal."""
    tender_result = processor.result(tcno)
    if tender_result is None:
        raise HTTPException(404, f'No result for TCNO {tcno} yet.')
    return tender_result


@api.get('/files/{tcno}/{name:path}')
def tender_file(tcno: str, name: str, processor: Processor):
    """
    Returns one of the tender's downloaded documents, name being its path as the result's files list it. PDFs,
    images and plain text open in the browser; other types (Word, Excel, zip, ...) are downloaded.
    """
    path = processor.tender_file(tcno, name)
    if path is None:
        raise HTTPException(404, f'Tender {tcno} has no file {name}. Its documents may have been downloaded again.')
    media_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return FileResponse(path, media_type=media_type, filename=path.name,
                        content_disposition_type='inline' if media_type in INLINE_TYPES else 'attachment',
                        headers={'X-Content-Type-Options': 'nosniff'})


@api.put('/result/{tcno}/criteria/{index}')
def mark_criterion(tcno: str, index: int, payload: StatusRequest, processor: Processor):
    """
    Sets the status of one criterion by hand, posted as {"status": "met" | "not_met" | "unknown" | null}, where
    null goes back to the model's; index is its position in the result's criteria. The verdict then follows the
    statuses. Returns the updated full result.
    """
    return _change_result(tcno, lambda: processor.mark_criterion(tcno, index, payload.status))


@api.put('/result/{tcno}/unreadable-checked')
def check_unreadable(tcno: str, payload: CheckedRequest, processor: Processor):
    """
    Records that the team checked the files the model could not read itself, posted as {"checked": true}, or
    takes it back with false. Checked, they no longer keep the verdict from Eligible. Returns the updated result.
    """
    return _change_result(tcno, lambda: processor.check_unreadable(tcno, payload.checked))


@api.post('/deep-analysis/{tcno}', status_code=202)
def start_deep_analysis(tcno: str, deep: DeepAnalyses):
    """
    Starts a deep analysis of the tender's completed result: five agents review its criteria, the company profile,
    the result and the statuses marked by hand, and the report is saved in the database.
    One runs at a time; others wait their turn. Returns 202 with its state, as GET returns it.
    """
    try:
        return deep.start(tcno)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except TenderBusy:
        raise HTTPException(409, f'Tender {tcno} is being checked. Run the deep analysis once that is done.') from None
    except DeepAnalysisRunning:
        raise HTTPException(409, f'A deep analysis of tender {tcno} is already running.') from None


@api.get('/deep-analysis/{tcno}')
def deep_analysis(tcno: str, deep: DeepAnalyses):
    """
    The tender's deep analysis: whether one is queued, running, completed or failed (or none), the progress of a
    running one, and the last saved report, with stale true when the result or company profile changed since.
    """
    return deep.state(tcno)


@api.get('/deep-analysis/{tcno}/file')
def deep_analysis_file(tcno: str, deep: DeepAnalyses):
    """Downloads the tender's saved deep analysis report as deep_analysis_<tcno>.json."""
    report = deep.report(tcno)
    if report is None:
        raise HTTPException(404, f'Tender {tcno} has no deep analysis yet.')
    return Response(json.dumps(report, indent=2, ensure_ascii=False), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="deep_analysis_{safe_name(tcno)}.json"',
                             'X-Content-Type-Options': 'nosniff'})


@api.get('/closed-downloads')
def closed_downloads(processor: Processor):
    """
    The checked tenders on record whose closing date has passed, longest closed first, and those with no closing
    date on record: {"closed": [...], "unknown": [...]}, each {"tcno", "name", "closing_date", "days_closed"}. From
    the database alone.
    """
    return processor.closed_downloads()


@api.get('/stray-downloads')
def stray_downloads(processor: Processor):
    """
    The tender folders in the download folder the database holds no result for (a check that never finished, an
    older version's): {"directory", "tenders": [{"tcno", "name", "files", "bytes"}]}, largest first. Looks through the
    download folder; DELETE /api/downloads/<tcno> deletes one.
    """
    return processor.stray_downloads()


@api.delete('/downloads/{tcno}')
def delete_downloads(tcno: str, processor: Processor, decisions: Decisions, deep: DeepAnalyses):
    """
    Deletes everything kept for the tender: its Pinecone namespace, its downloaded documents and deep analysis, its
    result and job items in the database, and the team's decision, so it reads as never checked. Only the statuses
    marked by hand stay, recorded with the bid team's marks. Returns {"tcno", "files", "bytes", "namespace_deleted"}:
    how many documents went, their size, and whether there was a namespace. 503 without a Pinecone key: then nothing
    is deleted, so no tender leaves its records in Pinecone behind.
    """
    if deep.running(tcno):
        raise HTTPException(409, f'A deep analysis of tender {tcno} is running. Delete the tender once it is done.')
    try:
        deleted = processor.delete_tender(tcno)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except TenderBusy:
        raise HTTPException(409, f'Tender {tcno} is being checked. Delete it once that is done.') from None
    except OSError as exc:
        log.exception('Could not delete the files of tender %s', tcno)
        raise HTTPException(409, f'Could not delete every file of tender {tcno}: {exc}. Close any program that has '
                                 'one open and try again.') from None
    deep.forget(tcno)
    decisions.forget(tcno)
    return deleted


@api.get('/namespace/{tcno}')
def namespace_status(tcno: str, processor: Processor):
    """
    The tender's Pinecone namespace: its name and index, whether it exists and is complete, and what it holds
    (sources with their chunk counts, the sources that could not be read, records, embedding model, when it was
    built).
    """
    return processor.index.status(tcno)


def _change_result(tcno, change):
    """Runs change, a hand edit of the tender's result, with its errors as HTTP errors."""
    try:
        return change()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except TenderBusy:
        raise HTTPException(409, f'Tender {tcno} is being checked. Change its result once that is done.') from None


@api.put('/decision/{tcno}')
def set_decision(tcno: str, payload: DecisionRequest, decisions: Decisions):
    """
    Marks the team's decision on a tender, posted as {"decision": "reviewed" | "shortlisted" | "dropped"}. A tender
    starts pending and only moves forward: pending to any of them, reviewed to shortlisted or dropped, shortlisted to
    dropped; dropped is final. 409, saying what it can be marked, for one that does not follow.
    """
    if tender_service.get_tender(tcno) is None:
        raise HTTPException(404, f'Unknown TCNO {tcno}. Reload the tender list and try again.')
    try:
        return {'tcno': tcno, 'decision': decisions.mark(tcno, payload.decision)}
    except DecisionNotAllowed as exc:
        raise HTTPException(409, str(exc)) from None


@api.get('/company')
def get_company(company: Company):
    """
    Returns the company profile every tender is checked against: as markdown, as the editor's form blocks,
    and the company name of the app bar.
    """
    return _company(company.read())


@api.get('/profile-gaps')
def profile_gaps(processor: Processor, company: Company, month: Month = None):
    """
    The profile-gap report of ?month=YYYY-MM (the current month by default): why the checks of that month marked
    criteria Cannot determine, most frequent reason first, and whether the company profile states each now.
    """
    return profile_gap_report(processor.saved_results(), company.read(), month)


@api.get('/profile-gaps/file')
def profile_gaps_file(processor: Processor, company: Company, month: Month = None):
    """Downloads the profile-gap report of ?month=YYYY-MM (the current month by default) as markdown."""
    report = profile_gap_report(processor.saved_results(), company.read(), month)
    return Response(report_markdown(report), media_type='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="profile-gaps-{report["month"]}.md"',
                             'X-Content-Type-Options': 'nosniff'})


@api.put('/company')
def save_company(payload: CompanyRequest, company: Company):
    """
    Saves the company profile edited in the form, posted as {"blocks": [...]} in the shape GET returns, as
    markdown in the database. Tenders checked from now on use it; existing results are not re-checked.
    """
    blocks = [block.model_dump() for block in payload.blocks]
    unnamed = unnamed_part(blocks)
    if unnamed:
        raise HTTPException(400, unnamed)
    details = render_profile(blocks)
    if not details:
        raise HTTPException(400, 'The company details cannot be empty.')
    company.save(details)
    log.info('Company profile saved')
    return _company(details)


def _company(details):
    return {'details': details, 'blocks': parse_profile(details) or EMPTY_PROFILE, **company_profile(details)}


def _error(message, status):
    return JSONResponse({'error': message}, status_code=status)


async def http_error(request, exc):
    """HTTPException, including FastAPI's own 404 / 405, as {"error": ...} instead of {"detail": ...}."""
    return JSONResponse({'error': exc.detail}, status_code=exc.status_code, headers=exc.headers)


async def validation_error(request, exc):
    """A request body that does not match its model is a 400 with a hint, not FastAPI's 422."""
    route = request.scope.get('route')
    message = BODY_ERRORS.get(getattr(route, 'name', None))
    if message is None:
        first = exc.errors()[0]
        message = f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
    return _error(message, 400)


async def tender_service_error(request, exc):
    """The upstream tender API failed: report it as a bad gateway."""
    return _error(str(exc), 502)


async def vector_store_error(request, exc):
    """Pinecone is not configured (503) or failed (502)."""
    return _error(str(exc), 503 if isinstance(exc, VectorStoreNotConfigured) else 502)


async def unexpected_error(request, exc):
    """Keeps unhandled errors in JSON. Starlette re-raises the exception afterwards, so the server logs it."""
    return _error('Internal server error. See logs/app.log for details.', 500)


EXCEPTION_HANDLERS = {
    StarletteHTTPException: http_error,
    RequestValidationError: validation_error,
    TenderServiceError: tender_service_error,
    VectorStoreError: vector_store_error,
    Exception: unexpected_error,
}
