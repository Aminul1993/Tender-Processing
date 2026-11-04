"""Reads every file of a tender and checks the company's eligibility with the LLM pipeline from ext.py."""
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from markitdown import MarkItDown

from ext import (COMPANY_DETAILS_FILE, ELIGIBILITY_GOAL, TenderTigerBlocked, assess_eligibility, company_details,
                 make_model, prequalification_criteria, summarize_for_goal)

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


def tender_files(tcno_folder):
    """The tender's documents: every file under tcno_folder except the saved criteria and result."""
    tcno_folder = Path(tcno_folder)
    saved = {tcno_folder / CRITERIA_FILE, tcno_folder / RESULT_FILE}
    return sorted(path for path in tcno_folder.rglob('*') if path.is_file() and path not in saved)


def process_tender_files(tcno_folder, on_progress=None, company_file=COMPANY_DETAILS_FILE,
                         prequalification_link=None):
    """
    Checks the company profile saved in company_file against the eligibility criteria found in every file
    under tcno_folder, and in the pre-qualification criteria behind prequalification_link (the tender's
    TenderTiger GetPreQualification link), when one is given. The criteria found are saved in
    tcno_folder/CRITERIA_FILE before the check, for recheck_saved_criteria().

    Returns {'verdict', 'details', 'score', 'criteria', 'unreadable_files'}:
      verdict           Eligible, Not Eligible or Requires Review
      details           the full assessment as markdown (criteria table, calculations, verdict)
      score             percentage of criteria marked Met, or None when the assessment has no criteria table
      criteria          the rows of the criteria table, see parse_criteria()
      unreadable_files  files that could not be read or summarised, so were not checked; PREQUALIFICATION_SOURCE
                        when the pre-qualification criteria could not be fetched

    on_progress, if given, is called with a short status message before each step, plus step and steps
    (1-based position and total, counting each file and the final check) when they are known.
    Raises RuntimeError when OLLAMA_API_KEY is missing, the company profile is empty, or there is nothing to check
    (no files and no pre-qualification criteria); errors from the final LLM call propagate.
    """
    report = on_progress or (lambda message, **position: None)
    # Read once, so every file of the tender is checked against the same profile
    company = _company(company_file)

    tcno_folder = Path(tcno_folder)
    files = tender_files(tcno_folder)
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

    llm = make_model()
    md = MarkItDown()
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
        if not text:
            log.info('No readable text in %s', name)
            skipped.append(name)
            continue
        try:
            summary = summarize_for_goal(llm, text, ELIGIBILITY_GOAL)
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
    return _assess(llm, summaries, skipped, company,
                   f'None of the {len(files)} file(s) contained eligibility criteria that could be read.')


def recheck_saved_criteria(tcno_folder, on_progress=None, company_file=COMPANY_DETAILS_FILE):
    """
    Checks the company profile saved in company_file against the criteria in tcno_folder/CRITERIA_FILE, as
    saved by process_tender_files() and perhaps edited since, without reading the documents again: one LLM call.
    The sources the file lists as unreadable stay unreadable, so the check is still marked incomplete.

    Returns the same dict as process_tender_files(); on_progress is called with step 1 of 1.
    Raises RuntimeError when OLLAMA_API_KEY is missing or the company profile is empty, FileNotFoundError
    without a criteria file; errors from the LLM call propagate.
    """
    report = on_progress or (lambda message, **position: None)
    company = _company(company_file)
    criteria, skipped = read_criteria(Path(tcno_folder) / CRITERIA_FILE)
    report('Checking eligibility against the saved criteria', step=1, steps=1)
    return _assess(make_model(), [criteria] if criteria else [], skipped, company,
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
    if not os.getenv('OLLAMA_API_KEY'):
        raise RuntimeError('OLLAMA_API_KEY is not set. Add it to .env and restart the app.')
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
    The outcome for the company as JV lead member, with the verdict labels: Not Eligible when it misses a lead
    member requirement, Eligible when it meets every one and no source was unreadable, otherwise Requires Review.
    None when no JV is allowed or the documents do not say.
    """
    if jv['allowed'] != 'yes':
        return None
    statuses = [row['lead_status'] for row in comparison]
    if 'not_met' in statuses:
        return NOT_ELIGIBLE
    if statuses and all(status == 'met' for status in statuses) and not skipped:
        return ELIGIBLE
    return REQUIRES_REVIEW


def _same_criterion(name, candidates):
    """The JV row in candidates for the criterion called name, or None."""
    key = _criterion_key(name)
    exact = next((row for row in candidates if _criterion_key(row['criterion']) == key), None)
    return exact or next((row for row in candidates
                          if key and _criterion_key(row['criterion'])
                          and (key in _criterion_key(row['criterion']) or _criterion_key(row['criterion']) in key)),
                         None)


def _criterion_key(name):
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
        key = _criterion_key(row['criterion'])
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
