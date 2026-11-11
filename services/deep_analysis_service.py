"""
Deep analysis: a second, multi-agent review of a tender's completed eligibility result. Five agents work in turn on
one shared state (a blackboard), each reading what the agents before it wrote:

  ingestion      Ingestion & Context agent (no LLM): validates the result, the criteria retrieved from the tender's
                 Pinecone namespace (see load_knowledge()) and the company profile, normalises money to rupees, dates to
                 ISO and financial years to their dates, flags expired or outdated profile entries, and collects the
                 statuses the bid team marked by hand.
  profiler       Company Profiler agent: flags company data that is missing, outdated, inconsistent or expired,
                 above all the data the criteria need. No external registry is connected, so it says what to check
                 against one (the MCA or GST portal, say) instead of claiming it did.
  audit          Eligibility Audit agent: a Pass / Fail / Insufficient data matrix, one row per criterion, judged
                 afresh from the criteria and the company data. It never sees the earlier statuses, so it cannot
                 lean on them.
  status_review  Discrepancy & Status Review agent: compares the statuses marked by hand (and the other manual
                 overrides: unreadable files marked checked, the shortlist decision) with the audit and the profiler,
                 hunting for confirmation bias and saying why a person might have marked something differently.
  supervisor     Supervisor / Judge agent: resolves the disagreements, asking the audit agent to re-examine disputed
                 criteria for up to MAX_ROUNDS rounds, then gives each criterion's final status and confidence, the
                 determination, and the data gaps.

The report, with an audit log of every step, is saved as DEEP_ANALYSIS_FILE in the tender's folder. It never
changes the result it reviews. DeepAnalysisRunner runs one deep analysis at a time in a background thread.
"""
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ext import (COMPANY_DETAILS_FILE, DEEP_AUDIT_MODEL, DEEP_PROFILER_MODEL, DEEP_STATUS_REVIEW_MODEL,
                 DEEP_SUPERVISOR_MODEL, company_details, make_model)
from services.analysis_service import DEEP_ANALYSIS_FILE, REQUIRES_REVIEW, TokenUsage, criterion_key, statuses_verdict
from services.company_service import parse_profile, profile_for_tender
from services.pinecone_service import NamespaceIncomplete, VectorStoreError
from services.processing_service import TenderBusy, explain_error
from utils.files import safe_name

log = logging.getLogger(__name__)

# The agents, in the order they first run
AGENTS = ('ingestion', 'profiler', 'audit', 'status_review', 'supervisor')
AGENT_TITLES = {
    'ingestion': 'Ingestion & Context', 'profiler': 'Company Profiler', 'audit': 'Eligibility Audit',
    'status_review': 'Discrepancy & Status Review', 'supervisor': 'Supervisor / Judge',
}
# The agents that call the LLM, whose tokens are counted
LLM_AGENTS = AGENTS[1:]
# The Groq model each of them runs on, set in .env (DEEP_PROFILER_MODEL and so on; see ext.py)
AGENT_MODELS = {'profiler': DEEP_PROFILER_MODEL, 'audit': DEEP_AUDIT_MODEL, 'status_review': DEEP_STATUS_REVIEW_MODEL,
                'supervisor': DEEP_SUPERVISOR_MODEL}
# How many times the supervisor may send disputed criteria back to the audit agent, and how many criteria a time
MAX_ROUNDS = 2
MAX_RECHECKS = 6
# Bumped when the saved report changes shape: 2 gives each agent's model ('models'), not one 'model'; 3 takes the
# criteria from the tender's Pinecone namespace ('inputs': 'criteria_source' names it, 'excerpts' counts them)
REPORT_VERSION = 3
# What the agents are given from the tender's Pinecone namespace, beside the criteria of the result: the excerpts found
# for each kind of requirement, and the excerpts nearest each criterion (Supporting Evidence; see load_knowledge())
KNOWLEDGE_QUERIES = {
    'Eligibility Criteria': [
        'Eligibility criteria and pre-qualification requirements a bidder must meet',
        'Who may bid: registration, constitution, OEM or authorised dealer, blacklisting and debarment',
    ],
    'Financial Requirements': [
        'Minimum average annual turnover, net worth, solvency, working capital and bid capacity formula',
        'Estimated cost or tender value, earnest money deposit and performance security',
    ],
    'Technical Requirements': [
        'Technical eligibility: certifications, licences, plant, equipment, key personnel and specifications',
    ],
    'Experience Requirements': [
        'Similar work experience: completed works of a stated value in the last years, and the definition of '
        'similar work',
    ],
    'JV Requirements': [
        'Joint venture or consortium: whether allowed, maximum members, lead member share, and what each member and '
        'the joint venture as a whole must meet',
    ],
}
EVIDENCE_LABEL = 'Supporting Evidence'

STATUSES = ('met', 'not_met', 'unknown')
AUDIT_RESULTS = ('pass', 'fail', 'insufficient_data', 'not_applicable')
# An audit result as a criterion status; not_applicable criteria do not count towards the determination
AUDIT_STATUS = {'pass': 'met', 'fail': 'not_met', 'insufficient_data': 'unknown', 'not_applicable': 'not_applicable'}
FINAL_STATUSES = STATUSES + ('not_applicable',)
FLAG_ISSUES = ('missing', 'outdated', 'mismatch', 'expired', 'ambiguous')
LEVELS = ('high', 'medium', 'low')
BIAS_RISKS = ('high', 'medium', 'low', 'none')


class DeepAnalysisError(RuntimeError):
    """The deep analysis cannot go on: nothing to review, or an agent's answer could not be read."""


class DeepAnalysisRunning(Exception):
    """A deep analysis of the tender is already queued or running."""


# ---------- the Ingestion & Context agent: plain Python, no LLM ----------

_AMOUNT = re.compile(
    r'(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?|million|mn|billion|bn)?\s*(?:/-|only)?',
    re.IGNORECASE)
_CURRENCY = re.compile(r'(₹|rs\.?|inr)', re.IGNORECASE)
_UNITS = {'crore': 10 ** 7, 'crores': 10 ** 7, 'cr': 10 ** 7, 'lakh': 10 ** 5, 'lakhs': 10 ** 5, 'lac': 10 ** 5,
          'lacs': 10 ** 5, 'million': 10 ** 6, 'mn': 10 ** 6, 'billion': 10 ** 9, 'bn': 10 ** 9}
# A bare number is money when its field or column is called one of these
_MONEY_NAMES = re.compile(r'turnover|value|worth|amount|balance|capital|liquidity|commitment|limit|cost|'
                          r'drawing\s+power|revenue|profit|emd|solvency|fee|price', re.IGNORECASE)
# Column names too general to make a bare number money: "Item | Value" tables hold NIC codes and PANs as well
_GENERIC_COLUMNS = {'value', 'values', 'detail', 'details'}
_DATE_FORMATS = ('%d-%b-%Y', '%d-%B-%Y', '%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y', '%Y-%m-%d', '%d %b %Y', '%d %B %Y',
                 '%b %d, %Y', '%B %d, %Y', '%d-%b-%y')
# A profile entry whose date is a validity or expiry date, so a past date means it has lapsed
_VALIDITY_NAMES = re.compile(r'valid|expir|renew', re.IGNORECASE)
# Days before a validity date runs out that the entry is flagged as expiring soon
EXPIRING_DAYS = 60


def parse_amount(text, money_name=False):
    """
    An amount in a profile cell as rupees (an int), or None. "₹ 12.5 Cr", "12.5 crore", "3 lakh" and
    "Rs. 1,25,00,000" are money by their unit or symbol; a bare number such as 94500000000 only when money_name is
    true (its field or column is called turnover, net worth and the like). Any other word in the cell (years,
    metres) means it is not an amount.
    """
    cell = ' '.join(str(text or '').split())
    match = _AMOUNT.fullmatch(cell)
    if not match:
        return None
    unit = (match.group(2) or '').lower().rstrip('.')
    if not unit and not _CURRENCY.match(cell) and not money_name:
        return None
    return round(float(match.group(1).replace(',', '')) * _UNITS.get(unit, 1))


def parse_date(text):
    """A date written in any of the usual ways (15-Jun-2024, 31/03/2025, 2025-03-31, 31 March 2025), or None."""
    cell = ' '.join(str(text or '').split())
    if not cell or len(cell) > 20:
        return None
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(cell, pattern).date()
        except ValueError:
            continue
    return None


def parse_financial_year(text):
    """
    An Indian financial year (April to March) named as FY24, FY 2023-24, 2023-24 or F.Y. 2023-2024, as
    {'label': 'FY 2023-24', 'start': date(2023, 4, 1), 'end': date(2024, 3, 31)}; None for anything else.
    FY24 is the year ending in March 2024.
    """
    cell = ' '.join(str(text or '').split()).upper().replace('F.Y.', 'FY').replace('–', '-')
    span = re.fullmatch(r"(?:FY\s*)?(\d{4})\s*[-/]\s*(\d{2}|\d{4})", cell)
    single = re.fullmatch(r"FY\s*'?(\d{2}|\d{4})", cell)
    if span:
        start = int(span.group(1))
        end = span.group(2)
        if int(end) != ((start + 1) % 100 if len(end) == 2 else start + 1):
            return None
    elif single:
        end = int(single.group(1))
        start = (2000 + end if end < 100 else end) - 1
    else:
        return None
    return financial_year(date(start, 4, 1))


