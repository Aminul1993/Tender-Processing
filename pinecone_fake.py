"""
An in-memory stand-in for pinecone.Pinecone, for the tests: just the calls services/pinecone_service.py makes, with
Pinecone's errors (404 for a missing index or namespace, 409 for an index that exists, 403 QUOTA_EXCEEDED past
max_indexes indexes or max_namespaces namespaces in an index). Embeddings are bags of words hashed into dimension
buckets, so texts sharing words are near each other.
"""
import hashlib
import math
import re
import threading
from types import SimpleNamespace

from pinecone import ApiError, ConflictError, ForbiddenError, NotFoundError


def embed_text(text, dimension):
    """A deterministic unit vector for text: its words counted into dimension buckets."""
    vector = [0.0] * dimension
    for word in re.findall(r'[a-z0-9]+', text.lower()):
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % dimension] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector] if any(vector) else [1.0] + [0.0] * (dimension - 1)


def _matches(metadata, conditions):
    """Pinecone's metadata filter, for $eq, $ne and $in."""
    for key, condition in (conditions or {}).items():
        condition = condition if isinstance(condition, dict) else {'$eq': condition}
        value = metadata.get(key)
        for operator, expected in condition.items():
            if operator == '$eq' and value != expected:
                return False
            if operator == '$ne' and value == expected:
                return False
            if operator == '$in' and value not in expected:
                return False
    return True


class FakeInfo:
    """pinecone's ResponseInfo: the log position a write committed at, or how far a search has caught up."""

    def __init__(self, committed=None, reconciled=None):
        self.lsn_committed = committed
        self.lsn_reconciled = reconciled

    def is_reconciled(self, target):
        return self.lsn_reconciled is not None and self.lsn_reconciled >= target


class FakeIndex:
    def __init__(self, client, name):
        self._client = client
        self.name = name

    def _index(self):
        with self._client.lock:
            if self.name not in self._client.indexes:
                raise NotFoundError(f'Index {self.name} not found', 404)
            return self._client.indexes[self.name]

    def _namespace(self, namespace, create=False):
        index = self._index()
        with self._client.lock:
            if namespace not in index['namespaces']:
                if not create:
                    return None
                if len(index['namespaces']) >= self._client.max_namespaces:
                    raise ForbiddenError('QUOTA_EXCEEDED: You have reached the max namespaces allowed in this index',
                                         403)
                index['namespaces'][namespace] = {}
            return index['namespaces'][namespace]

    def upsert(self, vectors, namespace='', show_progress=True, **options):
        self._client.calls.append(('upsert', namespace, len(vectors)))
        self._client.fail('upsert')
        dimension = self._index()['dimension']
        for vector in vectors:
            if len(vector['values']) != dimension:
                raise ApiError(f'Vector dimension {len(vector["values"])} does not match {dimension}', 400)
            if not any(vector['values']):
                raise ApiError('Dense vectors must contain at least one non-zero value', 400)
            if any(value is None for value in (vector.get('metadata') or {}).values()):
                raise ApiError('Metadata values cannot be null', 400)
        store = self._namespace(namespace, create=True)
        with self._client.lock:
            for vector in vectors:
                store[vector['id']] = {'values': list(vector['values']), 'metadata': dict(vector.get('metadata') or {})}
            position = self._client.log_positions[(self.name, namespace)] =                 self._client.log_positions.get((self.name, namespace), 0) + 1
        return SimpleNamespace(upserted_count=len(vectors), response_info=FakeInfo(committed=position))

    def query(self, vector, top_k, namespace='', filter=None, include_metadata=False, **options):
        self._client.calls.append(('query', namespace, top_k))
        self._client.fail('query')
        scored = []
        for identifier, record in (self._namespace(namespace) or {}).items():
            if not _matches(record['metadata'], filter):
                continue
            score = sum(a * b for a, b in zip(vector, record['values']))
            scored.append(SimpleNamespace(id=identifier, score=score,
                                          metadata=dict(record['metadata']) if include_metadata else None))
        scored.sort(key=lambda match: (-match.score, match.id))
        # Writes are applied at once here, so a search has always caught up with every one
        return SimpleNamespace(matches=scored[:top_k], response_info=FakeInfo(
            reconciled=self._client.log_positions.get((self.name, namespace), 0)))

    def fetch(self, ids, namespace=''):
        store = self._namespace(namespace) or {}
        return SimpleNamespace(vectors={identifier: SimpleNamespace(id=identifier,
                                                                    metadata=dict(store[identifier]['metadata']))
                                        for identifier in ids if identifier in store})

    def describe_index_stats(self):
        namespaces = self._index()['namespaces']
        return SimpleNamespace(total_vector_count=sum(len(store) for store in namespaces.values()),
                               namespaces={name: SimpleNamespace(vector_count=len(store))
                                           for name, store in namespaces.items()})

    def describe_namespace(self, name):
        store = self._namespace(name)
        if store is None:
            raise NotFoundError(f'Namespace {name} not found', 404)
        return SimpleNamespace(name=name, record_count=len(store))

    def list_namespaces(self, prefix=None, limit=None):
        names = sorted(name for name in self._index()['namespaces'] if name.startswith(prefix or ''))
        page = 2  # small pages, so the paging is exercised
        for start in range(0, len(names), page):
            yield SimpleNamespace(namespaces=[SimpleNamespace(name=name, record_count=len(self._namespace(name)))
                                              for name in names[start:start + page]])

    def delete_namespace(self, name):
        self._client.calls.append(('delete_namespace', name))
        index = self._index()
        with self._client.lock:
            if name not in index['namespaces']:
                raise NotFoundError(f'Namespace {name} not found', 404)
            del index['namespaces'][name]

    def delete(self, ids=None, delete_all=False, namespace='', filter=None):
        self._client.calls.append(('delete_all' if delete_all else 'delete', namespace))
        store = self._namespace(namespace)
        if store is None:
            raise NotFoundError(f'Namespace {namespace} not found', 404)
        with self._client.lock:
            if delete_all:
                store.clear()
            else:
                for identifier in ids or []:
                    store.pop(identifier, None)


