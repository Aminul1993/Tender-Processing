"""
Reads every file of a tender, indexes them all in the tender's own Pinecone namespace (tcno-<tcno>, in the shared
index), retrieves the passages on eligibility from it and checks the company's eligibility against them with the
LLM pipeline of utils/llm.py:

  download -> extract text -> chunk -> embed -> upsert into namespace tcno-<tcno> -> retrieve relevant chunks
  -> assessment

A re-check skips everything before the retrieval: the namespace is the tender's saved criteria.
"""
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from markitdown import MarkItDown, __version__ as MARKITDOWN_VERSION

from services.cache_service import file_sha256
from services.ocr_service import OCR_PAGE_COST, OCR_SETTINGS, ScanTooLong, add_ocr, ocr_parts
from services.pinecone_service import Document, Retrieval, document_type
from services.tender_service import to_row
from utils.llm import (ASSESSMENT_MODEL, OCR_MODEL, SECTION_MAX_OUTPUT_TOKENS, SECTION_MODEL, assess_eligibility,
                       choose_sections, make_model)
from utils.tendertiger import TenderTigerBlocked, prequalification_criteria

log = logging.getLogger(__name__)

ELIGIBLE = 'Eligible'
NOT_ELIGIBLE = 'Not Eligible'
REQUIRES_REVIEW = 'Requires Review'

# The prompt asks the model to finish with "an overall verdict (Eligible, Not eligible, or Cannot determine)"
_VERDICT = re.compile(
    r'verdict[\s\S]{0,80}?\b(not[\s*_]+eligible|ineligible|cannot[\s*_]+determine|eligible)\b',
    re.IGNORECASE,
)
_VERDICT_LABELS = {'eligible': ELIGIBLE, 'not eligible': NOT_ELIGIBLE, 'ineligible': NOT_ELIGIBLE}

# The source names the tender's TenderTiger texts are indexed and checked under, beside the file names
PREQUALIFICATION_SOURCE = 'TenderTiger Pre-Qualification'
TENDER_DATA_SOURCE = 'TenderTiger Tender Data'
# Where versions before the database saved a tender's last result and deep analysis, in its folder: one left there
# is no document
LEGACY_RESULT_FILE = 'eligibility_result.json'
LEGACY_DEEP_ANALYSIS_FILE = 'deep_analysis.json'
# Tender files that hold no eligibility criteria, so are not read: drawings (one tender's drawings ran to 300k
# tokens and held none) and GeM item lists (.csv of items, quantities and consignees). A BOQ is still read,
# because it may give the estimated cost the criteria depend on.
_NO_CRITERIA = re.compile(r'drawing|\.csv$', re.IGNORECASE)
# What a file's text depends on besides the file, the key of its cached text with the file's SHA-256: a text read
# under other settings is read again. Bump the number when the reading code changes what it reads.
_READER = f'1; MarkItDown {MARKITDOWN_VERSION}; OCR {OCR_SETTINGS}'

# What the eligibility check retrieves from the tender's namespace before calling the LLM: a few searches for each kind
# of requirement, so every kind gets its share of the excerpts (see PineconeService.retrieve())
ASSESSMENT_QUERIES = {
    'eligibility': [
        'Eligibility criteria and pre-qualification requirements a bidder must meet',
        'Minimum qualification criteria: who may bid, registration, bidder must be a company, firm, OEM or authorised '
        'dealer',
    ],
    'financial': [
        'Minimum average annual turnover required in the last financial years',
        'Net worth, solvency certificate, working capital, bank guarantee and bid capacity formula',
        'Estimated cost, tender value, earnest money deposit (EMD) and performance security',
    ],
    'technical': [
        'Technical eligibility: certifications, licences, plant, equipment and key personnel the bidder must have',
        'Technical specifications and scope of work the bidder must be able to deliver',
    ],
    'experience': [
        'Similar work experience: completed works of a stated value in the last years',
        'Definition of similar work and the documents that prove experience, such as completion certificates',
    ],
    'jv': [
        'Joint venture or consortium allowed, maximum number of members, lead member share',
        'Qualifying criteria the lead member, the other members and the joint venture as a whole must each meet',
    ],
    'compliance': [
        'Documents to submit with the bid: GST, PAN, registration, ISO certificates, affidavits and declarations',
        'Exemptions and relaxations for MSE, MSME, Udyam or startup bidders; blacklisting and debarment conditions',
    ],
}
# A check too large for one request on the Groq plan (its 413, "request too large ... tokens per minute") is tried
# again with its excerpts split in two batches, a batch still too large split in two again, up to MAX_BATCHES. A
# batch that reaches the plan's per-minute limit (its 429, "rate limit reached ... try again in 12.3s") waits as long
# as Groq says, up to RATE_LIMIT_WAITS times, and no more than RATE_LIMIT_MAX_SECONDS each
MAX_BATCHES = 3
RATE_LIMIT_WAITS = 4
RATE_LIMIT_MAX_SECONDS = 90
# The section of the documents that states the qualification criteria, taken whole before the searches above fill
# the rest of the budget (hierarchical retrieval, see PineconeService.retrieve()), under this label. SECTION_MODEL
# chooses it by the documents' headings; when it cannot, these searches' matches vote for the section they are in
QUALIFICATION_SECTION = {
    'qualification section': [
        'Eligibility criteria and qualification requirements the bidder must meet to qualify',
        'Qualifying criteria: similar works experience, average annual turnover, solvency or net worth, and '
        'disqualification',
    ],
}


class OcrBudgetExceeded(RuntimeError):
    """The OCR a tender's files need is over its budget; estimate is what ocr_estimate() found."""

    def __init__(self, estimate, budget):
        self.estimate = estimate
        limits = [f"{budget['pages']} pages" if budget.get('pages') else '',
                  f"${budget['usd']:.2f}" if budget.get('usd') else '']
        super().__init__(f"The scans need {estimate['pages']} page(s) of OCR, up to ${estimate['cost']:.2f}: over "
                         f"the OCR budget of {' or '.join(limit for limit in limits if limit)} a tender")


