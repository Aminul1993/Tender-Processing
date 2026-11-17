"""
The watchlist: the departments the team tracks for bidding, kept in the database (see read_rules()), each with the
TenderTiger organisations it covers and the states it is tracked in. Its search keeps a list of its own: every tender
of those departments, each with the department that claims it, which the page shows as saved.

That list cannot be had by narrowing TenderTiger's list. One search serves at most 10,000 tenders (asking past that
gets another search's tenders), and more than 10,000 close on a single day. The organisation filter takes one exact
name, not a list, and finds nothing when a location is added. So a refresh searches TenderTiger for each organisation
name the departments give ("search"), one at a time and closing from today (the watchlist's "filters"), and keeps the
tenders a department claims by its organisation, state, tender value and, for some, words in the description. A
department with a minimum value drops a tender TenderTiger gives no value for (0). Offices listed under
names of their own, such as West Bengal's "public works department||p.w. directorate||...", are found only on the
pages of TenderTiger's list: no exact name finds them, and a location search with the bid-documents filter finds
nothing at all. TenderTiger is asked once every TENDERTIGER_REQUEST_GAP seconds, so a refresh takes minutes: it runs
in a thread when Search again is pressed on the page, or in scripts/process_watchlist.py on a schedule, and the list
is saved in the database (the watchlist_tenders table; when it was refreshed, in app_state).
"""
import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timezone

import psycopg
from psycopg.types.json import Json

from services import tender_service

log = logging.getLogger(__name__)

CRORE = 10_000_000
# The name in app_state of when the list was refreshed ({'refreshed_at', 'rules': the fingerprint of the rules it
# searched with})
REFRESHED = 'watchlist_refresh'
SEARCH_PAGE = 500     # tenders asked for at once: the most TenderTiger sends
SEARCH_CAP = 10_000   # the most tenders one TenderTiger search serves
ANY_STATE = 'any'
# How errors name the watchlist, and what to do without one
SOURCE = 'the watchlist'
MISSING = 'There is no watchlist yet: add its departments with Edit watchlist on the page.'

# The states and union territories the watchlist may name, and TenderTiger's other names for some of them
STATES = {
    'andhra pradesh', 'arunachal pradesh', 'assam', 'bihar', 'chhattisgarh', 'goa', 'gujarat', 'haryana',
    'himachal pradesh', 'jharkhand', 'karnataka', 'kerala', 'madhya pradesh', 'maharashtra', 'manipur', 'meghalaya',
    'mizoram', 'nagaland', 'odisha', 'punjab', 'rajasthan', 'sikkim', 'tamil nadu', 'telangana', 'tripura',
    'uttar pradesh', 'uttarakhand', 'west bengal', 'andaman and nicobar islands', 'chandigarh',
    'dadra and nagar haveli and daman and diu', 'delhi', 'jammu and kashmir', 'ladakh', 'lakshadweep', 'puducherry',
}
STATE_ALIASES = {
    'orissa (odisha)': 'odisha', 'orissa': 'odisha', 'uttaranchal': 'uttarakhand', 'jammu-kashmir': 'jammu and kashmir',
    'jammu & kashmir': 'jammu and kashmir', 'andaman and nicobar': 'andaman and nicobar islands',
    'pondicherry': 'puducherry', 'new delhi': 'delhi', 'nct of delhi': 'delhi', 'chattisgarh': 'chhattisgarh',
}


class WatchlistError(Exception):
    """The database holds no valid watchlist."""


class WatchlistMissing(WatchlistError):
    """The database holds no watchlist: no department."""


def _norm(text):
    """Lower case, with runs of white space as one space: TenderTiger writes the same name both ways."""
    return ' '.join(str(text or '').lower().split())


def state_of(tender):
    """The tender's state as the watchlist names it (lower case); None for "multi state", "not classified" or none."""
    name = _norm(tender.get('statename'))
    name = STATE_ALIASES.get(name, name)
    return name if name in STATES else None


