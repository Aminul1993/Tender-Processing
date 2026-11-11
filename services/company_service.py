"""
The company profile as a form: splits the profile markdown into the blocks the editor shows (title,
section headings, tables of fields, free text) and turns the edited blocks back into markdown. Also keeps the
profile's table of the criteria statuses the bid team marked by hand.
"""
import re

_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*$')
_RULE = re.compile(r'^(-{3,}|\*{3,}|_{3,})$')
_SEPARATOR = re.compile(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$')
_UNESCAPED_PIPE = re.compile(r'(?<!\\)\|')
_NUMBERING = re.compile(r'^\s*\d+\.\s*')

# What the editor starts from when the profile file is empty or missing
EMPTY_PROFILE = [
    {'type': 'heading', 'level': 1, 'text': 'Company Profile'},
    {'type': 'table', 'columns': ['Item', 'Value'], 'rows': [['Company Name', '']]},
]

# The section of the profile that records the statuses the bid team marks by hand on tenders' criteria (see
# record_mark()), so the tenders checked after know what the team found
MARKED_HEADING = 'Criteria marked by the bid team'
MARKED_COLUMNS = ['Criterion', 'Requirement', 'Status', 'TCNO', 'Marked on']
# As the profile, and the model reading it, words each status
MARK_LABELS = {'met': 'Met', 'not_met': 'Not met', 'unknown': 'Cannot determine'}


def parse_profile(markdown):
    """
    Splits the profile markdown into blocks, in order:
      {'type': 'heading', 'level': 1-6, 'text': ...}
      {'type': 'table', 'columns': [...], 'rows': [[...], ...]}  every row has one cell per column
      {'type': 'text', 'text': ...}                              anything else, such as notes or lists
      {'type': 'rule'}                                           a --- line
    render_profile() turns the blocks back into markdown.
    """
    blocks = []
    text = []

    def end_text():
        joined = '\n'.join(text).strip()
        if joined:
            blocks.append({'type': 'text', 'text': joined})
        text.clear()

    lines = (markdown or '').splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        heading = _HEADING.match(line)
        is_table = line.startswith('|') and index + 1 < len(lines) and _SEPARATOR.match(lines[index + 1].strip())
        if not (heading or is_table or _RULE.match(line)):
            text.append(lines[index])
            index += 1
            continue
        end_text()
        if heading:
            blocks.append({'type': 'heading', 'level': len(heading.group(1)), 'text': heading.group(2)})
            index += 1
        elif is_table:
            columns = _cells(line)
            rows = []
            index += 2
            while index < len(lines) and lines[index].strip().startswith('|'):
                # Like GFM: missing cells are empty, extra cells are ignored
                rows.append((_cells(lines[index]) + [''] * len(columns))[:len(columns)])
                index += 1
            blocks.append({'type': 'table', 'columns': columns, 'rows': rows})
        else:
            blocks.append({'type': 'rule'})
            index += 1
    end_text()
    return blocks


def render_profile(blocks):
    """
    Turns blocks in the shape parse_profile() returns into markdown. Table rows with every cell empty,
    empty headings and empty text are left out, and so is a --- rule with nothing to separate (at the start,
    at the end, or after another rule). A cell is kept on one line, with any | escaped.
    """
    parts = []
    for block in blocks:
        kind = block['type']
        if kind == 'heading':
            text = ' '.join(block['text'].split())
            if text:
                parts.append(f"{'#' * block['level']} {text}")
        elif kind == 'table':
            columns = [_cell(column) for column in block['columns']]
            rows = [[_cell(cell) for cell in (row + [''] * len(columns))[:len(columns)]] for row in block['rows']]
            lines = [_row(columns), _row(['---'] * len(columns))]
            lines += [_row(row) for row in rows if any(row)]
            parts.append('\n'.join(lines))
        elif kind == 'text':
            text = block['text'].strip()
            if text:
                parts.append(text)
        elif kind == 'rule' and parts and parts[-1] != '---':
            parts.append('---')
    if parts and parts[-1] == '---':
        parts.pop()
    return '\n\n'.join(parts)


def unnamed_part(blocks):
    """
    What the editor must name before saving, as a message, or None: a heading below the title with no name
    (blank, or only a number such as "9."), or a table column with no name.
    """
    for block in blocks:
        if block['type'] == 'heading' and block['level'] > 1 and not _NUMBERING.sub('', block['text']).strip():
            return 'Every heading and sub-heading needs a name. Name it, or remove it.'
        if block['type'] == 'table' and not all(column.strip() for column in block['columns']):
            return 'Every table column needs a name. Name it, or remove the column.'
    return None


def record_mark(markdown, tcno, criterion, status, marked_on):
    """
    The profile markdown with the status the bid team marked by hand on criterion (a result's criterion, with its
    'criterion' name and 'requirement') of tender tcno recorded in the MARKED_HEADING table, replacing the row of
    that criterion and tender: status is 'met', 'not_met' or 'unknown', marked_on the date. None removes the row,
    and the section with its last row. The section is added at the end of the profile when it has none.
    """
    key = (str(tcno), _key(criterion['criterion']))

    def change(rows):
        kept = [row for row in rows if _mark_key(row) != key]
        if status:
            at = next((index for index, row in enumerate(rows) if _mark_key(row) == key), len(kept))
            kept.insert(at, [criterion['criterion'], criterion.get('requirement') or '', MARK_LABELS[status],
                             str(tcno), marked_on])
        return kept
    return _change_marks(markdown, change)


def profile_for_tender(markdown, tcno):
    """
    The profile markdown to check tender tcno against: without the tender's own rows in the MARKED_HEADING table.
    Its marks are carried over to its new result by the app (TenderProcessor._carry_marks()), so the model must
    not see them and return them as its own finding.
    """
    return _change_marks(markdown, lambda rows: [row for row in rows if row[3].strip() != str(tcno)])


def _change_marks(markdown, change):
    """
    Applies change to the rows of the MARKED_HEADING table (a list, empty when there is none) and returns the
    markdown, as it was when the rows did not change.
    """
    blocks = parse_profile(markdown)
    heading, table = _marks_table(blocks)
    rows = table['rows'] if table else []
    changed = change(list(rows))
    if changed == rows:
        return markdown
    if table is None:
        if heading is None:
            blocks += [{'type': 'rule'}, {'type': 'heading', 'level': 2, 'text': MARKED_HEADING}]
            heading = len(blocks) - 1
        table = {'type': 'table', 'columns': list(MARKED_COLUMNS), 'rows': []}
        blocks.insert(heading + 1, table)
    table['rows'] = changed
    if not changed:
        del blocks[heading:heading + 2]
    return render_profile(blocks)


def _marks_table(blocks):
    """
    The index in blocks of the MARKED_HEADING heading (a number before the name is ignored) and the table of
    MARKED_COLUMNS right under it: (None, None) without the heading, (index, None) without the table.
    """
    for index, block in enumerate(blocks):
        if block['type'] == 'heading' and _key(_NUMBERING.sub('', block['text'])) == _key(MARKED_HEADING):
            below = blocks[index + 1] if index + 1 < len(blocks) else {}
            columns = [_key(column) for column in below.get('columns', [])]
            return index, below if columns == [_key(column) for column in MARKED_COLUMNS] else None
    return None, None


def _mark_key(row):
    """A MARKED_COLUMNS row's tender and criterion, for matching."""
    return row[3].strip(), _key(row[0])


def _key(text):
    return ' '.join(text.split()).lower()


def company_profile(text):
    """The company name in the profile text (markdown), as the app bar shows it."""
    match = re.search(r'^\|\s*Company Name\s*\|\s*(.+?)\s*\|', text, re.MULTILINE | re.IGNORECASE)
    name = match.group(1) if match else ('Company profile' if text.strip() else 'No company profile')
    return {'name': name}


def _cells(line):
    """The cells of a '| a | b |' table row, with escaped pipes unescaped."""
    line = line.strip()
    if line.startswith('|'):
        line = line[1:]
    if line.endswith('|') and not line.endswith('\\|'):
        line = line[:-1]
    return [cell.strip().replace('\\|', '|') for cell in _UNESCAPED_PIPE.split(line)]


def _cell(text):
    """A value as one line of markdown table cell: whitespace and line breaks collapsed, | escaped."""
    return ' '.join(str(text).split()).replace('|', '\\|')


def _row(cells):
    return '| ' + ' | '.join(cells) + ' |'