def ocr_estimate(tcno_folder, cache=None, max_file_chars=300_000):
    """
    The OCR the tender's files would need, told without any OCR: {'pages', 'cost', 'files'}, where pages counts the
    pages and images to OCR, cost is the most that can cost (every one sent to OCR_MODEL, at OCR_PAGE_COST, in US
    dollars) and files is [{'name', 'pages'}] for each PDF that needs any. A file read before (in cache) needs none,
    nor does a page or image read before, nor a file that is not read (_NO_CRITERIA) or that is too long to OCR
    (max_file_chars).
    """
    tcno_folder = Path(tcno_folder)
    files = []
    for path in tender_files(tcno_folder):
        if path.suffix.lower() != '.pdf' or _NO_CRITERIA.search(path.name):
            continue
        if cache and cache.text(file_sha256(path), _READER) is not None:
            continue
        pages = ocr_parts(path, max_file_chars, cache)
        if pages:
            files.append({'name': path.relative_to(tcno_folder).as_posix(), 'pages': pages})
    pages = sum(file['pages'] for file in files)
    return {'pages': pages, 'cost': round(pages * OCR_PAGE_COST, 4), 'files': files}


def over_budget(estimate, budget):
    """Whether an ocr_estimate() is over budget, {'pages', 'usd'}: a limit of 0 or None is no limit."""
    budget = budget or {}
    return bool((budget.get('pages') and estimate['pages'] > budget['pages'])
                or (budget.get('usd') and estimate['cost'] > budget['usd']))


class TokenUsage:
    """
    Counts the LLM tokens spent on one tender, by step: 'criteria' (reading the documents: the pages of scanned PDFs
    sent to the vision model) and 'assessment' (choosing the sections to send whole by their headings, and checking
    the company against the criteria retrieved, for the final result). Given to
    process_tender_files() or recheck_saved_criteria(), it still holds the tokens spent before a failure. Other steps
    can be named instead, such as the agents of a deep analysis. Embedding tokens are Pinecone's, not counted here.
    """
    STEPS = ('criteria', 'assessment')

    def __init__(self, steps=STEPS):
        self._handlers = {step: UsageMetadataCallbackHandler() for step in steps}

    def track(self, llm, step):
        """llm, counting the tokens of every call it makes, retries and batches included, under step."""
        return llm.with_config(callbacks=[self._handlers[step]])

    def counts(self):
        """
        {'criteria', 'assessment', 'total'}, each {'input', 'output', 'total'} tokens; a step that made no LLM call
        (the criteria of a re-check) counts 0.
        """
        keys = ('input', 'output', 'total')
        # The handlers keep LangChain's usage metadata ({'input_tokens', ...}) per model name
        counts = {step: {key: sum(usage.get(f'{key}_tokens', 0) for usage in handler.usage_metadata.values())
                         for key in keys}
                  for step, handler in self._handlers.items()}
        counts['total'] = {key: sum(step[key] for step in counts.values()) for key in keys}
        return counts


def tender_files(tcno_folder):
    """The tender's documents: every file under tcno_folder except a result or deep analysis an older version saved
    there."""
    tcno_folder = Path(tcno_folder)
    saved = {tcno_folder / LEGACY_RESULT_FILE, tcno_folder / LEGACY_DEEP_ANALYSIS_FILE}
    return sorted(path for path in tcno_folder.rglob('*') if path.is_file() and path not in saved)


