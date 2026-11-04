"""
Loads the tender list through ext.tender_list(), narrowed by the filters of TenderTiger's search page, and keeps
every tender listed in memory.
"""
import logging
import threading
from datetime import date, datetime
from urllib.parse import urlencode

from ext import TENDER_PAGE_SIZE, TENDERTIGER_URL, tender_list

log = logging.getLogger(__name__)


class TenderServiceError(Exception):
    """The tender list could not be loaded."""


_lock = threading.Lock()
# tcno (str) -> raw tender dict, as last listed. Kept across loads, so a tender listed under other filters (in
# another browser, say) can still be processed.
_tenders = {}

# The filters, by the names the API takes, and the search form fields TenderTiger's search page posts for them
TEXT_FILTERS = {'word': 'wwordsearch', 'organisation': 'worgname', 'location': 'wlocation', 'tid': 'wtenderrefno',
                'industry': 'wcompanysubindustry'}
DATE_FILTERS = {'published': 'wtenderhostdate', 'closing': 'wtenderclosingdate'}
# name -> (what the error messages call it, comparison field, value field, range start field, range end field)
AMOUNT_FILTERS = {
    'qty': ('quantity', 'qtyType', 'qtyValue', 'fromQty', 'toQty'),
    'value': ('tender value', 'valType', 'tenderValue', 'fromVal', 'toVal'),
    'emd': ('EMD', 'emdType', 'emdValue', 'fromEmd', 'toEmd'),
}
COMPARISONS = {'gt': 'GreaterThan', 'gte': 'GreaterThanEqual', 'lt': 'LessThan', 'lte': 'LessThanEqual',
               'eq': 'EqualTo', 'between': 'Between'}
# One of two kinds of tender (the page's paired checkboxes): name -> (field, {choice: value})
CHOICE_FILTERS = {
    'gem': ('gemtender', {'gem': '1', 'non_gem': '0'}),
    'bidding': ('wbiddingtype', {'ncb': 'NCB', 'icb': 'ICB'}),
    'funding': ('wfundingAgency', {'funded': 'Funded', 'non_funded': 'NonFunded'}),
}
# Only tenders with this mark: name -> (field, value when set)
FLAG_FILTERS = {'msme': ('wismse', 'ismse'), 'bid_doc': ('isotherdocument', 'isotherdocument'),
                'startup': ('isstartup', '1')}
# The ends a date range open on one side is closed with
OPEN_DATES = (date(2000, 1, 1), date(2099, 12, 31))
# The orders the list can be sorted in, by the names the API takes, and the columns TenderTiger sorts them by
SORT_COLUMNS = {'closing': 'closingdate', 'value': 'tendervalue'}
SORT_ORDERS = ('asc', 'desc')
# How many tenders a page of the list may hold; TenderTiger sends a page of any of them in one request
PAGE_SIZES = (TENDER_PAGE_SIZE, 50, 100, 500)


def load_tenders(filters=None, page=1, sort='closing', order='asc', page_size=TENDER_PAGE_SIZE):
    """
    Fetches one page (page_size tenders, one of PAGE_SIZES; page from 1) of json_response['TenderList'], narrowed
    by filters (see search_fields()) and sorted by TenderTiger before it is paged: by sort (a SORT_COLUMNS name,
    closing date by default) in order (asc or desc). Keeps them in memory and returns them as a list of raw dicts;
    past the last page it is empty. Raises ValueError for invalid filters, sorting or page size, before asking
    TenderTiger, and TenderServiceError if the API fails.
    """
    fields = search_fields(filters or {})
    if sort not in SORT_COLUMNS or order not in SORT_ORDERS:
        raise ValueError(f'Sort by {", ".join(SORT_COLUMNS)}, in order {" or ".join(SORT_ORDERS)}.')
    if page_size not in PAGE_SIZES:
        raise ValueError(f'Show {", ".join(map(str, PAGE_SIZES[:-1]))} or {PAGE_SIZES[-1]} tenders a page.')
    try:
        json_response = tender_list(fields, page=page, sort=(SORT_COLUMNS[sort], order), page_size=page_size)
    except Exception as exc:
        log.exception('Could not fetch the tender list')
        raise TenderServiceError(f'Could not fetch the tender list: {exc}') from exc

    tenders = (json_response or {}).get('TenderList')
    if not isinstance(tenders, list):
        log.error('Unexpected tender list response: %.200s', json_response)
        raise TenderServiceError('Unexpected tender list response: it has no TenderList')

    with _lock:
        _tenders.update({str(t['tcno']): t for t in tenders if t.get('tcno') is not None})
    log.info('Loaded %d tenders (page %d, %d a page, by %s %s)%s', len(tenders), page, page_size, sort, order,
             f' with filters {fields}' if fields else '')
    return tenders