def financial_year(day):
    """The Indian financial year day falls in, as parse_financial_year() returns it."""
    start = day.year if day.month >= 4 else day.year - 1
    return {'label': f'FY {start}-{(start + 1) % 100:02d}', 'start': date(start, 4, 1), 'end': date(start + 1, 3, 31)}


def crore(rupees):
    """Rupees as the bid team reads them: ₹9,450.00 Cr."""
    return f'₹{rupees / 10 ** 7:,.2f} Cr'


def profile_facts(markdown):
    """
    Every cell of the company profile's tables as a fact: {'section', 'field', 'column', 'value'}, where field is
    the row's first cell (Company Name, FY24, ROB Package-1), column the cell's column and section the headings
    above it, without the profile's title and the section numbers.
    """
    facts = []
    headings = []  # (level, text) of the headings above
    for block in parse_profile(markdown):
        if block['type'] == 'heading':
            text = re.sub(r'^\s*\d+(\.\d+)*\.?\s+', '', block['text']).strip()
            headings = [heading for heading in headings if heading[0] < block['level']] + [(block['level'], text)]
        elif block['type'] == 'table':
            section = ' > '.join(text for level, text in headings if level > 1)
            for row in block['rows']:
                field = (row[0] if row else '').strip()
                for column, value in zip(block['columns'][1:], row[1:]):
                    if value.strip():
                        facts.append({'section': section, 'field': field, 'column': column.strip(),
                                      'value': value.strip()})
    return facts


def normalise_fact(fact):
    """
    The fact with what the ingestion agent can read from it: 'rupees' for an amount, 'date' (ISO) for a date and
    'financial_year' for a figure whose row is a financial year (FY24). Absent when the fact holds none.
    """
    fact = dict(fact)
    column_is_money = bool(_MONEY_NAMES.search(fact['column'])) and fact['column'].lower() not in _GENERIC_COLUMNS
    rupees = parse_amount(fact['value'], money_name=column_is_money or bool(_MONEY_NAMES.search(fact['field'])))
    if rupees is not None:
        fact['rupees'] = rupees
    day = parse_date(fact['value'])
    if day:
        fact['date'] = day.isoformat()
    year = parse_financial_year(fact['field'])
    if year:
        fact['financial_year'] = {'label': year['label'], 'start': year['start'].isoformat(),
                                  'end': year['end'].isoformat()}
    return fact


def date_checks(facts, today):
    """
    The profile entries the ingestion agent flags by itself, in the profiler's flag shape: validity dates that have
    passed (expired) or pass within EXPIRING_DAYS, and year-wise tables whose latest year is older than the last
    financial year to have ended (outdated).
    """
    flags = []
    for fact in facts:
        if 'date' not in fact or not _VALIDITY_NAMES.search(f"{fact['field']} {fact['column']}"):
            continue
        day = date.fromisoformat(fact['date'])
        name = _fact_name(fact)
        if day < today:
            flags.append(_flag(name, 'expired', f"{fact['value']} has passed (today is {today:%d %b %Y}).", 'high'))
        elif day <= today + timedelta(days=EXPIRING_DAYS):
            flags.append(_flag(name, 'expired', f"{fact['value']} is within {EXPIRING_DAYS} days: check it is still "
                                                'valid on the bid due date.', 'low'))
    last_ended = financial_year(financial_year(today)['start'] - timedelta(days=1))
    latest = {}  # section and column -> the latest financial year with a figure
    for fact in facts:
        if 'financial_year' in fact:
            key = (fact['section'], fact['column'])
            if key not in latest or fact['financial_year']['end'] > latest[key]['end']:
                latest[key] = fact['financial_year']
    for (section, column), year in latest.items():
        if date.fromisoformat(year['end']) < last_ended['end']:
            flags.append(_flag(f'{section} > {column}' if section else column, 'outdated',
                               f"The latest year given is {year['label']}, but {last_ended['label']} ended on "
                               f"{last_ended['end']:%d %b %Y}. Criteria on the last financial years may need it.",
                               'medium'))
    return flags


def load_knowledge(index, tcno, result):
    """
    What the agents read of the tender's documents, retrieved from its Pinecone namespace (index, a PineconeService):
    the excerpts for each kind of requirement in KNOWLEDGE_QUERIES, and for each criterion of the result the excerpts
    nearest its name and requirement (Supporting Evidence). Each excerpt is given once, numbered [E1], [E2], ... in
    document order, followed by which excerpts answer each kind and each criterion.

    Returns {'text', 'source', 'excerpts', 'warning'}; never raises: when the index is missing or Pinecone fails,
    text is empty and warning says why, so the audit works from the result's own requirements.
    """
    name = index.namespace(tcno)
    queries = dict(KNOWLEDGE_QUERIES)
    evidence = {}  # query label -> (criterion number, name)
    for ref, row in enumerate(result.get('criteria') or []):
        requirement = _text(row.get('requirement'))
        criterion = _text(row.get('criterion'))
        if criterion or requirement:
            label = f'{EVIDENCE_LABEL} [{ref}]'
            queries[label] = [f'{criterion}: {requirement}'.strip(': ')]
            evidence[label] = (ref, criterion or requirement)
    try:
        index.require()
        # Within the index's RETRIEVAL_MAX_CHARS, like the eligibility check, so one setting sizes every prompt
        retrieval = index.retrieve(tcno, queries)
    except NamespaceIncomplete:
        return {'text': '', 'source': None, 'excerpts': 0,
                'warning': f'The Pinecone namespace {name} is missing or incomplete'}
    except VectorStoreError as exc:
        log.warning('Deep analysis of %s: could not retrieve the criteria from %s', tcno, name, exc_info=True)
        return {'text': '', 'source': None, 'excerpts': 0,
                'warning': f'The Pinecone namespace {name} could not be searched ({exc})'}
    if not retrieval.chunks:
        return {'text': '', 'source': None, 'excerpts': 0, 'warning': f'The Pinecone namespace {name} holds no text'}

    labels = {}  # query label -> excerpt numbers
    blocks = []
    for number, chunk in enumerate(retrieval.chunks, 1):
        blocks.append(f"[E{number}] {chunk['source_file']} (part {chunk['chunk_index'] + 1})\n{chunk['text'].strip()}")
        for label in chunk['queries']:
            labels.setdefault(label, []).append(f'E{number}')
    guide = []
    for label in KNOWLEDGE_QUERIES:
        guide.append(f"- {label}: {', '.join(labels.get(label, [])) or 'nothing found'}")
    if evidence:
        guide.append(f'- {EVIDENCE_LABEL} (by criterion number):\n' + '\n'.join(
            f"  - [{ref}] {criterion}: {', '.join(labels.get(label, [])) or 'nothing found'}"
            for label, (ref, criterion) in evidence.items()))
    pinned = labels.get('pinned')
    if pinned:
        guide.append(f"- TenderTiger's own data and pre-qualification criteria, given in full: {', '.join(pinned)}")
    unread = retrieval.unreadable
    text = ('### Excerpts\n' + '\n\n'.join(blocks) + '\n\n### Where each kind of requirement is found\n'
            + '\n'.join(guide))
    if unread:
        text += '\n\n### Sources that could not be read, so are not in the index\n' + '\n'.join(f'- {s}' for s in unread)
    return {'text': text, 'source': f'Pinecone namespace {retrieval.namespace}', 'excerpts': len(retrieval.chunks),
            'warning': None}