def process_tender_files(tcno_folder, *, index, company_store, tcno=None, on_progress=None,
                         prequalification_link=None, tender=None, usage=None, max_file_chars=300_000, workers=4,
                         cache=None, ocr_budget=None):
    """
    Checks the company profile in company_store against the eligibility criteria of a tender: every file
    under tcno_folder, the pre-qualification criteria behind prequalification_link (the tender's TenderTiger
    GetPreQualification link) when one is given, and the tender's TenderTiger data (tender, the raw list row) when
    given. Every source is converted to text (files in parallel, workers at a time), and all of them are indexed
    together in the tender's own Pinecone namespace, built anew (index, a PineconeService; tcno defaults to the
    folder name). The chunks relevant to each kind of requirement (ASSESSMENT_QUERIES) are then retrieved from the
    namespace and checked by the LLM. Drawings and item lists are not read (see _NO_CRITERIA), and a file with more than
    max_file_chars of text is listed as unreadable rather than indexed.

    Returns {'verdict', 'details', 'score', 'criteria', 'unreadable_files', 'jv', 'comparison', 'index'}:
      verdict           Eligible, Not Eligible or Requires Review
      details           the full assessment as markdown (criteria table, calculations, verdict)
      score             percentage of criteria marked Met, or None when the assessment has no criteria table
      criteria          the rows of the criteria table, see parse_criteria()
      unreadable_files  files that could not be read, so were not indexed or checked; PREQUALIFICATION_SOURCE
                        when the pre-qualification criteria could not be fetched
      index             {'index', 'namespace', 'sources', 'chunks', 'upserted', 'kept_sources', 'deleted',
                        'embedding_tokens', 'retrieved_chunks', 'retrieved_characters', 'whole_sections'}: the
                        chunks upserted, the sources left as they were (unchanged since the last build) and the
                        chunks deleted, and the sections retrieved whole (see PineconeService.ingest() and
                        retrieve())

    on_progress, if given, is called with a short status message before each step, plus step and steps
    (1-based position and total: one per file, then indexing, then the final check) when they are known.
    usage, a TokenUsage, if given, counts the tokens of reading scanned files and of the final check. cache, a
    cache_service.Cache, if given, holds the text of files read before, so an unchanged file is not read again (its
    vectors are cached by index, when it has the cache too). ocr_budget, {'pages', 'usd'}, if given, is the most OCR
    the files may need (see ocr_estimate(); 0 is no limit): over it, OcrBudgetExceeded is raised before anything is
    read, so the team can approve the OCR first.
    Raises RuntimeError when GROQ_API_KEY is missing, the company profile is empty, or there is nothing to check
    (no files and no pre-qualification criteria); VectorStoreError when the index cannot be built or searched;
    errors from the final LLM call propagate (the namespace is kept, so a re-check can follow).
    """
    report = on_progress or (lambda message, **position: None)
    usage = usage or TokenUsage()
    tcno_folder = Path(tcno_folder)
    tcno = str(tcno or tcno_folder.name)
    # Read once, so every file of the tender is checked against the same profile
    company = _company(company_store, tcno)
    index.require()

    files = tender_files(tcno_folder)
    ignored = [path for path in files if _NO_CRITERIA.search(path.name)]
    if ignored:
        log.info('Not reading %s: drawings and item lists hold no eligibility criteria',
                 ', '.join(path.relative_to(tcno_folder).as_posix() for path in ignored))
        files = [path for path in files if path not in ignored]
    documents = []
    skipped = []
    if prequalification_link:
        report('Reading the pre-qualification criteria')
        try:
            prequalification = prequalification_criteria(prequalification_link)
        except TenderTigerBlocked:
            # Fail the tender, so the job stops calling the site, rather than check it without these criteria
            raise
        except Exception:
            log.warning('Could not fetch the pre-qualification criteria of %s', tcno, exc_info=True)
            skipped.append(PREQUALIFICATION_SOURCE)
        else:
            if prequalification:
                # Already a short list of criteria: indexed as written, and always sent in full (see retrieve())
                documents.append(Document(PREQUALIFICATION_SOURCE, prequalification, 'prequalification'))
    if not files and not documents:
        reason = 'could not be fetched' if skipped else 'were not found'
        raise RuntimeError(f'No tender files to check, and the pre-qualification criteria {reason}.')
    if tender:
        documents.insert(0, Document(TENDER_DATA_SOURCE, tender_data_text(tender), 'tender_data'))
    if ocr_budget:
        report('Estimating the OCR of scanned pages')
        estimate = ocr_estimate(tcno_folder, cache, max_file_chars)
        if over_budget(estimate, ocr_budget):
            log.warning('Tender %s: %d page(s) to OCR, up to $%.2f, is over the OCR budget %s; asking first', tcno,
                        estimate['pages'], estimate['cost'], ocr_budget)
            raise OcrBudgetExceeded(estimate, ocr_budget)
    # Before reading: a Pinecone plan with no room for the namespace fails the tender before any OCR is paid for
    # (TenderProcessor has already made it before downloading). The chunks of a complete namespace are kept, so
    # ingest() upserts only the sources that changed since it was built.
    index.reset_namespace(tcno, on_progress=report, keep=True)

    steps = len(files) + 2
    read, unreadable = read_documents(tcno_folder, files, usage, max_file_chars=max_file_chars, workers=workers,
                                      on_progress=lambda message, number: report(message, step=number, steps=steps),
                                      cache=cache)
    documents += read
    skipped += unreadable

    name = index.namespace(tcno)
    report(f'Indexing {len(documents)} source(s) in the Pinecone namespace {name}', step=steps - 1, steps=steps)
    built = index.ingest(tcno, documents, skipped,
                         on_progress=lambda message: report(message, step=steps - 1, steps=steps))
    manifest = built.pop('manifest')
    report(f'Checking eligibility against the criteria retrieved from {name}', step=steps, steps=steps)
    analysis = _assess_from_index(index, tcno, usage, company,
                                  f'None of the {len(files)} file(s) contained text that could be read.', manifest,
                                  tender=tender, on_progress=lambda message: report(message, step=steps, steps=steps))
    analysis['index'] = {**built, **analysis['index']}
    return analysis


