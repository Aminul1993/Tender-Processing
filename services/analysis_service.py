"""Reads every file of a tender and checks the company's eligibility with the LLM pipeline from ext.py."""
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from langchain_core.callbacks import UsageMetadataCallbackHandler
from markitdown import MarkItDown

from ext import (ASSESSMENT_MODEL, COMPANY_DETAILS_FILE, ELIGIBILITY_GOAL, OCR_MODEL, SUMMARY_MODEL,
                 TenderTigerBlocked, assess_eligibility, company_details, make_model, prequalification_criteria,
                 split_text, summarize_for_goal)

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

# The source name the tender's TenderTiger pre-qualification criteria are checked under, beside the file names
PREQUALIFICATION_SOURCE = 'TenderTiger Pre-Qualification'

# The criteria found in a tender's documents are saved in its folder under this name, so the tender can be
# checked again without reading the documents; see save_criteria() and recheck_saved_criteria()
CRITERIA_FILE = 'eligibility_criteria.md'
# The criteria file's section that lists the sources that could not be read
UNREADABLE_TITLE = 'Sources that could not be read'
# The result of a tender's last check is saved in its folder under this name; see processing_service.save_result()
RESULT_FILE = 'eligibility_result.json'
# The last deep analysis of that result is saved beside it under this name; see deep_analysis_service
DEEP_ANALYSIS_FILE = 'deep_analysis.json'
# Tender files that hold no eligibility criteria, so are not read: drawings (one tender's drawings ran to 300k
# tokens and held none) and GeM item lists (.csv of items, quantities and consignees). A BOQ is still read,
# because it may give the estimated cost the criteria depend on.
_NO_CRITERIA = re.compile(r'drawing|\.csv$', re.IGNORECASE)
# What markitdown-ocr writes besides the text it reads: a heading per page, and notes on pages it could not read
_OCR_FILLER = re.compile(r'^\s*(## Page \d+|\*\[(No text could be extracted|Error)\b.*\]\*)\s*$', re.MULTILINE)


class TokenUsage:
    """
    Counts the LLM tokens spent on one tender, by step: 'criteria' (reading the documents for eligibility criteria,
    OCR of scanned PDFs included) and 'assessment' (checking the company against them, for the final result). Given to process_tender_files() or
    recheck_saved_criteria(), it still holds the tokens spent before a failure. Other steps can be named instead,
    such as the agents of a deep analysis.
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
    """The tender's documents: every file under tcno_folder except the saved criteria, result and deep analysis."""
    tcno_folder = Path(tcno_folder)
    saved = {tcno_folder / CRITERIA_FILE, tcno_folder / RESULT_FILE, tcno_folder / DEEP_ANALYSIS_FILE}
    return sorted(path for path in tcno_folder.rglob('*') if path.is_file() and path not in saved)


