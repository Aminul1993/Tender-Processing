"""JSON API used by the tender page. Every error response has the shape {"error": "<message>"}."""
import logging
import mimetypes
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ext import company_details, save_company_details
from services import tender_service
from services.company_service import EMPTY_PROFILE, company_profile, parse_profile, render_profile, unnamed_part
from services.decision_service import DecisionStore
from services.deep_analysis_service import DeepAnalysisRunner, DeepAnalysisRunning
from services.processing_service import NoJobRunning, TenderBusy, TenderProcessor
from services.tender_service import TenderServiceError
from utils.files import safe_name

log = logging.getLogger(__name__)

api = APIRouter(prefix='/api')

# What to tell the caller when a request body fails validation, by endpoint name
BODY_ERRORS = {
    'process_tenders': ('Send a JSON body like {"tcnos": ["100091540"]} with at least one TCNO, and optionally '
                        '"regenerate": true.'),
    'set_decision': 'Send a JSON body like {"decision": "dropped"}; use "shortlisted", "dropped" or null.',
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


def get_company_file(request: Request) -> Path:
    """The company profile file, COMPANY_DETAILS_FILE in the config given to create_app()."""
    return request.app.state.company_file


def get_deep_analyses(request: Request) -> DeepAnalysisRunner:
    """The app's DeepAnalysisRunner, created in create_app()."""
    return request.app.state.deep_analyses


# Tender files of these types open in the browser. Any other is downloaded, so a web page or script among a
# tender's files never runs as part of the app
INLINE_TYPES = {'application/pdf', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'text/plain'}

Processor = Annotated[TenderProcessor, Depends(get_processor)]
Decisions = Annotated[DecisionStore, Depends(get_decisions)]
CompanyFile = Annotated[Path, Depends(get_company_file)]
DeepAnalyses = Annotated[DeepAnalysisRunner, Depends(get_deep_analyses)]


Comparison = Literal['gt', 'gte', 'lt', 'lte', 'eq', 'between']
Amount = Annotated[Decimal | None, Field(ge=0)]


class TenderFilters(BaseModel):
    """The filters of TenderTiger's search page, as query parameters; see tender_service.search_fields()."""
    model_config = ConfigDict(extra='forbid')  # a misspelt filter is an error, not silently ignored

    word: str = ''
    organisation: str = ''
    location: str = ''
    tid: str = ''
    industry: str = ''
    published_from: date | None = None
    published_to: date | None = None
    closing_from: date | None = None
    closing_to: date | None = None
    qty_op: Comparison = 'gt'
    qty: Amount = None
    qty_to: Amount = None
    value_op: Comparison = 'gt'
    value: Amount = None
    value_to: Amount = None
    emd_op: Comparison = 'gt'
    emd: Amount = None
    emd_to: Amount = None
    gem: Literal['gem', 'non_gem'] | None = None
    bidding: Literal['ncb', 'icb'] | None = None
    funding: Literal['funded', 'non_funded'] | None = None
    msme: bool = False
    bid_doc: bool = False
    startup: bool = False


class TenderQuery(TenderFilters):
    """
    The filters, how TenderTiger sorts what they find (by closing date or tender value, asc or desc), and which
    page of it (from 1) with how many tenders (one of tender_service.PAGE_SIZES, checked there: a Literal of
    numbers would refuse the query string's "50").
    """
    sort: Literal['closing', 'value'] = 'closing'
    order: Literal['asc', 'desc'] = 'asc'
    page: int = Field(1, ge=1)
    page_size: int = tender_service.TENDER_PAGE_SIZE


class ProcessRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)  # TCNOs may be sent as numbers

    tcnos: list[str] = Field(min_length=1)
    # Read the documents again and save new criteria, instead of checking the criteria saved by an earlier run
    regenerate: bool = False