def value_of(tender):
    """The tender's value in rupees; None when TenderTiger gives none (it sends 0) or gives it in another currency."""
    value = tender_service.tender_value(tender)
    currency = _norm(tender.get('currcode'))
    return value if value and value > 0 and currency in ('', 'inr') else None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Department:
    """One department of the watchlist: what it claims, and how the page names it."""

    def __init__(self, sl, sector, name, full_name, organisations, search, states, brief, min_value_cr=None):
        self.sl, self.sector, self.name, self.full_name = sl, sector, name, full_name
        self.organisations = organisations  # compiled patterns, matched against the organisation name
        self.search = search                # exact organisation names a refresh searches TenderTiger for
        self.states = states                # frozenset of lower-case states, or None for any
        self.brief = brief                  # compiled pattern the description must match, or None
        self.min_value_cr = min_value_cr    # the least tender value, in crore of rupees, or None for any

    def names(self, organisation):
        """Whether one of its patterns matches the organisation name (normalised)."""
        return any(pattern.search(organisation) for pattern in self.organisations)

    def claims(self, state, brief):
        """Whether a tender of one of its organisations, of this state (see state_of()) and description (normalised),
        is this one's, whatever its value."""
        return (self.states is None or state in self.states) and (self.brief is None or bool(self.brief.search(brief)))

    def big_enough(self, value):
        """Whether a tender of this value (rupees, see value_of(); None when not given) reaches the minimum. With a
        minimum, a tender with no value does not."""
        return self.min_value_cr is None or (value is not None and value >= self.min_value_cr * CRORE)

    def label(self):
        return {'sl': self.sl, 'name': self.name, 'full_name': self.full_name, 'sector': self.sector,
                'min_value_cr': self.min_value_cr}