def process_tender_files(tcno_folder, on_progress=None, company_file=COMPANY_DETAILS_FILE,
                         prequalification_link=None, usage=None, max_file_chars=300_000):
    """
    Checks the company profile saved in company_file against the eligibility criteria found in every file
    under tcno_folder, and in the pre-qualification criteria behind prequalification_link (the tender's
    TenderTiger GetPreQualification link), when one is given. The criteria found are saved in
    tcno_folder/CRITERIA_FILE before the check, for recheck_saved_criteria(). Drawings and item lists are not
    read (see _NO_CRITERIA), and a file with more than max_file_chars of text is listed as unreadable rather
    than sent to the LLM.

    Returns {'verdict', 'details', 'score', 'criteria', 'unreadable_files'}:
      verdict           Eligible, Not Eligible or Requires Review
      details           the full assessment as markdown (criteria table, calculations, verdict)
      score             percentage of criteria marked Met, or None when the assessment has no criteria table
      criteria          the rows of the criteria table, see parse_criteria()
      unreadable_files  files that could not be read or summarised, so were not checked; PREQUALIFICATION_SOURCE
                        when the pre-qualification criteria could not be fetched

    on_progress, if given, is called with a short status message before each step, plus step and steps
    (1-based position and total, counting each file and the final check) when they are known.
    usage, a TokenUsage, if given, counts the tokens of reading the files and of the final check.
    Raises RuntimeError when GROQ_API_KEY is missing, the company profile is empty, or there is nothing to check
    (no files and no pre-qualification criteria); errors from the final LLM call propagate.
    """
    report = on_progress or (lambda message, **position: None)
    usage = usage or TokenUsage()
    # Read once, so every file of the tender is checked against the same profile
    company = _company(company_file)

    tcno_folder = Path(tcno_folder)
    files = tender_files(tcno_folder)
    ignored = [path for path in files if _NO_CRITERIA.search(path.name)]
    if ignored:
        log.info('Not reading %s: drawings and item lists hold no eligibility criteria',
                 ', '.join(path.relative_to(tcno_folder).as_posix() for path in ignored))
        files = [path for path in files if path not in ignored]
    summaries = []
    skipped = []
    if prequalification_link:
        report('Reading the pre-qualification criteria')
        try:
            prequalification = prequalification_criteria(prequalification_link)
        except TenderTigerBlocked:
            # Fail the tender, so the job stops calling the site, rather than check it without these criteria
            raise
        except Exception:
            log.warning('Could not fetch the pre-qualification criteria of %s', tcno_folder.name, exc_info=True)
            skipped.append(PREQUALIFICATION_SOURCE)
        else:
            if prequalification:
                # Already a short list of criteria, so it is checked as written rather than summarised
                summaries.append(f'## {PREQUALIFICATION_SOURCE}\n{prequalification}')
    if not files and not summaries:
        reason = 'could not be fetched' if skipped else 'were not found'
        raise RuntimeError(f'No tender files to check, and the pre-qualification criteria {reason}.')

    reader = usage.track(make_model(SUMMARY_MODEL), 'criteria')
    md = MarkItDown()
    ocr = None  # an _OcrReader, made for the first PDF that needs one
    steps = len(files) + 1
    for number, path in enumerate(files, 1):
        # Relative path, because nested zips often contain files with the same name
        name = path.relative_to(tcno_folder).as_posix()
        report(f'Analysing file {number} of {len(files)}: {name}', step=number, steps=steps)
        try:
            text = (md.convert(path).markdown or '').strip()
        except Exception:
            log.warning('Could not read %s', path, exc_info=True)
            text = ''
        if not text and path.suffix.lower() == '.pdf':
            # No text layer, as in a scan: read the page images with OCR
            ocr = ocr or _OcrReader(usage.track(make_model(OCR_MODEL), 'criteria'))
            text = ocr.read(path)
        if not text:
            log.info('No readable text in %s', name)
            skipped.append(name)
            continue
        if len(text) > max_file_chars:
            # Summarising costs about a token per 2.6 characters, so one huge file could outspend 40 tenders
            log.warning('Not reading %s: %d characters is over MAX_FILE_CHARS (%d)', name, len(text), max_file_chars)
            skipped.append(f'{name} (too long: {len(text):,} characters; the MAX_FILE_CHARS limit is {max_file_chars:,})')
            continue
        log.info('Reading %s: %d characters in %d chunk(s)', name, len(text), len(split_text(text)))
        try:
            summary = summarize_for_goal(reader, text, ELIGIBILITY_GOAL)
        except Exception:
            log.warning('Could not summarize %s', path, exc_info=True)
            skipped.append(name)
            continue
        if summary:
            summaries.append(f'## {name}\n{summary}')
        else:
            log.info('Nothing relevant to eligibility in %s', name)

    # Saved before the final check, so a failure there does not lose the work of reading every file
    save_criteria(tcno_folder, summaries, skipped)
    report('Checking eligibility against the company details', step=steps, steps=steps)
    return _assess(usage.track(make_model(ASSESSMENT_MODEL), 'assessment'), summaries, skipped, company,
                   f'None of the {len(files)} file(s) contained eligibility criteria that could be read.')