class DecisionRequest(BaseModel):
    decision: Literal['shortlisted', 'dropped'] | None  # required; null clears the decision


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
def list_tenders(processor: Processor, decisions: Decisions, query: Annotated[TenderQuery, Query()]):
    """
    Returns one page (?page=, from 1, of ?page_size= 20, 50, 100 or 500 tenders) of the tenders TenderTiger finds
    with the filters given as query parameters (none by default), sorted by TenderTiger (?sort=closing or value,
    ?order=asc or desc; closing date, soonest first, by default), each as a table row with the result of any
    already processed and the team's decision. TenderTiger gives no total, so has_more only says the page was
    full: the next one may still be empty.
    """
    try:
        tenders = tender_service.load_tenders(query.model_dump(exclude={'page', 'page_size', 'sort', 'order'}),
                                              page=query.page, sort=query.sort, order=query.order,
                                              page_size=query.page_size)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    results = processor.summaries()
    recorded = decisions.all()
    rows = []
    for tender in tenders:
        row = tender_service.to_row(tender)
        row['result'] = results.get(row['tcno'])
        row['decision'] = recorded.get(row['tcno'])
        row['has_saved_criteria'] = processor.criteria_file(row['tcno']).is_file()
        rows.append(row)
    return {'count': len(rows), 'tenders': rows, 'page': query.page, 'page_size': query.page_size,
            'has_more': len(tenders) >= query.page_size, 'sort': query.sort, 'order': query.order}


@api.post('/process-tenders', status_code=202)
def process_tenders(payload: ProcessRequest, processor: Processor):
    """
    Queues the tenders posted as {"tcnos": ["...", ...], "regenerate": false}, in that order. Starts a job, or
    adds them to the running one. A tender with saved criteria is checked against them unless regenerate is
    true. Returns 202 with the job's progress.
    """
    tcnos = list(dict.fromkeys(payload.tcnos))  # drop duplicates, keep order
    tenders = [tender_service.get_tender(tcno) for tcno in tcnos]
    unknown = [tcno for tcno, tender in zip(tcnos, tenders) if tender is None]
    if unknown:
        raise HTTPException(400, f"Unknown TCNO(s): {', '.join(unknown)}. Reload the tender list and try again.")
    return processor.start(tenders, regenerate=payload.regenerate)


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
    the result and the statuses marked by hand, and the report is saved beside the result as deep_analysis.json.
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
    """Downloads the tender's saved deep analysis report, deep_analysis.json."""
    path = deep.path(tcno)
    if not path.is_file():
        raise HTTPException(404, f'Tender {tcno} has no deep analysis yet.')
    return FileResponse(path, media_type='application/json', filename=f'deep_analysis_{safe_name(tcno)}.json',
                        content_disposition_type='attachment', headers={'X-Content-Type-Options': 'nosniff'})


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
    """Records the team's decision on a tender, posted as {"decision": "shortlisted" | "dropped" | null}."""
    if tender_service.get_tender(tcno) is None:
        raise HTTPException(404, f'Unknown TCNO {tcno}. Reload the tender list and try again.')
    return {'tcno': tcno, 'decision': decisions.set(tcno, payload.decision)}


@api.get('/company')
def get_company(company_file: CompanyFile):
    """
    Returns the company profile every tender is checked against: as markdown, as the editor's form blocks,
    and the company name of the app bar.
    """
    return _company(company_details(company_file))


@api.put('/company')
def save_company(payload: CompanyRequest, company_file: CompanyFile):
    """
    Saves the company profile edited in the form, posted as {"blocks": [...]} in the shape GET returns, as
    markdown in COMPANY_DETAILS_FILE. Tenders checked from now on use it; existing results are not re-checked.
    """
    blocks = [block.model_dump() for block in payload.blocks]
    unnamed = unnamed_part(blocks)
    if unnamed:
        raise HTTPException(400, unnamed)
    details = render_profile(blocks)
    if not details:
        raise HTTPException(400, 'The company details cannot be empty.')
    save_company_details(details, company_file)
    log.info('Company profile saved to %s', company_file)
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


async def unexpected_error(request, exc):
    """Keeps unhandled errors in JSON. Starlette re-raises the exception afterwards, so the server logs it."""
    return _error('Internal server error. See logs/app.log for details.', 500)


EXCEPTION_HANDLERS = {
    StarletteHTTPException: http_error,
    RequestValidationError: validation_error,
    TenderServiceError: tender_service_error,
    Exception: unexpected_error,
}
