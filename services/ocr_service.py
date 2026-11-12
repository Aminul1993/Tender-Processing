"""
OCR of what a PDF's text layer leaves out: every page of a scan, a page with no text layer in an otherwise text PDF
(scanned, or drawn as vector graphics), and large images on the other pages (a scanned form pasted into an RFP).
Each is rendered and read locally with PaddleOCR first; only one PaddleOCR is unsure of, or that holds a table, is
sent to the vision model OCR_MODEL, which can also lay a table out as markdown. Clean prose costs no LLM tokens.
"""
import base64
import logging
import os
import statistics
import threading
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for

import numpy as np
import pymupdf

from ext import OCR_MODEL

log = logging.getLogger(__name__)

# PaddleOCR's text detection and recognition models are <PADDLEOCR_MODEL>_det and _rec. PP-OCRv6_small reads a scanned
# page in 5 to 15 seconds on a CPU; PP-OCRv6_tiny is about three times faster but makes more mistakes.
PADDLEOCR_MODEL = os.getenv('PADDLEOCR_MODEL', 'PP-OCRv6_small')
# A page whose text PaddleOCR is less sure of than this (its mean score per character, 0 to 1) goes to OCR_MODEL
OCR_MIN_CONFIDENCE = float(os.getenv('OCR_MIN_CONFIDENCE', '0.9'))
# The most one page or image sent to OCR_MODEL costs, in US dollars: 2,644 input and 882 output tokens (measured on
# tender scans) at qwen/qwen3.8-27b's Groq list price of $0.80 and $4.00 per million tokens (November 2026). Only the
# pages PaddleOCR is unsure of or that hold a table go there, so a cost at this price is the worst case.
OCR_PAGE_COST = float(os.getenv('OCR_PAGE_COST', '0.0056'))
# Characters of text a scanned page gives on average (2,133 over 32 pages of four tenders' scans), to tell from a scan's
# page count, before any OCR, that it is over MAX_FILE_CHARS
CHARS_PER_PAGE = 2000
_DPI = 200
# An image on a page with text is OCR'd only when it is at least this many pixels on each side, both in its own pixels
# and as drawn on the page at _DPI: logos and stamps are smaller, and so is the TenderTiger watermark in its own pixels
# (350 x 166, drawn across the page), while the GeM logo is 707 x 362 pixels drawn at about an inch
_MIN_IMAGE_PX = 300
# An image with this many words of the text layer inside it is already read: a scan with its OCR'd text kept
_COVERED_WORDS = 20
# A page or image holds a table with this many places where ruled lines cross (a bordered table of 2 x 2 cells has
# 9), or this many lines of text in three or more separate pieces (the rows of a table without borders)
_TABLE_CROSSINGS = 9
_TABLE_ROWS = 3
# Everything that changes what OCR reads, besides the file: a text cached under other settings is read again
OCR_SETTINGS = f'{PADDLEOCR_MODEL} below {OCR_MIN_CONFIDENCE} to {OCR_MODEL} at {_DPI} dpi'
_VISION_PROMPT = ('Extract all text from this image from a tender document. Return only the extracted text, in '
                  'reading order. Write each table as a markdown table. Do not add any commentary or description.')
# Pages and images read by OCR_MODEL at the same time in this process, all PDFs together, while PaddleOCR goes on
# with the next pages. Groq's free tier refuses that many at once (a rate limit, retried); paid tiers take them.
OCR_CONCURRENCY = int(os.getenv('OCR_CONCURRENCY', '8'))
_VISION = ThreadPoolExecutor(max_workers=max(1, OCR_CONCURRENCY), thread_name_prefix='ocr-vision')
# One page at a time on PaddleOCR in this process: one engine already uses every core, and more in parallel were no
# faster on a 10-core machine (8 pages: 77 s in turn, 71 s on 8 threads, 77 s in 4 processes; 8 processes ran out of
# memory at about 0.85 GB each)
_ENGINE = threading.Lock()
_engine = None


class ScanTooLong(Exception):
    """A PDF's text would be over the limit given to add_ocr(); the message says how much there is."""


class _ScanUnread(Exception):
    """A page of a scan could not be read, so none of it is."""