class _OcrReader:
    """
    Reads scanned PDFs with markitdown-ocr, which sends each page image to llm (an OCR_MODEL chat model) to read.
    markitdown-ocr calls an OpenAI client, so this stands in for one: client.chat.completions.create().
    """

    def __init__(self, llm):
        self._llm = llm
        self._error = None  # the last failed call of the current read()
        self.chat = SimpleNamespace(completions=self)
        self._md = MarkItDown(enable_plugins=True, llm_client=self, llm_model=OCR_MODEL)

    def create(self, model, messages, **options):
        """OpenAI's chat.completions.create(), answered by llm (already set to OCR_MODEL)."""
        try:
            reply = self._llm.invoke(messages)
        except Exception as error:
            # markitdown-ocr leaves out a page it could not read without a word, so the failure is kept for read()
            self._error = error
            raise
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply.content))])

    def read(self, path):
        """
        The text OCR'd from the PDF at path; '' when there is none, or when a page could not be read, so the
        file is listed as not checked rather than checked with pages missing.
        """
        self._error = None
        try:
            text = (self._md.convert(path).markdown or '').strip()
        except Exception:
            log.warning('Could not OCR %s', path, exc_info=True)
            return ''
        if self._error or '*[Error' in text:
            log.warning('Could not OCR every page of %s: %s', path.name, self._error or 'a page could not be rendered')
            return ''
        if not _OCR_FILLER.sub('', text).strip():
            return ''
        log.info('Read %s with OCR (%s)', path.name, OCR_MODEL)
        return text


def recheck_saved_criteria(tcno_folder, on_progress=None, company_file=COMPANY_DETAILS_FILE, usage=None):
    """
    Checks the company profile saved in company_file against the criteria in tcno_folder/CRITERIA_FILE, as
    saved by process_tender_files() and perhaps edited since, without reading the documents again: one LLM call.
    The sources the file lists as unreadable stay unreadable, so the check is still marked incomplete.

    Returns the same dict as process_tender_files(); on_progress is called with step 1 of 1. usage, a TokenUsage,
    if given, counts the tokens of the check; the criteria cost none.
    Raises RuntimeError when GROQ_API_KEY is missing or the company profile is empty, FileNotFoundError
    without a criteria file; errors from the LLM call propagate.
    """
    report = on_progress or (lambda message, **position: None)
    usage = usage or TokenUsage()
    company = _company(company_file)
    criteria, skipped = read_criteria(Path(tcno_folder) / CRITERIA_FILE)
    report('Checking eligibility against the saved criteria', step=1, steps=1)
    llm = usage.track(make_model(ASSESSMENT_MODEL), 'assessment')
    return _assess(llm, [criteria] if criteria else [], skipped, company,
                   f'The saved criteria in {CRITERIA_FILE} list no eligibility criteria.')


def save_criteria(tcno_folder, summaries, skipped):
    """
    Writes the criteria found in a tender (summaries: one '## <source>' section each) and the sources that
    could not be read to tcno_folder/CRITERIA_FILE, in the form read_criteria() reads back. Returns its path.
    """
    parts = ['# Eligibility criteria', (
        f"> Taken from the tender documents on {datetime.now():%d %b %Y %H:%M}. Checking this tender again "
        'compares the company profile with this file instead of reading the documents: edit it to correct a '
        'criterion, or regenerate it to read the documents again.'
    )]
    if not summaries:
        parts.append('> No eligibility criteria were found in the documents.')
    parts += [summary.strip() for summary in summaries]
    if skipped:
        parts.append(f'## {UNREADABLE_TITLE}\n\nThese were not checked, so any criteria they hold are missing '
                     'above:\n\n' + '\n'.join(f'- {name}' for name in skipped))
    path = Path(tcno_folder) / CRITERIA_FILE
    # Written beside and then moved, so a crash mid-write cannot leave half the criteria to be checked later
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n\n'.join(parts) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    log.info('Saved the eligibility criteria in %s', path)
    return path


