"""
The monthly profile-gap report: the reasons the eligibility checks of one month marked criteria Cannot determine,
most frequent first, grouped by the fact each criterion lacked, and whether the company profile states that fact
now. It reads the saved results only, without any LLM call, so a criterion's reason is found by keywords in its
name and requirement (TOPICS).
"""
import re
from collections import Counter
from datetime import date, datetime, timezone

from services.company_service import MARKED_HEADING, parse_profile

# The reasons, tried in order: a criterion's reason is the first whose pattern matches its name, or failing that its
# requirement (whose boilerplate, such as an MSE exemption, says less about it). in_profile is True for a fact a
# standing entry in the company profile can give, False for what each bid has to supply itself or the tender
# documents have to state; profile_pattern, when the pattern would also match unrelated profile rows, is what a
# profile row about the fact looks like.
TOPICS = [
    {'key': 'land_border', 'reason': 'Land border declaration', 'pattern': r'land\s*border', 'in_profile': True},
    {'key': 'make_in_india', 'reason': 'Make in India class and local content', 'in_profile': True,
     'pattern': r'make\s*in\s*india|\bmii\b|local\s*content|local\s+suppliers?\b'},
    {'key': 'integrity', 'reason': 'Not blacklisted, integrity and legal standing', 'in_profile': True,
     'pattern': r'blacklist|debar|banned|barred|integrity|transgression|conflict\s+of\s+interest|insolven|bankrupt|'
                r'liquidation|receivership|poor\s+performance|criminal|conviction'},
    {'key': 'labour', 'reason': 'Labour-code compliance, labour licence, PF and ESI', 'in_profile': True,
     'pattern': r'labou?r|code\s+on\s+wages|industrial\s+relations|social\s+security|occupational\s+safety|'
                r'\b(?:e?pf|esic?)\b'},
    {'key': 'oem_authorisation', 'reason': 'OEM authorisation', 'in_profile': True,
     'pattern': r'\bmaf\b|manufacturer\W{0,3}s?\s+authori[sz]ation|oem\s+authori[sz]ation|authori[sz]ation\s+letter|'
                r'authori[sz]ed\s+(?:dealer|partner|distributor|reseller)'},
    {'key': 'oem', 'reason': "The OEM's turnover, net worth and experience", 'pattern': r'\boem\b', 'in_profile': False},
    {'key': 'financial', 'reason': 'Turnover, net worth, profit, solvency and tax returns', 'in_profile': True,
     'pattern': r'turnover|net\W*worth|net\s*profit|solvency|liquidity|working\s+capital|bid\s+capacity|income|'
                r'\bit\s+returns?\b|\bitr\b|credit\s+rating'},
    {'key': 'experience', 'reason': 'Similar work, past performance and clients', 'in_profile': True,
     'pattern': r'experience|similar|past\s+performance|complet|executed|work\s+order|supplied|supplier\s+to|'
                r'\bclients\b'},
    {'key': 'gst_pan_udyam', 'reason': 'GST, PAN, Udyam and MSE status', 'in_profile': True,
     'pattern': r'\bgst|\bpan\b|udyam|\bmse|msme|nsic|aadha?ar|\bkyc\b'},
    {'key': 'iso_bis', 'reason': 'ISO and BIS certification', 'in_profile': True,
     'pattern': r'\biso\b|\bbis\b|bureau\s+of\s+indian\s+standards|standard\s+mark'},
    {'key': 'local_office', 'reason': 'Local office or service centre', 'in_profile': True,
     'pattern': r'local\s+presence|\boffices?\b|service\s+cent(?:re|er)|branch'},
    {'key': 'licences', 'reason': 'Trade licence and statutory consents', 'in_profile': True,
     'pattern': r'trade\s+licen[cs]e|fssai|incorporation|valid\s+licen[cs]e|licen[cs]e\s+issued|'
                r'(?:electrical|contractor)\W+licen[cs]e|air\W+water|consent\s+to\s+operate|environmental\s+clearance|'
                r'hazardous\s+waste|pollution|\bpcb\b'},
    {'key': 'registration', 'reason': 'Registration with the buyer or an authority', 'in_profile': True,
     'pattern': r'regist(?:ered|ration)|empanel|enlist|approved\s+vendor|vendor\s+(?:status|code)|prior\s+approval',
     # Not "GST Registration" and the like, which say nothing of registrations with buyers
     'profile_pattern': r'empanel|enlist|approved\s+vendor|vendor\s+regist|registered\s+(?:vendor|with)'},
    {'key': 'fees', 'reason': 'EMD, fees and securities', 'in_profile': False,
     'pattern': r'\bemd\b|earnest\s+money|tender\s+fee|bid\s+security|performance\s+security|security\s+deposit'},
    {'key': 'joint_venture', 'reason': 'Joint venture terms', 'pattern': r'joint\W*venture|\bjv\b|consortium',
     'in_profile': False},
    {'key': 'tender_value', 'reason': 'Estimated cost not given', 'pattern': r'estimated\s+cost|tender\s+value',
     'in_profile': False},
    {'key': 'bid_specific', 'reason': 'Offer, site and documents of the bid itself', 'in_profile': False,
     'pattern': r'specification|product|install|support|warranty|inspection|pre-?bid|site|delivery|sample|document|'
                r'declaration|certific|validity|pric|unit\s+rate|assent|undertaking|annexure|format|lift'},
]
OTHER = {'key': 'other', 'reason': 'Other', 'in_profile': False}
_PATTERNS = {entry['key']: re.compile(entry['pattern'], re.IGNORECASE) for entry in TOPICS}
_PROFILE_PATTERNS = {entry['key']: re.compile(entry.get('profile_pattern') or entry['pattern'], re.IGNORECASE)
                     for entry in TOPICS if entry['in_profile']}