def ingest(result, knowledge, company, decision=None, today=None):
    """
    The Ingestion & Context agent: checks the shape of the inputs and lays them out in the shared state for the
    other agents. result is the tender's completed result, knowledge what load_knowledge() retrieved from its
    Pinecone namespace (or None), company the profile markdown and decision the team's shortlisted / dropped decision,
    if any.

    Returns the context: {'tcno', 'name', 'today', 'financial_year', 'last_financial_year', 'criteria',
    'criteria_text', 'criteria_source', 'unreadable', 'unreadable_checked', 'verdict', 'ai_verdict', 'jv',
    'decision', 'company', 'facts', 'prechecks', 'warnings'}. Each criterion is {'ref' (its index in the result),
    'criterion', 'requirement', 'company', 'source', 'status' (now), 'ai_status', 'marked_by_user'}.
    Raises DeepAnalysisError when there are no criteria to review.
    """
    today = today or date.today()
    warnings = []
    criteria = []
    for index, row in enumerate(result.get('criteria') or []):
        name = _text(row.get('criterion')) or f'Criterion {index + 1}'
        status = row.get('status')
        if status not in STATUSES:
            warnings.append(f'{name}: status {status!r} is not met / not_met / unknown, so it is read as unknown.')
            status = 'unknown'
        marked = bool(row.get('marked_by_user'))
        ai_status = row.get('ai_status') if marked and row.get('ai_status') in STATUSES else status
        requirement = _text(row.get('requirement'))
        if not requirement:
            warnings.append(f'{name}: the result recorded no requirement.')
        criteria.append({'ref': index, 'criterion': name, 'requirement': requirement,
                         'company': _text(row.get('company')), 'source': _text(row.get('source')),
                         'status': status, 'ai_status': ai_status, 'marked_by_user': marked})
    if not criteria:
        warnings.append("The result has no criteria table, so the audit works from the tender's Pinecone namespace "
                        'alone.')

    knowledge = knowledge or {}
    criteria_text = knowledge.get('text') or ''
    criteria_source = knowledge.get('source') or ''
    excerpts = knowledge.get('excerpts') or 0
    if not criteria_text.strip():
        criteria_text = '\n'.join(f"- {row['criterion']}: {row['requirement'] or 'requirement not recorded'} "
                                  f"(source: {row['source'] or 'not given'})" for row in criteria)
        criteria_source = 'result'
        reason = knowledge.get('warning') or "The tender's Pinecone namespace returned no criteria"
        warnings.append(f'{reason}, so the audit works from the requirements the result recorded.')
    if not criteria_text.strip():
        raise DeepAnalysisError("There are no criteria to review: the result has none and the tender's Pinecone "
                                'namespace returned none.')

    facts = [normalise_fact(fact) for fact in profile_facts(company)]
    if not facts:
        warnings.append('The company profile has no tables of figures to normalise.')
    unreadable = list(result.get('unreadable_files') or [])
    this_year = financial_year(today)
    return {
        'tcno': str(result['tcno']), 'name': result.get('name') or '', 'today': today.isoformat(),
        'financial_year': this_year['label'],
        'last_financial_year': financial_year(this_year['start'] - timedelta(days=1))['label'],
        'criteria': criteria, 'criteria_text': criteria_text, 'criteria_source': criteria_source,
        'excerpts': excerpts, 'unreadable': unreadable,
        'unreadable_checked': [source for source in result.get('unreadable_checked') or [] if source in unreadable],
        'verdict': result.get('verdict'), 'ai_verdict': result.get('ai_verdict') or result.get('verdict'),
        'jv': result.get('jv') or {}, 'decision': decision, 'company': company, 'facts': facts,
        'prechecks': date_checks(facts, today), 'warnings': warnings,
    }


# ---------- the LLM agents ----------

_PREAMBLE = """You are the {title} agent, one of five agents reviewing an eligibility check of an Indian public-sector tender for one company. Your role: {role}

Work only from the inputs given. Never invent a fact, figure, registry record or document. Amounts are in Indian rupees: 1 crore = 1,00,00,000 = 10,000,000 and 1 lakh = 1,00,000; bring both sides to the same unit before comparing. Today is {today}; the current financial year is {financial_year} (April to March) and the last one to have ended is {last_financial_year}. Keep every text field to one or two sentences.

Reply with one JSON object only: no markdown fences and no text before or after it. Use exactly this shape:
{schema}"""

_PROFILER_ROLE = (
    'data verification. Check the company profile for fields that are missing, outdated, internally inconsistent '
    '(such as a figure that disagrees with another figure in the profile) or expired, above all the fields the '
    "tender's criteria need. No external registry or database is connected: verify against the profile itself, the "
    'normalised figures and today\'s date, and in verify_with name the external source that would settle a flag '
    '(such as the MCA, GST or Udyam portal), never claiming you checked it. Do not judge eligibility.'
)
_PROFILER_SCHEMA = """{
  "flags": [
    {"field": "<profile field>", "issue": "missing | outdated | mismatch | expired | ambiguous",
     "detail": "<what is wrong, quoting the profile's figures>", "needed_for": "<criterion it affects, or empty>",
     "severity": "high | medium | low", "verify_with": "<external source that would settle it, or empty>"}
  ],
  "reliable_fields": ["<fields that are complete and consistent>"],
  "summary": "<the profile's fitness for this tender>"
}"""

_AUDIT_ROLE = (
    "criteria assessment. Match the company data strictly against each eligibility criterion of the tender, rule by "
    'rule, and give each one Pass, Fail or Insufficient data. You are not shown any earlier statuses: judge every '
    'rule afresh.'
)
_AUDIT_RULES = """Rules:
- One rule per distinct eligibility criterion. Where it is one of the known criteria listed, give that number as ref and keep its name; otherwise ref is null.
- If a required value depends on another figure (such as the estimated cost), work it out in calculation, with units.
- pass: the company data states what the rule needs, and it satisfies it.
- fail: the company data states it, and it falls short.
- insufficient_data: the company data does not state it, the documents do not give the required value, or the figure it rests on is flagged as unreliable by the Company Profiler agent (say which). Say in missing what would decide it.
- not_applicable: not a condition for bidding (purchase preferences, price matching, payment or delivery terms).
- An exemption or relaxation (for MSEs, startups and the like) is not a rule of its own: apply it to the rule it relaxes, and only when the company data states that the company qualifies for it.
- Do not infer a fact the company data does not state, such as the country from an address or the MSE category from a Udyam number. For similar work, judge each project by what it was against this tender's definition of similar work, ignoring any "similar" label in the profile."""
_AUDIT_SCHEMA = """{
  "rules": [
    {"ref": <number of the known criterion, or null>, "criterion": "<name>", "requirement": "<as written>",
     "source": "<source file>", "company_evidence": "<the company figures used, quoted>",
     "calculation": "<the working, or empty>", "result": "pass | fail | insufficient_data | not_applicable",
     "reason": "<why>", "missing": "<what would decide it, or empty>"}
  ],
  "summary": "<what decides eligibility>"
}"""

_REVIEW_ROLE = (
    'user action bias correction. Compare the statuses the bid team marked by hand, and its other manual overrides, '
    'with the Company Profiler and Eligibility Audit agents, and hunt for confirmation bias: a status marked to fit '
    'the hoped-for outcome rather than the policy text. For every disagreement, say why a person might have marked '
    'it differently (knowledge or documents outside the profile, a relationship with the client, reading a '
    "relaxation into the text, a different reading of the requirement) and whether that reason stands on the tender's "
    'own terms. Review every criterion marked by hand, and every other criterion where the AI status and the audit '
    'disagree.'
)
_REVIEW_SCHEMA = """{
  "reviews": [
    {"ref": <criterion number>, "criterion": "<name>", "agreement": "agrees | disagrees | partly",
     "bias_risk": "high | medium | low | none", "finding": "<what the evidence shows against the mark>",
     "possible_reasons": ["<why a person might have marked it so>"], "recommendation": "keep | revert | verify"}
  ],
  "overrides": [
    {"kind": "unreadable_checked | decision | verdict", "detail": "<the override>",
     "bias_risk": "high | medium | low | none", "finding": "<whether the evidence supports it>"}
  ],
  "summary": "<the overall risk that the marks bend the result>"
}"""

_JUDGE_ROLE = (
    "final consensus and synthesis. Review every agent's output, resolve their disagreements and give the final "
    'status of every criterion with your confidence in it, the determination, and the data gaps that keep it from '
    'being certain.'
)
_JUDGE_RULES = """Rules:
- met only when the evidence supports it; not_met only when the company data states the figure and it falls short; unknown when the evidence is insufficient, rests on data flagged as unreliable, or a disagreement stays unresolved; not_applicable for a term that is not a condition for bidding. A wrong not_met loses a tender the company could win, so when unsure say unknown.
- A status marked by hand stands only when the evidence supports it, or when the reason the Discrepancy & Status Review agent gave for it stands on the tender's own terms. Otherwise give the evidence-based status, and say so in resolution.
- Give every known criterion, by ref, and every criterion only the audit found, with ref null.
- confidence runs from 0 (a guess) to 1 (certain on the evidence given).
- The determination is Not Eligible when any final status is not_met, Eligible when every one is met (not_applicable aside) and no unread source is left, otherwise Requires Review.
- recheck: the criteria whose disagreement the Eligibility Audit agent should re-examine before you decide, each with the question it must settle; at most {max_rechecks}. {rounds}"""
_JUDGE_SCHEMA = """{
  "criteria": [
    {"ref": <number of the known criterion, or null>, "criterion": "<name>",
     "final_status": "met | not_met | unknown | not_applicable", "confidence": <0 to 1>,
     "basis": "<the evidence the status rests on, naming the agents>", "resolution": "<how a disagreement was settled, or empty>"}
  ],
  "determination": "Eligible | Not Eligible | Requires Review", "confidence": <0 to 1>,
  "summary": "<the determination and what decides it>", "deciding_criteria": ["<criterion name>"],
  "data_gaps": [
    {"gap": "<missing or unreliable data>", "needed_for": "<criterion>", "how_to_close": "<the document or figure to get>",
     "severity": "high | medium | low"}
  ],
  "recheck": [{"ref": <number or null>, "criterion": "<name>", "question": "<what to settle>"}]
}"""


