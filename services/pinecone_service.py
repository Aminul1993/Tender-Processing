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

Regenerating keeps what has not changed: reset_namespace(keep=True) leaves the chunks of a complete namespace in
place and marks its manifest 'building' and kept, and ingest() then compares each source's SHA-256 (of its text and
everything that shapes its records) with the one the manifest lists, upserts only the sources that changed or are
new, and deletes the chunks of the sources gone or grown shorter. Before its first change it marks the manifest
'building' without kept, so a build that fails half-way leaves a namespace that is never kept, only rebuilt.

Retrieval (retrieve()) is hybrid, hierarchical and adaptive:
  hybrid        on an index with the dotproduct metric, each chunk also holds a sparse vector of its words and
                figures (sparse_encoder.py), and a search scores dense and sparse together, so clause numbers and
                rupee figures match exactly; dense alone on a cosine index
  hierarchical  each chunk knows the chunks of the document sections it is in (document_sections.py): the searches
                for the qualification section vote for the section their best matches are in, and that section is
                taken whole, before the other searches fill what budget is left
  adaptive      a search keeps its matches only down to a score floor, a share of its best match's score, instead
                of always its top_k

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
import math
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services import sparse_encoder
from services.document_sections import VERSION as SECTIONS_VERSION, chunk_sections

log = logging.getLogger(__name__)

