"""
What is slow or paid for to work out, kept in the database (services/database.py) so it is worked out once:

  texts    a tender file's text as read (MarkItDown, and OCR of what its text layer leaves out), by the SHA-256 of the
           file and the reading settings: regenerating a tender whose files are downloaded again unchanged, or
           checking another tender with the same file, reads nothing again and spends no OCR tokens
  pages    the OCR of one page or image, by the SHA-256 of its image as rendered and the OCR settings: a file only
           some of whose pages could be read (so not in texts) has only the rest read again, and so does a file
           that changed in a few pages, or is the same scan in another file (a corrigendum reissuing the NIT)
  vectors  the embedding of a chunk of text, by the SHA-256 of the chunk and the embedding model and dimension: the
           chunks of an unchanged file are never embedded twice, and a change of CHUNK_SIZE, CHUNK_OVERLAP or the
           model, which changes the chunks or their vectors, cannot serve a stale one

A cache that cannot be read or written is logged and passed over: the work is then done as if nothing was cached.
Empty the three tables to read and embed everything again.
"""
import hashlib
import logging
from array import array

import psycopg

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
    """The cache in db (a services.database.Database). Thread-safe."""

    def __init__(self, db):
        self.db = db

    def text(self, sha256, reader):
        """The text cached for the file with this SHA-256, read with the settings reader names, or None."""
        row = self._run('read a cached text', lambda db: db.execute(
            'SELECT text FROM texts WHERE sha256 = %s AND reader = %s', (sha256, reader)).fetchone())
        return row[0] if row else None

    def save_text(self, sha256, reader, text):
        self._run('cache a text', lambda db: db.execute(
            'INSERT INTO texts VALUES (%s, %s, %s) ON CONFLICT (sha256, reader) DO UPDATE SET text = excluded.text',
            (sha256, reader, text)))

    def page_text(self, sha256, reader):
        """The OCR cached for the page image with this SHA-256, read with the settings reader names, or None ('' for a
        blank page)."""
        row = self._run('read a cached page', lambda db: db.execute(
            'SELECT text FROM pages WHERE sha256 = %s AND reader = %s', (sha256, reader)).fetchone())
        return row[0] if row else None

    def save_page_text(self, sha256, reader, text):
        self._run('cache a page', lambda db: db.execute(
            'INSERT INTO pages VALUES (%s, %s, %s) ON CONFLICT (sha256, reader) DO UPDATE SET text = excluded.text',
            (sha256, reader, text)))

    def vectors(self, model, texts):
        """[the vector cached for each of texts under model (its name and dimension), or None]."""
        keys = [_text_sha256(text) for text in texts]

        def find(db):
            found = dict(db.execute('SELECT sha256, vector FROM vectors WHERE model = %s AND sha256 = ANY(%s)',
                                    (model, list(set(keys)))).fetchall())
            return [array('f', found[key]).tolist() if key in found else None for key in keys]

        return self._run('read cached vectors', find) or [None] * len(texts)

    def save_vectors(self, model, texts, vectors):
        rows = [(model, _text_sha256(text), array('f', vector).tobytes()) for text, vector in zip(texts, vectors)]

        def save(db):
            with db.cursor() as cursor:
                cursor.executemany('INSERT INTO vectors VALUES (%s, %s, %s) ON CONFLICT (model, sha256) DO UPDATE '
                                   'SET vector = excluded.vector', rows)

        self._run('cache vectors', save)

    def _run(self, what, work):
        """work(connection), in a transaction; None when the cache cannot be used."""
        try:
            with self.db.connection() as connection:
                return work(connection)
        except psycopg.Error as error:
            log.warning('Could not %s in the database: %s', what, error)
            return None