def profile_company(llm, context):
    """The Company Profiler agent: {'flags', 'reliable_fields', 'summary'}; the ingestion agent's date checks lead."""
    user = '\n\n'.join([
        f"## Company profile\n{context['company']}",
        f'## Normalised figures (Ingestion & Context agent)\n{_facts_text(context)}',
        f"## Date checks (Ingestion & Context agent)\n{_flags_text(context['prechecks']) or 'Nothing expired or outdated.'}",
        f"## What the tender's criteria ask for\n{_requirements_text(context)}",
    ])
    raw = _ask(llm, 'profiler', _system('profiler', _PROFILER_ROLE, _PROFILER_SCHEMA, context), user)
    flags = [_flag(_text(item.get('field')) or 'Company profile', _choice(item.get('issue'), FLAG_ISSUES, 'ambiguous'),
                   _text(item.get('detail')), _choice(item.get('severity'), LEVELS, 'medium'),
                   needed_for=_text(item.get('needed_for')), verify_with=_text(item.get('verify_with')),
                   source='profiler')
             for item in _items(raw.get('flags'))]
    return {'flags': context['prechecks'] + flags,
            'reliable_fields': [_text(field) for field in _list(raw.get('reliable_fields')) if _text(field)],
            'summary': _text(raw.get('summary'))}


def audit_criteria(llm, context, profile, focus=None, round_number=0, earlier=None):
    """
    The Eligibility Audit agent: {'rules', 'summary'}, each rule {'ref', 'criterion', 'requirement', 'source',
    'company_evidence', 'calculation', 'result', 'reason', 'missing', 'round'}. With focus, a list of the
    supervisor's recheck requests, it re-examines only those criteria; earlier is then its last audit, whose
    rows it is shown beside each request with the other agents' views.
    """
    parts = [
        f"## The tender's eligibility criteria (from {context['criteria_source']})\n{context['criteria_text']}",
        f'## Known criteria (use their numbers as ref)\n{_known_text(context)}',
        f"## Company profile\n{context['company']}",
        f'## Normalised figures (Ingestion & Context agent)\n{_facts_text(context)}',
        f"## Company data flagged by the Company Profiler agent\n{_flags_text(profile['flags']) or 'None.'}",
    ]
    if focus:
        parts.append(f'## Re-examine only these criteria (Supervisor / Judge agent, round {round_number})\n'
                     + '\n'.join(_recheck_text(request, context, earlier) for request in focus)
                     + '\nReturn only these criteria in rules, each with its ref.')
    raw = _ask(llm, 'audit', _system('audit', f'{_AUDIT_ROLE}\n\n{_AUDIT_RULES}', _AUDIT_SCHEMA, context),
               '\n\n'.join(parts))
    rules = []
    for item in _items(raw.get('rules')):
        rules.append({
            'ref': _ref(item.get('ref'), context), 'criterion': _text(item.get('criterion')) or 'Unnamed criterion',
            'requirement': _text(item.get('requirement')), 'source': _text(item.get('source')),
            'company_evidence': _text(item.get('company_evidence')), 'calculation': _text(item.get('calculation')),
            'result': _choice(item.get('result'), AUDIT_RESULTS, 'insufficient_data'),
            'reason': _text(item.get('reason')), 'missing': _text(item.get('missing')), 'round': round_number,
        })
    return {'rules': rules, 'summary': _text(raw.get('summary'))}


def review_statuses(llm, context, profile, audit):
    """
    The Discrepancy & Status Review agent: {'reviews', 'overrides', 'summary', 'skipped'}. With nothing marked or
    decided by hand, and the audit agreeing with every AI status, there is nothing to review: it makes no LLM call
    and says so (skipped is true).
    """
    matched, extra = match_rules(context['criteria'], audit['rules'])
    disputed = [criterion for criterion in context['criteria'] if criterion['marked_by_user'] or (
        criterion['ref'] in matched and AUDIT_STATUS[matched[criterion['ref']]['result']] != criterion['ai_status'])]
    if not disputed and not context['unreadable_checked'] and not context['decision']:
        return {'reviews': [], 'overrides': [], 'skipped': True,
                'summary': 'Nothing was marked or decided by hand, and the audit agrees with every AI status, so '
                           'there was nothing to review.'}
    lines = []
    for criterion in context['criteria']:
        rule = matched.get(criterion['ref'])
        lines.append(f"[{criterion['ref']}] {criterion['criterion']}\n"
                     f"    Requirement: {criterion['requirement'] or 'not recorded'}\n"
                     f"    Company figure the first check used: {criterion['company'] or 'none'}\n"
                     f'    Status now: {_status_line(criterion)}\n'
                     f'    Audit: {_rule_line(rule)}')
    extra_lines = '\n'.join(f"- {rule['criterion']}: {_rule_line(rule)}" for rule in extra)
    user = '\n\n'.join(filter(None, [
        f"## Criteria: the result's statuses, the marks by hand and the audit\n{chr(10).join(lines) or 'None.'}",
        extra_lines and f'## Criteria only the audit found\n{extra_lines}',
        f'## Other manual overrides and the verdict\n{_overrides_text(context)}',
        f"## Company data flagged by the Company Profiler agent\n{_flags_text(profile['flags']) or 'None.'}",
    ]))
    raw = _ask(llm, 'status_review', _system('status_review', _REVIEW_ROLE, _REVIEW_SCHEMA, context), user)
    by_ref = {criterion['ref']: criterion for criterion in context['criteria']}
    reviews = []
    for item in _items(raw.get('reviews')):
        ref = _ref(item.get('ref'), context)
        criterion = by_ref.get(ref) or _named(context['criteria'], item.get('criterion'))
        if criterion is None:
            continue
        rule = matched.get(criterion['ref'])
        reviews.append({
            'ref': criterion['ref'], 'criterion': criterion['criterion'],
            'marked_by_user': criterion['marked_by_user'],
            'user_status': criterion['status'] if criterion['marked_by_user'] else None,
            'ai_status': criterion['ai_status'], 'audit_result': rule['result'] if rule else None,
            'agreement': _choice(item.get('agreement'), ('agrees', 'disagrees', 'partly'), 'partly'),
            'bias_risk': _choice(item.get('bias_risk'), BIAS_RISKS, 'low'),
            'finding': _text(item.get('finding')),
            'possible_reasons': [_text(reason) for reason in _list(item.get('possible_reasons')) if _text(reason)],
            'recommendation': _choice(item.get('recommendation'), ('keep', 'revert', 'verify'), 'verify'),
        })
    overrides = [{'kind': _choice(item.get('kind'), ('unreadable_checked', 'decision', 'verdict'), 'verdict'),
                  'detail': _text(item.get('detail')), 'bias_risk': _choice(item.get('bias_risk'), BIAS_RISKS, 'low'),
                  'finding': _text(item.get('finding'))}
                 for item in _items(raw.get('overrides'))]
    return {'reviews': reviews, 'overrides': overrides, 'summary': _text(raw.get('summary')), 'skipped': False}