def add_ocr(path, text, llm, max_chars=None):
    """
    text, the text layer of the PDF at path (as MarkItDown read it), with what OCR reads of the rest after it: every
    page when text is empty, otherwise each page with no text layer and each large image on the other pages (see
    _parts_to_ocr()). Each is a '## Page <n>' or '## Page <n>, image <k>' section: PaddleOCR's reading, or llm's (an
    OCR_MODEL chat model) for one PaddleOCR is unsure of or that holds a table. Blank ones are left out. PaddleOCR
    reads one page at a time while llm reads those sent to it, OCR_CONCURRENCY at a time.
    Returns (text, unread): the text with what OCR read, and the parts that could not be read ('page 16',
    'page 132, image 1'), which a PDF with a text layer is checked without. A scan (text empty) with a page that
    could not be read gives ('', []) instead, as does one with no text at all, so the file is listed as not checked
    rather than checked with pages missing.
    Raises ScanTooLong when the text would be over max_chars, if given: before any OCR when the pages to OCR make it
    likely (at CHARS_PER_PAGE a page), otherwise once the part that takes it over is read, leaving the rest unread.
    """
    try:
        document = pymupdf.open(path)
    except Exception as error:
        log.warning('Could not open %s for OCR: %s', path.name, error)
        return text, []
    with document:
        parts = _parts_to_ocr(document, every_page=not text)
        if not parts:
            return text, []
        pages = sum(1 for _, clip, _ in parts if clip is None)
        estimate = len(text) + pages * CHARS_PER_PAGE
        if max_chars is not None and estimate > max_chars:
            raise ScanTooLong(f'{len(text):,} characters of text and {pages} page(s) to OCR, about {estimate:,} characters'
                              if text else f'{pages} scanned pages, about {estimate:,} characters')
        sections = [text] if text else []
        characters = len(text)  # of the text so far, as returned
        sent = Counter()  # why parts went to OCR_MODEL
        unread = []
        # The parts read so far and not yet added, in order: (page number, heading, its text, the Future of llm's
        # reading, or the error that stopped it)
        pending = []

        def settle(wait):
            """Adds the parts at the head of pending that are read (every one, when wait) to sections, in order."""
            nonlocal characters
            while pending and (wait or not isinstance(pending[0][2], Future) or pending[0][2].done()):
                number, heading, read = pending.pop(0)
                part = heading[0].lower() + heading[1:]
                try:
                    if isinstance(read, Exception):
                        raise read
                    if isinstance(read, Future):
                        read = read.result()
                except Exception as error:
                    log.warning('Could not OCR %s of %s: %s', part, path.name, error,
                                exc_info=(type(error), error, error.__traceback__))
                    if not text:
                        raise _ScanUnread() from error
                    unread.append(part)
                    continue
                if read:
                    sections.append(f'## {heading}\n\n{read}')
                    characters += len(sections[-1]) + (2 if len(sections) > 1 else 0)  # with the '\n\n' before it
                    if max_chars is not None and characters > max_chars:
                        raise ScanTooLong(f"{characters:,} characters once page {number} of {document.page_count} "
                                          f"was OCR'd")

        try:
            for number, clip, heading in parts:
                try:
                    pixmap = document[number - 1].get_pixmap(dpi=_DPI, clip=clip, colorspace=pymupdf.csRGB,
                                                             alpha=False)
                    with _ENGINE:
                        read, reason = _read_locally(pixmap)
                    if reason:
                        sent[reason] += 1
                        read = _VISION.submit(_read_with_vision, llm, pixmap.tobytes('png'))
                except Exception as error:
                    read = error
                pending.append((number, heading, read))
                settle(wait=False)
                # No further ahead of the parts added than OCR_CONCURRENCY: past max_chars, at most that many are
                # read for nothing
                while len(pending) >= OCR_CONCURRENCY:
                    if isinstance(pending[0][2], Future):
                        wait_for([pending[0][2]])  # its error, if any, is settle()'s to handle
                    settle(wait=False)
            settle(wait=True)
        except _ScanUnread:
            return '', []
        finally:
            for _, _, read in pending:  # left over by an error or ScanTooLong: those not started yet never are
                if isinstance(read, Future):
                    read.cancel()
    log.info('OCR of %s: %d page(s) and %d image(s), %d read by PaddleOCR (%s), %d by %s %s, %d not read', path.name,
             pages, len(parts) - pages, len(parts) - sum(sent.values()) - len(unread), PADDLEOCR_MODEL,
             sum(sent.values()), OCR_MODEL, dict(sent), len(unread))
    return '\n\n'.join(sections), unread


def ocr_parts(path, max_chars=None):
    """
    How many pages and images add_ocr() would OCR in the PDF at path, told without any OCR (the text layer as
    PyMuPDF reads it stands in for MarkItDown's): 0 when there are none, when the PDF cannot be opened, or when its
    page count says it is too long to OCR under max_chars.
    """
    try:
        with pymupdf.open(path) as document:
            text = sum(len(page.get_text().strip()) for page in document)
            parts = _parts_to_ocr(document, every_page=not text)
    except Exception:
        return 0
    pages = sum(1 for _, clip, _ in parts if clip is None)
    if max_chars is not None and text + pages * CHARS_PER_PAGE > max_chars:
        return 0
    return len(parts)