def read_documents(tcno_folder, files, usage, max_file_chars=300_000, workers=4, on_progress=None, cache=None):
    """
    Converts each of files (under tcno_folder) to text with MarkItDown, workers files at a time; what a PDF's text
    layer leaves out (a scan, pages with no text layer, large images) is read with OCR (see ocr_service.add_ocr(); the
    tokens of the parts sent to OCR_MODEL are counted in usage under 'criteria'). With cache (a cache_service.Cache),
    a file read before, the same to the byte (SHA-256) and under the same settings, is not read again.
    Returns (documents, skipped) in the order of files: a Document for each file with text, and the names of the
    files with none or too much (over max_file_chars; a scan's OCR stops once it is known to be), or, for a PDF with
    a text layer indexed without them, the pages and images OCR could not read. on_progress, if given, is called
    with (message, number) as
    each file is done, number counting the files done so far.
    """
    report = on_progress or (lambda message, number: None)
    tcno_folder = Path(tcno_folder)
    local = threading.local()  # a MarkItDown per thread

    def read(path):
        # Relative path, because nested zips often contain files with the same name
        name = path.relative_to(tcno_folder).as_posix()
        if path.suffix.lower() == '.zip':
            # An inner zip the download could not extract (damaged, encrypted, nested too deep): not unpacked here
            # either, where nothing would limit what it unpacks to
            return name, None, f'{name} (a ZIP file that could not be extracted)'
        digest = file_sha256(path) if cache else None
        text = cache.text(digest, _READER) if cache else None
        unread = []  # the pages and images of a PDF with a text layer that OCR could not read
        if text is not None:
            log.info('Read %s from the cache: unchanged since it was read', name)
        else:
            if not hasattr(local, 'md'):
                local.md = MarkItDown()
            try:
                text = (local.md.convert(path).markdown or '').strip()
            except Exception:
                log.warning('Could not read %s', path, exc_info=True)
                text = ''
            if path.suffix.lower() == '.pdf' and len(text) <= max_file_chars:
                # OCR what the text layer leaves out: every page of a scan, pages with no text layer, large images
                # (one PDF at a time). add_ocr() stops as soon as the file is known to be too long, so no OCR is
                # spent on pages that would only be dropped with it.
                try:
                    text, unread = add_ocr(path, text, usage.track(make_model(OCR_MODEL), 'criteria'),
                                           max_chars=max_file_chars, cache=cache)
                except ScanTooLong as error:
                    log.warning('Not reading %s: %s is over MAX_FILE_CHARS (%d)', name, error, max_file_chars)
                    return name, None, f'{name} (too long: {error}; the MAX_FILE_CHARS limit is {max_file_chars:,})'
            # Only a whole reading is kept: one with parts OCR could not read (often a timeout) or no text is tried
            # again next time, and so is a PDF too long to OCR, whose text so far is its text layer alone
            if cache and text and not unread and len(text) <= max_file_chars:
                cache.save_text(digest, _READER, text)
        if not text:
            log.info('No readable text in %s', name)
            return name, None, name
        if len(text) > max_file_chars:
            # Indexing is cheap, but a file this long is usually a drawing set or a data dump that would crowd the
            # criteria out of the search results
            log.warning('Not reading %s: %d characters is over MAX_FILE_CHARS (%d)', name, len(text), max_file_chars)
            return name, None, f'{name} (too long: {len(text):,} characters; the MAX_FILE_CHARS limit is {max_file_chars:,})'
        log.info('Read %s: %d characters', name, len(text))
        # Indexed without the parts OCR could not read, which are listed so the check is not taken as complete
        missing = f"{name} (could not be read: {'; '.join(unread)})" if unread else None
        return name, Document(name, text, document_type(name)), missing

    results = {}
    if files:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(files)))) as pool:
            futures = {pool.submit(read, path): path for path in files}
            for number, future in enumerate(as_completed(futures), 1):
                path = futures[future]
                results[path] = future.result()
                report(f'Read file {number} of {len(files)}: {results[path][0]}', number)
    documents = [results[path][1] for path in files if results[path][1]]
    skipped = [results[path][2] for path in files if results[path][2]]
    return documents, skipped


def tender_data_text(tender):
    """The tender's own data as TenderTiger lists it, as text to index beside its documents."""
    row = to_row(tender)
    value = row.get('tender_value')
    lines = [
        ('Tender', row.get('name')), ('TCNO', row.get('tcno')), ('Tender reference', row.get('reference')),
        ('Organisation', row.get('organisation')), ('Location', row.get('location')),
        ('Published', row.get('published_date')), ('Closing date', row.get('closing_date')),
        # Said when missing, so the model does not take a value it was never given for granted
        ('Tender value as listed on TenderTiger (not from the tender documents)',
         f"{row.get('currency') or 'INR'} {value:,.2f}" if value else 'not listed'),
    ]
    return '\n'.join(f'{label}: {text}' for label, text in lines if text)


def recheck_saved_criteria(tcno, *, index, company_store, on_progress=None, usage=None,
                           tender=None):
    """
    Checks the company profile in company_store against the criteria retrieved from the tender's Pinecone
    namespace (built by process_tender_files()), without downloading or reading the documents again: the embedding
    of the queries and one LLM call. The sources the namespace lists as unreadable stay unreadable, so the check is
    still marked incomplete. tender, the raw TenderTiger list row, if given, is sent as it is now, in place of the
    copy indexed with the documents (a closing date may have moved since).

    Returns the same dict as process_tender_files(); on_progress is called with step 1 of 1. usage, a TokenUsage,
    if given, counts the tokens of the check; the criteria cost none.
    Raises RuntimeError when GROQ_API_KEY is missing or the company profile is empty, NamespaceIncomplete without
    a complete namespace, VectorStoreError when Pinecone fails; errors from the LLM call propagate.
    """
    report = on_progress or (lambda message, **position: None)
    usage = usage or TokenUsage()
    company = _company(company_store, tcno)
    index.require()
    name = index.namespace(tcno)
    report(f'Checking eligibility against the criteria retrieved from {name}', step=1, steps=1)
    return _assess_from_index(index, str(tcno), usage, company,
                              f'The Pinecone namespace {name} holds no document text.', tender=tender,
                              on_progress=lambda message: report(message, step=1, steps=1))


def _assess_from_index(index, tcno, usage, company, nothing_found, manifest=None, tender=None, on_progress=None):
    """
    Retrieves the qualification section and the chunks of ASSESSMENT_QUERIES from the tender's namespace and makes
    the final check on them. The sections to send whole are chosen by SECTION_MODEL from the documents' headings
    (choose_sections()), or, failing that, by the QUALIFICATION_SECTION searches. The check is made in one request,
    or in batches when that is too large for the Groq plan (see
    _assess_in_batches()). manifest is the one the namespace was just built with, if it was. tender, the raw
    TenderTiger list row, if given, goes to the model as the tender's details (tender value, organisation, closing
    date, ...), and its copy in the namespace is left out of the excerpts. on_progress(message), if given, is told
    of the batches and of waits for the plan's per-minute limit.
    """
    def choose(sections):
        # Called only when there are headings to choose from; its tokens count under 'assessment', the step it serves
        return choose_sections(usage.track(make_model(SECTION_MODEL, SECTION_MAX_OUTPUT_TOKENS, 'low'), 'assessment'),
                               sections)

    retrieval = index.retrieve(tcno, ASSESSMENT_QUERIES, manifest=manifest, section_queries=QUALIFICATION_SECTION,
                               choose_sections=choose)
    details = tender_data_text(tender) if tender else None
    llm = usage.track(make_model(ASSESSMENT_MODEL), 'assessment')
    try:
        analysis = _assess(llm, _excerpts(retrieval, retrieval.chunks, tender), retrieval.unreadable, company,
                           nothing_found, details)
    except Exception as exc:
        if not _too_large(exc):
            raise
        log.warning('Tender %s: the check is too large for one request on the Groq plan; checking it in batches',
                    tcno)
        analysis = _assess_in_batches(llm, retrieval, tender, company, details, exc,
                                      on_progress or (lambda message: None))
    analysis['index'] = {'index': index.index_name, 'namespace': retrieval.namespace,
                         'retrieved_chunks': len(retrieval.chunks), 'retrieved_characters': retrieval.characters,
                         'whole_sections': retrieval.whole_sections,
                         'assessment_batches': analysis.pop('batches', 1)}
    return analysis


