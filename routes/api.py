"""JSON API used by the tender page. Every error response has the shape {"error": "<message>"}."""
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ext import company_details, save_company_details
from services import tender_service
from services.company_service import EMPTY_PROFILE, company_profile, parse_profile, render_profile, unnamed_part
from services.decision_service import DecisionStore
from services.processing_service import NoJobRunning, TenderProcessor
from services.tender_service import TenderServiceError

log = logging.getLogger(__name__)

api = APIRouter(prefix='/api')

# What to tell the caller when a request body fails validation, by endpoint name
BODY_ERRORS = {
    'process_tenders': ('Send a JSON body like {"tcnos": ["100091540"]} with at least one TCNO, and optionally '
                        '"regenerate": true.'),
    'set_decision': 'Send a JSON body like {"decision": "dropped"}; use "shortlisted", "dropped" or null.',
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


Processor = Annotated[TenderProcessor, Depends(get_processor)]
Decisions = Annotated[DecisionStore, Depends(get_decisions)]
CompanyFile = Annotated[Path, Depends(get_company_file)]


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
