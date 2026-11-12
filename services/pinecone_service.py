"""
One Pinecone namespace per tender, in one shared index. Every document of tender <tcno> (its downloaded files, the
TenderTiger pre-qualification text and the tender's TenderTiger data) is chunked, embedded and stored in the
namespace tcno-<tcno> of the index PINECONE_INDEX_NAME, and the eligibility check, its re-checks and the deep
analysis retrieve what they need from that namespace alone: every read and write names it, so tenders never see
each other's records.

A namespace per tender, rather than an index per tender, because Pinecone caps the indexes of a project (5 on
Starter, 20 on Standard) far below the namespaces of an index (100 on Starter, 100,000 on Standard).

Each namespace holds two kinds of record, told apart by the metadata field record_type:
  chunk     a piece of one source's text, with {tcno, source_file, document_type, chunk_id, chunk_index, overlap,
            created_at, text}
  manifest  one record, MANIFEST_ID. reset_namespace() writes it with status 'building', which also creates the
            namespace (so a plan with no room for another fails the tender before any download or OCR); ingest()
            rewrites it last, with status 'complete', once every chunk can be queried: the sources indexed (with their
            chunk counts), the sources that could not be read, and the embedding model (a long source list is split
            over manifest_part records written just before it). A namespace whose manifest is not complete was left
            half-built by a failure and is rebuilt rather than trusted.

Chunk IDs are derived from the source name and the chunk's position, so the manifest alone gives every chunk's ID
(knowledge() fetches them all without listing the namespace) and an upsert repeated after a failure overwrites
rather than duplicates.

Pinecone serverless applies writes a few seconds after accepting them. A build therefore waits, before it writes
the complete manifest and again after, until searches of the namespace have caught up with its last write. Pinecone
reports each write's log position (lsn_committed) and how far each search has caught up (lsn_reconciled), which
gives read-your-writes. Deleting a namespace empties it first, in order with those writes; see delete_namespace().

Embeddings come from Pinecone's hosted models (PINECONE_EMBED_MODEL, llama-text-embed-v2 by default), so the
PINECONE_API_KEY is the only credential needed. Every call is retried on rate limits, server errors and network
errors; embedding and upserting run in parallel batches, so tenders with thousands of chunks index in minutes.
"""
import hashlib
import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# The ID of the record that marks a namespace as complete and describes what it holds
MANIFEST_ID = '__manifest__'
# Bumped when the records change shape; a namespace of another version is rebuilt by the migration script
SCHEMA_VERSION = 2
# The most inputs Pinecone's hosted embedding models take in one request
EMBED_BATCH = 96
# IDs fetched per request when reading chunks back by ID
FETCH_BATCH = 100
# Pinecone's limits on index and namespace names
INDEX_NAME_LIMIT = 45
NAMESPACE_NAME_LIMIT = 512
# Sources every retrieval includes in full: short, and the criteria TenderTiger states for the tender
PINNED_TYPES = ('prequalification', 'tender_data')
# Characters of the manifest's source list per record: Pinecone allows 40 KB of metadata per record, so a tender
# with hundreds of files has its list split over parts (__manifest__-000, ...)
MANIFEST_PART_CHARS = 30_000

_CHUNK_FILTER = {'record_type': {'$eq': 'chunk'}}


class VectorStoreError(RuntimeError):
    """A tender's Pinecone namespace could not be built, read or deleted."""


class VectorStoreNotConfigured(VectorStoreError):
    """PINECONE_API_KEY is not set."""


class QuotaExceeded(VectorStoreError):
    """The Pinecone plan has no room left: for another namespace in the index, or for the index itself."""


class NamespaceIncomplete(VectorStoreError):
    """The tender has no namespace, or one left half-built (no complete manifest): it must be indexed again."""


class _CachedFailure(VectorStoreError):
    """Listing the namespaces failed a moment ago, so it is not tried again yet."""


class _NotFound(VectorStoreError):
    """Pinecone answered 404: no such index or namespace."""


class _Exists(VectorStoreError):
    """Pinecone answered 409: it already exists."""


@dataclass(frozen=True)
class Document:
    """One source of a tender: a downloaded file (name is its path in the tender folder) or a TenderTiger text."""
    name: str
    text: str
    document_type: str


@dataclass
class Retrieval:
    """
    What retrieve() found in a tender's namespace. chunks are in document order (by source as indexed, then
    position), each {'id', 'source_file', 'document_type', 'chunk_index', 'overlap', 'text', 'score', 'queries'},
    where queries are the labels of the queries that found it and score the best of their scores (1.0 for a pinned
    chunk).
    """
    namespace: str
    manifest: dict
    chunks: list = field(default_factory=list)

    @property
    def unreadable(self):
        """The sources that could not be read when the namespace was built, so are not in it."""
        return list(self.manifest.get('unreadable') or [])

    @property
    def characters(self):
        return sum(len(chunk['text']) for chunk in self.chunks)

    def sections(self):
        """
        The chunks as one '## <source>' section per source, in the order sources were indexed. Neighbouring chunks
        are joined without the text they repeat (their overlap); a gap between chunks is marked with '[…]'.
        """
        sections, source, parts, last = [], None, [], None
        for chunk in self.chunks:
            if chunk['source_file'] != source:
                if parts:
                    sections.append(f'## {source}\n' + '\n\n'.join(parts))
                source, parts, last = chunk['source_file'], [], None
            text = chunk['text']
            if last is not None and chunk['chunk_index'] == last + 1:
                parts[-1] = f"{parts[-1]}\n{text[chunk['overlap']:].lstrip()}"
            else:
                if last is not None:
                    parts.append('[…]')
                parts.append(text)
            last = chunk['chunk_index']
        if parts:
            sections.append(f'## {source}\n' + '\n\n'.join(parts))
        return sections


