"""Keeps the bid team's decision on each tender (shortlisted or dropped) in a JSON file, so it survives a restart."""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DECISIONS = ('shortlisted', 'dropped')


class DecisionStore:
    """tcno -> {'decision': 'shortlisted' | 'dropped', 'decided_at': ISO 8601 UTC}, saved on every change."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._decisions = self._load()

    def all(self):
        """Returns a copy of every recorded decision."""
        with self._lock:
            return {tcno: dict(entry) for tcno, entry in self._decisions.items()}

    def set(self, tcno, decision):
        """
        Records decision for tcno, or clears it when decision is None. Returns the stored entry (None when
        cleared). Raises ValueError for anything other than None or one of DECISIONS.
        """
        if decision is not None and decision not in DECISIONS:
            raise ValueError(f'decision must be one of {", ".join(DECISIONS)} or null')
        tcno = str(tcno)
        entry = None
        with self._lock:
            if decision is None:
                self._decisions.pop(tcno, None)
            else:
                entry = {'decision': decision, 'decided_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
                self._decisions[tcno] = entry
            self._save()
        log.info('Tender %s: decision %s', tcno, decision or 'cleared')
        return dict(entry) if entry else None

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception('Could not read %s; starting without decisions', self.path)
            return {}
        return {str(tcno): entry for tcno, entry in data.items()
                if isinstance(entry, dict) and entry.get('decision') in DECISIONS} if isinstance(data, dict) else {}

    def _save(self):
        """Writes to a temporary file first, so a crash mid-write cannot leave a half-written file. Holds the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps(self._decisions, indent=2), encoding='utf-8')
        os.replace(temporary, self.path)
