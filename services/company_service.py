"""
The company profile every tender is checked against, kept in the database as its blocks (CompanyStore), and as a
form: splits the profile markdown into the blocks the editor shows (title, section headings, tables of fields, free
text) and turns the edited blocks back into markdown. Also keeps, apart from the profile, the criteria statuses the
bid team marked by hand, which the tenders checked after are given with it.
"""
import re
from collections import defaultdict
from contextlib import contextmanager

_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*$')
_RULE = re.compile(r'^(-{3,}|\*{3,}|_{3,})$')
_SEPARATOR = re.compile(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$')
_UNESCAPED_PIPE = re.compile(r'(?<!\\)\|')
_NUMBERING = re.compile(r'^\s*\d+\.\s*')

# The advisory lock a save of the profile is made under
_PROFILE_LOCK = 7_260_003


class CompanyStore:
    """
    The company profile, edited on the page, in db (a services.database.Database): its blocks (parse_profile()) in
    the company_profile_blocks table, in order, and the rows of its tables in company_profile_rows, each the child of
    its table's block. Also the statuses the bid team marked by hand on tenders' criteria (the criterion_marks table),
    one per tender and criterion.
    """

    def __init__(self, db):
        self.db = db

    def read(self):
        """The profile as markdown, '' before one is saved."""
        return render_profile(self.blocks())

    def blocks(self):
        """The profile's blocks, in the shape parse_profile() gives; [] before one is saved."""
        with self.db.connection() as connection:
            blocks = connection.execute('SELECT id, type, level, text, columns FROM company_profile_blocks '
                                        'ORDER BY position').fetchall()
            rows = connection.execute('SELECT block_id, cells FROM company_profile_rows '
                                      'ORDER BY block_id, position').fetchall()
        cells = defaultdict(list)
        for block_id, row in rows:
            cells[block_id].append(row)
        found = []
        for block_id, kind, level, text, columns in blocks:
            if kind == 'heading':
                found.append({'type': kind, 'level': level, 'text': text})
            elif kind == 'table':
                found.append({'type': kind, 'columns': columns, 'rows': cells[block_id]})
            elif kind == 'text':
                found.append({'type': kind, 'text': text})
            else:
                found.append({'type': kind})
        return found

    def save(self, text):
        """Saves the profile markdown, as its blocks and table rows, in place of the last, all or nothing."""
        with self._locked() as connection:
            connection.execute('DELETE FROM company_profile_blocks')  # and their rows
            for position, block in enumerate(parse_profile(text)):
                block_id = connection.execute(
                    'INSERT INTO company_profile_blocks (position, type, level, text, columns) '
                    'VALUES (%s, %s, %s, %s, %s) RETURNING id',
                    (position, block['type'], block.get('level'), block.get('text'), block.get('columns'))
                ).fetchone()[0]
                if block['type'] == 'table' and block['rows']:
                    with connection.cursor() as cursor:
                        cursor.executemany('INSERT INTO company_profile_rows (block_id, position, cells) '
                                           'VALUES (%s, %s, %s)',
                                           [(block_id, number, row) for number, row in enumerate(block['rows'])])

    def marks(self):
        """Every status marked by hand, as MARKED_COLUMNS rows, in the order they were first marked."""
        with self.db.connection() as connection:
            rows = connection.execute('SELECT criterion, requirement, status, tcno, marked_on FROM criterion_marks '
                                      'ORDER BY seq').fetchall()
        return [[criterion, requirement, MARK_LABELS[status], tcno, marked_on]
                for criterion, requirement, status, tcno, marked_on in rows]

    def mark(self, tcno, criterion, status, marked_on):
        """
        Records the status the bid team marked by hand on criterion (a result's criterion, with its 'criterion'
        name and 'requirement') of tender tcno, in place of its last: 'met', 'not_met' or 'unknown', marked_on the
        date. None removes it.
        """
        key = (str(tcno), _key(criterion['criterion']))
        with self.db.connection() as connection:
            if status is None:
                connection.execute('DELETE FROM criterion_marks WHERE tcno = %s AND criterion_key = %s', key)
            else:
                connection.execute(
                    'INSERT INTO criterion_marks (tcno, criterion_key, criterion, requirement, status, marked_on) '
                    'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tcno, criterion_key) DO UPDATE SET '
                    'criterion = excluded.criterion, requirement = excluded.requirement, status = excluded.status, '
                    'marked_on = excluded.marked_on',
                    (*key, criterion['criterion'], criterion.get('requirement') or '', status, marked_on))

    def tender_marks(self, tcno):
        """The statuses marked by hand on tender tcno's criteria, as (criterion, status) pairs, first marked first."""
        with self.db.connection() as connection:
            return connection.execute('SELECT criterion, status FROM criterion_marks WHERE tcno = %s ORDER BY seq',
                                      (str(tcno),)).fetchall()

    def for_tender(self, tcno):
        """
        The profile to check tender tcno against: with the marks of the other tenders, not its own (see
        profile_for_tender()). '' while the profile is empty.
        """
        return profile_for_tender(self.read(), [row for row in self.marks() if row[3] != str(tcno)])

    @contextmanager
    def _locked(self):
        with self.db.transaction() as connection:
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (_PROFILE_LOCK,))
            yield connection


# What the editor starts from when the profile is empty
EMPTY_PROFILE = [
    {'type': 'heading', 'level': 1, 'text': 'Company Profile'},
    {'type': 'table', 'columns': ['Item', 'Value'], 'rows': [['Company Name', '']]},
]

# The table of the statuses the bid team marked by hand on tenders' criteria, given with the profile (see
# profile_for_tender()), so the tenders checked after know what the team found
MARKED_HEADING = 'Criteria marked by the bid team'
MARKED_COLUMNS = ['Criterion', 'Requirement', 'Status', 'TCNO', 'Marked on']
# As that table, and the model reading it, words each status
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


def profile_for_tender(markdown, marks):
    """
    The profile markdown a tender is checked against: the profile, with marks (MARKED_COLUMNS rows, the statuses
    the bid team marked by hand on other tenders' criteria) in a MARKED_HEADING table at its end; the profile alone
    without marks, and '' while it is empty. A tender's own marks are never given: the app carries them over to its
    new result (TenderProcessor._carry_marks()), so the model must not see them and return them as its own finding.
    """
    if not markdown.strip() or not marks:
        return markdown
    return markdown.rstrip() + '\n\n---\n\n' + render_profile([
        {'type': 'heading', 'level': 2, 'text': MARKED_HEADING},
        {'type': 'table', 'columns': list(MARKED_COLUMNS), 'rows': marks}])


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
