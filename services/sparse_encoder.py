"""
Sparse (lexical) vectors for hybrid retrieval. Each chunk, and each search, gets a sparse vector of the words and
figures in it beside its dense embedding, so a search also rewards exact matches that the dense model blurs:
  amounts     however they are written: ₹ 25,00,000, Rs. 25 lakh and 2.5 crore are all amt:2500000 / amt:25000000
  clauses     clause 4.2.1, Section-III, Annexure 3 and a bare 4.2.1 heading (ref:clause:4.2.1, ref:4.2.1, ...)
  percentages 51%, 51 per cent (pct:51)
  years       financial years written 2023-24, FY 2023-2024 (fy:2023-24)

A term weighs 1 + ln(its count), and a figure FIGURE_WEIGHT times that; each vector is scaled to unit length, so a
chunk and a search score the cosine of their terms, 0 to 1, on the same scale as the dense score it is added to.
There are no corpus statistics (IDF): the words nearly every tender uses ('tender', 'bidder', 'shall') are left out
instead. Terms are hashed into Pinecone's 32-bit sparse indices.
"""
import hashlib
import math
import re
import unicodedata
from collections import Counter

# Bumped when the terms or their weights change, so chunks indexed with the old ones are indexed again
VERSION = 1
# The most terms a vector keeps, heaviest first: Pinecone takes 1,000 non-zero values in a sparse vector of a dense
# index
MAX_TERMS = 1000
# How much more an amount, clause, percentage or financial year weighs than a word
FIGURE_WEIGHT = 2.0

_UNITS = {'thousand': 1e3, 'lakh': 1e5, 'lac': 1e5, 'crore': 1e7, 'cr': 1e7, 'million': 1e6, 'mn': 1e6,
          'billion': 1e9, 'bn': 1e9}
_CURRENCY = re.compile(r'₹|\brs\b\.?|\binr\b|\brupees?\b')
_FINANCIAL_YEAR = re.compile(r'\b(?:fy\s*)?((?:19|20)\d{2})\s*[-–/]\s*((?:19|20)?\d{2})\b')
_PERCENT = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)')
_AMOUNT = re.compile(r'(?<![\w.,])(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?'
                     r'(?:\s*(thousands?|lakhs?|lacs?|crores?|crs?|millions?|mn|billions?|bn)\b\.?)?')
_REFERENCE = re.compile(r'\b(clause|cl|section|sec|para|paragraph|article|annexure|annex|appendix|schedule|form|'
                        r'format|chapter|part|item|table)\b\.?\s*(?:no\.?\s*)?[-:–]?\s*'
                        r'(\d{1,3}(?:\.\d{1,3})*|[ivx]{1,6}\b|[a-z]\b)')
_DOTTED = re.compile(r'(?<![\w.])\d{1,2}(?:\.\d{1,2}){1,3}(?![\w.])')
_NUMBER = re.compile(r'(?<![\w.])\d{2,}(?![\w.])')
_WORD = re.compile(r'[a-z][a-z0-9]*')
_KINDS = {'cl': 'clause', 'sec': 'section', 'paragraph': 'para', 'annex': 'annexure', 'format': 'form'}
_ROMAN = {'i': 1, 'v': 5, 'x': 10}
_STOPWORDS = frozenset('''
    a about above after again against all also am an and any are as at be been before being below between both but
    by can could did do does doing down during each either etc few for from further had has have having here how if
    in into is it its itself just more most no nor not now of off on once only or other our out over own same she so
    some such than that the their them then there these they this those through to too under until up upon very via
    was we were what when where which while who whom why with within without would you your
    inr shall will must may should hereby herein thereof whereas said per vide
    tender tenderer tenderers bid bids bidder bidders document documents page
'''.split())