def read_criteria(path):
    """
    Reads a criteria file written by save_criteria(). Returns (criteria, unreadable): the criteria as markdown,
    without the title, the notes under it and the unreadable section, and the sources that section lists.
    Raises FileNotFoundError.
    """
    text = Path(path).read_text(encoding='utf-8-sig')
    head, *sections = re.split(r'(?m)^(?=## )', text)
    # Only the title and the notes are dropped from above the first section; anything added there is kept
    parts = ['\n'.join(line for line in head.splitlines() if not line.startswith(('# ', '>')))]
    unreadable = []
    for section in sections:
        title, _, body = section.partition('\n')
        if title[3:].strip() == UNREADABLE_TITLE:
            unreadable += [line[2:].strip() for line in body.splitlines() if line.startswith('- ')]
        else:
            parts.append(section)
    return '\n\n'.join(part.strip() for part in parts if part.strip()), unreadable


def _company(company_file):
    """The company profile in company_file, after checking the LLM can be called. Raises RuntimeError."""
    if not os.getenv('GROQ_API_KEY'):
        raise RuntimeError('GROQ_API_KEY is not set. Add it to .env and restart the app.')
    company = company_details(company_file)
    if not company:
        raise RuntimeError(f'The company profile in {company_file} is empty.')
    return company


def _assess(llm, summaries, skipped, company, nothing_found):
    """
    The final check of process_tender_files() and recheck_saved_criteria(); nothing_found explains no criteria.
    Besides the verdict for bidding alone, returns 'jv' ({'allowed', 'max_members', 'verdict', 'conditions'}, where
    verdict is for the company as JV lead member, see jv_verdict()) and 'comparison' (see compare_routes()).
    """
    if not summaries:
        return {'verdict': REQUIRES_REVIEW, 'details': _note_skipped(nothing_found, skipped), 'score': None,
                'criteria': [], 'unreadable_files': skipped, 'comparison': [],
                'jv': {'allowed': 'unknown', 'max_members': None, 'verdict': None, 'conditions': []}}
    assessment = plain_maths(assess_eligibility(llm, summaries, company))
    verdict = parse_verdict(assessment)
    # A verdict that never saw some documents must not read as complete
    if skipped and verdict == ELIGIBLE:
        verdict = REQUIRES_REVIEW
    criteria = parse_criteria(assessment)
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
    return exact or next((row for row in candidates
                          if key and criterion_key(row['criterion'])
                          and (key in criterion_key(row['criterion']) or criterion_key(row['criterion']) in key)),
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
        row = {key: cells[index] if index is not None and index < len(cells) else ''
               for key, index in columns.items()}
        row['status'] = _status(row['status'])
        row['criterion'] = row['criterion'] or row['requirement'] or f'Criterion {len(criteria) + 1}'
        criteria.add(row)
    return criteria.rows()


class _Rows:
    """
    Criteria rows read from one or more tables, in order. A row replaces the row of the same criterion from an
    earlier table (a corrected table); within one table, rows of the same name are all kept.
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
            earlier[0], earlier[2] = self._table, row
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
    start = next((i for i, line in enumerate(lines) if _heading_level(line) and _JV_WORDS.search(line)), None)
    if start is None:
        return lines, []
    top = min(_heading_level(lines[start]), 6)

    def ends(line):
        level = _heading_level(line)
        return bool(re.search(r'overall\s+verdict', line, re.IGNORECASE)) or (level is not None and level <= top)

    end = next((i for i in range(start + 1, len(lines)) if ends(lines[i])), len(lines))
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
    """Met / Not met / anything else, as 'met', 'not_met' or 'unknown'."""
    text = text.lower()
    if re.search(r'\bnot\s+met\b', text):
        return 'not_met'
    return 'met' if re.search(r'\bmet\b', text) else 'unknown'


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