class FakeInference:
    def __init__(self, client):
        self._client = client

    def embed(self, model, inputs, parameters=None):
        self._client.calls.append(('embed', (parameters or {}).get('input_type'), len(inputs)))
        self._client.fail('embed')
        if len(inputs) > 96:
            raise ApiError('Batch size exceeds 96', 400)
        dimension = (parameters or {}).get('dimension', self._client.dimension)
        items = [SimpleNamespace(values=embed_text(text, dimension)) for text in inputs]
        return _Embeddings(items, sum(len(text.split()) for text in inputs))


class _Embeddings(list):
    def __init__(self, items, tokens):
        super().__init__(items)
        self.usage = SimpleNamespace(total_tokens=tokens)


class FakePinecone:
    """
    The fake client. indexes maps name -> {'dimension', 'namespaces': {namespace: {id: record}}, ...}; calls records
    what was asked. failures, {operation: [exception, ...]}, makes the next calls of an operation (upsert, query,
    embed, create) raise those.
    """

    def __init__(self, dimension=64, max_indexes=20, max_namespaces=100):
        self.dimension = dimension
        self.max_indexes = max_indexes
        self.max_namespaces = max_namespaces
        self.indexes = {}
        self.calls = []
        self.failures = {}
        self.log_positions = {}  # (index, namespace) -> the last write's log position
        self.lock = threading.RLock()
        self.inference = FakeInference(self)

    def fail(self, operation):
        with self.lock:
            pending = self.failures.get(operation)
            if pending:
                raise pending.pop(0)

    def list_indexes(self):
        with self.lock:
            names = list(self.indexes)
        return SimpleNamespace(names=lambda: names)

    def create_index(self, name, dimension, metric='cosine', spec=None, deletion_protection='disabled', tags=None,
                     timeout=None):
        self.calls.append(('create_index', name))
        self.fail('create')
        with self.lock:
            if not re.fullmatch(r'[a-z0-9-]{1,45}', name):
                raise ApiError(f'Invalid index name {name}', 400)
            if name in self.indexes:
                raise ConflictError(f'Index {name} already exists', 409)
            if len(self.indexes) >= self.max_indexes:
                raise ForbiddenError('Request failed: QUOTA_EXCEEDED: max serverless indexes reached', 403)
            self.indexes[name] = {'dimension': dimension, 'namespaces': {}, 'tags': dict(tags or {}),
                                  'metric': metric, 'spec': spec}
        return SimpleNamespace(name=name, dimension=dimension, status={'ready': True})

    def describe_index(self, name):
        with self.lock:
            if name not in self.indexes:
                raise NotFoundError(f'Index {name} not found', 404)
            return SimpleNamespace(name=name, dimension=self.indexes[name]['dimension'], status={'ready': True})

    def delete_index(self, name, timeout=None):
        self.calls.append(('delete_index', name))
        with self.lock:
            if name not in self.indexes:
                raise NotFoundError(f'Index {name} not found', 404)
            del self.indexes[name]

    def Index(self, name='', host=''):
        with self.lock:
            if name not in self.indexes:
                raise NotFoundError(f'Index {name} not found', 404)
        return FakeIndex(self, name)

    # Helpers for the tests

    def namespaces(self, index='tender-processing'):
        """The namespaces of the index, as {name: {id: record}}."""
        return self.indexes[index]['namespaces'] if index in self.indexes else {}

    def chunks(self, namespace, index='tender-processing'):
        """The chunk records of a namespace, as {id: metadata}."""
        return {identifier: record['metadata'] for identifier, record in self.namespaces(index)[namespace].items()
                if record['metadata'].get('record_type') == 'chunk'}