def _parts_to_ocr(document, every_page):
    """
    What of the PDF document to OCR, in page order: [(page number, clip, heading)], clip None for a whole page.
    A page is OCR'd whole when every_page is true, or when it has no text layer: it is scanned, or drawn as vector
    graphics, and then its images (a logo, say) are not all there is to read. On a page with text, an image is OCR'd
    when it is at least _MIN_IMAGE_PX on each side, in its own pixels and as drawn, unless the text layer already
    has _COVERED_WORDS words inside it.
    """
    parts = []
    for page in document:
        number = page.number + 1
        if every_page or not page.get_text().strip():
            parts.append((number, None, f'Page {number}'))
            continue
        words = None
        # An image drawn more than once is listed once per drawing; get_image_rects() gives every place it is drawn
        sizes = {xref: (width, height) for xref, _, width, height, *_ in page.get_images(full=True)}
        for xref, (width, height) in sizes.items():
            for rect in page.get_image_rects(xref):
                if min(width, height, rect.width * _DPI / 72, rect.height * _DPI / 72) < _MIN_IMAGE_PX:
                    continue
                if words is None:
                    words = page.get_text('words')
                if sum(1 for word in words if pymupdf.Rect(word[:4]) in rect) >= _COVERED_WORDS:
                    continue
                images = sum(1 for part in parts if part[0] == number)
                parts.append((number, rect, f'Page {number}, image {images + 1}'))
    return parts


def _read_locally(pixmap):
    """
    (text, reason) for the image of a page or part of one: PaddleOCR's text, and why it must go to OCR_MODEL instead
    ('unsure' or 'table'), or None when PaddleOCR's text will do. An image with no text at all is blank: ('', None).
    """
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)[:, :, ::-1]
    image = np.ascontiguousarray(image)  # PaddleOCR and OpenCV take BGR
    lines = _paddle_lines(image)
    characters = sum(len(text) for text, _, _ in lines)
    if not characters:
        return '', None
    confidence = sum(len(text) * score for text, score, _ in lines) / characters
    if confidence < OCR_MIN_CONFIDENCE:
        return '', 'unsure'
    if _ruled_crossings(image) >= _TABLE_CROSSINGS or _split_lines([box for _, _, box in lines]) >= _TABLE_ROWS:
        return '', 'table'
    return '\n'.join(text for text, _, _ in lines), None


def _paddle_lines(image):
    """PaddleOCR's lines of text in the image (BGR), in reading order: [(text, score 0 to 1, [x1, y1, x2, y2])]."""
    global _engine
    if _engine is None:
        # Imported here: Paddle takes seconds to load, and only scans need it. The models are downloaded on first use.
        from paddleocr import PaddleOCR
        _engine = PaddleOCR(text_detection_model_name=f'{PADDLEOCR_MODEL}_det',
                            text_recognition_model_name=f'{PADDLEOCR_MODEL}_rec', use_doc_orientation_classify=False,
                            use_doc_unwarping=False, use_textline_orientation=False)
    result = _engine.predict(image)[0]
    return list(zip(result['rec_texts'], result['rec_scores'], np.asarray(result['rec_boxes']).tolist()))


def _ruled_crossings(image):
    """
    How many places ruled lines cross in the image (BGR): a bordered table, or a form drawn as one. Lines count from
    a third of an inch across and a quarter of an inch down at _DPI, longer than any stroke of a letter.
    """
    import cv2  # OpenCV comes with PaddleOCR
    ink = cv2.adaptiveThreshold(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY_INV, 15, 10)
    across = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (_DPI // 3, 1)))
    down = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, _DPI // 4)))
    crossings = cv2.dilate(cv2.bitwise_and(across, down), np.ones((5, 5), np.uint8))
    return cv2.connectedComponents(crossings)[0] - 1  # less the background


def _split_lines(boxes):
    """How many lines of text are in three or more separate pieces, as the rows of a table are."""
    if not boxes:
        return 0
    height = statistics.median(bottom - top for _, top, _, bottom in boxes)
    rows = []  # [middle of the row's first piece, pieces]
    for _, top, _, bottom in sorted(boxes, key=lambda box: box[1] + box[3]):
        middle = (top + bottom) / 2
        if rows and middle - rows[-1][0] < height / 2:
            rows[-1][1] += 1
        else:
            rows.append([middle, 1])
    return sum(1 for _, pieces in rows if pieces >= 3)


def _read_with_vision(llm, png):
    """The text llm reads in the image (PNG bytes), tables as markdown. Errors propagate."""
    image = base64.b64encode(png).decode('ascii')
    reply = llm.invoke([{'role': 'user', 'content': [
        {'type': 'text', 'text': _VISION_PROMPT},
        {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{image}'}},
    ]}])
    return (reply.content or '').strip()