def split_pieces(text, max_chars):
    """
    Splits text into pieces of at most max_chars, each ending at a paragraph break, else a line break, else a space
    found in the second half of the piece, so a piece is never cut short by a break near its start nor ends mid-word
    (unless a single word is longer than max_chars).
    """
    pieces, start, text = [], 0, text.strip()
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            for separator in ('\n\n', '\n', ' '):
                cut = text.rfind(separator, start + max_chars // 2, end)
                if cut > start:
                    end = cut
                    break
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    return pieces


def chunk_text(text, size=1600, overlap=200):
    """
    Splits text into chunks of about size characters (see split_pieces()). Each chunk after the first starts with
    the last overlap characters of the one before (from a word boundary), so a criterion split across two chunks is
    whole in one of them. Returns [(chunk, overlap)], where overlap is the number of leading characters the chunk
    repeats from the previous one.
    """
    overlap = max(0, min(overlap, size // 2))
    pieces = split_pieces(text or '', max(1, size - overlap))
    chunks = []
    for number, piece in enumerate(pieces):
        tail = ''
        if number and overlap:
            tail = pieces[number - 1][-overlap:]
            space = tail.find(' ')
            if 0 <= space < len(tail) - 1:
                tail = tail[space + 1:]
            tail += '\n'
        chunks.append((tail + piece, len(tail)))
    return chunks


# A file's kind, from words in its name: the first that matches
_DOCUMENT_TYPES = (
    ('corrigendum', r'corrigend\w*|addend\w*|amendment\w*|clarification\w*|pre ?bid'),
    ('boq', r'boq|bill of quantit\w*|price bid|financial bid|schedule of rates|sor'),
    ('nit', r'nit|nie|notice inviting\w*|tender notice|rfp|rfq|itb|instructions? to bidders?|bid document'),
    ('technical', r'technical|specifications?|specs?|scope of work|gcc|scc|conditions of contract'),
    ('form', r'forms?|formats?|annex\w*|appendix|declarations?|affidavit|undertaking'),
)


def document_type(name):
    """The kind of tender document a file name suggests: nit, boq, corrigendum, technical, form or other."""
    words = ' '.join(re.sub(r'[^a-z0-9]+', ' ', name.lower()).split())
    for kind, pattern in _DOCUMENT_TYPES:
        if re.search(rf'\b(?:{pattern})\b', words):
            return kind
    return 'other'


def source_key(name):
    """The stable part of the IDs of a source's chunks."""
    return hashlib.sha1(name.encode('utf-8')).hexdigest()[:16]


def chunk_id(name, position):
    return f'{source_key(name)}-{position:05d}'


class PineconeService:
    """
    The tenders' Pinecone namespaces: one per TCNO, all in the index index_name. Thread-safe; one instance is shared
    by the app (create_app()).

    Without an API key the service is unconfigured: has_namespace() answers False and every other call raises
    VectorStoreNotConfigured, so the tender list still loads and each analysis fails with a clear message.
    client, if given, stands in for pinecone.Pinecone (the tests pass an in-memory fake).
    """

    def __init__(self, api_key=None, index_name='tender-processing', namespace_prefix='tcno-', cloud='aws',
                 region='us-east-1', embed_model='llama-text-embed-v2', dimension=1024, metric='cosine',
                 chunk_size=1600, chunk_overlap=200, top_k=8, max_chars=60_000, upsert_batch=50, workers=4,
                 attempts=4, ready_timeout=300, freshness_timeout=120, cache_seconds=30, settle_seconds=5,
                 client=None, cache=None):
        self._api_key = api_key
        # A cache_service.Cache of chunk vectors, so a chunk embedded once is never embedded again; None embeds all
        self.cache = cache
        self.index_name = index_name
        self.namespace_prefix = namespace_prefix
        self.cloud = cloud
        self.region = region
        self.embed_model = embed_model
        self.dimension = dimension
        self.metric = metric
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.top_k = top_k
        self.max_chars = max_chars
        self.upsert_batch = upsert_batch
        self.workers = max(1, workers)
        self.attempts = max(1, attempts)
        self.ready_timeout = ready_timeout
        self.freshness_timeout = freshness_timeout
        self.cache_seconds = cache_seconds
        # How long a deleted namespace must stay gone before the delete is trusted (see delete_namespace())
        self.settle_seconds = settle_seconds
        self._client = client
        self._client_lock = threading.Lock()
        self._names = None  # the namespaces last listed
        self._names_at = 0.0  # time.monotonic() of that listing, or of the last failed one
        self._names_error = None
        self._names_lock = threading.Lock()
        self._handle = None  # the pinecone Index of index_name
        self._handle_lock = threading.Lock()
        self._ready = False  # index_name is known to exist, be ready and have the right dimension
        self._ready_lock = threading.Lock()

    @classmethod
    def from_config(cls, config, client=None, cache=None):
        """A service with the PINECONE_* and retrieval settings of config (see config.Config), and cache, if given."""
        return cls(api_key=config.PINECONE_API_KEY, index_name=config.PINECONE_INDEX_NAME,
                   namespace_prefix=config.PINECONE_NAMESPACE_PREFIX, cloud=config.PINECONE_CLOUD,
                   region=config.PINECONE_REGION, embed_model=config.PINECONE_EMBED_MODEL,
                   dimension=config.PINECONE_EMBED_DIMENSION, chunk_size=config.CHUNK_SIZE,
                   chunk_overlap=config.CHUNK_OVERLAP, top_k=config.RETRIEVAL_TOP_K,
                   max_chars=config.RETRIEVAL_MAX_CHARS, upsert_batch=config.PINECONE_UPSERT_BATCH,
                   workers=config.INGEST_WORKERS, client=client, cache=cache)

    # ---------- configuration and names ----------

    @property
    def configured(self):
        return bool(self._api_key or self._client)

    def require(self):
        """Raises VectorStoreNotConfigured unless an API key (or a client) was given."""
        if not self.configured:
            raise VectorStoreNotConfigured('PINECONE_API_KEY is not set. Add it to .env and restart the app.')

    def client(self):
        """The pinecone.Pinecone client, made on first use."""
        self.require()
        with self._client_lock:
            if self._client is None:
                from pinecone import Pinecone, RetryConfig
                # The SDK retries 408 / 429 / 5xx itself; _call() adds a few slower retries on top for longer outages
                self._client = Pinecone(api_key=self._api_key, retry_config=RetryConfig(max_retries=3, max_wait=20))
            return self._client

    def namespace(self, tcno):
        """
        The tender's namespace: PINECONE_NAMESPACE_PREFIX + TCNO, such as tcno-12345678. Raises VectorStoreError when
        that is not printable ASCII of at most 512 characters, as Pinecone requires.
        """
        name = f'{self.namespace_prefix}{tcno}'
        if not name or len(name) > NAMESPACE_NAME_LIMIT or not re.fullmatch(r'[\x21-\x7e]+', name):
            raise VectorStoreError(f'TCNO {tcno!r} gives no valid Pinecone namespace (printable ASCII without '
                                   f'spaces, at most {NAMESPACE_NAME_LIMIT} characters): {name!r}')
        return name

    def namespaces(self, refresh=False):
        """
        The tender namespaces (those starting with the prefix) in the index, listed at most every cache_seconds
        (refresh lists them now). A failed listing is not retried until cache_seconds have passed, so a page of 500
        tenders makes one call, not 500. Raises VectorStoreError when Pinecone cannot be reached.
        """
        # The lock guards the cache only, never a call to Pinecone, so a slow listing with retries cannot hold up the
        # request threads; two threads refreshing at once just both list
        with self._names_lock:
            fresh = time.monotonic() - self._names_at < self.cache_seconds
            if not refresh and fresh:
                if self._names_error is not None:
                    raise _CachedFailure(f'{self._names_error} (not asked again for {self.cache_seconds}s)')
                if self._names is not None:
                    return set(self._names)

        def listing():
            return {description.name for page in self._index().list_namespaces(prefix=self.namespace_prefix)
                    for description in page.namespaces}

        try:
            names = self._call(f'list the namespaces of {self.index_name}', listing, attempts=None if refresh else 1)
        except _NotFound:
            names = set()  # no index yet, so no namespace
        except VectorStoreError as exc:
            with self._names_lock:
                self._names, self._names_at, self._names_error = None, time.monotonic(), str(exc)
            raise
        with self._names_lock:
            self._names, self._names_at, self._names_error = set(names), time.monotonic(), None
        return set(names)

    def has_namespace(self, tcno, strict=False):
        """
        Whether the tender's namespace exists (complete or not; see manifest()). Unless strict, False also when
        Pinecone is not configured or cannot be reached (logged once per cache period), so listing tenders never
        fails on Pinecone. strict lists the namespaces afresh and raises VectorStoreError instead.
        """
        if not strict and not self.configured:
            return False
        try:
            return self.namespace(tcno) in self.namespaces(refresh=strict)
        except _CachedFailure:
            if strict:
                raise
            return False
        except VectorStoreError as exc:
            if strict:
                raise
            log.warning('Could not tell whether tender %s has a Pinecone namespace: %s', tcno, exc)
            return False

    # ---------- the shared index ----------

    def ensure_index(self):
        """
        Makes sure the shared index exists and is ready, creating it (serverless) on first use, and that its
        dimension matches PINECONE_EMBED_DIMENSION. Returns its name. Raises QuotaExceeded when the project has no
        room for it, VectorStoreError when Pinecone fails or the dimension differs.
        """
        if self._ready:
            return self.index_name
        with self._ready_lock:
            if self._ready:
                return self.index_name
            from pinecone import ServerlessSpec

            name = self.index_name
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,43}[a-z0-9])?', name):
                raise VectorStoreError(f'PINECONE_INDEX_NAME {name!r} is not a valid Pinecone index name (lowercase '
                                       f'letters, digits and hyphens, at most {INDEX_NAME_LIMIT} characters)')
            names = self._call('list the Pinecone indexes', lambda: self.client().list_indexes().names())
            if name not in names:
                try:
                    self._call(f'create the Pinecone index {name}', lambda: self.client().create_index(
                        name=name, dimension=self.dimension, metric=self.metric,
                        spec=ServerlessSpec(cloud=self.cloud, region=self.region), deletion_protection='disabled',
                        tags={'app': 'tender-processing'}, timeout=self.ready_timeout))
                    log.info('Created the shared Pinecone index %s (%d dimensions, %s)', name, self.dimension,
                             self.metric)
                except _Exists:
                    pass
            description = self._wait_ready(name)
            dimension = getattr(description, 'dimension', None)
            if dimension and int(dimension) != self.dimension:
                raise VectorStoreError(f'The Pinecone index {name} has {dimension} dimensions, but '
                                       f'PINECONE_EMBED_DIMENSION is {self.dimension}. Set PINECONE_INDEX_NAME to a '
                                       'new index, or the dimension to match.')
            self._ready = True
            return name

    # ---------- building and deleting a tender's namespace ----------

    def reset_namespace(self, tcno, on_progress=None):
        """
        Makes the tender's namespace empty and marked as being built, ready for ingest(): the shared index is made if
        need be, a namespace holding anything is emptied (and waited on until Pinecone counts it empty), and a
        manifest with status 'building' is written, which creates the namespace. Called before a tender's documents
        are downloaded and read, so a Pinecone plan with no room for another namespace fails the tender before any
        download or OCR is spent. Returns the namespace. Raises QuotaExceeded, or VectorStoreError when Pinecone
        fails.
        """
        report = on_progress or (lambda message: None)
        self.ensure_index()
        namespace = self.namespace(tcno)
        index = self._index()
        if namespace in self.namespaces(refresh=True):
            report(f'Emptying the Pinecone namespace {namespace}')
            # Applied in order before every write that follows, so no old record survives the rebuild; ingest()
            # waits until searches see its last write, so they no longer see these either
            try:
                self._call(f'empty the namespace {namespace}',
                           lambda: index.delete(delete_all=True, namespace=namespace))
            except _NotFound:
                pass
        report(f'Preparing the Pinecone namespace {namespace}')
        self._upsert(namespace, [{'id': MANIFEST_ID, 'values': self._unit(), 'metadata': {
            'record_type': 'manifest', 'tcno': str(tcno), 'status': 'building', 'created_at': _now()}}],
            f'create the namespace {namespace}')
        self._remember(namespace, exists=True)
        return namespace

    def ingest(self, tcno, documents, unreadable=(), on_progress=None):
        """
        Fills the tender's namespace, made ready by reset_namespace(): chunks, embeds and upserts every document
        (chunk_documents(), upsert_chunks()), waits until Pinecone counts every chunk, then rewrites the manifest last,
        as complete, and waits until it can be read back, so a namespace with a complete manifest is always complete.
        unreadable are the sources that could not be read, kept in the manifest so a re-check still knows the check
        is incomplete.

        Returns {'index', 'namespace', 'sources', 'chunks', 'embedding_tokens', 'manifest'}, manifest as manifest()
        reads it (to retrieve from at once, without reading it back). on_progress, if given, is called with a short
        status message as the work goes on. Raises VectorStoreError; the namespace is then left without a complete
        manifest, so it is never trusted and is rebuilt next time.
        """
        report = on_progress or (lambda message: None)
        tcno = str(tcno)
        namespace = self.namespace(tcno)
        started = time.monotonic()
        records, sources = self.chunk_documents(tcno, documents)
        tokens, written = self.upsert_chunks(tcno, records, on_progress=report)
        if records:
            report(f'Waiting until Pinecone can search all {len(records):,} chunks')
            if not self._wait_until_searchable(namespace, written):
                # No log position to wait for: wait for the count instead (the 'building' manifest counts too)
                self._wait_for_count(namespace, lambda count: count >= len(records) + 1, 'to count every chunk')
        manifest = self._write_manifest(tcno, len(records), sources, unreadable)
        log.info('Indexed tender %s in %s/%s: %d source(s), %d chunk(s), %d embedding tokens, %.1fs', tcno,
                 self.index_name, namespace, len(sources), len(records), tokens, time.monotonic() - started)
        return {'index': self.index_name, 'namespace': namespace, 'sources': len(sources), 'chunks': len(records),
                'embedding_tokens': tokens, 'manifest': manifest}

    def rebuild_namespace(self, tcno, documents, unreadable=(), on_progress=None):
        """Builds the tender's namespace from scratch: reset_namespace() then ingest(). Returns what ingest() does."""
        self.reset_namespace(tcno, on_progress=on_progress)
        return self.ingest(tcno, documents, unreadable, on_progress=on_progress)

    def delete_namespace(self, tcno):
        """
        Deletes the tender's namespace and everything in it, and makes sure it stays gone. Returns False when there
        was none.

        Deleting a namespace is not ordered after the writes made to it: a write Pinecone has accepted but not yet
        applied brings the namespace back, with those records, after the delete (seen live, seconds after a build).
        So the namespace is first emptied with delete_all, which is applied in order after every earlier write, and
        only deleted once searches show that every write, the delete_all included, has been applied; then, after
        settle_seconds, it is checked again, and deleted again if it came back.
        """
        namespace = self.namespace(tcno)
        try:
            index = self._index()
        except _NotFound:
            return False  # no shared index, so no namespace
        if self._namespace_count(namespace) is None and namespace not in self.namespaces(refresh=True):
            return False
        for attempt in range(1, 4):
            try:
                self._call(f'empty the namespace {namespace}', lambda: index.delete(delete_all=True,
                                                                                    namespace=namespace))
                # Pinecone gives no log position for a delete, so a marker is written after it: once searches see
                # the marker, the delete and every write before it have been applied, and none is left pending
                marker = self._upsert(namespace, [{'id': '__deleting__', 'values': self._unit(), 'metadata': {
                    'record_type': 'deleting', 'created_at': _now()}}], f'mark the namespace {namespace} deleted')
                if not self._wait_until_searchable(namespace, marker):
                    self._wait_for_count(namespace, lambda count: count <= 1, 'to be counted empty')
                self._call(f'delete the namespace {namespace}', lambda: index.delete_namespace(name=namespace))
            except _NotFound:
                pass
            deadline = time.monotonic() + self.freshness_timeout
            while self._namespace_count(namespace) is not None and time.monotonic() < deadline:
                time.sleep(1)
            time.sleep(self.settle_seconds)
            if self._namespace_count(namespace) is None:
                break
            log.warning('The Pinecone namespace %s came back after it was deleted (attempt %d of 3); deleting it '
                        'again', namespace, attempt)
        else:
            raise VectorStoreError(f'The Pinecone namespace {namespace} keeps coming back after it is deleted')
        self._remember(namespace, exists=False)
        log.info('Deleted the Pinecone namespace %s of tender %s', namespace, tcno)
        return True

    def _write_manifest(self, tcno, chunks, sources, unreadable):
        """
        Upserts the complete manifest (and, when its source list is too long for one record's metadata, the parts
        holding it, first), waits until it can be read back, and returns it as manifest() reads it.
        """
        namespace = self.namespace(tcno)
        # ASCII, so characters are bytes and each part stays under Pinecone's 40 KB of metadata per record
        payload = json.dumps({'sources': sources, 'unreadable': list(unreadable)})
        parts = [payload[start:start + MANIFEST_PART_CHARS] for start in range(0, len(payload), MANIFEST_PART_CHARS)]
        if len(parts) == 1:
            parts = []
        unit = [1.0] + [0.0] * (self.dimension - 1)  # Pinecone refuses all-zero vectors; nothing queries these
        created_at = _now()
        records = [{'id': _part_id(number), 'values': unit, 'metadata': {
            'record_type': 'manifest_part', 'tcno': str(tcno), 'part': number, 'payload': part}}
            for number, part in enumerate(parts)]
        metadata = {'record_type': 'manifest', 'tcno': str(tcno), 'schema_version': SCHEMA_VERSION,
                    'status': 'complete', 'created_at': created_at, 'embed_model': self.embed_model,
                    'dimension': self.dimension, 'chunks': chunks, 'parts': len(parts)}
        if not parts:
            metadata['payload'] = payload
        # The parts first, the manifest alone after them: a complete manifest is never visible without its parts
        batches = [records[start:start + self.upsert_batch] for start in range(0, len(records), self.upsert_batch)]
        written = None
        for batch in batches + [[{'id': MANIFEST_ID, 'values': unit, 'metadata': metadata}]]:
            written = self._upsert(namespace, batch, f'write the manifest of {namespace}')
        if not self._wait_until_searchable(namespace, written):
            self._wait_until_written(namespace, [record['id'] for record in records], created_at)
        return {'index': self.index_name, 'namespace': namespace, 'tcno': str(tcno), 'created_at': created_at,
                'embed_model': self.embed_model, 'schema_version': SCHEMA_VERSION, 'chunks': chunks,
                'sources': sources, 'unreadable': list(unreadable)}

    # ---------- chunking, embedding and upserting ----------

    def chunk_documents(self, tcno, documents):
        """
        The records to store for the tender's documents, and the manifest's list of sources. Each record is {'id',
        'text', 'metadata'}, the metadata {'record_type', 'tcno', 'source_file', 'document_type', 'chunk_id',
        'chunk_index', 'overlap', 'created_at'}. Sources with the same name are kept once (the first).
        """
        created_at = _now()
        records, sources, seen = [], [], set()
        for document in documents:
            if document.name in seen:
                log.warning('Tender %s: source %s is listed twice; indexing it once', tcno, document.name)
                continue
            seen.add(document.name)
            pieces = chunk_text(document.text, self.chunk_size, self.chunk_overlap)
            sources.append({'name': document.name, 'type': document.document_type, 'chunks': len(pieces)})
            for position, (text, overlap) in enumerate(pieces):
                identifier = chunk_id(document.name, position)
                records.append({'id': identifier, 'text': text, 'metadata': {
                    'record_type': 'chunk', 'tcno': str(tcno), 'source_file': document.name,
                    'document_type': document.document_type, 'chunk_id': identifier, 'chunk_index': position,
                    'overlap': overlap, 'created_at': created_at,
                }})
        return records, sources

    def embed(self, texts, input_type='passage'):
        """
        Embeds texts with the hosted embedding model, EMBED_BATCH at a time, in parallel. input_type is 'passage'
        for stored text and 'query' for searches. Returns (vectors in input order, tokens counted).
        """
        batches = [texts[start:start + EMBED_BATCH] for start in range(0, len(texts), EMBED_BATCH)]
        if len(batches) <= 1:
            results = [self._embed_batch(batch, input_type) for batch in batches]
        else:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as pool:
                results = list(pool.map(lambda batch: self._embed_batch(batch, input_type), batches))
        return [vector for vectors, _ in results for vector in vectors], sum(tokens for _, tokens in results)

    def upsert_chunks(self, tcno, records, on_progress=None):
        """
        Embeds and upserts records (from chunk_documents()) into the tender's namespace: EMBED_BATCH records per
        embedding request, upsert_batch vectors per upsert, workers groups at a time. A chunk whose vector is in the
        cache is not embedded again. Returns (the embedding tokens, the highest log position Pinecone committed them
        at, or None when it gave none).
        """
        report = on_progress or (lambda message: None)
        if not records:
            return 0, None
        namespace = self.namespace(tcno)
        groups = [records[start:start + EMBED_BATCH] for start in range(0, len(records), EMBED_BATCH)]

        def store(group):
            vectors, tokens = self._embed_passages([record['text'] for record in group])
            payload = [{'id': record['id'], 'values': vector, 'metadata': {**record['metadata'], 'text': record['text']}}
                       for record, vector in zip(group, vectors)]
            positions = [self._upsert(namespace, payload[start:start + self.upsert_batch],
                                      f'upsert {len(payload[start:start + self.upsert_batch])} chunk(s) into {namespace}')
                         for start in range(0, len(payload), self.upsert_batch)]
            return len(group), tokens, _highest(positions)

        done = tokens = 0
        written = []
        report(f'Embedding and indexing {len(records):,} chunks in {namespace}')
        with ThreadPoolExecutor(max_workers=min(self.workers, len(groups))) as pool:
            futures = [pool.submit(store, group) for group in groups]
            try:
                for future in as_completed(futures):
                    count, used, position = future.result()
                    done += count
                    tokens += used
                    written.append(position)
                    report(f'Indexed {done:,} of {len(records):,} chunks in {namespace}')
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        return tokens, _highest(written)

    # ---------- reading ----------

    def manifest(self, tcno):
        """
        The tender's complete manifest: {'index', 'namespace', 'tcno', 'created_at', 'embed_model', 'chunks',
        'sources' ([{'name', 'type', 'chunks'}]), 'unreadable'}; None when the tender has no namespace, or one being
        built or left half-built. Raises VectorStoreError when Pinecone cannot be reached.
        """
        namespace = self.namespace(tcno)
        if namespace not in self.namespaces():
            return None
        try:
            found = self._fetch_records(namespace, [MANIFEST_ID])
        except _NotFound:
            self._remember(namespace, exists=False)
            return None
        record = found.get(MANIFEST_ID)
        if record is None:
            return None
        metadata = dict(record.metadata or {})
        if metadata.get('status') != 'complete':
            return None
        payload = metadata.get('payload')
        parts = int(metadata.get('parts') or 0)
        if parts:
            ids = [_part_id(number) for number in range(parts)]
            found = self._fetch_records(namespace, ids)
            if any(identifier not in found for identifier in ids):
                log.warning('The manifest of %s is missing some of its %d parts; treating it as incomplete',
                            namespace, parts)
                return None
            payload = ''.join((found[identifier].metadata or {}).get('payload', '') for identifier in ids)
        content = json.loads(payload or '{}')
        return {'index': self.index_name, 'namespace': namespace, 'tcno': metadata.get('tcno', str(tcno)),
                'created_at': metadata.get('created_at'), 'embed_model': metadata.get('embed_model'),
                'schema_version': metadata.get('schema_version'), 'chunks': int(metadata.get('chunks') or 0),
                'sources': content.get('sources') or [], 'unreadable': content.get('unreadable') or []}

    def status(self, tcno):
        """
        What the tender's namespace holds, for the API: {'tcno', 'index', 'namespace', 'exists', 'complete',
        'chunks', 'sources', 'unreadable', 'created_at', 'embed_model', 'vector_count'}.
        """
        namespace = self.namespace(tcno)
        state = {'tcno': str(tcno), 'index': self.index_name, 'namespace': namespace,
                 'exists': self.has_namespace(tcno, strict=True), 'complete': False, 'chunks': None, 'sources': [],
                 'unreadable': [], 'created_at': None, 'embed_model': None, 'vector_count': None}
        if not state['exists']:
            return state
        manifest = self.manifest(tcno)
        state['vector_count'] = self._namespace_count(namespace)
        if manifest:
            state.update(complete=True, chunks=manifest['chunks'], sources=manifest['sources'],
                         unreadable=manifest['unreadable'], created_at=manifest['created_at'],
                         embed_model=manifest['embed_model'])
        return state

    def query(self, tcno, vector, top_k=None, filter=None):
        """
        The chunks of the tender's namespace nearest to vector (from embed(..., 'query')), best first, as chunk dicts
        (see Retrieval) with their score. filter narrows them further by metadata.
        """
        namespace = self.namespace(tcno)
        conditions = {**_CHUNK_FILTER, **(filter or {})}
        response = self._call(f'query {namespace}', lambda: self._index().query(
            vector=vector, top_k=top_k or self.top_k, namespace=namespace, filter=conditions, include_metadata=True))
        return [_chunk(match.metadata or {}, match.id, match.score) for match in response.matches or []]

    def retrieve(self, tcno, queries, top_k=None, max_chars=None, manifest=None):
        """
        The chunks of the tender's namespace that answer queries, {label: [query text, ...]}, within max_chars of
        text. Every chunk of the short TenderTiger sources (PINNED_TYPES) comes first; then each query's top_k matches
        are taken in turn, best first, one query after another, so every query gets its share before the budget runs
        out. A chunk found by several queries is taken once, with each label in its 'queries'.

        manifest, if given, is the one ingest() just wrote, so it is not read back (Pinecone serverless makes new
        records visible a few seconds after they are written).

        Returns a Retrieval with the chunks in document order. Raises NamespaceIncomplete when the tender has no
        complete namespace, VectorStoreError when Pinecone fails.
        """
        tcno = str(tcno)
        budget = max_chars or self.max_chars
        manifest = manifest or self.manifest(tcno)
        if manifest is None:
            raise NamespaceIncomplete(f'Tender {tcno} has no complete Pinecone namespace ({self.namespace(tcno)}): '
                                      'its documents must be indexed again.')
        retrieval = Retrieval(namespace=manifest['namespace'], manifest=manifest)
        if not manifest['chunks']:
            return retrieval

        pinned_ids = [chunk_id(source['name'], position) for source in manifest['sources']
                      if source['type'] in PINNED_TYPES for position in range(source['chunks'])]
        pinned = [{**chunk, 'score': 1.0, 'queries': ['pinned']} for chunk in self._fetch(tcno, pinned_ids)]

        labelled = [(label, text) for label, texts in queries.items() for text in texts if text and text.strip()]
        vectors, _ = self.embed([text for _, text in labelled], 'query') if labelled else ([], 0)
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(vectors)))) as pool:
            matches = list(pool.map(lambda vector: self.query(tcno, vector, top_k=top_k), vectors))

        chosen, used = {}, 0
        for chunk in pinned:
            chosen[chunk['id']] = chunk
            used += len(chunk['text'])
        for label, match in _round_robin(labelled, matches):
            if match['id'] in chosen:
                taken = chosen[match['id']]
                taken['score'] = max(taken['score'], match['score'])
                if label not in taken['queries']:
                    taken['queries'].append(label)
                continue
            if used + len(match['text']) > budget:
                continue
            chosen[match['id']] = {**match, 'queries': [label]}
            used += len(match['text'])

        order = {source['name']: position for position, source in enumerate(manifest['sources'])}
        retrieval.chunks = sorted(chosen.values(),
                                  key=lambda chunk: (order.get(chunk['source_file'], len(order)), chunk['chunk_index']))
        log.info('Retrieved %d chunk(s), %d characters, from %s for %d quer(ies)', len(retrieval.chunks),
                 retrieval.characters, manifest['namespace'], len(labelled))
        return retrieval

    def knowledge(self, tcno):
        """
        Everything the tender's namespace holds: a Retrieval of every chunk, in document order, fetched by the IDs
        the manifest gives. Raises NamespaceIncomplete without a complete namespace.
        """
        manifest = self.manifest(tcno)
        if manifest is None:
            raise NamespaceIncomplete(f'Tender {tcno} has no complete Pinecone namespace ({self.namespace(tcno)}).')
        ids = [chunk_id(source['name'], position) for source in manifest['sources']
               for position in range(source['chunks'])]
        chunks = self._fetch(tcno, ids)
        if len(chunks) < len(ids):
            log.warning('The Pinecone namespace %s returned %d of its %d chunks', manifest['namespace'], len(chunks),
                        len(ids))
        order = {source['name']: position for position, source in enumerate(manifest['sources'])}
        chunks.sort(key=lambda chunk: (order.get(chunk['source_file'], len(order)), chunk['chunk_index']))
        return Retrieval(namespace=manifest['namespace'], manifest=manifest, chunks=chunks)

    def first_chunk(self, tcno, name):
        """
        The text of the first chunk of the source called name in the tender's namespace, or None when it holds no
        such chunk. One fetch by ID: no search, no embedding. Raises VectorStoreError when Pinecone fails.
        """
        chunks = self._fetch(tcno, [chunk_id(name, 0)])
        return chunks[0]['text'] if chunks else None

    # ---------- internals ----------

    def _fetch(self, tcno, ids):
        """The chunks with these IDs, as chunk dicts with score 0; IDs not found are left out."""
        found = self._fetch_records(self.namespace(tcno), ids)
        return [_chunk(found[identifier].metadata or {}, identifier, 0.0) for identifier in ids if identifier in found]

    def _fetch_records(self, namespace, ids):
        """{id: record} of the records with these IDs in namespace, FETCH_BATCH per request."""
        found = {}
        index = self._index()
        for start in range(0, len(ids), FETCH_BATCH):
            part = ids[start:start + FETCH_BATCH]
            response = self._call(f'fetch {len(part)} record(s) from {namespace}',
                                  lambda part=part: index.fetch(ids=part, namespace=namespace))
            found.update(response.vectors or {})
        return found

    def _unit(self):
        """A vector for records nothing searches for (Pinecone refuses all-zero vectors)."""
        return [1.0] + [0.0] * (self.dimension - 1)

    def _upsert(self, namespace, vectors, what):
        """Upserts vectors into namespace. Returns the log position Pinecone committed them at, or None."""
        response = self._call(what, lambda: self._index().upsert(vectors=vectors, namespace=namespace,
                                                                 show_progress=False))
        position = getattr(getattr(response, 'response_info', None), 'lsn_committed', None)
        return int(position) if position is not None else None

    def _wait_until_searchable(self, namespace, position):
        """
        Waits until searches of namespace see every write up to log position (an upsert's lsn_committed): Pinecone's
        read-your-writes check, as a search reports how far the namespace has been indexed. Returns True once they
        do; False at once when position is None (unknown), or after freshness_timeout seconds (logged).
        """
        if position is None:
            return False
        deadline = time.monotonic() + self.freshness_timeout
        delay = 0.25
        while True:
            response = self._call(f'search {namespace}', lambda: self._index().query(
                vector=self._unit(), top_k=1, namespace=namespace))
            info = getattr(response, 'response_info', None)
            if info is not None and info.is_reconciled(position):
                return True
            if time.monotonic() > deadline:
                log.warning('Searches of the Pinecone namespace %s had not caught up with write %d after %ds',
                            namespace, position, self.freshness_timeout)
                return False
            time.sleep(delay)
            delay = min(delay * 2, 5)

    def _namespace_count(self, namespace):
        """The records Pinecone counts in namespace (eventually consistent), or None when it does not exist."""
        try:
            description = self._call(f'describe the namespace {namespace}',
                                     lambda: self._index().describe_namespace(name=namespace))
        except _NotFound:
            return None
        return int(getattr(description, 'record_count', 0) or 0)

    def _wait_for_count(self, namespace, done, what):
        """
        Waits until done(count) is true of the namespace's record count, as Pinecone serverless counts writes a few
        seconds after they are made. Gives up with a warning after freshness_timeout seconds: the data is written,
        only its search lags, so retrieval may then miss a few chunks for a while.
        """
        deadline = time.monotonic() + self.freshness_timeout
        delay = 0.5
        while True:
            count = self._namespace_count(namespace) or 0
            if done(count):
                return
            if time.monotonic() > deadline:
                log.warning('Gave up waiting for the Pinecone namespace %s %s after %ds (it counts %d)', namespace,
                            what, self.freshness_timeout, count)
                return
            time.sleep(delay)
            delay = min(delay * 2, 5)

    def _wait_until_written(self, namespace, part_ids, created_at):
        """Waits, up to freshness_timeout seconds, until the manifest written at created_at can be read back."""
        deadline = time.monotonic() + self.freshness_timeout
        delay = 0.5
        while True:
            found = self._fetch_records(namespace, part_ids + [MANIFEST_ID])
            manifest = (getattr(found.get(MANIFEST_ID), 'metadata', None) or {})
            if all(identifier in found for identifier in part_ids) and manifest.get('created_at') == created_at:
                return
            if time.monotonic() > deadline:
                log.warning('The manifest of %s could not be read back after %ds; a re-check soon after may rebuild '
                            'the namespace', namespace, self.freshness_timeout)
                return
            time.sleep(delay)
            delay = min(delay * 2, 5)

    def _embed_passages(self, texts):
        """
        _embed_batch() of the passages texts, taking those already embedded from the cache and adding the rest to
        it. Returns (vectors in input order, tokens of the passages embedded now).
        """
        if self.cache is None:
            return self._embed_batch(texts, 'passage')
        model = f'{self.embed_model}/{self.dimension}'
        vectors = self.cache.vectors(model, texts)
        missing = [number for number, vector in enumerate(vectors) if vector is None]
        tokens = 0
        if missing:
            fresh, tokens = self._embed_batch([texts[number] for number in missing], 'passage')
            for number, vector in zip(missing, fresh):
                vectors[number] = vector
            self.cache.save_vectors(model, [texts[number] for number in missing], fresh)
        return vectors, tokens

    def _embed_batch(self, texts, input_type):
        parameters = {'input_type': input_type, 'truncate': 'END'}
        if self.embed_model.startswith('llama-text-embed'):
            parameters['dimension'] = self.dimension  # the only hosted model whose dimension can be chosen
        response = self._call(f'embed {len(texts)} text(s) with {self.embed_model}', lambda: self.client().inference.embed(
            model=self.embed_model, inputs=list(texts), parameters=parameters))
        vectors = [list(item.values) for item in response]
        if len(vectors) != len(texts):
            raise VectorStoreError(f'{self.embed_model} returned {len(vectors)} embeddings for {len(texts)} texts')
        usage = getattr(response, 'usage', None)
        return vectors, int(getattr(usage, 'total_tokens', 0) or 0)

    def _index(self):
        """The (cached) handle of the shared index. Raises _NotFound when it does not exist."""
        with self._handle_lock:
            handle = self._handle
        if handle is None:
            try:
                handle = self._call(f'open the Pinecone index {self.index_name}',
                                    lambda: self.client().Index(name=self.index_name))
            except _NotFound:
                self._ready = False  # deleted behind the app's back: ensure_index() makes it again
                raise
            with self._handle_lock:
                self._handle = self._handle or handle
                handle = self._handle
        return handle

    def _remember(self, namespace, exists):
        """Keeps the cached namespace names in step with a namespace created or deleted here."""
        with self._names_lock:
            if self._names is not None:
                (self._names.add if exists else self._names.discard)(namespace)

    def _wait_ready(self, name):
        """Waits until the index called name is ready, up to ready_timeout seconds. Returns its description."""
        deadline = time.monotonic() + self.ready_timeout
        while True:
            description = self._call(f'describe the Pinecone index {name}', lambda: self.client().describe_index(name))
            status = getattr(description, 'status', None)
            ready = status.get('ready') if isinstance(status, dict) else getattr(status, 'ready', True)
            if ready:
                return description
            if time.monotonic() > deadline:
                raise VectorStoreError(f'The Pinecone index {name} was not ready after {self.ready_timeout} seconds')
            time.sleep(2)

    def _call(self, what, run, attempts=None):
        """
        run(), retried with exponential backoff and jitter on rate limits, server errors, timeouts and network
        errors. Pinecone errors become VectorStoreError (with what in the message): QuotaExceeded for a plan limit,
        _NotFound for 404, _Exists for 409.
        """
        attempts = attempts or self.attempts
        for attempt in range(1, attempts + 1):
            try:
                return run()
            except VectorStoreError:
                raise
            except Exception as exc:
                kind = _classify(exc)
                if kind == 'not_found':
                    raise _NotFound(f'Could not {what}: not found ({exc})') from exc
                if kind == 'conflict':
                    raise _Exists(f'Could not {what}: it already exists ({exc})') from exc
                if kind == 'quota':
                    raise QuotaExceeded(
                        f"Could not {what}: the Pinecone plan's limit is reached (namespaces per index, or indexes "
                        f'per project). Delete the namespaces of tenders no longer needed, or move to a larger '
                        f'plan. ({exc})') from exc
                if kind == 'auth':
                    raise VectorStoreError(f'Could not {what}: Pinecone refused the API key ({exc})') from exc
                if kind != 'transient' or attempt == attempts:
                    raise VectorStoreError(f'Could not {what}: {exc}') from exc
                delay = min(30.0, 2.0 ** attempt) * random.uniform(0.5, 1.0)
                log.warning('Could not %s (attempt %d of %d): %s; retrying in %.1fs', what, attempt, attempts, exc,
                            delay)
                time.sleep(delay)