# How many of a reason's most frequent criterion names the report shows
EXAMPLES = 3


def topic(criterion):
    """
    The TOPICS (or OTHER) entry of a criterion ({'criterion', 'requirement', ...}): the first that matches its name,
    else the first that matches its requirement.
    """
    for text in (criterion.get('criterion'), criterion.get('requirement')):
        entry = next((entry for entry in TOPICS if text and _PATTERNS[entry['key']].search(text)), None)
        if entry:
            return entry
    return OTHER


def model_status(criterion):
    """The status the model gave the criterion, under any status the bid team marked by hand since."""
    return criterion.get('ai_status') or criterion.get('status')


def profile_statuses(markdown):
    """
    {topic key: 'stated', 'empty' or 'missing'} for the profile topics of TOPICS: 'stated' when a row of the
    profile's tables is about it (by the row's first cell and the headings above it) and has a value, 'empty' when
    such rows have none, 'missing' when there is no such row. The table of criteria marked by the bid team does not
    count: it records findings on tenders, not facts about the company.
    """
    rows = []  # (text to match, has a value)
    headings = {}  # level -> heading text
    for block in parse_profile(markdown):
        if block['type'] == 'heading':
            headings = {level: text for level, text in headings.items() if level < block['level']}
            headings[block['level']] = re.sub(r'^\s*\d+(\.\d+)*\.?\s+', '', block['text'])
        elif block['type'] == 'table':
            if any(' '.join(text.split()).lower() == MARKED_HEADING.lower() for text in headings.values()):
                continue
            above = ' '.join(text for level, text in sorted(headings.items()) if level > 1)
            for row in block['rows']:
                if row and row[0].strip():
                    rows.append((f'{above} {row[0]}', any(cell.strip() for cell in row[1:])))
    statuses = {}
    for key, pattern in _PROFILE_PATTERNS.items():
        found = [has_value for text, has_value in rows if pattern.search(text)]
        statuses[key] = 'stated' if any(found) else 'empty' if found else 'missing'
    return statuses