def judge(llm, context, profile, audit, review, conflicts, round_number, rounds_left):
    """
    The Supervisor / Judge agent: {'criteria', 'determination', 'confidence', 'summary', 'deciding_criteria',
    'data_gaps', 'recheck'}. recheck, the criteria to send back to the audit agent, is empty once no round is left.
    """
    matched, extra = match_rules(context['criteria'], audit['rules'])
    reviews = {item['ref']: item for item in review['reviews']}
    lines = []
    for criterion in context['criteria']:
        item = reviews.get(criterion['ref'])
        lines.append(f"[{criterion['ref']}] {criterion['criterion']}: {criterion['requirement'] or 'requirement not recorded'}\n"
                     f'    Result: {_status_line(criterion)}\n'
                     f"    Audit: {_rule_line(matched.get(criterion['ref']))}\n"
                     + (f"    Status review: {item['agreement']}, bias risk {item['bias_risk']}. {item['finding']}"
                        if item else '    Status review: not reviewed'))
    lines += [f"[audit only] {rule['criterion']}: {rule['requirement']}\n    Audit: {_rule_line(rule)}"
              for rule in extra]
    rounds = (f'The Eligibility Audit agent has re-examined disputed criteria {_plural(round_number, "time")}. '
              if round_number else '')
    rounds += (f'Re-examination rounds left: {rounds_left}.' if rounds_left else
               'No re-examination rounds are left: leave recheck empty and decide.')
    user = '\n\n'.join([
        f"## Disagreements between the agents\n{_conflicts_text(conflicts) or 'None: every view agrees.'}",
        f"## Every criterion, with each agent's view\n{chr(10).join(lines)}",
        f"## Company Profiler agent\n{profile['summary'] or 'No summary.'}\n{_flags_text(profile['flags'])}",
        f"## Eligibility Audit agent\n{audit['summary'] or 'No summary.'}",
        f"## Discrepancy & Status Review agent\n{review['summary'] or 'No summary.'}\n"
        + '\n'.join(f"- {item['kind']}: {item['detail']} (bias risk {item['bias_risk']}) {item['finding']}"
                    for item in review['overrides']),
        f'## Unread sources and manual overrides\n{_overrides_text(context)}',
        f'## Rounds\n{rounds}',
    ])
    rules = _JUDGE_RULES.format(max_rechecks=MAX_RECHECKS, rounds=rounds)
    raw = _ask(llm, 'supervisor', _system('supervisor', f'{_JUDGE_ROLE}\n\n{rules}', _JUDGE_SCHEMA, context), user)
    criteria = [{
        'ref': _ref(item.get('ref'), context), 'criterion': _text(item.get('criterion')) or 'Unnamed criterion',
        'final_status': _choice(item.get('final_status'), FINAL_STATUSES, 'unknown'),
        'confidence': _confidence(item.get('confidence')), 'basis': _text(item.get('basis')),
        'resolution': _text(item.get('resolution')),
    } for item in _items(raw.get('criteria'))]
    recheck = [{'ref': _ref(item.get('ref'), context), 'criterion': _text(item.get('criterion')),
                'question': _text(item.get('question'))}
               for item in _items(raw.get('recheck')) if _text(item.get('criterion')) or _ref(item.get('ref'), context) is not None]
    return {
        'criteria': criteria,
        'determination': _determination(raw.get('determination')),
        'confidence': _confidence(raw.get('confidence')), 'summary': _text(raw.get('summary')),
        'deciding_criteria': [_text(name) for name in _list(raw.get('deciding_criteria')) if _text(name)],
        'data_gaps': [{'gap': _text(item.get('gap')), 'needed_for': _text(item.get('needed_for')),
                       'how_to_close': _text(item.get('how_to_close')),
                       'severity': _choice(item.get('severity'), LEVELS, 'medium')}
                      for item in _items(raw.get('data_gaps')) if _text(item.get('gap'))],
        'recheck': recheck[:MAX_RECHECKS] if rounds_left else [],
    }


# ---------- orchestration ----------

def run_deep_analysis(result, knowledge, company, decision=None, on_progress=None, usage=None, today=None):
    """
    Runs the five agents over a tender's completed result and returns the report saved as DEEP_ANALYSIS_FILE (see
    compile_report()). knowledge is what load_knowledge() retrieved from the tender's Pinecone namespace (None when it
    has none); company is the profile markdown; decision the team's shortlisted / dropped decision, if any.

    on_progress, if given, is called with (agent, stage, round) as each agent starts, round being 0 for the first
    pass and the re-examination round after it. usage, a TokenUsage over LLM_AGENTS, counts each agent's tokens.
    Raises RuntimeError without GROQ_API_KEY or with an empty profile, DeepAnalysisError when there is nothing to
    review or an agent's answer cannot be read; LLM errors propagate.
    """
    notify = on_progress or (lambda agent, stage, round_number=0: None)
    usage = usage or TokenUsage(LLM_AGENTS)
    if not os.getenv('GROQ_API_KEY'):
        raise RuntimeError('GROQ_API_KEY is not set. Add it to .env and restart the app.')
    if not (company or '').strip():
        raise RuntimeError('The company profile is empty.')
    started = time.monotonic()
    audit_log = []

    def step(agent, stage, run, round_number=0):
        """Runs one agent, with its progress and its audit log line, and returns what it wrote to the state."""
        notify(agent, stage, round_number)
        begun = time.monotonic()
        output, event = run()
        # The model the agent ran on; none for the ingestion agent, or a status review with nothing to review
        name = AGENT_MODELS.get(agent) if not output.get('skipped') else None
        audit_log.append({'at': _now(), 'agent': agent, 'round': round_number, 'model': name, 'event': event,
                          'seconds': round(time.monotonic() - begun, 1)})
        log.info('Deep analysis of %s: %s agent%s: %s', result['tcno'], AGENT_TITLES[agent],
                 f' ({name})' if name else '', event)
        return output

    def model(agent):
        return usage.track(make_model(AGENT_MODELS[agent]), agent)

    state = {}  # the blackboard every agent reads and writes
    state['context'] = step('ingestion', 'reading the result, the criteria and the company profile',
                            lambda: _logged(ingest(result, knowledge, company, decision, today), _ingestion_event))
    context = state['context']
    state['profile'] = step('profiler', 'checking the company data the criteria need',
                            lambda: _logged(profile_company(model('profiler'), context), _profiler_event))
    state['audit'] = step('audit', 'checking every criterion afresh against the company data',
                          lambda: _logged(audit_criteria(model('audit'), context, state['profile']), _audit_event))
    state['status_review'] = step(
        'status_review', 'comparing the statuses marked by hand with the audit',
        lambda: _logged(review_statuses(model('status_review'), context, state['profile'], state['audit']),
                        _review_event))

    rounds = 0
    while True:
        conflicts = find_conflicts(context, state['audit']['rules'])
        rounds_left = MAX_ROUNDS - rounds
        state['judgement'] = step(
            'supervisor', 'resolving the disagreements' if conflicts else 'compiling the final determination',
            lambda: _logged(judge(model('supervisor'), context, state['profile'], state['audit'],
                                  state['status_review'], conflicts, rounds, rounds_left),
                            lambda out: _judge_event(out, conflicts)),
            rounds)
        recheck = state['judgement']['recheck']
        if not recheck:
            break
        rounds += 1
        earlier = state['audit']
        reexamined = step(
            'audit', f're-examining {_plural(len(recheck), "disputed criterion", "disputed criteria")} '
                     f'for the supervisor (round {rounds})',
            lambda: _logged(audit_criteria(model('audit'), context, state['profile'], focus=recheck,
                                           round_number=rounds, earlier=earlier), _audit_event),
            rounds)
        state['audit'] = {'rules': merge_rules(earlier['rules'], reexamined['rules']),
                          'summary': reexamined['summary'] or earlier['summary']}

    payload = compile_report(state, rounds, audit_log, usage)
    payload['duration_seconds'] = round(time.monotonic() - started, 1)
    return payload


def match_rules(criteria, rules):
    """
    Pairs the audit's rules with the result's criteria: by ref, then by name. Returns ({ref: rule}, the rules that
    match no criterion, in order).
    """
    matched, extra = {}, []
    by_ref = {criterion['ref'] for criterion in criteria}
    for rule in rules:
        ref = rule['ref'] if rule['ref'] in by_ref and rule['ref'] not in matched else None
        if ref is None:
            named = _named([criterion for criterion in criteria if criterion['ref'] not in matched], rule['criterion'])
            ref = named['ref'] if named else None
        if ref is None:
            extra.append(rule)
        else:
            matched[ref] = rule
    return matched, extra


def merge_rules(rules, reexamined):
    """The audit's rules with the re-examined ones in place of the rows they re-examine, by ref or by name."""
    merged = list(rules)
    for rule in reexamined:
        index = next((i for i, old in enumerate(merged)
                      if (rule['ref'] is not None and old['ref'] == rule['ref'])
                      or (rule['ref'] is None and criterion_key(old['criterion']) == criterion_key(rule['criterion']))),
                     None)
        if index is None:
            merged.append(rule)
        else:
            merged[index] = rule
    return merged


def find_conflicts(context, rules):
    """
    Where the views of a criterion disagree: the AI's status, the status marked by hand and the audit's result.
    Returns [{'ref', 'criterion', 'kind', 'views'}], kind being 'disagreement', 'not_audited' (the audit left the
    criterion out) or 'audit_only' (the audit found a criterion the result does not have).
    """
    matched, extra = match_rules(context['criteria'], rules)
    conflicts = []
    for criterion in context['criteria']:
        views = {'ai': criterion['ai_status']}
        if criterion['marked_by_user']:
            views['user'] = criterion['status']
        rule = matched.get(criterion['ref'])
        if rule is None:
            conflicts.append({'ref': criterion['ref'], 'criterion': criterion['criterion'], 'kind': 'not_audited',
                              'views': views})
            continue
        views['audit'] = AUDIT_STATUS[rule['result']]
        if len(set(views.values())) > 1:
            conflicts.append({'ref': criterion['ref'], 'criterion': criterion['criterion'], 'kind': 'disagreement',
                              'views': views})
    conflicts += [{'ref': None, 'criterion': rule['criterion'], 'kind': 'audit_only',
                   'views': {'audit': AUDIT_STATUS[rule['result']]}}
                  for rule in extra if rule['result'] != 'not_applicable']
    return conflicts