def _excerpts(retrieval, chunks, tender):
    """The chunks of a retrieval as the model's excerpts, one section per source (Retrieval.sections()); the
    TenderTiger data indexed with the documents left out when the tender's list row goes with them."""
    sections = Retrieval(namespace=retrieval.namespace, manifest=retrieval.manifest, chunks=chunks).sections()
    if tender:
        sections = [section for section in sections if not section.startswith(f'## {TENDER_DATA_SOURCE}\n')]
    return sections


def _too_large(exc):
    """Whether exc is Groq's 413: one request over the plan's tokens per minute, which no retry can pass."""
    text = str(exc).lower()
    return 'request too large' in text and 'tokens per minute' in text


def _rate_limit_wait(exc):
    """
    The seconds to wait when exc is Groq's 429 (the plan's per-minute limit reached by requests just made), from its
    "try again in 1m2.5s"; None for any other error.
    """
    text = str(exc)
    if 'rate limit reached' not in text.lower():
        return None
    match = re.search(r'try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)', text)
    seconds = 60.0
    if match:
        units = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600}
        seconds = sum(float(number) * units[unit]
                      for number, unit in re.findall(r'(\d+(?:\.\d+)?)(ms|h|m|s)', match.group(1)))
    return min(seconds + 1, RATE_LIMIT_MAX_SECONDS)


def _paced(call, report):
    """call(), waiting out the Groq plan's per-minute token limit when a batch reaches it (see _rate_limit_wait())."""
    for attempt in range(RATE_LIMIT_WAITS + 1):
        try:
            return call()
        except Exception as exc:
            wait = _rate_limit_wait(exc)
            if wait is None or attempt == RATE_LIMIT_WAITS:
                raise
            report(f"Waiting {wait:.0f} s for the Groq plan's per-minute token limit")
            time.sleep(wait)


def _halves(chunks):
    """chunks (in document order) as two runs of about the same length of text, so a section stays together."""
    total, used = sum(len(chunk['text']) for chunk in chunks), 0
    for at, chunk in enumerate(chunks[:-1], 1):
        used += len(chunk['text'])
        if used >= total / 2:
            return [chunks[:at], chunks[at:]]
    return [chunks[:-1], chunks[-1:]]


def _assess_in_batches(llm, retrieval, tender, company, details, too_large, report):
    """
    The final check when one request is too large for the Groq plan (too_large, its 413). The excerpts, in document
    order, are split in two runs of about the same length, each checked on its own with the same instructions,
    company profile and tender details; a run still too large is split in two again, up to MAX_BATCHES. Their
    criteria are joined (a criterion found in more than one batch once, by the most telling status: not met, then met,
    then cannot determine), the JV terms are taken from the batch that states them, and the verdict, score and JV
    verdict are worked out from the joined statuses, as statuses_verdict() does. Returns the analysis _assess() does,
    with 'batches'. Raises the error of a batch still too large at MAX_BATCHES, and any other error of a batch.
    """
    pending, batches, replies = _halves(retrieval.chunks), 2, []
    while pending:
        chunks = pending.pop(0)
        excerpts = _excerpts(retrieval, chunks, tender)
        if not excerpts:
            continue
        number = len(replies) + 1
        report(f'Checking eligibility in {batches} batches: batch {number}')
        try:
            replies.append(plain_maths(_paced(
                lambda: assess_eligibility(llm, excerpts, company, details, part=(number, batches)), report)))
        except Exception as exc:
            if not _too_large(exc) or batches >= MAX_BATCHES or len(chunks) < 2:
                raise
            pending[:0] = _halves(chunks)
            batches += 1
    if not replies:
        raise too_large
    return _join_batches(replies, retrieval.unreadable, too_large)


# A criterion found in more than one batch keeps its most telling status: not met, then met, then cannot determine
_STATUS_WEIGHT = {'not_met': 2, 'met': 1, 'unknown': 0}


def _join_batches(replies, skipped, too_large):
    """The analysis of the batches' replies, joined (see _assess_in_batches())."""
    criteria = {}
    for reply in replies:
        for criterion in parse_criteria(reply):
            key = criterion_key(criterion['criterion'])
            kept = criteria.get(key)
            if kept is None or _STATUS_WEIGHT[criterion['status']] > _STATUS_WEIGHT[kept['status']]:
                criteria[key] = criterion
    criteria = list(criteria.values())
    jvs = [parse_jv(reply) for reply in replies]
    jv = next((parsed for parsed in jvs if parsed['allowed'] == 'yes'), None) \
        or next((parsed for parsed in jvs if parsed['allowed'] == 'no'), jvs[0])
    jv = {**jv, 'conditions': list(dict.fromkeys(condition for parsed in jvs for condition in parsed['conditions']))}
    comparison = compare_routes(criteria, jv)
    statuses = [criterion['status'] for criterion in criteria]
    met = sum(1 for status in statuses if status == 'met')
    limit = re.search(r'Limit \d+, Requested \d+', str(too_large))
    note = (f"**Checked in {len(replies)} batches.** The documents' excerpts were too many tokens for one request on "
            f"the Groq plan{f' ({limit.group(0)} tokens per minute)' if limit else ''}, so they were checked in "
            f'{len(replies)} parts, one after another. The verdict, score and JV verdict are worked out from the '
            'statuses of all of them together; each part ends with its own verdict, for that part alone.')
    details = '\n\n'.join([note] + [f'## Batch {number} of {len(replies)}\n\n{reply}'
                                     for number, reply in enumerate(replies, 1)])
    return {'verdict': statuses_verdict(statuses, skipped), 'details': _note_skipped(details, skipped),
            'score': round(100 * met / len(criteria)) if criteria else None, 'criteria': criteria,
            'unreadable_files': skipped, 'comparison': comparison,
            'jv': {'allowed': jv['allowed'], 'max_members': jv['max_members'],
                   'verdict': jv_verdict(comparison, jv, skipped), 'conditions': jv['conditions']},
            'batches': len(replies)}


