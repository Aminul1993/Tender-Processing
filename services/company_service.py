"""
The company profile as a form: splits the profile markdown into the blocks the editor shows (title,
section headings, tables of fields, free text) and turns the edited blocks back into markdown.
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