def compile_report(state, rounds, audit_log, usage):
    """
    The final payload: {'version', 'tcno', 'name', 'status', 'models', 'finished_at', 'based_on', 'inputs', 'final',
    'criteria', 'data_gaps', 'profile_flags', 'status_review', 'agents', 'rounds', 'audit_log', 'tokens'}.

    models is the model each LLM agent ran on ({'profiler': ..., 'audit': ..., ...}); each audit_log entry also
    names the model of its step, or None for a step that made no LLM call. final is {'determination', 'confidence', 'summary', 'deciding_criteria', 'current_verdict',
    'agrees_with_current', 'supervisor_determination'}. The determination follows the final statuses as a verdict
    does (statuses_verdict(), with the unread sources nobody checked); where the supervisor said otherwise it is
    Requires Review, since the agents did not settle it. Each criteria row lines up every view of one criterion:
    {'ref', 'criterion', 'requirement', 'source', 'current_status', 'ai_status', 'marked_by_user', 'audit_result',
    'audit_reason', 'audit_evidence', 'audit_missing', 'final_status', 'confidence', 'basis', 'resolution',
    'conflict', 'bias_risk'}; criteria only the audit found come last with ref null.
    """
    context, judgement = state['context'], state['judgement']
    matched, extra = match_rules(context['criteria'], state['audit']['rules'])
    conflicts = {conflict['ref'] for conflict in find_conflicts(context, state['audit']['rules'])
                 if conflict['ref'] is not None}
    reviews = {item['ref']: item for item in state['status_review']['reviews']}
    decided = list(judgement['criteria'])

    def final(ref, name, rule):
        """The supervisor's row for a criterion, taken off decided, or the audit's result when it gave none."""
        row = next((item for item in decided if ref is not None and item['ref'] == ref), None) \
            or _named([item for item in decided if item['ref'] is None or ref is None], name)
        if row:
            decided.remove(row)
            return row
        return {'final_status': AUDIT_STATUS[rule['result']] if rule else 'unknown', 'confidence': None,
                'basis': 'The Supervisor / Judge agent gave no final status, so the audit result stands.' if rule
                else 'Neither the audit nor the Supervisor / Judge agent assessed this criterion.',
                'resolution': ''}

    rows = []
    for criterion in context['criteria']:
        rule = matched.get(criterion['ref'])
        verdict = final(criterion['ref'], criterion['criterion'], rule)
        review = reviews.get(criterion['ref'])
        rows.append({
            'ref': criterion['ref'], 'criterion': criterion['criterion'], 'requirement': criterion['requirement'],
            'source': criterion['source'], 'current_status': criterion['status'], 'ai_status': criterion['ai_status'],
            'marked_by_user': criterion['marked_by_user'], **_audit_fields(rule),
            'final_status': verdict['final_status'], 'confidence': verdict['confidence'], 'basis': verdict['basis'],
            'resolution': verdict['resolution'], 'conflict': criterion['ref'] in conflicts,
            'bias_risk': review['bias_risk'] if review else None,
        })
    for rule in extra:
        verdict = final(None, rule['criterion'], rule)
        rows.append({
            'ref': None, 'criterion': rule['criterion'], 'requirement': rule['requirement'], 'source': rule['source'],
            'current_status': None, 'ai_status': None, 'marked_by_user': False, **_audit_fields(rule),
            'final_status': verdict['final_status'], 'confidence': verdict['confidence'], 'basis': verdict['basis'],
            'resolution': verdict['resolution'], 'conflict': rule['result'] != 'not_applicable', 'bias_risk': None,
        })

    unchecked = [source for source in context['unreadable'] if source not in context['unreadable_checked']]
    by_statuses = statuses_verdict([row['final_status'] for row in rows if row['final_status'] != 'not_applicable'],
                                   unchecked)
    said = judgement['determination']
    determination = by_statuses if said in (None, by_statuses) else REQUIRES_REVIEW
    if said and said != by_statuses:
        audit_log.append({'at': _now(), 'agent': 'supervisor', 'round': rounds, 'model': None, 'seconds': 0,
                          'event': f'The supervisor said {said}, but the final statuses give {by_statuses}: the '
                                   f'determination is {determination}, as the agents did not settle it.'})
    confidences = [row['confidence'] for row in rows if row['confidence'] is not None]
    confidence = judgement['confidence']
    if confidence is None and confidences:
        confidence = round(sum(confidences) / len(confidences), 2)

    data_gaps = judgement['data_gaps'] or _fallback_gaps(state)
    # The reviewer saw the audit before any re-examination: show each review beside the audit result that stands
    status_review = copy.deepcopy(state['status_review'])
    for item in status_review['reviews']:
        rule = matched.get(item['ref'])
        item['audit_result'] = rule['result'] if rule else None
    tokens = usage.counts()
    return {
        'version': REPORT_VERSION, 'tcno': context['tcno'], 'name': context['name'], 'status': 'completed',
        'models': dict(AGENT_MODELS), 'finished_at': _now(),
        'inputs': {
            'criteria_source': context['criteria_source'], 'excerpts': context['excerpts'],
            'criteria': len(context['criteria']),
            'user_marked': sum(1 for criterion in context['criteria'] if criterion['marked_by_user']),
            'unreadable': context['unreadable'], 'unreadable_checked': context['unreadable_checked'],
            'decision': context['decision'], 'normalised_facts': sum(1 for fact in context['facts']
                                                                     if _normalised(fact)),
            'warnings': context['warnings'],
        },
        'final': {
            'determination': determination, 'confidence': confidence, 'summary': judgement['summary'],
            'deciding_criteria': judgement['deciding_criteria'], 'current_verdict': context['verdict'],
            'agrees_with_current': determination == context['verdict'], 'supervisor_determination': said,
        },
        'criteria': rows,
        'data_gaps': data_gaps,
        'profile_flags': state['profile']['flags'],
        'status_review': status_review,
        'agents': {
            'ingestion': {'summary': _ingestion_event(context), 'warnings': context['warnings'],
                          'normalised_facts': [fact for fact in context['facts'] if _normalised(fact)]},
            'profiler': {'summary': state['profile']['summary'], 'reliable_fields': state['profile']['reliable_fields']},
            'audit': state['audit'],
            'status_review': {'summary': state['status_review']['summary']},
            'supervisor': {'summary': judgement['summary'], 'determination': said,
                           'confidence': judgement['confidence']},
        },
        'rounds': rounds,
        'audit_log': audit_log,
        'tokens': tokens,
    }


# ---------- saving, staleness and the background runner ----------

def result_fingerprint(result, company):
    """
    What a deep analysis was based on, as a short hash: the result's run, its statuses and marks, the unread files
    checked by hand, and the company profile. A change to any of them makes the saved deep analysis stale.
    """
    basis = {
        'processed_at': result.get('processed_at'),
        'criteria': [[row.get('criterion'), row.get('status'), row.get('ai_status'), bool(row.get('marked_by_user'))]
                     for row in result.get('criteria') or []],
        'unreadable_checked': result.get('unreadable_checked') or [],
        'company': (company or '').strip(),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]


def save_report(folder, report):
    """Writes the report to folder/DEEP_ANALYSIS_FILE, which read_report() reads back. Returns its path."""
    path = Path(folder) / DEEP_ANALYSIS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and then moved, so a crash mid-write cannot leave a report that cannot be read back
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)
    log.info('Saved the deep analysis of tender %s in %s', report['tcno'], path)
    return path


def read_report(path):
    """The report saved by save_report(), or None when there is none or it cannot be read (logged)."""
    try:
        report = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning('Could not read the deep analysis %s', path, exc_info=True)
        return None
    return report if isinstance(report, dict) and report.get('status') == 'completed' else None


def explain_deep_error(exc):
    """Returns (title, action): why a deep analysis failed and what to do, in the bid team's terms."""
    text = str(exc)
    if isinstance(exc, DeepAnalysisError):
        if 'no completed result' in text.lower():
            return ('The result is gone', 'Check the tender again, then run the deep analysis.')
        if 'no criteria' in text:
            return ('Nothing to review', "The result has no criteria and the tender's Pinecone namespace is missing. "
                    'Regenerate the criteria, then run the deep analysis again.')
        return ('An agent gave an answer the app could not read', 'Run the deep analysis again. If it fails again, '
                'see logs/app.log.')
    title, action = explain_error(exc)
    if title == 'The analysis failed':
        return ('The deep analysis failed', 'Run it again. If it fails again, see logs/app.log.')
    return title, action