def terms(text):
    """Counter of the terms of text: its figures (amt:, ref:, pct:, fy:), other numbers of two digits or more, and
    its words (lower case, a plural s dropped), stop words left out."""
    text = _CURRENCY.sub(' inr ', unicodedata.normalize('NFKC', text or '').lower())
    found = Counter()

    def take(pattern, make):
        """Counts the terms make(match) gives for each match of pattern, and blanks the matches it gave any for."""
        def replace(match):
            made = make(match)
            if not made:
                return match.group(0)
            found.update(made)
            return ' '
        return pattern.sub(replace, text)

    text = take(_FINANCIAL_YEAR, _financial_year)
    text = take(_PERCENT, lambda match: [f'pct:{_plain(match.group(1))}'])
    text = take(_AMOUNT, _amount)
    text = take(_REFERENCE, _reference)
    text = take(_DOTTED, lambda match: [f'ref:{match.group(0)}'])
    found.update(_NUMBER.findall(text))
    found.update(_stem(word) for word in _WORD.findall(text) if len(word) > 1 and word not in _STOPWORDS)
    return found


def sparse_vector(text):
    """The sparse vector of text, {'indices', 'values'} of unit length; None when it has no terms."""
    weights = {}
    for term, count in terms(text).items():
        weight = (1.0 + math.log(count)) * (FIGURE_WEIGHT if ':' in term else 1.0)
        index = _index(term)
        weights[index] = weights.get(index, 0.0) + weight  # two terms hashed alike share an index
    if not weights:
        return None
    heaviest = sorted(weights.items(), key=lambda item: (-item[1], item[0]))[:MAX_TERMS]
    norm = math.sqrt(sum(weight * weight for _, weight in heaviest))
    return {'indices': [index for index, _ in heaviest], 'values': [weight / norm for _, weight in heaviest]}


def scaled(vector, factor):
    """A sparse vector with every value times factor."""
    return {'indices': list(vector['indices']), 'values': [value * factor for value in vector['values']]}


def _index(term):
    return int.from_bytes(hashlib.blake2b(term.encode('utf-8'), digest_size=4).digest(), 'big')


def _financial_year(match):
    start, end = match.group(1), match.group(2)[-2:]
    if (int(start) + 1) % 100 != int(end):
        return None  # a range of years, not one financial year
    return [f'fy:{start}-{end}']


def _amount(match):
    """amt:<rupees> for a sum: one with a unit (lakh, crore, ...), Indian or western comma groups, or after a
    currency word; None for a bare number (a year, a count), left for the later patterns."""
    whole, fraction, unit = match.groups()
    before = match.string[max(0, match.start() - 8):match.start()]
    if not (unit or ',' in whole or 'inr' in before):
        return None
    value = float(whole.replace(',', '') + (f'.{fraction}' if fraction else ''))
    if unit:
        value *= _UNITS[unit.rstrip('s') if unit.rstrip('s') in _UNITS else unit]
    return [f'amt:{round(value)}']


def _reference(match):
    kind = _KINDS.get(match.group(1), match.group(1))
    name = match.group(2)
    if re.fullmatch(r'[ivx]+', name) and _roman(name):
        name = str(_roman(name))
    made = [f'ref:{kind}:{name}']
    if '.' in name:
        made.append(f'ref:{name}')  # "clause 4.2" finds the heading "4.2 Eligibility"
    return made


def _roman(numeral):
    """The value of a Roman numeral of i, v and x (iii, iv, xii), or None for anything else."""
    total = 0
    for position, letter in enumerate(numeral):
        value = _ROMAN[letter]
        following = _ROMAN[numeral[position + 1]] if position + 1 < len(numeral) else 0
        total += -value if value < following else value
    return total if 0 < total < 40 else None


def _plain(number):
    """'51.00' as '51', '2.50' as '2.5'."""
    return number.rstrip('0').rstrip('.') if '.' in number else number


def _stem(word):
    """A word without its plural s: certificates and certificate are one term."""
    return word[:-1] if len(word) > 4 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')) else word