def _company(company_store, tcno):
    """
    The company profile in company_store (a CompanyStore) to check tender tcno against, with the other tenders'
    marks (CompanyStore.for_tender()), after checking the LLM can be called. Raises RuntimeError.
    """
    if not os.getenv('GROQ_API_KEY'):
        raise RuntimeError('GROQ_API_KEY is not set. Add it to .env and restart the app.')
    company = company_store.for_tender(tcno)
    if not company:
        raise RuntimeError('The company profile is empty. Add it with Edit profile on the page.')
    return company


def _assess(llm, summaries, skipped, company, nothing_found, tender_details=None):
    """
    The final check of process_tender_files() and recheck_saved_criteria(); nothing_found explains no criteria.
    tender_details is the tender's data from the TenderTiger list as text, if known.
    Besides the verdict for bidding alone, returns 'jv' ({'allowed', 'max_members', 'verdict', 'conditions'}, where
    verdict is for the company as JV lead member, see jv_verdict()) and 'comparison' (see compare_routes()).
    """
    if not summaries:
        return {'verdict': REQUIRES_REVIEW, 'details': _note_skipped(nothing_found, skipped), 'score': None,
                'criteria': [], 'unreadable_files': skipped, 'comparison': [],
                'jv': {'allowed': 'unknown', 'max_members': None, 'verdict': None, 'conditions': []}}
    assessment = plain_maths(assess_eligibility(llm, summaries, company, tender_details))
    verdict = parse_verdict(assessment)
    criteria = parse_criteria(assessment)
    # Eligible must hold up: not when some documents were never seen (the check is not complete), nor when the reply's
    # own table says a criterion is not met (the model slipped, or text in a document steered the verdict)
    if verdict == ELIGIBLE and (skipped or any(criterion['status'] == 'not_met' for criterion in criteria)):
        verdict = REQUIRES_REVIEW
    jv = parse_jv(assessment)
    comparison = compare_routes(criteria, jv)
    return {'verdict': verdict, 'details': _note_skipped(assessment, skipped), 'score': parse_score(assessment),
            'criteria': criteria, 'unreadable_files': skipped, 'comparison': comparison,
            'jv': {'allowed': jv['allowed'], 'max_members': jv['max_members'],
                   'verdict': jv_verdict(comparison, jv, skipped), 'conditions': jv['conditions']}}


def compare_routes(criteria, jv):
    """
    Lines up each criterion for bidding alone (parse_criteria()) with the same criterion for a JV (parse_jv()), so
    the two routes read side by side. JV rows are matched by name, exactly and then by one name containing the other;
    the JV rows left over (requirements only a JV has) come last.

    Returns [{'criterion', 'company', 'source', 'alone_requirement', 'alone_status', 'lead_member', 'lead_company',
    'lead_status', 'other_members', 'combined'}]. alone_requirement and alone_status are None for a JV-only row.
    lead_status is 'met', 'not_met' or 'unknown' (also when the JV table left the criterion out), or None, like the
    other JV fields, when no JV is allowed; lead_company is the JV table's note on the company as lead member.
    """
    allowed = jv['allowed'] == 'yes'
    remaining = list(jv['criteria']) if allowed else []
    rows = []
    for criterion in criteria:
        match = _same_criterion(criterion['criterion'], remaining)
        if match:
            remaining.remove(match)
        rows.append({
            'criterion': criterion['criterion'], 'company': criterion['company'],
            'source': criterion['source'] or (match or {}).get('source', ''),
            'alone_requirement': criterion['requirement'], 'alone_status': criterion['status'],
            **_lead(match, allowed),
        })
    for match in remaining:
        rows.append({'criterion': match['criterion'], 'company': '', 'source': match['source'],
                     'alone_requirement': None, 'alone_status': None, **_lead(match, allowed)})
    return rows


def jv_verdict(comparison, jv, skipped):
    """
    The outcome for the company as JV lead member, from its statuses (see statuses_verdict()). None when no JV is
    allowed or the documents do not say.
    """
    if jv['allowed'] != 'yes':
        return None
    return statuses_verdict([row['lead_status'] for row in comparison], skipped)


def statuses_verdict(statuses, skipped):
    """
    A verdict worked out from criteria statuses: Not Eligible when any is not met, Eligible when every one is met
    and no source was unreadable (skipped is empty), otherwise Requires Review.
    """
    if 'not_met' in statuses:
        return NOT_ELIGIBLE
    if statuses and all(status == 'met' for status in statuses) and not skipped:
        return ELIGIBLE
    return REQUIRES_REVIEW


def _same_criterion(name, candidates):
    """The JV row in candidates for the criterion called name, or None."""
    key = criterion_key(name)
    exact = next((row for row in candidates if criterion_key(row['criterion']) == key), None)
    # Then one name holding the other's words whole: "PAN" is not in "Company registration"
    return exact or next((row for row in candidates
                          if key and criterion_key(row['criterion'])
                          and (f' {key} ' in f" {criterion_key(row['criterion'])} "
                               or f" {criterion_key(row['criterion'])} " in f' {key} ')),
                         None)


def criterion_key(name):
    """A criterion name for matching: lower case, letters and digits only."""
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', (name or '').lower()).split())