def _classify(exc):
    """What a Pinecone (or network) error means for a retry: not_found, conflict, quota, auth, transient or fatal."""
    status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
    text = f"{exc} {getattr(exc, 'error_code', '') or ''}".upper()
    if status == 404:
        return 'not_found'
    if status == 409:
        return 'conflict'
    # Before the quota check: a 429 is a rate limit (even one that speaks of a token quota), so it is retried
    if status in (408, 429) or (isinstance(status, int) and status >= 500):
        return 'transient'
    if status == 402:
        return 'quota'
    if status == 403:
        if 'PROTECT' in text:
            return 'fatal'  # deletion protection
        # Pinecone answers 403 QUOTA_EXCEEDED past a plan's object limits, with a message such as "You've reached
        # the max ... allowed"
        if any(word in text for word in ('QUOTA', 'LIMIT', 'MAX', 'REACHED', 'EXCEED', 'UPGRADE')):
            return 'quota'
        return 'auth'
    if status == 401:
        return 'auth'
    if isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__name__ in (
            'PineconeConnectionError', 'PineconeTimeoutError', 'ConnectError', 'ReadTimeout', 'RemoteProtocolError'):
        return 'transient'
    return 'fatal'


def _highest(positions):
    """The highest of these log positions, or None when any is unknown (then nothing can be waited for)."""
    return None if not positions or any(position is None for position in positions) else max(positions)


def _part_id(number):
    """The ID of a part of a long manifest."""
    return f'{MANIFEST_ID}-{number:03d}'


def _round_robin(labelled, matches):
    """(label, match) pairs: every query's best match, then every query's second best, and so on."""
    depth = max((len(found) for found in matches), default=0)
    for rank in range(depth):
        for (label, _), found in zip(labelled, matches):
            if rank < len(found):
                yield label, found[rank]


def _chunk(metadata, identifier, score):
    """A chunk record's metadata as the chunk dict Retrieval holds."""
    return {'id': identifier, 'source_file': metadata.get('source_file', ''),
            'document_type': metadata.get('document_type', 'other'), 'chunk_index': int(metadata.get('chunk_index') or 0),
            'overlap': int(metadata.get('overlap') or 0), 'text': metadata.get('text', ''), 'score': float(score or 0.0),
            'queries': []}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')