def search_fields(filters):
    """
    The fields TenderTiger's search page posts for filters, a dict with the keys of routes.api.TenderFilters:
      word, organisation, location, tid, industry  text: word search, organisation name, location, TID or tender
                                                   number, company industry
      published_from, published_to,                dates (datetime.date); a range may be open at one end
      closing_from, closing_to
      qty, value, emd                              an amount (value and EMD in rupees) compared by qty_op, value_op
                                                   or emd_op: gt (the default), gte, lt, lte, eq, or between it and
                                                   qty_to, value_to or emd_to
      gem, bidding, funding                        gem or non_gem, ncb or icb, funded or non_funded
      msme, bid_doc, startup                       true for MSME tenders, those with bid documents, startup tenders
    Filters left empty are left out. Raises ValueError for a range with one end missing or ending before it starts.
    """
    fields = {}
    for name, field in TEXT_FILTERS.items():
        text = ' '.join(str(filters.get(name) or '').split())
        if text:
            # The search page sends the word search in lower case
            fields[field] = text.lower() if name == 'word' else text
    for name, field in DATE_FILTERS.items():
        start, end = filters.get(f'{name}_from'), filters.get(f'{name}_to')
        if start or end:
            start, end = start or OPEN_DATES[0], end or OPEN_DATES[1]
            if start > end:
                raise ValueError(f'The {name} date range ends before it starts.')
            # As the search page writes a range; built from dates only, so no other text gets into it
            fields[field] = f"{start:%Y-%m-%d}' and '{end:%Y-%m-%d}"
    for name, (label, comparison, value_field, start_field, end_field) in AMOUNT_FILTERS.items():
        value, end, how = filters.get(name), filters.get(f'{name}_to'), filters.get(f'{name}_op') or 'gt'
        if how == 'between' and (value is not None or end is not None):
            if value is None or end is None:
                raise ValueError(f'Give both ends of the {label} range.')
            if value > end:
                raise ValueError(f'The {label} range ends before it starts.')
            fields.update({comparison: COMPARISONS[how], start_field: _amount(value), end_field: _amount(end)})
        elif how != 'between' and value is not None:
            fields.update({comparison: COMPARISONS[how], value_field: _amount(value)})
    for name, (field, values) in CHOICE_FILTERS.items():
        if filters.get(name):
            fields[field] = values[filters[name]]
    for name, (field, value) in FLAG_FILTERS.items():
        if filters.get(name):
            fields[field] = value
    return fields


def _amount(number):
    """A number as the search form sends it: 500000000, 2.5; never 5E+8."""
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def get_tender(tcno):
    """Returns the raw tender with this TCNO, or None. Loads the list first if it was never loaded."""
    with _lock:
        loaded = bool(_tenders)
    if not loaded:
        load_tenders()
    with _lock:
        return _tenders.get(str(tcno))


def has_documents(tender):
    """True when TenderTiger has documents for the tender, to download with DownloadAllFiles."""
    return str(tender.get('isdocument')) == '1'


def prequalification_url(tender):
    """The tender's GetPreQualification link on TenderTiger, or None without the IDs it needs."""
    if not tender.get('tcno') or not tender.get('tenderprocid'):
        return None
    query = urlencode({'hdnTcNo': tender['tcno'], 'hdntenderprocId': tender['tenderprocid']})
    return f'{TENDERTIGER_URL}/TenderDetail/GetPreQualification?{query}'


def to_row(tender):
    """
    Picks the fields the tender table shows. The browser only ever sends TCNOs back; the server looks up the
    TenderTiger IDs the downloads need.
    """
    closing = _parse_date(tender.get('closingdate'))
    return {
        'tcno': str(tender.get('tcno')),
        'name': _text(tender.get('tendersbrief')),
        'reference': _text(tender.get('tenderrefno')),
        'organisation': _text(tender.get('companyname')),
        'location': ', '.join(filter(None, (_text(tender.get('cityname')), _text(tender.get('statename'))))),
        'published_date': _date_text(tender.get('publicationdate')),
        'closing_date': _date_text(tender.get('closingdate')),
        'days_left': (closing - date.today()).days if closing else None,
        'tender_value': _number(tender.get('tendervalue')),
        'currency': _text(tender.get('currcode')).upper(),
        'has_document': has_documents(tender),
        # Without documents, a tender is checked on its pre-qualification criteria alone
        'has_prequalification': bool(prequalification_url(tender)),
    }


def _text(value):
    """API text fields can be None or contain line breaks; return one clean line."""
    return '' if value is None else ' '.join(str(value).split())


def _parse_date(value):
    """The list's dates are DD-MM-YYYY; returns a date, or None."""
    try:
        return datetime.strptime(_text(value), '%d-%m-%Y').date()
    except ValueError:
        return None


def _date_text(value):
    """A DD-MM-YYYY date as the table shows it, "21-Nov-2026"; anything else as it came."""
    parsed = _parse_date(value)
    return parsed.strftime('%d-%b-%Y') if parsed else _text(value)


def _number(value):
    """The list sends amounts as text; returns a float, or None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
