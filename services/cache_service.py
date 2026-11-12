"""
What is slow or paid for to work out, kept in one SQLite file (CACHE_FILE) so it is worked out once:

  texts    a tender file's text as read (MarkItDown, and OCR of what its text layer leaves out), by the SHA-256 of the
           file and the reading settings: regenerating a tender whose files are downloaded again unchanged, or
           checking another tender with the same file, reads nothing again and spends no OCR tokens
  vectors  the embedding of a chunk of text, by the SHA-256 of the chunk and the embedding model and dimension: the
           chunks of an unchanged file are never embedded twice, and a change of CHUNK_SIZE, CHUNK_OVERLAP or the
           model, which changes the chunks or their vectors, cannot serve a stale one

A cache that cannot be read or written is logged and passed over: the work is then done as if nothing was cached.
Delete the file to read and embed everything again.
"""
import hashlib
import logging
import sqlite3
import threading
from array import array
from contextlib import closing
from pathlib import Path

log = logging.getLogger(__name__)


def file_sha256(path):
    """The SHA-256 of the file at path, as hex."""
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _text_sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class Cache:
    """The cache in the SQLite file at path, made on first use. Thread-safe."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._made = False

    def text(self, sha256, reader):
        """The text cached for the file with this SHA-256, read with the settings reader names, or None."""
        row = self._run('read a cached text', lambda db: db.execute(
            'SELECT text FROM texts WHERE sha256 = ? AND reader = ?', (sha256, reader)).fetchone())
        return row[0] if row else None

    def save_text(self, sha256, reader, text):
        self._run('cache a text', lambda db: db.execute('INSERT OR REPLACE INTO texts VALUES (?, ?, ?)',
                                                         (sha256, reader, text)))

    def vectors(self, model, texts):
        """[the vector cached for each of texts under model (its name and dimension), or None]."""
        keys = [_text_sha256(text) for text in texts]

        def find(db):
            rows = (db.execute('SELECT vector FROM vectors WHERE model = ? AND sha256 = ?', (model, key)).fetchone()
                    for key in keys)
            return [array('f', row[0]).tolist() if row else None for row in rows]

        return self._run('read cached vectors', find) or [None] * len(texts)

    def save_vectors(self, model, texts, vectors):
        rows = [(model, _text_sha256(text), array('f', vector).tobytes()) for text, vector in zip(texts, vectors)]
        self._run('cache vectors', lambda db: db.executemany('INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)', rows))

    def _run(self, what, work):
        """work(connection), in a transaction; None when the cache cannot be used."""
        try:
            with self._lock:
                if not self._made:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                    if not self._made:
                        db.execute('CREATE TABLE IF NOT EXISTS texts '
                                   '(sha256 TEXT, reader TEXT, text TEXT, PRIMARY KEY (sha256, reader))')
                        db.execute('CREATE TABLE IF NOT EXISTS vectors '
                                   '(model TEXT, sha256 TEXT, vector BLOB, PRIMARY KEY (model, sha256))')
                        self._made = True
                    return work(db)
        except (sqlite3.Error, OSError) as error:
            log.warning('Could not %s in %s: %s', what, self.path, error)
            return None