def profile_gap_report(results, profile_markdown, month=None, today=None):
    """
    The profile-gap report of month ('YYYY-MM', by default the current one in UTC, as results are timestamped) from
    results, the saved tender results (each tender's latest check): of the completed checks processed that month,
    every criterion the model marked Cannot determine, grouped by reason (topic()), most frequent first.

    Returns {'month', 'label', 'tenders', 'criteria', 'cannot_determine', 'reasons', 'generated_at'}, where reasons
    is a list of {'key', 'reason', 'criteria', 'tenders', 'share', 'profile', 'examples'}: profile is 'stated',
    'empty' or 'missing' for a fact the company profile can give (see profile_statuses()), None for one each bid or
    the tender documents must supply; examples are the reason's most frequent criterion names.
    """
    today = today or datetime.now(timezone.utc).date()
    month = month or f'{today:%Y-%m}'
    checked = [result for result in results
               if result.get('status') == 'completed' and str(result.get('processed_at') or '').startswith(month)]
    statuses = profile_statuses(profile_markdown)
    total = 0
    found = {}  # topic key -> {'entry', 'criteria', 'tenders', 'names'}
    for result in checked:
        for criterion in result.get('criteria') or []:
            total += 1
            if model_status(criterion) != 'unknown':
                continue
            entry = topic(criterion)
            reason = found.setdefault(entry['key'], {'entry': entry, 'criteria': 0, 'tenders': set(),
                                                     'names': Counter()})
            reason['criteria'] += 1
            reason['tenders'].add(str(result.get('tcno')))
            reason['names'][' '.join(str(criterion.get('criterion') or '').split())] += 1
    unknown = sum(reason['criteria'] for reason in found.values())
    order = {entry['key']: position for position, entry in enumerate(TOPICS + [OTHER])}
    reasons = [{
        'key': key, 'reason': reason['entry']['reason'], 'criteria': reason['criteria'],
        'tenders': len(reason['tenders']), 'share': round(100 * reason['criteria'] / unknown),
        'profile': statuses.get(key) if reason['entry']['in_profile'] else None,
        'examples': [name for name, _ in reason['names'].most_common(EXAMPLES) if name],
    } for key, reason in sorted(found.items(), key=lambda item: (-item[1]['criteria'], -len(item[1]['tenders']),
                                                                 order[item[0]]))]
    return {'month': month, 'label': f"{datetime.strptime(month, '%Y-%m'):%B %Y}", 'tenders': len(checked),
            'criteria': total, 'cannot_determine': unknown, 'reasons': reasons,
            'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}


_PROFILE_LABELS = {'stated': 'Stated', 'empty': 'Empty: fill it in', 'missing': 'Not in the profile: add it',
                   None: 'n/a'}


def report_markdown(report):
    """The report as a markdown document, for the bid team."""
    lines = [f"# Profile-gap report: {report['label']}", '']
    if not report['tenders']:
        lines.append(f"No tender check was completed in {report['label']}.")
        return '\n'.join(lines) + '\n'
    unknown, criteria = report['cannot_determine'], report['criteria']
    share = f' ({round(100 * unknown / criteria)}%)' if criteria else ''
    lines += [
        f"{report['tenders']} tender(s) checked in {report['label']}, counting each tender's latest check. Of their "
        f"{criteria} criteria, {unknown}{share} were marked Cannot determine. Below is what those criteria needed, "
        'most frequent first.', '',
    ]
    if report['reasons']:
        lines += ['| # | Reason | Criteria | Tenders | Company profile | Most frequent criteria |',
                  '| --- | --- | --- | --- | --- | --- |']
        for rank, reason in enumerate(report['reasons'], 1):
            examples = '; '.join(_cell(name) for name in reason['examples'])
            lines.append(f"| {rank} | {reason['reason']} | {reason['criteria']} ({reason['share']}%) | "
                         f"{reason['tenders']} | {_PROFILE_LABELS[reason['profile']]} | {examples} |")
        lines += [
            '',
            '**Company profile:** *Stated*: the profile has it now; checks made before it was added still lack it, '
            'so check those tenders again. *Empty* or *Not in the profile*: add the fact, under Standing '
            'declarations and certificates for a declaration. *n/a*: each bid, or the tender documents, must '
            'supply it.',
        ]
    lines += ['', f"Criteria marked by hand count under the status the model gave. Generated "
                  f"{report['generated_at'][:10]} from the saved results, without the LLM: reasons are found by "
                  'keywords in each criterion.']
    return '\n'.join(lines) + '\n'


def _cell(text):
    """Text as one markdown table cell."""
    return ' '.join(str(text).split()).replace('|', '\\|')
