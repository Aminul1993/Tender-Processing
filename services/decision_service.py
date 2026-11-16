"""
Keeps the bid team's decision on each tender in the database, so it survives a restart. A tender starts pending (no
decision) and only moves forward: pending to reviewed (looked at, undecided), shortlisted or dropped; reviewed to
shortlisted or dropped; shortlisted to dropped. Dropped is final.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DECISIONS = ('reviewed', 'shortlisted', 'dropped')
# What a tender can be marked next, by its decision (None: pending)
NEXT = {
    None: ('reviewed', 'shortlisted', 'dropped'),
    'reviewed': ('shortlisted', 'dropped'),
    'shortlisted': ('dropped',),
    'dropped': (),
}


class DecisionNotAllowed(Exception):
    """The tender's decision cannot move to the one asked for."""

    def __init__(self, tcno, current, decision):
        allowed = NEXT[current]
        then = f'it can only be marked {" or ".join(allowed)}' if allowed else 'that is final'
        super().__init__(f'Tender {tcno} is {current or "pending"}, so it cannot be marked {decision}: {then}.')
        self.current = current


class DecisionStore:
    """tcno -> {'decision': 'reviewed' | 'shortlisted' | 'dropped', 'decided_at': ISO 8601 UTC}, in db (a Database)."""

    def __init__(self, db):
        self.db = db

    def all(self):
        """Returns every recorded decision."""
        with self.db.connection() as connection:
            rows = connection.execute('SELECT tcno, decision, decided_at FROM decisions').fetchall()
        return {tcno: {'decision': decision, 'decided_at': decided_at} for tcno, decision, decided_at in rows}

    def mark(self, tcno, decision):
        """
        Marks the tender with decision, one NEXT allows after its own, and returns the stored entry. Raises
        ValueError for anything other than one of DECISIONS, and DecisionNotAllowed when it does not follow the
        tender's decision. Checked and written in one statement, so two people marking the same tender cannot both
        move it from where it was.
        """
        if decision not in DECISIONS:
            raise ValueError(f'decision must be one of {", ".join(DECISIONS)}')
        tcno = str(tcno)
        entry = {'decision': decision, 'decided_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        # The decisions it can follow; pending (no row yet) can be followed by any, so the insert needs no check
        after = [current for current, following in NEXT.items() if current and decision in following]
        with self.db.connection() as connection:
            row = connection.execute(
                'INSERT INTO decisions VALUES (%s, %s, %s) ON CONFLICT (tcno) DO UPDATE SET decision = excluded.decision, '
                'decided_at = excluded.decided_at WHERE decisions.decision = ANY(%s::text[]) RETURNING decision',
                (tcno, decision, entry['decided_at'], after)).fetchone()
            if row is None:
                current = connection.execute('SELECT decision FROM decisions WHERE tcno = %s', (tcno,)).fetchone()
                raise DecisionNotAllowed(tcno, current[0] if current else None, decision)
        log.info('Tender %s: marked %s', tcno, decision)
        return entry

    def forget(self, tcno):
        """Forgets the tender's decision once the tender is deleted, so it reads as never seen: pending."""
        with self.db.connection() as connection:
            connection.execute('DELETE FROM decisions WHERE tcno = %s', (str(tcno),))
