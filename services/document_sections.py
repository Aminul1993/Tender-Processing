"""
The sections of a tender document, told from its headings, for hierarchical retrieval: a search that finds part of
the qualification criteria can then take the whole section they are in (PineconeService.retrieve()).

Headings come in two tiers:
  major  a part of the document: a markdown # or ## heading, a line starting SECTION, CHAPTER, PART, ANNEXURE,
         APPENDIX, SCHEDULE or FORM, or a short line in capitals (NOTICE INVITING E-TENDER (NIT))
  minor  a numbered or lettered clause heading in capitals or title case (4.2 Financial Criteria, B. AVERAGE ANNUAL
         TURNOVER:), a markdown ### heading or a line in bold
A major section runs to the next major heading, a minor one to the next heading of either tier, so a major section
holds the minor ones after its heading. Lines repeated on many pages (the running header and footer) are never
headings, nor are table rows and the '## Page <n>' lines OCR adds.

Each chunk is then given the chunks of every section it overlaps, as ranges 'first-last' of its source's chunks,
smallest first: a chunk holding the end of one section and the heading of the next is in both. Only a section of
two chunks or more is listed, as one within a chunk adds nothing to finding that chunk. A search's matches can then
vote for the sections they are in: every match in a minor section is in its major one too, so the major section
gathers the votes of all of them.

The sections of each document are also listed whole (contents()), with their headings, for a model to choose the
ones that state the qualification criteria from (utils/llm.py choose_sections()).
"""
import re
from collections import Counter

# Bumped when headings or ranges are told differently, so chunks indexed the old way are indexed again. 2: a bare
# keyword and number ("ANNEXURE 1") is a heading
VERSION = 2
# A major section with less text than this after its heading line is joined to the next one, titles too: a heading
# split over two lines, or a heading followed at once by another (SECTION III, then QUALIFICATION CRITERIA)
MIN_BODY_CHARS = 120
# Longest title kept in a chunk's metadata
TITLE_CHARS = 120
_MAX_HEADING_CHARS = 100
_MAX_HEADING_WORDS = 12
# A line that appears this often is a running header or footer
_REPEATED = 3

_PAGE_MARKER = re.compile(r'#{1,6}\s+page \d+(?:, image \d+)?', re.IGNORECASE)
_MARKDOWN = re.compile(r'(#{1,6})\s+(.+?)[\s#]*')
_BOLD = re.compile(r'(\*\*|__)([^*_]+?)\1:?')
_KEYWORD = re.compile(r'(?:section|chapter|part|volume|annexure|annex|appendix|schedule|form|format|proforma)\b',
                      re.IGNORECASE)
_NUMBERED = re.compile(r'\(?(?:\d{1,2}(?:\.\d{1,2}){0,3}|[a-h]|[ivx]{1,4})[.)]\s*(\S.*)'
                       r'|\d{1,2}(?:\.\d{1,2}){1,3}\s+(\S.*)', re.IGNORECASE)
_PAGE_NUMBER_END = re.compile(r'(?:\.{3,}|\s)\s*\d{1,3}(?:\s*[-–]\s*\d{1,3})?$')
# A keyword and its number alone ("ANNEXURE 1", "Section 3"): a heading, though it ends like a contents line
_KEYWORD_NUMBER = re.compile(_KEYWORD.pattern + r'\s*[-–:.]?\s*\d{1,3}[a-z]?', re.IGNORECASE)


def chunk_sections(text, spans):
    """
    The section metadata of each chunk of text, whose pieces (each chunk without the overlap it repeats) span
    spans, [(start, end)] in text: [{'section': the title of the section most of the chunk is in, a minor one
    before a major one, 'sections': ['first-last', ...] of every section of two chunks or more it overlaps,
    smallest first, 'section_titles': their titles}], keys left out when there is nothing to give.
    """
    majors, minors = _sections(text)
    tiers = [_ranges(minors, spans), _ranges(majors, spans)]
    places = []
    for number, (start, end) in enumerate(spans):
        place = {}
        for tier in tiers:
            best = max(tier, key=lambda section: _overlap(section, start, end), default=None)
            if best is not None and _overlap(best, start, end) > 0 and best[4]:
                place['section'] = best[4][:TITLE_CHARS]
                break
        ranges = sorted({(last - first, first, last, title[:TITLE_CHARS]) for tier in tiers
                         for _, _, first, last, title in tier if first <= number <= last and last > first})
        if ranges:
            place['sections'] = [f'{first}-{last}' for _, first, last, _ in ranges]
            place['section_titles'] = [title for *_, title in ranges]
        places.append(place)
    return places