def _lead(jv_row, allowed):
    """The JV half of a comparison row: what each JV member must meet and the company as lead member."""
    if not allowed:
        return dict.fromkeys(('lead_member', 'lead_company', 'lead_status', 'other_members', 'combined'))
    jv_row = jv_row or {}
    return {'lead_member': jv_row.get('lead_member', ''), 'lead_company': jv_row.get('company', ''),
            'lead_status': jv_row.get('status', 'unknown'), 'other_members': jv_row.get('other_members', ''),
            'combined': jv_row.get('combined', '')}


# LaTeX the model writes despite being asked not to, as the symbols the page can show
_LATEX_SYMBOLS = [(r'\geq', '≥'), (r'\leq', '≤'), (r'\ge', '≥'), (r'\le', '≤'), (r'\times', '×'),
                  (r'\approx', '≈'), (r'\%', '%'), (r'\,', ' ')]


def plain_maths(text):
    """Turns inline LaTeX such as $\\ge 26\\%$ into plain text (≥ 26%). Dollar amounts without a \\command stay."""
    def replace(match):
        body = match.group(1)
        for latex, symbol in _LATEX_SYMBOLS:
            body = body.replace(latex, symbol)
        return ' '.join(body.split())

    return re.sub(r'\$([^$\n]*\\[A-Za-z%,][^$\n]*)\$', replace, text or '')


def parse_verdict(assessment):
    """Maps the model's last stated verdict to Eligible / Not Eligible; anything else is Requires Review."""
    matches = _VERDICT.findall(assessment or '')
    if not matches:
        return REQUIRES_REVIEW
    words = re.sub(r'[\s*_]+', ' ', matches[-1]).lower()
    return _VERDICT_LABELS.get(words, REQUIRES_REVIEW)


def parse_score(assessment):
    """
    Returns the percentage (0-100) of rows in the assessment's Status column that say Met.
    'Not met' and 'Cannot determine' count against it. Returns None if no table has a Status column.
    """
    criteria = parse_criteria(assessment)
    met = sum(1 for criterion in criteria if criterion['status'] == 'met')
    return round(100 * met / len(criteria)) if criteria else None


def parse_criteria(assessment):
    """
    Reads every row of the assessment's criteria tables (markdown tables with a Status column), outside its
    joint venture section. A row of a later table replaces the earlier row of the same criterion: the model
    sometimes corrects its own table in a revised one.

    Returns a list of {'criterion', 'requirement', 'company', 'status', 'source'} in table order, where
    status is 'met', 'not_met' or 'unknown' ('Cannot determine' or anything else). Columns the model
    left out are empty strings.
    """
    criteria = _Rows()
    columns = None
    for line in _split_jv(assessment)[0]:
        line = line.strip()
        if not line.startswith('|'):
            columns = None  # a markdown table ends at the first non-table line
            continue
        cells = [_cell_text(cell) for cell in line.strip('|').split('|')]
        if columns is None:
            columns = _criteria_columns([cell.lower() for cell in cells])
            criteria.next_table()
            continue
        status_column = columns['status']
        if status_column >= len(cells) or set(cells[status_column]) <= set('-: '):
            continue  # the |---|---| separator row, or a malformed row
        if all(set(cell) <= set('-–—: ') for cell in cells):
            continue  # | — | — | — |: the model's way of saying the excerpts hold no criteria
        row = {key: cells[index] if index is not None and index < len(cells) else ''
               for key, index in columns.items()}
        row['status'] = _status(row['status'])
        row['criterion'] = row['criterion'] or row['requirement'] or f'Criterion {len(criteria) + 1}'
        criteria.add(row)
    return criteria.rows()


class _Rows:
    """
    Criteria rows read from one or more tables, in order. A row replaces the row of the same criterion from an
    earlier table (a corrected table), but for the fields it leaves empty, which keep the earlier row's: a short
    summary table after the verdict (Criterion | Status) does not blank its requirement, figures and source. Within
    one table, rows of the same name are all kept.
    """

    def __init__(self):
        self._rows = []  # [table number, criterion key, row]
        self._table = 0

    def __len__(self):
        return len(self._rows)

    def next_table(self):
        self._table += 1

    def add(self, row):
        key = criterion_key(row['criterion'])
        earlier = next((entry for entry in self._rows if entry[1] == key and entry[0] < self._table), None)
        if earlier:
            kept = {field: value for field, value in row.items() if value}
            earlier[0], earlier[2] = self._table, {**earlier[2], **kept}
        else:
            self._rows.append([self._table, key, row])

    def rows(self):
        return [row for _, _, row in self._rows]


def _criteria_columns(header):
    """Maps each criteria field to its column in a table header, or returns None if it has no Status column."""
    if 'status' not in header:
        return None

    def find(word):
        return next((index for index, name in enumerate(header) if word in name), None)

    criterion = find('criterion')
    return {
        'criterion': 0 if criterion is None else criterion,
        'requirement': find('requirement'),
        'company': find('company'),
        'status': header.index('status'),
        'source': find('source'),
    }