class DeepAnalysisRunner:
    """
    Runs deep analyses in background threads, one at a time (each makes several long LLM calls), and reports on
    them. A tender's last report is read from its folder; only the run in progress, or the last failure, is kept
    in memory. A failed run leaves the last saved report as it was.
    """

    def __init__(self, processor, decisions=None, company_file=COMPANY_DETAILS_FILE):
        self.processor = processor
        self.decisions = decisions
        self.company_file = company_file
        self._lock = threading.Lock()
        self._runs = {}  # tcno -> {'status': 'queued' | 'running' | 'failed', ...}
        self._slot = threading.Semaphore(1)

    def path(self, tcno):
        """Where the tender's deep analysis is saved."""
        return self.processor.target_directory / safe_name(tcno) / DEEP_ANALYSIS_FILE

    def start(self, tcno):
        """
        Queues a deep analysis of the tender's completed result and returns its state (see state()). Raises
        LookupError without a completed result, TenderBusy while the tender is queued or being checked, and
        DeepAnalysisRunning while a deep analysis of it is queued or running.
        """
        tcno = str(tcno)
        result = self.processor.result(tcno)
        if not result or result['status'] != 'completed':
            raise LookupError(f'No completed result for TCNO {tcno}: check the tender first.')
        if self.processor.busy(tcno):
            raise TenderBusy()
        with self._lock:
            run = self._runs.get(tcno)
            if run and run['status'] in ('queued', 'running'):
                raise DeepAnalysisRunning()
            self._runs[tcno] = {'status': 'queued', 'agent': None, 'round': 0, 'done': [],
                                'stage': 'Waiting for another deep analysis to finish', 'queued_at': _now(),
                                'started_at': None, 'started': None}
        threading.Thread(target=self._run, args=(tcno,), name=f'deep-{safe_name(tcno)}', daemon=True).start()
        log.info('Deep analysis of tender %s queued', tcno)
        return self.state(tcno)

    def running(self, tcno):
        """True while a deep analysis of the tender is queued or running."""
        with self._lock:
            run = self._runs.get(str(tcno))
            return bool(run and run['status'] in ('queued', 'running'))

    def forget(self, tcno):
        """Drops the tender's last failed run from memory, once the tender is deleted (its report goes with its folder)."""
        with self._lock:
            run = self._runs.get(str(tcno))
            if run and run['status'] == 'failed':
                del self._runs[str(tcno)]

    def state(self, tcno):
        """
        {'tcno', 'status', 'progress', 'error', 'error_title', 'error_action', 'analysis', 'stale'}: status is
        'none', 'queued', 'running', 'completed' or 'failed'; progress, while queued or running, is {'agent',
        'round', 'done', 'stage', 'queued_at', 'started_at', 'elapsed_seconds'}; analysis is the last saved report
        (also while a new one runs, or after one failed), and stale is true when the result or the company profile
        has changed since it was made.
        """
        tcno = str(tcno)
        with self._lock:
            run = copy.deepcopy(self._runs.get(tcno))
        analysis = read_report(self.path(tcno))
        state = {'tcno': tcno, 'status': run['status'] if run else ('completed' if analysis else 'none'),
                 'progress': None, 'error': None, 'error_title': None, 'error_action': None,
                 'analysis': analysis, 'stale': False}
        if run and run['status'] == 'failed':
            state.update({key: run[key] for key in ('error', 'error_title', 'error_action')})
        elif run:
            started = run.pop('started')
            run['elapsed_seconds'] = round(time.monotonic() - started) if started else 0
            state['progress'] = {key: run[key] for key in ('agent', 'round', 'done', 'stage', 'queued_at',
                                                            'started_at', 'elapsed_seconds')}
        if analysis:
            result = self.processor.result(tcno)
            based_on = (analysis.get('based_on') or {}).get('fingerprint')
            state['stale'] = not result or based_on != result_fingerprint(result, self._company(tcno))
        return state

    def _company(self, tcno):
        """
        The company profile the tender's agents review, without the tender's own marks (profile_for_tender()):
        the audit judges afresh, and the status review sees the marks in the result.
        """
        return profile_for_tender(company_details(self.company_file), tcno)

    def _update(self, tcno, **changes):
        with self._lock:
            self._runs[tcno].update(changes)

    def _progress(self, tcno, agent, stage, round_number=0):
        """Records the agent now running; every agent that ran before it is done."""
        with self._lock:
            run = self._runs[tcno]
            if run['agent'] and run['agent'] not in run['done']:
                run['done'].append(run['agent'])
            if agent in run['done']:
                run['done'].remove(agent)
            run.update(agent=agent, stage=f'{AGENT_TITLES[agent]} agent: {stage}', round=round_number)

    def _run(self, tcno):
        """Thread body: waits for its turn, runs the agents over the result as it is then, and saves the report."""
        with self._slot:
            self._update(tcno, status='running', stage='Starting', started_at=_now(), started=time.monotonic())
            usage = TokenUsage(LLM_AGENTS)
            try:
                result = self.processor.result(tcno)
                if not result or result['status'] != 'completed':
                    raise DeepAnalysisError(f'No completed result for TCNO {tcno}: it was checked again or failed.')
                company = self._company(tcno)
                entry =(self.decisions.all().get(tcno) if self.decisions else None) or {}
                folder = self.processor.target_directory / safe_name(tcno)
                self._progress(tcno, 'ingestion', 'retrieving the criteria from the Pinecone namespace')
                knowledge = load_knowledge(self.processor.index, tcno, result)
                report = run_deep_analysis(result, knowledge, company, decision=entry.get('decision'), usage=usage,
                                           on_progress=lambda *args: self._progress(tcno, *args))
                report['based_on'] = {'processed_at': result.get('processed_at'), 'verdict': result.get('verdict'),
                                      'fingerprint': result_fingerprint(result, company)}
                with self._lock:
                    report['started_at'] = self._runs[tcno]['started_at']
                save_report(folder, report)
            except Exception as exc:
                log.exception('Deep analysis of tender %s failed', tcno)
                title, action = explain_deep_error(exc)
                self._update(tcno, status='failed', error=str(exc) or exc.__class__.__name__, error_title=title,
                             error_action=action)
            else:
                with self._lock:
                    self._runs.pop(tcno, None)
                log.info('Deep analysis of tender %s: %s, confidence %s, %d LLM tokens', tcno,
                         report['final']['determination'], report['final']['confidence'],
                         report['tokens']['total']['total'])


# ---------- helpers ----------

def _system(agent, role, schema, context):
    return _PREAMBLE.format(title=AGENT_TITLES[agent], role=role, schema=schema, today=context['today'],
                            financial_year=context['financial_year'],
                            last_financial_year=context['last_financial_year'])


def _ask(llm, agent, system, user):
    """
    Sends one agent's prompt and returns its JSON object. An answer that is not one is sent back once to be
    rewritten; a second bad answer raises DeepAnalysisError.
    """
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    reply = _content(llm.invoke(messages))
    try:
        return parse_json_reply(reply)
    except ValueError as exc:
        log.warning('The %s agent did not reply with JSON (%s); asking again', AGENT_TITLES[agent], exc)
        messages += [{'role': 'assistant', 'content': reply},
                     {'role': 'user', 'content': f'That reply was not one valid JSON object ({exc}). Reply again '
                                                 'with only the JSON object, in the shape asked for.'}]
    reply = _content(llm.invoke(messages))
    try:
        return parse_json_reply(reply)
    except ValueError as exc:
        raise DeepAnalysisError(f'The {AGENT_TITLES[agent]} agent did not reply with valid JSON, even when asked '
                                f'again: {exc}') from None


def parse_json_reply(text):
    """
    The JSON object in a model's reply, which may wrap it in a ```json fence, a <think> block or a sentence.
    Raises ValueError when there is none.
    """
    text = re.sub(r'<think>.*?</think>', '', text or '', flags=re.DOTALL).strip()
    fence = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find('{')
    if start < 0:
        raise ValueError('no JSON object in the reply')
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid JSON: {exc.msg} at character {exc.pos}') from None
    if not isinstance(value, dict):
        raise ValueError('the reply is not a JSON object')
    return value


def _content(response):
    """A chat model reply's text; some models send a list of content blocks."""
    content = getattr(response, 'content', response)
    if isinstance(content, list):
        return ''.join(part.get('text', '') if isinstance(part, dict) else str(part) for part in content)
    return str(content or '')


def _logged(output, describe):
    """(output, its audit log line): what step() expects from an agent."""
    return output, describe(output)


def _ingestion_event(context):
    marked = sum(1 for criterion in context['criteria'] if criterion['marked_by_user'])
    parts = [f"{_plural(len(context['criteria']), 'criterion', 'criteria')} from the result "
             f"({marked} marked by user)", f"criteria text from {context['criteria_source']}",
             f"{_plural(sum(1 for fact in context['facts'] if _normalised(fact)), 'profile figure')} normalised",
             f"{_plural(len(context['prechecks']), 'date flag')}"]
    if context['unreadable']:
        parts.append(f"{len(context['unreadable'])} unread source(s), {len(context['unreadable_checked'])} "
                     'checked by user')
    if context['warnings']:
        parts.append(f"{_plural(len(context['warnings']), 'warning')}: {' '.join(context['warnings'])}")
    return '; '.join(parts) + '.'


def _profiler_event(profile):
    high = sum(1 for flag in profile['flags'] if flag['severity'] == 'high')
    return f"{_plural(len(profile['flags']), 'flag')} on the company data ({high} high)."


def _audit_event(audit):
    counts = {result: sum(1 for rule in audit['rules'] if rule['result'] == result) for result in AUDIT_RESULTS}
    return (f"{_plural(len(audit['rules']), 'rule')}: {counts['pass']} pass, {counts['fail']} fail, "
            f"{counts['insufficient_data']} insufficient data, {counts['not_applicable']} not applicable.")