class Watchlist:
    """The departments of the watchlist, in their order, and the filters its refresh searches with."""

    def __init__(self, departments, filters, fingerprint):
        self.departments = departments
        self.filters = filters  # its "filters", checked; read again on each refresh, so "today" is that day
        self.fingerprint = fingerprint  # of the rules it was made from, to tell when they change
        # Organisation name -> the departments whose patterns match it, in order: a list of thousands of tenders has
        # a few dozen organisations, so each is matched against the patterns once
        self._named = {}

    @classmethod
    def load(cls, db):
        """
        Reads and checks the watchlist in db (see read_rules()). Raises WatchlistMissing without one, WatchlistError
        saying what is wrong.
        """
        try:
            raw = read_rules(db)
        except psycopg.Error as exc:
            raise WatchlistError(f'Could not read the watchlist from the database: {exc}') from None
        if raw is None:
            raise WatchlistMissing(MISSING)
        return cls.parse(raw)

    @classmethod
    def parse(cls, raw):
        """Checks rules in the shape read_rules() gives. Raises WatchlistError saying what is wrong."""
        if not isinstance(raw, dict) or not isinstance(raw.get('departments'), list) or not raw['departments']:
            raise WatchlistError('The watchlist needs at least one department.')
        filters = raw.get('filters') or {}
        try:
            # As a refresh will search with them, so filters it cannot search with are refused before
            tender_service.search_fields(tender_service.stored_filters(filters, f'{SOURCE}, filters').model_dump())
        except ValueError as exc:
            raise WatchlistError(str(exc)) from None
        groups = raw.get('state_groups') or {}
        if not isinstance(groups, dict):
            raise WatchlistError(f'{SOURCE}, state_groups: give an object of group name -> list of states.')
        for group, states in groups.items():
            _states(states, {}, f'{SOURCE}, state group {group!r}')
        default_min = _crore(raw.get('min_value_cr'), f'{SOURCE}, min_value_cr')
        departments = [cls._department(entry, number, groups, SOURCE, default_min)
                       for number, entry in enumerate(raw['departments'], 1)]
        return cls(departments, filters, fingerprint(raw))

    @staticmethod
    def _department(entry, number, groups, source, default_min=None):
        where = f'{source}, department {number}'
        if not isinstance(entry, dict) or not str(entry.get('name') or '').strip():
            raise WatchlistError(f'{where}: give each department an object with a "name".')
        where = f'{where} ({entry["name"]})'
        patterns = entry.get('organisations')
        if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
            raise WatchlistError(f'{where}: "organisations" must list the patterns of its organisation names.')
        organisations = [_pattern(pattern, f'{where}, organisation') for pattern in patterns]
        search = entry.get('search') or []
        if not isinstance(search, list) or not all(isinstance(name, str) and name.strip() for name in search):
            raise WatchlistError(f'{where}: "search" must list organisation names, as TenderTiger writes them.')
        for name in search:
            # A name its own patterns do not claim would be searched for nothing
            if not any(pattern.search(_norm(name)) for pattern in organisations):
                raise WatchlistError(f'{where}: the search name {name!r} matches none of its "organisations".')
            # So would one with an apostrophe, and it would stop the whole refresh (see tender_service.search_fields())
            if "'" in name:
                raise WatchlistError(f"{where}: leave the apostrophe out of the search name {name!r}: TenderTiger's "
                                     'search finds nothing with one.')
        states = entry.get('states')
        if states == ANY_STATE:
            states = None
        else:
            states = frozenset(_states(states, groups, where))
        brief = _pattern(entry['brief'], f'{where}, brief') if entry.get('brief') else None
        min_value_cr = _crore(entry['min_value_cr'], f'{where}, min_value_cr') if 'min_value_cr' in entry \
            else default_min
        return Department(entry.get('sl'), entry.get('sector') or '', entry['name'].strip(),
                          entry.get('full_name') or entry['name'].strip(), organisations,
                          [_norm(name) for name in search], states, brief, min_value_cr)

    def search_filters(self):
        """The filters a refresh searches with, as tender_service.search_fields() takes them, as of today."""
        filters = tender_service.stored_filters(self.filters, f'{SOURCE}, filters')
        return filters.model_dump(exclude_defaults=True)

    def search_names(self):
        """Every organisation name a refresh searches for, once each, in the departments' order."""
        return list(dict.fromkeys(name for department in self.departments for name in department.search))

    def department(self, tender):
        """The first department that claims the raw tender, or None."""
        organisation = _norm(tender.get('companyname'))
        named = self._named.get(organisation)
        if named is None:
            named = self._named[organisation] = [d for d in self.departments if d.names(organisation)]
        if not named:
            return None
        state, brief, value = state_of(tender), _norm(tender.get('tendersbrief')), value_of(tender)
        return next((d for d in named if d.claims(state, brief) and d.big_enough(value)), None)


def fingerprint(raw):
    """A fingerprint of rules in the shape read_rules() gives, to tell when they change."""
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode('utf-8')).hexdigest()