def parse_jv(assessment):
    """
    Reads the assessment's joint venture section ("## Joint venture", as ELIGIBILITY_TASK asks for).

    Returns {'allowed', 'max_members', 'criteria', 'conditions'}:
      allowed      'yes', 'no' or 'unknown' (not stated, or no such section)
      max_members  the most members a JV may have, lead member included, or None when not stated
      criteria     the rows of its table, in order: {'criterion', 'lead_member', 'other_members', 'combined',
                   'company', 'status', 'source'}, where company and status are the company as lead member
                   and status is 'met', 'not_met' or 'unknown'
      conditions   the other JV conditions, one bullet each
    """
    lines = _split_jv(assessment)[1]
    text = '\n'.join(lines)
    allowed = _JV_ALLOWED.search(text)
    allowed = allowed.group(1).lower() if allowed and allowed.group(1).lower() in ('yes', 'no') else 'unknown'
    maximum = _JV_MAXIMUM.search(text)
    max_members = _number(maximum.group(1)) if maximum and allowed != 'no' else None

    criteria, conditions = _Rows(), []
    columns = None  # the JV table's columns while inside it; {} inside any other table
    for line in lines[1:]:
        line = line.strip()
        if line.startswith('|'):
            cells = [_cell_text(cell) for cell in line.strip('|').split('|')]
            if columns is None:
                columns = _jv_columns([cell.lower() for cell in cells]) or {}
                criteria.next_table()
            elif columns and not all(set(cell) <= set('-: ') for cell in cells):
                row = {key: cells[index] if index is not None and index < len(cells) else ''
                       for key, index in columns.items()}
                row['status'] = _status(row['company'])
                row['criterion'] = row['criterion'] or f'Criterion {len(criteria) + 1}'
                criteria.add(row)
            continue
        columns = None
        bullet = re.match(r'[-*•]\s+(.+)', line)
        if bullet and not _JV_ALLOWED.search(line) and not _JV_MAXIMUM.search(line):
            conditions.append(_cell_text(bullet.group(1)))
    return {'allowed': allowed, 'max_members': max_members, 'criteria': criteria.rows(), 'conditions': conditions}


_JV_WORDS = re.compile(r'\b(joint\s+venture|jv|consortium)\b', re.IGNORECASE)
_JV_ALLOWED = re.compile(r'\b(?:jv|joint\s+venture|consortium)\b[^:\n]*\ballowed\b\W*(yes|not\s+stated|no)\b',
                         re.IGNORECASE)
_JV_MAXIMUM = re.compile(r'\bmaximum\b[^:\n]*\bmembers?\b\W*(\d+|two|three|four|five|six|seven|eight|nine|ten)\b',
                         re.IGNORECASE)
_NUMBERS = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
# A markdown heading (its #s captured), or a line that is all bold: a heading the model wrote as **text**
_HEADING = re.compile(r'\s*(#{1,6})\s|\s*\*\*[^*]+\*\*:?\s*$')


def _number(word):
    """'3' or 'three' as 3."""
    return int(word) if word.isdigit() else _NUMBERS[word.lower()]


def _heading_level(line):
    """1 to 6 for a markdown heading, 7 for an all-bold line, None for any other line."""
    match = _HEADING.match(line)
    if not match:
        return None
    return len(match.group(1)) if match.group(1) else 7


def _split_jv(assessment):
    """
    Splits the assessment's lines into those outside its joint venture section and those of the section,
    heading first. The section ends at the overall verdict, or at the next heading of its level or above;
    one written in bold ends at any markdown heading, but not at bold lines such as "**Conditions:**".
    """
    lines = (assessment or '').splitlines()
    starts = [i for i, line in enumerate(lines) if _heading_level(line) and _JV_WORDS.search(line)]
    if not starts:
        return lines, []

    def end_of(start):
        top = min(_heading_level(lines[start]), 6)

        def ends(line):
            level = _heading_level(line)
            return bool(re.search(r'overall\s+verdict', line, re.IGNORECASE)) or (level is not None and level <= top)

        return next((i for i in range(start + 1, len(lines)) if ends(lines[i])), len(lines))

    # The section that says whether a JV may bid, as ELIGIBILITY_TASK asks for, not a heading of the working-out
    # before it such as "### 2. Joint venture share"; the first one if none says
    start = next((start for start in starts if _JV_ALLOWED.search('\n'.join(lines[start:end_of(start)]))), starts[0])
    end = end_of(start)
    return lines[:start] + lines[end:], lines[start:end]


def _jv_columns(header):
    """Maps each JV criteria field to its column in a table header, or None without a lead member column."""
    def find(test):
        return next((index for index, name in enumerate(header) if test(name)), None)

    lead = find(lambda name: 'lead' in name and 'company' not in name)
    company = find(lambda name: 'company' in name)
    if lead is None and company is None:
        return None
    criterion = find(lambda name: 'criterion' in name)
    return {
        'criterion': 0 if criterion is None else criterion,
        'lead_member': lead,
        'other_members': find(lambda name: 'other' in name or 'each' in name),
        'combined': find(lambda name: 'combined' in name or 'whole' in name),
        'company': company,
        'source': find(lambda name: 'source' in name),
    }


def _status(text):
    """
    Met / Not met / anything else, as 'met', 'not_met' or 'unknown'. A status starting "Not met" is not met; else one
    the model hedges is unknown ("Cannot determine whether the 26% share is met", "Partially met"), and "not" a word
    or two before "met" is not met ("Not fully met", "has not been met"). Only then does "met" alone mean met.
    """
    text = text.lower()
    if re.match(r'\W*not\s+met\b', text):
        return 'not_met'
    if _UNSURE.search(text):
        return 'unknown'
    if re.search(r'\bnot(?:\s+\w+){0,2}\s+met\b|\bunmet\b', text):
        return 'not_met'
    return 'met' if re.search(r'\bmet\b', text) else 'unknown'


_UNSURE = re.compile(r"\b(?:cannot|can't|could not|unable|unclear|uncertain|undetermined|partial(?:ly)?)\b")


def _cell_text(cell):
    """A table cell as plain text: no surrounding emphasis, bold markers or <br> tags."""
    cell = re.sub(r'<br\s*/?>', ' ', cell, flags=re.IGNORECASE)
    cell = re.sub(r'(\*\*|__)(.+?)\1', r'\2', cell)
    return ' '.join(cell.split()).strip(' *_`')


def _note_skipped(details, skipped):
    """Appends the files that could not be checked, so an incomplete result is visible as such."""
    if not skipped:
        return details
    return f"{details}\n\n---\n\n**Incomplete: these files could not be checked:** {', '.join(skipped)}"