def _review_event(review):
    if review['skipped']:
        return review['summary']
    flagged = [item['criterion'] for item in review['reviews'] if item['bias_risk'] in ('high', 'medium')]
    return (f"{_plural(len(review['reviews']), 'criterion', 'criteria')} reviewed, "
            f"{_plural(len(review['overrides']), 'other override')}; "
            + (f"bias risk on {', '.join(flagged)}." if flagged else 'no medium or high bias risk.'))


def _judge_event(judgement, conflicts):
    text = (f"{_plural(len(conflicts), 'disagreement')} to resolve; determination "
            f"{judgement['determination'] or 'not stated'}, confidence {judgement['confidence']}.")
    if judgement['recheck']:
        names = ', '.join(item['criterion'] or f"criterion {item['ref']}" for item in judgement['recheck'])
        text += f' Sent back to the audit: {names}.'
    return text


def _audit_fields(rule):
    return {'audit_result': rule['result'] if rule else None, 'audit_reason': rule['reason'] if rule else '',
            'audit_evidence': rule['company_evidence'] if rule else '', 'audit_missing': rule['missing'] if rule else ''}


def _fallback_gaps(state):
    """The data gaps when the supervisor listed none: missing profile data and what the audit could not decide."""
    gaps = [{'gap': f"{flag['field']}: {flag['detail']}", 'needed_for': flag['needed_for'],
             'how_to_close': flag['verify_with'], 'severity': flag['severity']}
            for flag in state['profile']['flags'] if flag['issue'] == 'missing']
    gaps += [{'gap': rule['missing'], 'needed_for': rule['criterion'], 'how_to_close': '', 'severity': 'medium'}
             for rule in state['audit']['rules'] if rule['result'] == 'insufficient_data' and rule['missing']]
    return gaps


def _flag(field, issue, detail, severity, needed_for='', verify_with='', source='ingestion'):
    return {'field': field, 'issue': issue, 'detail': detail, 'needed_for': needed_for, 'severity': severity,
            'verify_with': verify_with, 'source': source}


def _fact_name(fact):
    return ' > '.join(part for part in (fact['section'], fact['field'], fact['column']) if part)


def _normalised(fact):
    return any(key in fact for key in ('rupees', 'date', 'financial_year'))


def _facts_text(context):
    lines = []
    for fact in context['facts']:
        if not _normalised(fact):
            continue
        name = _fact_name(fact)
        if 'financial_year' in fact:
            year = fact['financial_year']
            name += f" ({year['label']}, {year['start']} to {year['end']})"
        values = [fact['value']]
        if 'rupees' in fact:
            values.append(f"{crore(fact['rupees'])} ({fact['rupees']:,} rupees)")
        if 'date' in fact:
            values.append(fact['date'])
        lines.append(f"- {name}: {' = '.join(values)}")
    return '\n'.join(lines) or 'None found.'


def _flags_text(flags):
    return '\n'.join(f"- [{flag['severity']}] {flag['issue']}: {flag['field']}: {flag['detail']}"
                     + (f" (needed for {flag['needed_for']})" if flag['needed_for'] else '') for flag in flags)


def _requirements_text(context):
    if not context['criteria']:
        return context['criteria_text']
    return '\n'.join(f"[{criterion['ref']}] {criterion['criterion']}: {criterion['requirement'] or 'not recorded'}"
                     + (f" (source: {criterion['source']})" if criterion['source'] else '')
                     for criterion in context['criteria'])


def _known_text(context):
    return '\n'.join(f"[{criterion['ref']}] {criterion['criterion']}" for criterion in context['criteria']) or 'None.'


_STATUS_WORDS = {'met': 'Met', 'not_met': 'Not met', 'unknown': 'Cannot determine', 'not_applicable': 'Not applicable'}
_RESULT_WORDS = {'pass': 'Pass', 'fail': 'Fail', 'insufficient_data': 'Insufficient data',
                 'not_applicable': 'Not applicable'}


def _status_line(criterion):
    if criterion['marked_by_user']:
        return (f"{_STATUS_WORDS[criterion['status']]}, marked by user; the AI said "
                f"{_STATUS_WORDS[criterion['ai_status']]}")
    return f"{_STATUS_WORDS[criterion['status']]} (the AI's status, not marked by user)"


def _rule_line(rule):
    if not rule:
        return 'not assessed'
    parts = [_RESULT_WORDS[rule['result']], rule['reason']]
    if rule['company_evidence']:
        parts.append(f"Evidence: {rule['company_evidence']}")
    if rule['missing']:
        parts.append(f"Missing: {rule['missing']}")
    return ' — '.join(part for part in parts if part)


def _recheck_text(request, context, earlier):
    criterion = next((item for item in context['criteria'] if item['ref'] == request['ref']), None) \
        or _named(context['criteria'], request['criterion'])
    name = criterion['criterion'] if criterion else request['criterion']
    ref = criterion['ref'] if criterion else None
    lines = [f"- [{'ref ' + str(ref) if ref is not None else 'new'}] {name}: {request['question']}"]
    if criterion:
        lines.append(f'  Result: {_status_line(criterion)}')
    if earlier:
        matched, extra = match_rules(context['criteria'], earlier['rules'])
        rule = matched.get(ref) if ref is not None else _named(extra, name)
        lines.append(f'  Your earlier audit: {_rule_line(rule)}')
    return '\n'.join(lines)


def _overrides_text(context):
    lines = []
    if context['unreadable']:
        checked = context['unreadable_checked']
        lines.append(f"- Sources the AI could not read: {', '.join(context['unreadable'])}. "
                     + (f"Marked as checked by the user: {', '.join(checked)}." if checked
                        else 'Nobody has marked them checked.'))
    else:
        lines.append('- Every source was read.')
    if context['verdict'] != context['ai_verdict']:
        lines.append(f"- Verdict now {context['verdict']}, worked out from the statuses marked by user; the AI's own "
                     f"verdict was {context['ai_verdict']}.")
    else:
        lines.append(f"- Verdict: {context['verdict']}.")
    lines.append(f"- The bid team's decision: {context['decision'] or 'none recorded'}.")
    jv = context['jv'] or {}
    if jv.get('allowed') == 'yes':
        lines.append(f"- A joint venture may bid; as JV lead member: {jv.get('verdict') or 'not determined'}.")
    return '\n'.join(lines)


def _conflicts_text(conflicts):
    lines = []
    for conflict in conflicts:
        views = '; '.join(f"{_VIEW_NAMES[key]}: {_STATUS_WORDS[value]}" for key, value in conflict['views'].items())
        label = f"[{conflict['ref']}]" if conflict['ref'] is not None else '[audit only]'
        lines.append(f"- {label} {conflict['criterion']} ({conflict['kind'].replace('_', ' ')}): {views}")
    return '\n'.join(lines)


_VIEW_NAMES = {'ai': 'the AI', 'user': 'marked by user', 'audit': 'the audit'}


def _named(rows, name):
    """The row in rows whose criterion is called name: exactly, then one name containing the other."""
    key = criterion_key(name if isinstance(name, str) else '')
    if not key:
        return None
    exact = next((row for row in rows if criterion_key(row['criterion']) == key), None)
    return exact or next((row for row in rows if criterion_key(row['criterion'])
                          and (key in criterion_key(row['criterion']) or criterion_key(row['criterion']) in key)),
                         None)


def _ref(value, context):
    """A criterion number from an agent, or None when it is not one of the result's criteria."""
    if isinstance(value, bool):
        return None
    try:
        ref = int(value)
    except (TypeError, ValueError):
        return None
    return ref if 0 <= ref < len(context['criteria']) and float(value) == ref else None


def _choice(value, allowed, default):
    """value as one of allowed (matched loosely: "Insufficient data" is insufficient_data), or default."""
    text = re.sub(r'[\s-]+', '_', str(value or '').strip().lower())
    return text if text in allowed else default


_DETERMINATION_WORDS = {'eligible': 'Eligible', 'not eligible': 'Not Eligible', 'ineligible': 'Not Eligible',
                        'requires review': 'Requires Review', 'needs review': 'Requires Review',
                        'cannot determine': 'Requires Review'}


def _determination(value):
    """An agent's determination as the API's verdict (Eligible, Not Eligible, Requires Review), or None."""
    return _DETERMINATION_WORDS.get(' '.join(re.sub(r'[_*]+', ' ', str(value or '')).lower().split()))


def _confidence(value):
    """A confidence between 0 and 1, rounded to 2 places; a percentage is read as one. None when not a number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().rstrip('%'))
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100
    return round(min(max(number, 0.0), 1.0), 2)


def _text(value):
    """A value from a result or an agent as one clean line of text."""
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        value = '; '.join(str(item) for item in value)
    return ' '.join(str(value).split())


def _list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _items(value):
    """The objects of a list from an agent; anything else in it is dropped."""
    return [item for item in _list(value) if isinstance(item, dict)]


def _plural(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _now():
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