def read_rules(db):
    """
    The watchlist's rules in db, as {'about', 'filters', 'min_value_cr', 'state_groups': {name: [state, ...]},
    'departments': [{'sl', 'sector', 'name', 'full_name', 'organisations': [pattern, ...], 'search': [name, ...],
    'states': [state or group, ...] or 'any', 'brief', 'min_value_cr'}, ...]}, a value left out when the database
    holds none; None without any department. From the tables:

      watchlist_settings       one row: about (a note), filters (JSON, as GET /api/tenders names them), min_value_cr
                               (the least tender value in crore of rupees, for a department without its own)
      watchlist_state_groups   name, states (text[])
      watchlist_departments    position (the claiming order), sl, sector, name, full_name, states (text[], of states
                               and state groups; NULL for any state), brief (a pattern its descriptions must match),
                               min_value_cr (NULL for the settings' one)
      watchlist_organisations  department_id, position, pattern: a regular expression of its organisation names
      watchlist_searches       department_id, position, name: an exact organisation name a refresh searches for
    """
    with db.connection() as connection:
        settings = connection.execute('SELECT about, filters, min_value_cr FROM watchlist_settings').fetchone()
        groups = connection.execute('SELECT name, states FROM watchlist_state_groups ORDER BY name').fetchall()
        departments = connection.execute(
            'SELECT id, sl, sector, name, full_name, states, brief, min_value_cr FROM watchlist_departments '
            'ORDER BY position').fetchall()
        children = {table: connection.execute(f'SELECT department_id, {column} FROM {table} '
                                              f'ORDER BY department_id, position').fetchall()
                    for table, column in (('watchlist_organisations', 'pattern'), ('watchlist_searches', 'name'))}
    if not departments:
        return None
    lists = {table: defaultdict(list) for table in children}
    for table, rows in children.items():
        for department_id, value in rows:
            lists[table][department_id].append(value)
    about, filters, default_min = settings or (None, {}, None)
    raw = {'about': about, 'filters': filters, 'min_value_cr': default_min, 'state_groups': dict(groups), 'departments': [
        {key: value for key, value in {
            'sl': sl, 'sector': sector, 'name': name, 'full_name': full_name,
            'organisations': lists['watchlist_organisations'][department_id],
            'search': lists['watchlist_searches'][department_id], 'states': ANY_STATE if states is None else states,
            'brief': brief, 'min_value_cr': min_value_cr}.items() if value is not None}
        for department_id, sl, sector, name, full_name, states, brief, min_value_cr in departments]}
    return {key: value for key, value in raw.items() if value is not None}


def save_rules(db, raw):
    """Saves rules in the shape read_rules() gives in place of those in db, all or nothing."""
    with db.transaction() as connection:
        for table in ('watchlist_departments', 'watchlist_state_groups', 'watchlist_settings'):
            connection.execute(f'DELETE FROM {table}')  # a department's organisations and searches go with it
        connection.execute('INSERT INTO watchlist_settings (about, filters, min_value_cr) VALUES (%s, %s, %s)',
                           (raw.get('about'), Json(raw.get('filters') or {}), raw.get('min_value_cr')))
        for name, states in (raw.get('state_groups') or {}).items():
            connection.execute('INSERT INTO watchlist_state_groups VALUES (%s, %s)', (name, states))
        for position, entry in enumerate(raw['departments'], 1):
            states = entry.get('states')
            department_id = connection.execute(
                'INSERT INTO watchlist_departments (position, sl, sector, name, full_name, states, brief, '
                'min_value_cr) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                (position, entry.get('sl'), entry.get('sector'), entry['name'], entry.get('full_name'),
                 None if states == ANY_STATE else states, entry.get('brief'), entry.get('min_value_cr'))).fetchone()[0]
            with connection.cursor() as cursor:
                for table, column, values in (('watchlist_organisations', 'pattern', entry.get('organisations')),
                                              ('watchlist_searches', 'name', entry.get('search'))):
                    if values:
                        cursor.executemany(f'INSERT INTO {table} (department_id, position, {column}) '
                                           f'VALUES (%s, %s, %s)',
                                           [(department_id, number, value) for number, value in enumerate(values, 1)])