def contents(text, spans):
    """
    The table of contents of text, for a model to choose sections from by their headings: [[tier, first chunk, last
    chunk, characters, title]] of each section with a title, in document order (a major section before the minor
    ones in it), where first and last are the chunks of spans (as in chunk_sections()) the section overlaps.
    """
    majors, minors = _sections(text)
    found = sorted((start, tier, end, title) for tier, sections in ((1, majors), (2, minors))
                   for start, end, title in sections if title)
    entries = []
    for start, tier, end, title in found:
        chunks = [number for number, (piece_start, piece_end) in enumerate(spans)
                  if piece_start < end and piece_end > start]
        if chunks:
            entries.append([tier, chunks[0], chunks[-1], end - start, title[:TITLE_CHARS]])
    return entries


def headings(text):
    """[(offset of the line in text, tier 1 or 2, title, offset of the line's end)] of the heading lines of text, in
    order."""
    lines = []
    offset = 0
    for line in text.splitlines(keepends=True):
        lines.append((offset, line.strip()))
        offset += len(line)
    repeated = Counter(_normalised(line) for _, line in lines if line)
    found = []
    for offset, line in lines:
        heading = _heading(line) if line else None
        if heading and repeated[_normalised(line)] < _REPEATED:
            found.append((offset, *heading, offset + len(line)))
    return found


def _sections(text):
    """(major sections, minor sections) of text, each [(start, end, title)]; text before the first major heading
    is a major section without a title."""
    found = headings(text)
    major_starts = [(offset, title, line_end) for offset, tier, title, line_end in found if tier == 1]
    if not major_starts or major_starts[0][0] > 0:
        major_starts.insert(0, (0, '', 0))
    majors = []  # [start, end, title, end of its heading line]
    for number, (start, title, line_end) in enumerate(major_starts):
        end = major_starts[number + 1][0] if number + 1 < len(major_starts) else len(text)
        if majors and majors[-1][1] - majors[-1][3] < MIN_BODY_CHARS:
            before = majors.pop()  # next to nothing under its heading: joined to this one
            start, title = before[0], ' / '.join(part for part in (before[2], title) if part)
        majors.append((start, end, title, line_end))
    offsets = [offset for offset, *_ in found] + [len(text)]
    minors = [(offset, offsets[number + 1], title) for number, (offset, tier, title, _) in enumerate(found)
              if tier == 2]
    return [major[:3] for major in majors], minors


def _ranges(sections, spans):
    """[(start, end, first chunk, last chunk, title)] of the sections that hold any chunk."""
    ranges = []
    for start, end, title in sections:
        chunks = [number for number, (piece_start, piece_end) in enumerate(spans)
                  if piece_start < end and piece_end > start]
        if chunks:
            ranges.append((start, end, chunks[0], chunks[-1], title))
    return ranges


def _overlap(section, start, end):
    return min(section[1], end) - max(section[0], start)


def _heading(line):
    """(tier, title) when line (stripped) is a heading, else None. See the module's docstring."""
    if len(line) > _MAX_HEADING_CHARS or line.startswith('|') or '...' in line or _PAGE_MARKER.fullmatch(line):
        return None  # too long, a table row, a contents entry or schedule with leader dots, or a page OCR marked
    match = _MARKDOWN.fullmatch(line)
    if match:
        return (1 if len(match.group(1)) <= 2 else 2), _clean(match.group(2))
    match = _BOLD.fullmatch(line)
    if match:
        return 2, _clean(match.group(2))
    if line.endswith((',', ';')) or len(line.split()) > _MAX_HEADING_WORDS:
        return None
    match = _NUMBERED.fullmatch(line)
    if match:
        title = match.group(1) or match.group(2)
        capitals = _capitals(title)
        if _PAGE_NUMBER_END.search(title) or (title.endswith('.') and capitals < 0.9):
            return None  # a contents entry, or a sentence
        return (2, _clean(line)) if capitals >= 0.8 or _title_case(title) >= 0.6 else None
    if _KEYWORD.match(line) and (not _PAGE_NUMBER_END.search(line) or _KEYWORD_NUMBER.fullmatch(line)) \
            and (_capitals(line) >= 0.6 or (_title_case(line) >= 0.6 and not line.endswith('.'))):
        return 1, _clean(line)
    words = re.findall(r'[A-Za-z]{2,}', line)
    if len(words) >= 2 and sum(len(word) for word in words) >= 6 and _capitals(line) >= 0.9:
        return 1, _clean(line)
    return None


def _capitals(text):
    """The share of the letters of text in capitals; 0 with fewer than three letters."""
    letters = [character for character in text if character.isalpha()]
    return sum(character.isupper() for character in letters) / len(letters) if len(letters) >= 3 else 0.0


def _title_case(text):
    """The share of the words of four letters or more that start with a capital."""
    words = [word for word in re.findall(r'[A-Za-z]+', text) if len(word) >= 4]
    return sum(word[0].isupper() for word in words) / len(words) if words else 0.0


def _clean(title):
    return ' '.join(title.strip(' *_:#').split())


def _normalised(line):
    """A line as compared to tell running headers: lower case, without digits or extra spaces."""
    return ' '.join(re.sub(r'\d+', '', line.lower()).split())