# The ID of the record that marks a namespace as complete and describes what it holds
MANIFEST_ID = '__manifest__'
# Bumped when the records change shape; a namespace of another version is rebuilt by the migration script, and its
# sources are all upserted again by a regenerate. 3: chunks hold their sections, and sparse values on a dotproduct
# index; sources their SHA-256.
SCHEMA_VERSION = 3
# The most inputs Pinecone's hosted embedding models take in one request
EMBED_BATCH = 96
# IDs fetched per request when reading chunks back by ID, and deleted per request
FETCH_BATCH = 100
DELETE_BATCH = 1000
# A search keeps at least this many matches, whatever their score, so a score floor never leaves a kind of
# requirement with nothing
MIN_MATCHES = 2
# The most sections one retrieval takes whole
MAX_SECTIONS = 2
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
    position), each {'id', 'source_file', 'document_type', 'chunk_index', 'overlap', 'text', 'score', 'queries',
    'section', 'sections', 'section_titles'}, where queries are the labels of the queries that found it and score
    the best of their scores (1.0 for a pinned chunk), section the title of the document section it is in, and
    sections and section_titles the chunk ranges and titles of the sections it is in (see document_sections.py).
    whole_sections are the sections taken whole, each {'source_file', 'title', 'chunks' ('first-last'), 'label'}.
    """
    namespace: str
    manifest: dict
    chunks: list = field(default_factory=list)
    whole_sections: list = field(default_factory=list)

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
    text = text.strip()
    return [text[start:end] for start, end in _piece_spans(text, max_chars)]


def _piece_spans(text, max_chars):
    """split_pieces() of text, already stripped, as the (start, end) of each piece in it."""
    spans, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            for separator in ('\n\n', '\n', ' '):
                cut = text.rfind(separator, start + max_chars // 2, end)
                if cut > start:
                    end = cut
                    break
        piece = text[start:end]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            spans.append((start + lead, start + lead + len(piece.strip())))
        start = end
    return spans


def chunk_text(text, size=1600, overlap=200):
    """
    Splits text into chunks of about size characters (see split_pieces()). Each chunk after the first starts with
    the last overlap characters of the one before (from a word boundary), so a criterion split across two chunks is
    whole in one of them. Returns [(chunk, overlap)], where overlap is the number of leading characters the chunk
    repeats from the previous one.
    """
    return [(chunk, repeated) for chunk, repeated, _ in _chunks((text or '').strip(), size, overlap)]


def _chunks(text, size, overlap):
    """chunk_text() of text, already stripped, with the (start, end) in text of each chunk's own piece."""
    overlap = max(0, min(overlap, size // 2))
    spans = _piece_spans(text, max(1, size - overlap))
    chunks = []
    for number, (start, end) in enumerate(spans):
        tail = ''
        if number and overlap:
            tail = text[spans[number - 1][0]:spans[number - 1][1]][-overlap:]
            space = tail.find(' ')
            if 0 <= space < len(tail) - 1:
                tail = tail[space + 1:]
            tail += '\n'
        chunks.append((tail + text[start:end], len(tail), (start, end)))
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
                 region='us-east-1', embed_model='llama-text-embed-v2', dimension=1024, metric='dotproduct',
                 chunk_size=1600, chunk_overlap=200, top_k=8, max_chars=60_000, hybrid_alpha=0.75, score_floor=0.85,
                 min_score=0.2, section_share=0.5, upsert_batch=50, workers=4, attempts=4, ready_timeout=300,
                 freshness_timeout=120, cache_seconds=30, settle_seconds=5, client=None, cache=None):
        self._api_key = api_key
        # A cache_service.Cache of chunk vectors, so a chunk embedded once is never embedded again; None embeds all
        self.cache = cache
        self.index_name = index_name
        self.namespace_prefix = namespace_prefix
        self.cloud = cloud
        self.region = region
        self.embed_model = embed_model
        self.dimension = dimension
        # The metric of an index made here. dotproduct scores the (unit length) dense vectors as cosine does, and
        # also takes sparse values, for hybrid retrieval; an existing index keeps the metric it was made with.
        self.metric = metric
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.top_k = top_k
        self.max_chars = max_chars
        # The weight of the dense score in a hybrid search, the sparse one weighing the rest; 1 is dense alone
        self.hybrid_alpha = min(1.0, max(0.0, hybrid_alpha))
        # A search keeps its matches down to score_floor times its best match's score, and none under min_score (on
        # the dense scale: times hybrid_alpha in a hybrid search), MIN_MATCHES whatever their score; 0 turns either off
        self.score_floor = max(0.0, score_floor)
        self.min_score = max(0.0, min_score)
        # The most of max_chars the sections taken whole may fill; 0 takes none
        self.section_share = min(1.0, max(0.0, section_share))
        self._sparse = False  # the shared index takes sparse values (its metric is dotproduct); see ensure_index()
        self._metric_warned = False
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
        self._names_version = 0  # counts the namespaces created or deleted here, so a listing begun before one is not kept
        self._names_refreshing = False  # a listing runs in the background (see namespaces())
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
                   dimension=config.PINECONE_EMBED_DIMENSION, metric=config.PINECONE_METRIC,
                   chunk_size=config.CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP, top_k=config.RETRIEVAL_TOP_K,
                   max_chars=config.RETRIEVAL_MAX_CHARS, hybrid_alpha=config.RETRIEVAL_HYBRID_ALPHA,
                   score_floor=config.RETRIEVAL_SCORE_FLOOR, min_score=config.RETRIEVAL_MIN_SCORE,
                   section_share=config.RETRIEVAL_SECTION_SHARE, upsert_batch=config.PINECONE_UPSERT_BATCH,
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
        The tender namespaces (those starting with the prefix) in the index, listed at most every cache_seconds. Once
        the last list is older, it is still given at once while a fresh one is listed in the background (a listing
        takes a second or more), so the tender lists never wait on Pinecone after the first; refresh lists them now
        and waits. A failed listing is not retried until cache_seconds have passed, so a page of 500 tenders makes one
        call, not 500. Raises VectorStoreError when Pinecone cannot be reached and there is no list to give.
        """
        # The lock guards the cache only, never a call to Pinecone, so a slow listing with retries cannot hold up the
        # request threads; two threads refreshing at once just both list
        with self._names_lock:
            stale = time.monotonic() - self._names_at >= self.cache_seconds
            if not refresh and self._names is not None:
                if stale and not self._names_refreshing:
                    self._names_refreshing = True
                    threading.Thread(target=self._list_in_background, name='pinecone-namespaces', daemon=True).start()
                return set(self._names)
            if not refresh and not stale and self._names_error is not None:
                raise _CachedFailure(f'{self._names_error} (not asked again for {self.cache_seconds}s)')
        try:
            return self._list_names(attempts=None if refresh else 1)
        except VectorStoreError as exc:
            with self._names_lock:
                self._names, self._names_at, self._names_error = None, time.monotonic(), str(exc)
            raise

    def _list_names(self, attempts):
        """Lists the namespaces from Pinecone and keeps them, unless one was created or deleted here meanwhile."""
        with self._names_lock:
            version = self._names_version

        def listing():
            return {description.name for page in self._index().list_namespaces(prefix=self.namespace_prefix)
                    for description in page.namespaces}

        try:
            names = self._call(f'list the namespaces of {self.index_name}', listing, attempts=attempts)
        except _NotFound:
            names = set()  # no index yet, so no namespace
        with self._names_lock:
            # A namespace created or deleted here during the listing may be missing from it: the next call lists again
            if self._names_version == version:
                self._names, self._names_at, self._names_error = set(names), time.monotonic(), None
        return set(names)

    def _list_in_background(self):
        """namespaces()'s background listing. A failure keeps the last list for another cache_seconds."""
        try:
            self._list_names(attempts=1)
        except VectorStoreError as exc:
            log.warning('Could not list the Pinecone namespaces again, so the last list stands a while: %s', exc)
            with self._names_lock:
                self._names_at = time.monotonic()
        finally:
            with self._names_lock:
                self._names_refreshing = False

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
            metric = getattr(description, 'metric', None)
            metric = str(getattr(metric, 'value', metric) or '').lower()
            self._sparse = metric == 'dotproduct'
            if not self._sparse and self.hybrid_alpha < 1 and not self._metric_warned:
                self._metric_warned = True
                log.warning('The Pinecone index %s uses the %s metric, so retrieval is dense only: hybrid retrieval '
                            '(clause numbers and rupee figures matched exactly) needs the dotproduct metric. Set '
                            'PINECONE_INDEX_NAME to a new index: each tender is indexed into it when next checked, '
                            'its files and chunks in the cache not read or embedded again.', name,
                            metric or 'unknown')
            self._ready = True
            return name

    # ---------- building and deleting a tender's namespace ----------

    def reset_namespace(self, tcno, on_progress=None, keep=False):
        """
        Makes the tender's namespace ready for ingest(), marked as being built: the shared index is made if need be,
        a namespace holding anything is emptied, and a manifest with status 'building' is written, which creates the
        namespace. Called before a tender's documents are downloaded and read, so a Pinecone plan with no room for
        another namespace fails the tender before any download or OCR is spent.

        keep leaves a namespace that ingest() can add to in place instead of emptying it (one with a complete
        manifest, built with this embedding model and schema, or one this kept already and nothing has changed
        since): its manifest is rewritten as building and kept, so ingest() upserts only the sources that changed.

        The manifest is waited on until searches see it, so ingest() reads this one back, never the one before.
        Returns the namespace. Raises QuotaExceeded, or VectorStoreError when Pinecone fails.
        """
        report = on_progress or (lambda message: None)
        self.ensure_index()
        namespace = self.namespace(tcno)
        index = self._index()
        manifest = {'record_type': 'manifest', 'tcno': str(tcno), 'status': 'building', 'created_at': _now()}
        if namespace in self.namespaces(refresh=True):
            kept = self._reusable(namespace) if keep else None
            if kept:
                report(f'Keeping the unchanged sources of the Pinecone namespace {namespace}')
                manifest = {**kept[0], 'status': 'building', 'kept': True, 'created_at': manifest['created_at']}
            else:
                report(f'Emptying the Pinecone namespace {namespace}')
                # Applied in order before every write that follows, so no old record survives the rebuild; ingest()
                # waits until searches see its last write, so they no longer see these either
                try:
                    self._call(f'empty the namespace {namespace}',
                               lambda: index.delete(delete_all=True, namespace=namespace))
                except _NotFound:
                    pass
        report(f'Preparing the Pinecone namespace {namespace}')
        written = self._upsert(namespace, [{'id': MANIFEST_ID, 'values': self._unit(), 'metadata': manifest}],
                               f'create the namespace {namespace}')
        self._remember(namespace, exists=True)
        self._wait_until_searchable(namespace, written)
        return namespace

    def ingest(self, tcno, documents, unreadable=(), on_progress=None):
        """
        Fills the tender's namespace, made ready by reset_namespace(): chunks, embeds and upserts every document
        (chunk_documents(), upsert_chunks()), waits until Pinecone counts every chunk, then rewrites the manifest last,
        as complete, and waits until it can be read back, so a namespace with a complete manifest is always complete.
        unreadable are the sources that could not be read, kept in the manifest so a re-check still knows the check
        is incomplete.

        In a namespace reset_namespace(keep=True) kept, a source with the same name and SHA-256 as before is left as
        it is (its chunks are not upserted again), and the chunks no source has any more are deleted. The manifest is
        first marked building and no longer kept, so a failure part-way leaves a namespace that is rebuilt from
        scratch next time.

        Returns {'index', 'namespace', 'sources', 'chunks', 'upserted', 'kept_sources', 'deleted',
        'embedding_tokens', 'manifest'}: the chunks upserted now, the sources left as they were and the chunks
        deleted, and the manifest as manifest() reads it (to retrieve from at once, without reading it back).
        on_progress, if given, is called with a short status message as the work goes on. Raises VectorStoreError;
        the namespace is then left without a complete manifest, so it is never trusted and is rebuilt next time.
        """
        report = on_progress or (lambda message: None)
        tcno = str(tcno)
        namespace = self.namespace(tcno)
        started = time.monotonic()
        self.ensure_index()  # knows whether the index takes sparse values
        records, sources = self.chunk_documents(tcno, documents)
        kept, content = self._reusable(namespace, kept_only=True) or ({}, {})
        before = {source['name']: source for source in content.get('sources') or []}
        unchanged = {source['name'] for source in sources
                     if (before.get(source['name']) or {}).get('sha256') == source['sha256']
                     and before[source['name']].get('chunks') == source['chunks']}
        counts = {source['name']: source['chunks'] for source in sources}
        stale = [chunk_id(name, position) for name, source in before.items()
                 for position in range(counts.get(name, 0), int(source.get('chunks') or 0))]
        fresh = [record for record in records if record['metadata']['source_file'] not in unchanged]
        if kept and (fresh or stale):
            # The kept chunks no longer match the old manifest from here: a failure now must not keep them
            self._upsert(namespace, [{'id': MANIFEST_ID, 'values': self._unit(), 'metadata': {
                'record_type': 'manifest', 'tcno': tcno, 'status': 'building', 'created_at': _now()}}],
                f'mark the namespace {namespace} as being rebuilt')
        if unchanged:
            report(f'Keeping {len(unchanged)} unchanged source(s) in {namespace}')
        if stale:
            report(f'Deleting {len(stale):,} chunk(s) of sources gone or shorter from {namespace}')
            self._delete_ids(namespace, stale)
        tokens, written = self.upsert_chunks(tcno, fresh, on_progress=report)
        if fresh:
            report(f'Waiting until Pinecone can search all {len(fresh):,} chunks')
            if not self._wait_until_searchable(namespace, written):
                # No log position to wait for: wait for the count instead (the 'building' manifest counts too)
                self._wait_for_count(namespace, lambda count: count >= len(records) + 1, 'to count every chunk')
        manifest = self._write_manifest(tcno, len(records), sources, unreadable, int(kept.get('parts') or 0))
        log.info('Indexed tender %s in %s/%s: %d source(s), %d chunk(s); %d upserted, %d source(s) kept unchanged, '
                 '%d deleted; %d embedding tokens, %.1fs', tcno, self.index_name, namespace, len(sources),
                 len(records), len(fresh), len(unchanged), len(stale), tokens, time.monotonic() - started)
        return {'index': self.index_name, 'namespace': namespace, 'sources': len(sources), 'chunks': len(records),
                'upserted': len(fresh), 'kept_sources': len(unchanged), 'deleted': len(stale),
                'embedding_tokens': tokens, 'manifest': manifest}

    def rebuild_namespace(self, tcno, documents, unreadable=(), on_progress=None, keep=False):
        """
        Builds the tender's namespace: reset_namespace() then ingest(). Returns what ingest() does. keep upserts only
        the sources that changed (see reset_namespace()); otherwise it is built from scratch.
        """
        self.reset_namespace(tcno, on_progress=on_progress, keep=keep)
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

    def _write_manifest(self, tcno, chunks, sources, unreadable, old_parts=0):
        """
        Upserts the complete manifest (and, when its source list is too long for one record's metadata, the parts
        holding it, first), waits until it can be read back, and returns it as manifest() reads it. old_parts is how
        many parts the namespace's last manifest had: those past the new ones are deleted.
        """
        namespace = self.namespace(tcno)
        # ASCII, so characters are bytes and each part stays under Pinecone's 40 KB of metadata per record
        payload = json.dumps({'sources': sources, 'unreadable': list(unreadable)})
        parts = [payload[start:start + MANIFEST_PART_CHARS] for start in range(0, len(payload), MANIFEST_PART_CHARS)]
        if len(parts) == 1:
            parts = []
        if old_parts > len(parts):
            self._delete_ids(namespace, [_part_id(number) for number in range(len(parts), old_parts)])
        unit = [1.0] + [0.0] * (self.dimension - 1)  # Pinecone refuses all-zero vectors; nothing queries these
        created_at = _now()
        records = [{'id': _part_id(number), 'values': unit, 'metadata': {
            'record_type': 'manifest_part', 'tcno': str(tcno), 'part': number, 'payload': part}}
            for number, part in enumerate(parts)]
        metadata = {'record_type': 'manifest', 'tcno': str(tcno), 'schema_version': SCHEMA_VERSION,
                    'status': 'complete', 'created_at': created_at, 'embed_model': self.embed_model,
                    'dimension': self.dimension, 'chunks': chunks, 'parts': len(parts),
                    'chunk_size': self.chunk_size, 'chunk_overlap': self.chunk_overlap, 'hybrid': self._sparse}
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
                'chunk_size': self.chunk_size, 'hybrid': self._sparse, 'sources': sources,
                'unreadable': list(unreadable)}

    # ---------- chunking, embedding and upserting ----------

    def chunk_documents(self, tcno, documents):
        """
        The records to store for the tender's documents, and the manifest's list of sources ({'name', 'type',
        'chunks', 'sha256'}, sha256 of the source's text and of everything else its records depend on, so a source
        with the same name and SHA-256 has the same records). Each record is {'id', 'text', 'metadata'}, the
        metadata {'record_type', 'tcno', 'source_file', 'document_type', 'chunk_id', 'chunk_index', 'overlap',
        'created_at'}, with 'section', 'sections' and 'section_titles' when the chunk is in a section of the
        document (see document_sections.chunk_sections()). Sources with the same name are kept once (the first).
        """
        created_at = _now()
        records, sources, seen = [], [], set()
        for document in documents:
            if document.name in seen:
                log.warning('Tender %s: source %s is listed twice; indexing it once', tcno, document.name)
                continue
            seen.add(document.name)
            text = (document.text or '').strip()
            pieces = _chunks(text, self.chunk_size, self.chunk_overlap)
            places = chunk_sections(text, [span for _, _, span in pieces])
            sources.append({'name': document.name, 'type': document.document_type, 'chunks': len(pieces),
                            'sha256': self._source_digest(document)})
            for position, ((chunk, overlap, _), place) in enumerate(zip(pieces, places)):
                identifier = chunk_id(document.name, position)
                records.append({'id': identifier, 'text': chunk, 'metadata': {
                    'record_type': 'chunk', 'tcno': str(tcno), 'source_file': document.name,
                    'document_type': document.document_type, 'chunk_id': identifier, 'chunk_index': position,
                    'overlap': overlap, 'created_at': created_at, **place,
                }})
        return records, sources

    def _source_digest(self, document):
        """The SHA-256 of a source's text and type and of the settings that shape its records."""
        shape = [SCHEMA_VERSION, SECTIONS_VERSION, sparse_encoder.VERSION if self._sparse else None, self.embed_model,
                 self.dimension, self.chunk_size, self.chunk_overlap, document.document_type]
        return hashlib.sha256(json.dumps([shape, document.text or '']).encode('utf-8')).hexdigest()

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
        cache is not embedded again. Each vector is scaled to unit length, and on an index that takes them each
        record also gets the sparse values of its text (sparse_encoder.py). Returns (the embedding tokens, the
        highest log position Pinecone committed them at, or None when it gave none).
        """
        report = on_progress or (lambda message: None)
        if not records:
            return 0, None
        namespace = self.namespace(tcno)
        groups = [records[start:start + EMBED_BATCH] for start in range(0, len(records), EMBED_BATCH)]

        def store(group):
            vectors, tokens = self._embed_passages([record['text'] for record in group])
            payload = []
            for record, vector in zip(group, vectors):
                item = {'id': record['id'], 'values': _unit_length(vector),
                        'metadata': {**record['metadata'], 'text': record['text']}}
                # With the title of its section, so a clause number or term in the heading matches every chunk under it
                sparse = sparse_encoder.sparse_vector(
                    f"{record['metadata'].get('section', '')}\n{record['text']}") if self._sparse else None
                if sparse:
                    item['sparse_values'] = sparse
                payload.append(item)
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
            found = self._read_manifest(namespace, ('complete',))
        except _NotFound:
            self._remember(namespace, exists=False)
            return None
        if found is None:
            return None
        metadata, content = found
        return {'index': self.index_name, 'namespace': namespace, 'tcno': metadata.get('tcno', str(tcno)),
                'created_at': metadata.get('created_at'), 'embed_model': metadata.get('embed_model'),
                'schema_version': metadata.get('schema_version'), 'chunks': int(metadata.get('chunks') or 0),
                'chunk_size': int(metadata.get('chunk_size') or 0) or None, 'hybrid': bool(metadata.get('hybrid')),
                'sources': content.get('sources') or [], 'unreadable': content.get('unreadable') or []}

    def _read_manifest(self, namespace, statuses):
        """
        (metadata, content) of the namespace's manifest when its status is one of statuses, content {'sources',
        'unreadable'} read from its payload or parts; None when there is none, or a part is missing.
        """
        record = self._fetch_records(namespace, [MANIFEST_ID]).get(MANIFEST_ID)
        if record is None:
            return None
        metadata = dict(record.metadata or {})
        if metadata.get('status') not in statuses:
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
        try:
            return metadata, json.loads(payload or '{}')
        except ValueError:
            # Parts rewritten by a build that stopped before its manifest: not to be trusted, nor kept
            log.warning('The manifest of %s does not match its parts; treating it as incomplete', namespace)
            return None

    def restore_kept(self, tcno):
        """
        After a regenerate that stopped before ingest() changed anything (its download failed, say): writes the
        manifest reset_namespace(keep=True) marked as being built back as complete, so the namespace serves re-checks
        as before. Returns True when there was such a manifest. Raises VectorStoreError when Pinecone fails.
        """
        namespace = self.namespace(tcno)
        kept = self._reusable(namespace, kept_only=True)
        if not kept:
            return False
        metadata = {key: value for key, value in kept[0].items() if key != 'kept'}
        metadata['status'] = 'complete'
        written = self._upsert(namespace, [{'id': MANIFEST_ID, 'values': self._unit(), 'metadata': metadata}],
                               f'restore the manifest of {namespace}')
        self._wait_until_searchable(namespace, written)
        return True

    def _reusable(self, namespace, kept_only=False):
        """
        (metadata, content) of the namespace's manifest (see _read_manifest()) when ingest() may keep the chunks it
        lists: one built with this embedding model, dimension and schema, complete or kept by reset_namespace(keep=
        True) (only kept, with kept_only); else None.
        """
        try:
            found = self._read_manifest(namespace, ('building',) if kept_only else ('complete', 'building'))
        except _NotFound:
            return None
        if found is None:
            return None
        metadata = found[0]
        if (metadata.get('status') == 'building' and not metadata.get('kept')) \
                or int(metadata.get('schema_version') or 0) != SCHEMA_VERSION \
                or metadata.get('embed_model') != self.embed_model \
                or int(metadata.get('dimension') or 0) != self.dimension:
            return None
        return found

    def status(self, tcno):
        """
        What the tender's namespace holds, for the API: {'tcno', 'index', 'namespace', 'exists', 'complete',
        'chunks', 'sources', 'unreadable', 'created_at', 'embed_model', 'vector_count'}.
        """
        namespace = self.namespace(tcno)
        state = {'tcno': str(tcno), 'index': self.index_name, 'namespace': namespace,
                 'exists': self.has_namespace(tcno, strict=True), 'complete': False, 'chunks': None, 'sources': [],
                 'unreadable': [], 'created_at': None, 'embed_model': None, 'hybrid': None, 'vector_count': None}
        if not state['exists']:
            return state
        manifest = self.manifest(tcno)
        state['vector_count'] = self._namespace_count(namespace)
        if manifest:
            state.update(complete=True, chunks=manifest['chunks'], sources=manifest['sources'],
                         unreadable=manifest['unreadable'], created_at=manifest['created_at'],
                         embed_model=manifest['embed_model'], hybrid=manifest['hybrid'])
        return state

    def query(self, tcno, vector, top_k=None, filter=None, sparse=None):
        """
        The chunks of the tender's namespace nearest to vector (from embed(..., 'query')), best first, as chunk dicts
        (see Retrieval) with their score. filter narrows them further by metadata. sparse, the sparse values of the
        query, makes it a hybrid search (an index with the dotproduct metric only): its score is the sum of the dense
        and the sparse dot products.
        """
        namespace = self.namespace(tcno)
        request = {'vector': vector, 'top_k': top_k or self.top_k, 'namespace': namespace,
                   'filter': {**_CHUNK_FILTER, **(filter or {})}, 'include_metadata': True}
        if sparse:
            request['sparse_vector'] = sparse
        response = self._call(f'query {namespace}', lambda: self._index().query(**request))
        return [_chunk(match.metadata or {}, match.id, match.score) for match in response.matches or []]

    def retrieve(self, tcno, queries, top_k=None, max_chars=None, manifest=None, section_queries=None):
        """
        The chunks of the tender's namespace that answer queries, {label: [query text, ...]}, within max_chars of
        text, in three steps:
          1. every chunk of the short TenderTiger sources (PINNED_TYPES), budget or not
          2. the sections section_queries ({label: [query text, ...]}, if given) are about, whole: their matches vote
             (with their scores) for each section they are in that fits section_share of the budget, and the best
             MAX_SECTIONS sections that do not overlap, within the score floor of the best one, are taken (see
             _pick_sections())
          3. each query's matches, in turn, best first, one query after another, so every query gets its share
             before the budget runs out
        A search keeps its top_k matches only down to its score floor (score_floor times its best score, and not
        under min_score), and at least MIN_MATCHES. On a namespace built with sparse values, each search is hybrid,
        the dense score weighing hybrid_alpha and the sparse one the rest. A chunk found by several queries is taken
        once, with each label in its 'queries'.

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
        hybrid = bool(manifest.get('hybrid')) and self.hybrid_alpha < 1

        pinned_ids = [chunk_id(source['name'], position) for source in manifest['sources']
                      if source['type'] in PINNED_TYPES for position in range(source['chunks'])]
        pinned = [{**chunk, 'score': 1.0, 'queries': ['pinned']} for chunk in self._fetch(tcno, pinned_ids)]

        def flatten(searches):
            return [(label, text) for label, texts in (searches or {}).items()
                    for text in texts if text and text.strip()]

        sectioned = flatten(section_queries) if self.section_share else []
        labelled = flatten(queries)
        found = self._search(tcno, [text for _, text in sectioned + labelled], top_k, hybrid)
        section_matches = [self._above_floor(matches, hybrid) for matches in found[:len(sectioned)]]
        matches = [self._above_floor(matches, hybrid) for matches in found[len(sectioned):]]

        chosen, used = {}, 0

        def take(match, label):
            """Adds match under label, or label to it when taken already; False when it is over the budget."""
            nonlocal used
            if match['id'] in chosen:
                taken = chosen[match['id']]
                taken['score'] = max(taken['score'], match['score'])
                if label not in taken['queries']:
                    taken['queries'].append(label)
                return True
            if used + len(match['text']) > budget:
                return False
            chosen[match['id']] = {**match, 'queries': [label]}
            used += len(match['text'])
            return True

        for chunk in pinned:
            chosen[chunk['id']] = chunk
            used += len(chunk['text'])
        if sectioned:
            limit = min(budget, used + int(budget * self.section_share))
            chunk_size = manifest.get('chunk_size') or self.chunk_size
            for label, source, first, last, title, score in self._pick_sections(sectioned, section_matches,
                                                                                 limit - used, chunk_size):
                ids = [chunk_id(source, position) for position in range(first, last + 1)]
                section = self._fetch(tcno, ids)
                if used + sum(len(chunk['text']) for chunk in section if chunk['id'] not in chosen) > limit:
                    continue
                for chunk in section:
                    take({**chunk, 'score': score}, label)
                retrieval.whole_sections.append({'source_file': source, 'title': title, 'chunks': f'{first}-{last}',
                                                 'label': label})
        for label, match in _round_robin(labelled, matches):
            take(match, label)

        order = {source['name']: position for position, source in enumerate(manifest['sources'])}
        retrieval.chunks = sorted(chosen.values(),
                                  key=lambda chunk: (order.get(chunk['source_file'], len(order)), chunk['chunk_index']))
        log.info('Retrieved %d chunk(s), %d characters, from %s for %d quer(ies)%s%s', len(retrieval.chunks),
                 retrieval.characters, manifest['namespace'], len(labelled) + len(sectioned),
                 ', hybrid' if hybrid else '',
                 ''.join(f"; section {section['title'] or '(untitled)'!r} of {section['source_file']} whole "
                         f"(chunks {section['chunks']})" for section in retrieval.whole_sections))
        return retrieval

    def _search(self, tcno, texts, top_k, hybrid):
        """[the matches of each of texts] in the tender's namespace, hybrid searches when hybrid, in parallel."""
        if not texts:
            return []
        vectors, _ = self.embed(texts, 'query')
        searches = []
        for text, vector in zip(texts, vectors):
            dense, sparse = _unit_length(vector), None
            if hybrid:
                dense = [value * self.hybrid_alpha for value in dense]
                sparse = sparse_encoder.sparse_vector(text)
                sparse = sparse_encoder.scaled(sparse, 1 - self.hybrid_alpha) if sparse else None
            searches.append((dense, sparse))
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(searches)))) as pool:
            return list(pool.map(lambda search: self.query(tcno, search[0], top_k=top_k, sparse=search[1]), searches))

    def _above_floor(self, matches, hybrid):
        """The matches of one search (best first) down to its score floor, and at least MIN_MATCHES of them."""
        if not matches:
            return matches
        floor = max(self.min_score * (self.hybrid_alpha if hybrid else 1.0), self.score_floor * matches[0]['score'])
        return [match for rank, match in enumerate(matches) if rank < MIN_MATCHES or match['score'] >= floor]

    def _pick_sections(self, sectioned, section_matches, room, chunk_size):
        """
        The sections to take whole: [(label, source, first chunk, last chunk, title, best score)], best first. Each
        match votes, with its score, for every section it is in that can fit room characters (at chunk_size a
        chunk), so a section gathers the votes of the sections within it; the sections with most votes (the larger
        on a tie) are picked, up to MAX_SECTIONS that do not overlap, while their votes stay within the score floor
        of the best one's.
        """
        votes, best, about = {}, {}, {}
        for (label, _), matches in zip(sectioned, section_matches):
            for match in matches:
                if match.get('document_type') in PINNED_TYPES:
                    continue  # sent whole already, at no cost to room: it would take a section's place
                titles = match['section_titles'] + [''] * len(match['sections'])
                seen = set()
                for span, title in zip(match['sections'], titles):
                    # A minor heading over the same chunks as its major one lists the range twice
                    if span in seen:
                        continue
                    seen.add(span)
                    first, last = (int(part) for part in span.split('-'))
                    if (last - first + 1) * chunk_size <= room:
                        key = (match['source_file'], first, last)
                        votes[key] = votes.get(key, 0.0) + match['score']
                        best[key] = max(best.get(key, 0.0), match['score'])
                        about.setdefault(key, (label, title))
        picked = []
        top = max(votes.values(), default=0.0)
        for key, vote in sorted(votes.items(), key=lambda item: (-round(item[1], 9), item[0][1] - item[0][2],
                                                                  item[0])):
            if len(picked) == MAX_SECTIONS or vote < self.score_floor * top:
                break
            if any(key[0] == other[1] and key[1] <= other[3] and other[2] <= key[2] for other in picked):
                continue  # overlaps a section picked already
            label, title = about[key]
            picked.append((label, *key, title, best[key]))
        return picked

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

    def _delete_ids(self, namespace, ids):
        """Deletes the records with these IDs from namespace, DELETE_BATCH per request."""
        index = self._index()
        for start in range(0, len(ids), DELETE_BATCH):
            part = ids[start:start + DELETE_BATCH]
            self._call(f'delete {len(part)} record(s) from {namespace}',
                       lambda part=part: index.delete(ids=part, namespace=namespace))

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
            self._names_version += 1
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
            'queries': [], 'section': metadata.get('section', ''), 'sections': list(metadata.get('sections') or []),
            'section_titles': list(metadata.get('section_titles') or [])}


def _unit_length(vector):
    """vector scaled to length 1, so a dotproduct index scores it as cosine does."""
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else list(vector)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')