def _crore(value, where):
    """A minimum tender value in crore: a number, 0 or more; None when not given."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise WatchlistError(f'{where}: give the least tender value in crore of rupees, a number like 25 or 2.5.')
    return value


def _pattern(text, where):
    if not isinstance(text, str) or not text:
        raise WatchlistError(f'{where}: give a pattern (a regular expression).')
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error as exc:
        raise WatchlistError(f'{where}: {text!r} is not a valid regular expression ({exc}).') from None


def _states(names, groups, where):
    """The states a list names, a state group standing for its states; lower case."""
    if not isinstance(names, list) or not names:
        raise WatchlistError(f'{where}: "states" must be "{ANY_STATE}" or a list of states and state groups.')
    found = []
    for name in names:
        state = STATE_ALIASES.get(_norm(name), _norm(name)) if isinstance(name, str) else None
        if isinstance(name, str) and name in groups:
            found += [STATE_ALIASES.get(_norm(member), _norm(member)) for member in groups[name]]
        elif state in STATES:
            found.append(state)  # TenderTiger's names too: "Orissa (Odisha)" is Odisha
        else:
            raise WatchlistError(f'{where}: {name!r} is neither a state nor a state group.')
    return found


class WatchlistRunner:
    """
    The watchlist's tenders and their search. The rules apply only when TenderTiger is searched (a refresh, in a
    background thread, one at a time): each tender a department claims is saved, with that department, in the
    watchlist_tenders table, and the list shows that table as it is. An edit of the rules shows at the next search.
    """

    def __init__(self, db):
        self.db = db  # a services.database.Database: the rules and the list are in it
        self._lock = threading.Lock()
        self._refreshed_at = None   # when the last refresh finished (ISO, UTC)
        self._searched_rules = None  # the fingerprint of the rules it searched with
        self._run = None            # the refresh in progress, or the last one
        self._load_cache()

    # ---------- the rules ----------

    def watchlist(self):
        """The watchlist's rules in the database, checked. Raises WatchlistMissing, or WatchlistError."""
        return Watchlist.load(self.db)

    def rules(self):
        """The rules as read_rules() gives them, or a starter (the usual filters, no department) before any."""
        return read_rules(self.db) or {'filters': {'closing_from': 'today', 'bid_doc': True}, 'state_groups': {},
                                        'departments': []}

    def save_rules(self, raw):
        """
        Saves rules in the shape read_rules() gives, edited on the page, in place of the last, and returns them as
        saved; they apply from the next search. Raises WatchlistError, saying what is wrong, when they are not a valid
        watchlist: then nothing changes.
        """
        Watchlist.parse(raw)
        save_rules(self.db, raw)
        log.info('Watchlist saved: %d departments', len(raw['departments']))
        return read_rules(self.db)

    def departments(self, tenders):
        """
        The department label ({sl, name, full_name, sector, min_value_cr}) of each raw tender, as the last search
        saved it, or None for a tender it did not keep. From the database alone; all None when it cannot be read, so
        the tender list still loads.
        """
        tcnos = [str(tender.get('tcno')) for tender in tenders]
        try:
            with self.db.connection() as connection:
                found = dict(connection.execute('SELECT tcno, department FROM watchlist_tenders WHERE tcno = ANY(%s)',
                                                (tcnos,)).fetchall())
        except psycopg.Error as exc:
            log.warning('Tenders are not named with their watchlist departments: %s', exc)
            return [None] * len(tenders)
        return [found.get(tcno) for tcno in tcnos]

    # ---------- the list ----------

    def _read(self, query, params=()):
        try:
            with self.db.connection() as connection:
                return connection.execute(query, params).fetchall()
        except psycopg.Error as exc:
            raise WatchlistError(f'Could not read the watchlist from the database: {exc}') from None

    def page(self, page=1, page_size=tender_service.TENDER_PAGE_SIZE, sort='closing', order='asc'):
        """
        One page of the tenders the last search saved, as they are (the rules are not applied again), sorted by
        closing date or tender value (sort) in order, those without one last. Returns (raw tenders, how many there
        are in all). Sorted on the closing dates and values alone; only the page's tenders are read whole. Raises
        WatchlistError when the database cannot be read, and ValueError for the sorting or page size.
        """
        tender_service.check_page(sort, order, page_size)
        keys = [{'tcno': tcno, 'closingdate': closing, 'tendervalue': value, 'currcode': currency}
                for tcno, closing, value, currency in self._read(
                    "SELECT tcno, tender->>'closingdate', tender->>'tendervalue', tender->>'currcode' "
                    'FROM watchlist_tenders')]
        # Soonest closing first among equals, and among those without a value
        keys.sort(key=lambda t: tender_service.closing_date(t) or date.max)
        # By the value in rupees: TenderTiger's 0 is no value, and a value in another currency cannot be compared
        key = tender_service.closing_date if sort == 'closing' else value_of
        known = sorted((t for t in keys if key(t) is not None), key=key, reverse=order == 'desc')
        ordered = [t['tcno'] for t in known + [t for t in keys if key(t) is None]]
        shown = ordered[(page - 1) * page_size:page * page_size]
        tenders = dict(self._read('SELECT tcno, tender FROM watchlist_tenders WHERE tcno = ANY(%s)', (shown,)))
        return [tenders[tcno] for tcno in shown if tcno in tenders], len(ordered)

    def status(self):
        """
        The watchlist's state: {status: idle | running | completed | failed, refreshed_at, tenders (saved by the last
        search), departments (with any), searched, searches, current (the organisation name being searched), found,
        started_at, finished_at, error, capped (names whose tenders did not all fit TenderTiger's search),
        changed (the rules changed since the last search)}.
        """
        self._sync()
        try:
            [(tenders, departments)] = self._read(
                "SELECT count(*), count(DISTINCT department->>'name') FROM watchlist_tenders")
            raw = read_rules(self.db)
        except (WatchlistError, psycopg.Error):
            tenders = departments = 0
            raw = None
        with self._lock:
            refreshed_at, run, searched_rules = self._refreshed_at, dict(self._run or {}), self._searched_rules
        return {
            'status': run.get('status', 'idle'), 'refreshed_at': refreshed_at, 'tenders': tenders,
            'departments': departments, 'searched': run.get('searched', 0),
            'searches': run.get('searches', 0), 'current': run.get('current'), 'found': run.get('found', 0),
            'started_at': run.get('started_at'), 'finished_at': run.get('finished_at'), 'error': run.get('error'),
            'capped': run.get('capped', []),
            'changed': bool(raw and searched_rules and fingerprint(raw) != searched_rules),
        }

    # ---------- refreshing ----------

    def start(self, wait=False):
        """
        Starts a refresh in a background thread, unless one is running, and returns the status; with wait, refreshes in
        this thread and returns the status once it is done (scripts/process_watchlist.py). Raises WatchlistMissing /
        WatchlistError when the database holds no valid watchlist.
        """
        rules = self.watchlist()
        try:
            filters = rules.search_filters()
        except ValueError as exc:
            raise WatchlistError(str(exc)) from None
        names = rules.search_names()
        with self._lock:
            running = bool(self._run and self._run['status'] == 'running')
            if not running:
                self._run = {'status': 'running', 'searched': 0, 'searches': len(names), 'current': None, 'found': 0,
                             'started_at': _now(),
                             'started': time.monotonic(), 'finished_at': None, 'error': None, 'capped': []}
        if running:
            return self.status()
        log.info('Watchlist refresh started: %d organisations to search', len(names))
        if wait:
            self._refresh(rules, filters, names)
        else:
            threading.Thread(target=self._refresh, args=(rules, filters, names), name='watchlist-refresh',
                             daemon=True).start()
        return self.status()

    def _refresh(self, rules, filters, names):
        found = {}
        try:
            for index, name in enumerate(names):
                with self._lock:
                    self._run.update(current=name, searched=index)
                # The rules apply here, and only here: the list shows what this keeps, as it keeps it
                for tender in self._search(name, filters):
                    department = rules.department(tender)
                    if department:
                        found[str(tender['tcno'])] = (tender, department.label())
                with self._lock:
                    self._run['found'] = len(found)
        except Exception as exc:  # the list stays as the last refresh left it
            log.exception('Watchlist refresh failed')
            with self._lock:
                self._run.update(status='failed', error=str(exc) or type(exc).__name__, current=None,
                                 finished_at=_now())
            return
        rows = list(found.values())
        tenders = [tender for tender, _ in rows]
        refreshed_at = _now()
        try:
            save_list(self.db, rows, refreshed_at, rules.fingerprint)
        except psycopg.Error as exc:
            # The list shows what the database holds, the last refresh's: not refreshed, so another is due as before
            log.exception('Could not save the watchlist in the database')
            with self._lock:
                self._run.update(status='failed', current=None, finished_at=_now(),
                                 error=f'The tenders found could not be saved in the database: {exc}')
            return
        tender_service.remember(tenders)
        with self._lock:
            self._refreshed_at, self._searched_rules = refreshed_at, rules.fingerprint
            took = time.monotonic() - self._run['started']
            self._run.update(status='completed', searched=len(names), current=None, finished_at=refreshed_at)
        log.info('Watchlist refreshed: %d tenders from %d organisation searches in %.0f s', len(tenders), len(names),
                 took)

    def _search(self, name, filters):
        """
        Every tender TenderTiger lists under the organisation name with the filters, a page of SEARCH_PAGE at a time
        by closing date. Past SEARCH_CAP, searches again closing from the last date reached.
        """
        start = filters.get('closing_from')
        tenders = {}
        while True:
            fields = tender_service.search_fields({**filters, 'organisation': name, 'closing_from': start})
            last = None
            for page in range(1, SEARCH_CAP // SEARCH_PAGE + 1):
                response = tender_service.tender_list(fields, page=page, page_size=SEARCH_PAGE,
                                                      sort=('closingdate', 'asc')) or {}
                rows = response.get('TenderList') or []
                # Asked past its last tender, TenderTiger answers with another search's (categoryflag 2)
                if str(response.get('categoryflag', '1')) != '1':
                    rows = []
                tenders.update((str(row['tcno']), row) for row in rows if row.get('tcno') is not None)
                if len(rows) < SEARCH_PAGE:
                    return list(tenders.values())
                last = rows[-1]
            reached = tender_service.closing_date(last)
            if reached is None or (start is not None and reached <= start):
                log.warning('Watchlist: TenderTiger lists more than %d tenders of %r closing on %s; the rest are missed',
                            SEARCH_CAP, name, reached)
                with self._lock:
                    self._run['capped'].append(name)
                return list(tenders.values())
            start = reached

    # ---------- the saved list ----------

    def _load_cache(self):
        try:
            rows, refreshed = load_list(self.db)
        except psycopg.Error as exc:
            log.warning('Could not read the saved watchlist: %s', exc)
            return
        if refreshed:
            self._refreshed_at, self._searched_rules = refreshed['refreshed_at'], refreshed.get('rules')
        # Listed again, so they can be processed after a restart
        tender_service.remember([tender for tender, _ in rows])

    def _sync(self):
        """
        Takes up, from the database, a refresh another process made (scripts/process_watchlist.py, say): when the
        saved list was refreshed, and with which rules.
        """
        try:
            refreshed = self.db.value(REFRESHED)
        except psycopg.Error:
            return
        if refreshed:
            with self._lock:
                self._refreshed_at, self._searched_rules = refreshed['refreshed_at'], refreshed.get('rules')


def load_list(db):
    """
    ([(raw tender, department label)] the last refresh saved in db, {'refreshed_at', 'rules'} of it or None before
    any).
    """
    with db.connection() as connection:
        rows = connection.execute('SELECT tender, department FROM watchlist_tenders ORDER BY tcno').fetchall()
    return [(tender, department) for tender, department in rows], db.value(REFRESHED)


def save_list(db, rows, refreshed_at, rules=None):
    """
    Saves the [(raw tender, department label)] of a refresh in db in place of the last one's, all or nothing, with
    the fingerprint of the rules it searched with (Watchlist.fingerprint).
    """
    with db.transaction() as connection:
        connection.execute('DELETE FROM watchlist_tenders')
        with connection.cursor() as cursor, \
                cursor.copy('COPY watchlist_tenders (tcno, tender, department) FROM STDIN') as copy:
            for tender, department in rows:
                copy.write_row((str(tender['tcno']), json.dumps(tender), json.dumps(department)))
        db.set_value(REFRESHED, {'refreshed_at': refreshed_at, 'rules': rules})
