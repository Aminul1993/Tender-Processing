"""Tests for services/pinecone_service.py, against the in-memory FakePinecone (no network)."""
import time
import unittest
from unittest import mock

from pinecone import ForbiddenError, RateLimitError, ServiceError, UnauthorizedError

from tests.pinecone_fake import FakePinecone
from services import sparse_encoder
from services.document_sections import chunk_sections, headings
from services.pinecone_service import (MANIFEST_ID, Document, NamespaceIncomplete, PineconeService, QuotaExceeded,
                                       VectorStoreError, VectorStoreNotConfigured, _chunk, _chunks, chunk_id,
                                       chunk_text, document_type)

NIT = ('Eligibility criteria. The bidder must have a minimum average annual turnover of Rs 10 crore in the last three '
       'financial years.\n\nSimilar work: one completed work of 40 crore in the last seven years.\n\n'
       'Joint venture: a JV of up to 3 members is allowed; the lead member holds at least 51%.')
BOQ = 'Item 1: Earthwork 500 cum.\n\nItem 2: Concrete 200 cum.\n\nEstimated cost: Rs 98 crore.'


def paragraphs(topic, count):
    return '\n\n'.join(f'{topic} paragraph {number}: the contractor shall arrange {topic.lower()} works and keep '
                       f'records of materials and labour for item {number}.' for number in range(count))


# A notice whose qualification criteria are a section of their own, between a long preamble and long other sections
SECTIONED_NIT = '\n\n'.join([
    'NOTICE INVITING TENDER\n\nTender for road works in the district. ' + paragraphs('General', 4),
    'SECTION 3: QUALIFICATION CRITERIA\n\nThe bidder must meet every qualifying criterion below.',
    '3.1 Financial Criteria\n\nAverage annual turnover of Rs. 25 lakh in the last three financial years. Net worth '
    'must be positive. Solvency certificate of Rs 10 lakh.',
    '3.2 Experience Criteria\n\nSimilar works: one completed work of value 2.5 crore in the last seven years, or two '
    'of 1.5 crore each.',
    'SECTION 4: SCOPE OF WORK\n\n' + paragraphs('Drainage', 8),
    'SECTION 5: SPECIAL CONDITIONS OF CONTRACT\n\n' + paragraphs('Paving', 8),
])
QUALIFICATION = {'qualification section': ['qualification criteria the bidder must meet',
                                           'qualifying criteria: turnover, similar works']}


def service(client=None, **options):
    options = {'dimension': 64, 'chunk_size': 120, 'chunk_overlap': 30, 'top_k': 3, 'max_chars': 10_000,
               'freshness_timeout': 1, 'attempts': 3, 'settle_seconds': 0, **options}
    return PineconeService(api_key='test-key', client=client or FakePinecone(), **options)


class NamingTest(unittest.TestCase):
    def test_each_tender_has_the_namespace_tcno_dash_tcno(self):
        index = service()
        self.assertEqual(index.index_name, 'tender-processing')
        self.assertEqual(index.namespace('12345678'), 'tcno-12345678')
        self.assertEqual(service(namespace_prefix='tender-').namespace('AB/12'), 'tender-AB/12')
        with self.assertRaises(VectorStoreError):
            index.namespace('9' * 600)
        with self.assertRaises(VectorStoreError):
            index.namespace('12 34')

    def test_document_types_come_from_the_file_name(self):
        self.assertEqual([document_type(name) for name in (
            'NIT_PGMS.pdf', 'Annexure/BOQ.xlsx', 'Corrigendum_1.pdf', 'Technical_Document.pdf', 'Forms/Form-3.docx',
            'doc6255363863059291992.pdf')], ['nit', 'boq', 'corrigendum', 'technical', 'form', 'other'])


class ChunkingTest(unittest.TestCase):
    def test_chunks_overlap_and_cover_the_whole_text(self):
        text = ' '.join(f'word{number}' for number in range(400))
        chunks = chunk_text(text, size=200, overlap=40)
        self.assertGreater(len(chunks), 5)
        self.assertEqual(chunks[0][1], 0)
        for (previous, _), (chunk, overlap) in zip(chunks, chunks[1:]):
            self.assertLessEqual(len(chunk), 201)
            self.assertGreater(overlap, 0)
            self.assertTrue(previous.endswith(chunk[:overlap - 1]))  # the overlap repeats the end of the one before
        joined = chunks[0][0] + ''.join(' ' + chunk[overlap:].strip() for chunk, overlap in chunks[1:])
        self.assertEqual(joined.split(), text.split())

    def test_empty_text_has_no_chunks(self):
        self.assertEqual(chunk_text('  '), [])


class NamespaceTest(unittest.TestCase):
    def setUp(self):
        self.client = FakePinecone()
        self.index = service(self.client)

    def build(self, tcno='12345678', unreadable=('scan.pdf',)):
        documents = [Document('TenderTiger Pre-Qualification', 'Net worth must be positive.', 'prequalification'),
                     Document('NIT.pdf', NIT, 'nit'), Document('BOQ.xlsx', BOQ, 'boq')]
        return self.index.rebuild_namespace(tcno, documents, list(unreadable))

    def test_every_document_is_indexed_in_the_tenders_own_namespace_with_its_metadata(self):
        built = self.build()

        # One shared index, made on first use, and one namespace for the tender
        self.assertEqual(list(self.client.indexes), ['tender-processing'])
        self.assertEqual(list(self.client.namespaces()), ['tcno-12345678'])
        chunks = self.client.chunks('tcno-12345678')
        self.assertEqual(built['chunks'], len(chunks))
        self.assertEqual((built['index'], built['namespace']), ('tender-processing', 'tcno-12345678'))
        self.assertEqual({meta['source_file'] for meta in chunks.values()},
                         {'TenderTiger Pre-Qualification', 'NIT.pdf', 'BOQ.xlsx'})
        first = chunks[chunk_id('NIT.pdf', 0)]
        self.assertEqual({key: first[key] for key in ('tcno', 'source_file', 'document_type', 'chunk_id')},
                         {'tcno': '12345678', 'source_file': 'NIT.pdf', 'document_type': 'nit',
                          'chunk_id': chunk_id('NIT.pdf', 0)})
        self.assertTrue(first['created_at'].endswith('+00:00'))
        self.assertTrue(first['text'].startswith('Eligibility criteria.'))
        self.assertTrue(self.index.has_namespace('12345678'))
        self.assertFalse(self.index.has_namespace('87654321'))

        manifest = self.index.manifest('12345678')
        self.assertEqual(manifest['unreadable'], ['scan.pdf'])
        self.assertEqual([(source['name'], source['type']) for source in manifest['sources']],
                         [('TenderTiger Pre-Qualification', 'prequalification'), ('NIT.pdf', 'nit'),
                          ('BOQ.xlsx', 'boq')])
        # Every write names the namespace; the manifest is the first (building) and the last (complete) one
        upserts = [call for call in self.client.calls if call[0] == 'upsert']
        self.assertEqual({call[1] for call in upserts}, {'tcno-12345678'})
        self.assertEqual((upserts[0], upserts[-1]), (('upsert', 'tcno-12345678', 1), ('upsert', 'tcno-12345678', 1)))

    def test_each_tender_is_isolated_in_its_own_namespace(self):
        self.build('111')
        self.index.rebuild_namespace('222', [Document('Other.pdf', 'Turnover of Rs 1 crore.', 'other')])

        self.assertEqual(list(self.client.indexes), ['tender-processing'])  # still one index
        self.assertEqual(sorted(self.client.namespaces()), ['tcno-111', 'tcno-222'])
        self.assertEqual({meta['tcno'] for meta in self.client.chunks('tcno-222').values()}, {'222'})
        self.assertEqual({chunk['source_file'] for chunk in self.index.knowledge('222').chunks}, {'Other.pdf'})
        self.client.calls.clear()
        retrieval = self.index.retrieve('222', {'x': ['minimum average annual turnover joint venture']})
        self.assertEqual({chunk['source_file'] for chunk in retrieval.chunks}, {'Other.pdf'})
        self.assertTrue(all(call[1] == 'tcno-222' for call in self.client.calls if call[0] == 'query'))

    def test_rebuilding_empties_the_namespace_first(self):
        self.build()
        self.index.rebuild_namespace('12345678', [Document('NIT.pdf', 'Turnover of Rs 2 crore.', 'nit')])

        self.assertIn(('delete_all', 'tcno-12345678'), self.client.calls)
        self.assertEqual({meta['source_file'] for meta in self.client.chunks('tcno-12345678').values()}, {'NIT.pdf'})
        self.assertEqual(self.index.manifest('12345678')['unreadable'], [])

    def test_a_namespace_being_built_is_not_complete(self):
        self.index.reset_namespace('12345678')
        self.assertTrue(self.index.has_namespace('12345678'))
        self.assertIsNone(self.index.manifest('12345678'))
        with self.assertRaises(NamespaceIncomplete):
            self.index.retrieve('12345678', {'x': ['turnover']})

    def test_retrieval_pins_the_tendertiger_sources_and_keeps_document_order(self):
        self.build()
        retrieval = self.index.retrieve('12345678', {
            'financial': ['minimum average annual turnover crore'],
            'jv': ['joint venture lead member'],
        })

        sources = [chunk['source_file'] for chunk in retrieval.chunks]
        self.assertEqual(sources[0], 'TenderTiger Pre-Qualification')
        self.assertIn('NIT.pdf', sources)
        self.assertEqual(sources, sorted(sources, key=['TenderTiger Pre-Qualification', 'NIT.pdf', 'BOQ.xlsx'].index))
        found = {label for chunk in retrieval.chunks for label in chunk['queries']}
        self.assertEqual(found, {'pinned', 'financial', 'jv'})
        self.assertEqual(retrieval.unreadable, ['scan.pdf'])
        sections = retrieval.sections()
        self.assertTrue(sections[0].startswith('## TenderTiger Pre-Qualification\nNet worth must be positive.'))
        self.assertTrue(any(section.startswith('## NIT.pdf\n') for section in sections))
        # Only chunks are searched, never the manifest
        self.assertNotIn(MANIFEST_ID, [chunk['id'] for chunk in retrieval.chunks])

    def test_neighbouring_chunks_are_joined_without_their_overlap(self):
        self.build()
        everything = self.index.knowledge('12345678')
        nit = [section for section in everything.sections() if section.startswith('## NIT.pdf')][0]
        self.assertEqual(' '.join(nit.split()[2:]), ' '.join(NIT.split()))

    def test_retrieval_stays_within_the_character_budget(self):
        self.build()
        retrieval = self.index.retrieve('12345678', {'all': ['turnover', 'work', 'joint venture', 'cost']},
                                        max_chars=200)
        pinned = [chunk for chunk in retrieval.chunks if chunk['queries'] == ['pinned']]
        self.assertEqual(len(pinned), 1)  # always sent, budget or not
        self.assertLessEqual(retrieval.characters - sum(len(chunk['text']) for chunk in pinned), 200)

    def test_a_namespace_another_process_built_is_found_at_once(self):
        self.assertEqual(self.index.namespaces(), set())  # this process (the web app) has listed none
        worker = service(self.client)  # a worker process, building the tender's namespace afterwards
        worker.rebuild_namespace('12345678', [Document('NIT.pdf', NIT, 'nit')])

        # Its cached list is out of date, but a missing namespace is looked for again before it is taken as missing
        self.assertEqual(self.index.manifest('12345678')['sources'][0]['name'], 'NIT.pdf')
        self.assertTrue(self.index.retrieve('12345678', {'turnover': ['average annual turnover']}).chunks)
        self.assertIsNone(self.index.manifest('87654321'))

    def test_a_tender_without_a_namespace_cannot_be_retrieved(self):
        with self.assertRaises(NamespaceIncomplete):
            self.index.retrieve('12345678', {'x': ['turnover']})
        self.assertIsNone(self.index.manifest('12345678'))
        self.assertFalse(self.index.has_namespace('12345678'))

    def test_a_tender_with_no_text_has_a_complete_empty_namespace(self):
        self.index.rebuild_namespace('12345678', [], ['scan.pdf'])
        retrieval = self.index.retrieve('12345678', {'x': ['turnover']})
        self.assertEqual((retrieval.chunks, retrieval.unreadable), ([], ['scan.pdf']))

    def test_delete_namespace(self):
        self.build()
        self.build('87654321')
        self.assertTrue(self.index.delete_namespace('12345678'))
        self.assertFalse(self.index.has_namespace('12345678'))
        self.assertTrue(self.index.has_namespace('87654321'))  # the other tender is untouched
        self.assertFalse(self.index.delete_namespace('12345678'))
        self.assertIsNone(self.index.manifest('12345678'))

    def test_a_namespace_brought_back_by_pending_writes_is_deleted_again(self):
        self.build()
        fake_index = self.client.Index(name='tender-processing')
        original = type(fake_index).delete_namespace
        resurrected = []

        def delete_then_apply_a_pending_write(this, name):
            original(this, name)
            if not resurrected:  # Pinecone applies a write it had accepted before the delete
                resurrected.append(name)
                this._namespace(name, create=True)['late'] = {'values': [1.0], 'metadata': {}}

        with mock.patch.object(type(fake_index), 'delete_namespace', delete_then_apply_a_pending_write),                 self.assertLogs('services.pinecone_service', 'WARNING') as logs:
            self.assertTrue(self.index.delete_namespace('12345678'))
        self.assertIn('came back after it was deleted', logs.output[0])
        self.assertNotIn('tcno-12345678', self.client.namespaces())
        # Emptied (in order after every write) before each delete of the namespace itself
        deletes = [call[0] for call in self.client.calls if call[0] in ('delete_all', 'delete_namespace')]
        self.assertEqual(deletes, ['delete_all', 'delete_namespace', 'delete_all', 'delete_namespace'])

    def test_status(self):
        self.assertEqual(self.index.status('12345678')['exists'], False)
        self.build()
        status = self.index.status('12345678')
        self.assertEqual((status['index'], status['namespace'], status['exists'], status['complete']),
                         ('tender-processing', 'tcno-12345678', True, True))
        self.assertEqual(status['vector_count'], status['chunks'] + 1)

    def test_the_shared_index_must_have_the_configured_dimension(self):
        self.build()
        other = service(self.client, dimension=32)
        with self.assertRaises(VectorStoreError) as raised:
            other.reset_namespace('1')
        self.assertIn('has 64 dimensions, but PINECONE_EMBED_DIMENSION is 32', str(raised.exception))

    def test_a_long_source_list_is_split_over_manifest_parts(self):
        documents = [Document(f'Annexures/{"long-folder-name/" * 3}Form-{number:04d}.pdf', f'Form {number}.', 'form')
                     for number in range(600)]
        self.index.rebuild_namespace('12345678', documents, [f'scan-{number}.pdf' for number in range(300)])

        records = self.client.namespaces()['tcno-12345678']
        parts = [identifier for identifier, record in records.items()
                 if record['metadata']['record_type'] == 'manifest_part']
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(records[identifier]['metadata']['payload']) <= 30_000 for identifier in parts))
        manifest = self.index.manifest('12345678')
        self.assertEqual(len(manifest['sources']), 600)
        self.assertEqual(len(manifest['unreadable']), 300)
        self.assertEqual(len(self.index.knowledge('12345678').chunks), 600)
        # A part gone missing makes the namespace incomplete, never half-read
        del records[parts[-1]]
        self.assertIsNone(self.index.manifest('12345678'))

    def test_the_manifest_just_written_is_used_without_reading_it_back(self):
        built = self.build()
        with mock.patch.object(self.index, 'manifest', side_effect=AssertionError('read back')):
            retrieval = self.index.retrieve('12345678', {'x': ['turnover']}, manifest=built['manifest'])
        self.assertTrue(retrieval.chunks)

    def test_large_tenders_are_embedded_and_upserted_in_batches(self):
        big = '\n\n'.join(f'Clause {number}: the bidder shall submit document {number}.' for number in range(400))
        built = self.index.rebuild_namespace('12345678', [Document('NIT.pdf', big, 'nit')])

        embeds = [call for call in self.client.calls if call[0] == 'embed' and call[1] == 'passage']
        upserts = [call for call in self.client.calls if call[0] == 'upsert']
        self.assertGreater(built['chunks'], 96)
        self.assertTrue(all(size <= 96 for _, _, size in embeds))
        self.assertEqual(sum(size for _, _, size in embeds), built['chunks'])
        self.assertTrue(all(size <= 50 for _, _, size in upserts))
        self.assertEqual(len(self.client.chunks('tcno-12345678')), built['chunks'])
        self.assertGreater(built['embedding_tokens'], 0)

    def test_the_namespace_listing_follows_every_page(self):
        for tcno in ('1', '2', '3', '4', '5'):
            self.index.reset_namespace(tcno)
        self.index._names = None  # forget the cache
        self.assertEqual(self.index.namespaces(refresh=True), {f'tcno-{n}' for n in '12345'})


class IncrementalBuildTest(unittest.TestCase):
    """Regenerating a namespace kept by reset_namespace(keep=True) upserts only the sources that changed."""

    def setUp(self):
        self.client = FakePinecone()
        self.index = service(self.client)
        self.first = [Document('TenderTiger Pre-Qualification', 'Net worth must be positive.', 'prequalification'),
                      Document('NIT.pdf', NIT, 'nit'), Document('BOQ.xlsx', BOQ, 'boq')]
        self.index.rebuild_namespace('1', self.first, ['scan.pdf'])

    def chunks(self, source):
        return {identifier: meta for identifier, meta in self.client.chunks('tcno-1').items()
                if meta['source_file'] == source}

    def test_only_changed_and_new_sources_are_upserted_and_those_gone_are_deleted(self):
        nit_before = {identifier: meta['created_at'] for identifier, meta in self.chunks('NIT.pdf').items()}
        shorter_boq = 'Item 1: Earthwork 500 cum.'
        self.client.calls.clear()

        built = self.index.rebuild_namespace('1', [
            Document('NIT.pdf', NIT, 'nit'), Document('BOQ.xlsx', shorter_boq, 'boq'),
            Document('Corrigendum-1.pdf', 'The closing date is extended by a week.', 'corrigendum')], [], keep=True)

        self.assertNotIn(('delete_all', 'tcno-1'), self.client.calls)
        # NIT.pdf is untouched: neither embedded nor written again
        self.assertEqual({identifier: meta['created_at'] for identifier, meta in self.chunks('NIT.pdf').items()},
                         nit_before)
        new_chunks = len(chunk_text(shorter_boq, 120, 30)) + len(chunk_text('The closing date is extended by a week.',
                                                                            120, 30))
        embedded = sum(call[2] for call in self.client.calls if call[:2] == ('embed', 'passage'))
        self.assertEqual((built['upserted'], embedded, built['kept_sources']), (new_chunks, new_chunks, 1))
        # The pre-qualification text is gone, and so are the BOQ's chunks past its new end
        self.assertEqual(self.chunks('TenderTiger Pre-Qualification'), {})
        self.assertEqual([meta['text'] for meta in self.chunks('BOQ.xlsx').values()], [shorter_boq])
        self.assertEqual(built['deleted'], len(chunk_text('Net worth must be positive.', 120, 30))
                         + len(chunk_text(BOQ, 120, 30)) - 1)
        manifest = self.index.manifest('1')
        self.assertEqual([source['name'] for source in manifest['sources']],
                         ['NIT.pdf', 'BOQ.xlsx', 'Corrigendum-1.pdf'])
        self.assertEqual(manifest['unreadable'], [])
        self.assertEqual(len(self.index.knowledge('1').chunks), built['chunks'])
        self.assertEqual(len(self.client.chunks('tcno-1')), built['chunks'])

    def test_nothing_is_upserted_again_when_nothing_changed(self):
        self.client.calls.clear()
        built = self.index.rebuild_namespace('1', self.first, ['scan.pdf'], keep=True)

        self.assertEqual((built['upserted'], built['deleted'], built['kept_sources']), (0, 0, 3))
        self.assertFalse([call for call in self.client.calls if call[0] == 'embed'])
        # Only the manifest is written: kept, then complete again
        self.assertEqual([call for call in self.client.calls if call[0] == 'upsert'],
                         [('upsert', 'tcno-1', 1), ('upsert', 'tcno-1', 1)])
        self.assertEqual(self.index.manifest('1')['unreadable'], ['scan.pdf'])

    def test_a_build_that_fails_after_changing_the_namespace_is_rebuilt_from_scratch_next_time(self):
        self.index.reset_namespace('1', keep=True)
        self.assertIsNone(self.index.manifest('1'))  # kept, but not complete while being built
        with mock.patch.object(self.index, 'upsert_chunks', side_effect=VectorStoreError('upsert failed')), \
                self.assertRaises(VectorStoreError):
            self.index.ingest('1', [Document('NIT.pdf', 'Turnover of Rs 2 crore.', 'nit')])

        self.client.calls.clear()
        self.index.reset_namespace('1', keep=True)
        self.assertIn(('delete_all', 'tcno-1'), self.client.calls)  # not kept: its chunks no longer match

    def test_a_namespace_reset_twice_with_keep_is_still_kept(self):
        # As a regenerate does: the processor before the download, process_tender_files() before reading
        self.index.reset_namespace('1', keep=True)
        self.index.reset_namespace('1', keep=True)
        built = self.index.ingest('1', self.first, ['scan.pdf'])
        self.assertEqual((built['kept_sources'], built['upserted']), (3, 0))

    def test_without_keep_or_with_other_settings_nothing_is_kept(self):
        self.client.calls.clear()
        built = self.index.rebuild_namespace('1', self.first, ['scan.pdf'])
        self.assertIn(('delete_all', 'tcno-1'), self.client.calls)
        self.assertEqual(built['kept_sources'], 0)

        other = service(self.client, chunk_size=200)  # chunks of another size: every source is upserted again
        built = other.rebuild_namespace('1', self.first, ['scan.pdf'], keep=True)
        self.assertEqual((built['kept_sources'], built['upserted']), (0, built['chunks']))
        self.assertEqual(len(self.client.chunks('tcno-1')), built['chunks'])

    def test_manifest_parts_no_longer_needed_are_deleted(self):
        forms = [Document(f'Annexures/{"long-folder-name/" * 3}Form-{number:04d}.pdf', f'Form {number}.', 'form')
                 for number in range(600)]
        self.index.rebuild_namespace('1', forms)
        self.index.rebuild_namespace('1', forms[:1], keep=True)

        records = self.client.namespaces()['tcno-1']
        self.assertEqual([identifier for identifier, record in records.items()
                          if record['metadata']['record_type'] == 'manifest_part'], [])
        self.assertEqual(len(self.client.chunks('tcno-1')), 1)
        self.assertEqual(len(self.index.manifest('1')['sources']), 1)


class AdaptiveRetrievalTest(unittest.TestCase):
    """A search keeps its matches down to a score floor, not always its top_k."""

    def matches(self, *scores):
        return [_chunk({'source_file': 'NIT.pdf', 'chunk_index': number, 'text': f'chunk {number}'},
                       chunk_id('NIT.pdf', number), score) for number, score in enumerate(scores)]

    def retrieved(self, matches, **options):
        index = service(top_k=8, **options)
        index.rebuild_namespace('1', [Document('NIT.pdf', NIT, 'nit')])
        with mock.patch.object(index, 'query', return_value=matches):
            return [chunk['chunk_index'] for chunk in index.retrieve('1', {'x': ['turnover']}).chunks]

    def test_matches_under_a_share_of_the_best_score_are_left_out(self):
        matches = self.matches(0.60, 0.55, 0.52, 0.40, 0.30)
        self.assertEqual(self.retrieved(matches, hybrid_alpha=1), [0, 1, 2])  # 0.85 x 0.60 = 0.51
        self.assertEqual(self.retrieved(matches, hybrid_alpha=1, score_floor=0, min_score=0), [0, 1, 2, 3, 4])

    def test_matches_under_the_minimum_score_are_left_out_but_never_the_best_two(self):
        self.assertEqual(self.retrieved(self.matches(0.30, 0.25, 0.19, 0.18), hybrid_alpha=1), [0, 1])
        self.assertEqual(self.retrieved(self.matches(0.15, 0.14, 0.14), hybrid_alpha=1), [0, 1])
        # In a hybrid search the minimum is on the dense part's scale: 0.2 x 0.75
        self.assertEqual(self.retrieved(self.matches(0.18, 0.17, 0.16, 0.14)), [0, 1, 2])
        self.assertEqual(self.retrieved(self.matches(0.18, 0.17, 0.16, 0.14), hybrid_alpha=1), [0, 1])


class HybridRetrievalTest(unittest.TestCase):
    """On a dotproduct index each chunk also holds sparse values, so figures and clauses match exactly."""

    DOCUMENTS = [Document('A.pdf', 'Similar work: one completed work of value 2.5 crore in the last seven years.', 'nit'),
                 Document('B.pdf', 'Similar work: one completed work of value 4 crore in the last seven years.', 'nit')]

    def test_an_amount_written_another_way_finds_its_chunk(self):
        client = FakePinecone()
        index = service(client, chunk_size=400, score_floor=0, min_score=0)
        built = index.rebuild_namespace('1', self.DOCUMENTS)

        self.assertTrue(built['manifest']['hybrid'])
        self.assertTrue(client.sparse('tcno-1', chunk_id('A.pdf', 0)))
        retrieval = index.retrieve('1', {'x': ['similar completed work of value Rs 250 lakh']})
        scores = {chunk['source_file']: chunk['score'] for chunk in retrieval.chunks}
        self.assertGreater(scores['A.pdf'], scores['B.pdf'])
        dense = service(client, chunk_size=400, score_floor=0, min_score=0, hybrid_alpha=1)
        scores = {chunk['source_file']: chunk['score']
                  for chunk in dense.retrieve('1', {'x': ['similar completed work of value Rs 250 lakh']}).chunks}
        self.assertLessEqual(scores['A.pdf'], scores['B.pdf'])  # the dense fake cannot tell 2.5 crore from 4

    def test_a_cosine_index_is_searched_dense_only(self):
        client = FakePinecone()
        index = service(client, metric='cosine')
        with self.assertLogs('services.pinecone_service', 'WARNING') as logs:
            built = index.rebuild_namespace('1', self.DOCUMENTS)
        self.assertIn('retrieval is dense only', logs.output[0])
        self.assertFalse(built['manifest']['hybrid'])
        self.assertEqual(client.sparse('tcno-1', chunk_id('A.pdf', 0)), {})
        # The fake refuses a sparse query on a cosine index, as Pinecone does
        self.assertEqual(len(index.retrieve('1', {'x': ['similar work 2.5 crore']}).chunks), 2)


class HierarchicalRetrievalTest(unittest.TestCase):
    """The section the qualification searches point to is taken whole."""

    def setUp(self):
        self.client = FakePinecone(dimension=512)  # wide enough that the fake's hashed words rarely collide
        self.index = service(self.client, dimension=512, chunk_size=300, chunk_overlap=40, top_k=4, max_chars=4000)
        self.index.rebuild_namespace('1', [Document('NIT.pdf', SECTIONED_NIT, 'nit')])

    def test_the_qualification_section_is_taken_whole_before_the_other_searches(self):
        retrieval = self.index.retrieve('1', {'scope': ['drainage works']}, section_queries=QUALIFICATION)

        section = [chunk['chunk_index'] for chunk in self.client.chunks('tcno-1').values()
                   if any(words in chunk['text'] for words in ('QUALIFICATION CRITERIA', '3.1 Financial',
                                                               'Average annual turnover', 'Similar works'))]
        self.assertEqual(retrieval.whole_sections, [{
            'source_file': 'NIT.pdf', 'title': 'SECTION 3: QUALIFICATION CRITERIA',
            'chunks': f'{min(section)}-{max(section)}', 'label': 'qualification section', 'picked_by': 'search'}])
        taken = {chunk['chunk_index'] for chunk in retrieval.chunks if 'qualification section' in chunk['queries']}
        self.assertEqual(taken, set(range(min(section), max(section) + 1)))
        self.assertIn('scope', {label for chunk in retrieval.chunks for label in chunk['queries']})

    def test_a_match_votes_once_for_a_range_and_pinned_texts_do_not_vote(self):
        def match(source, spans, score, document_type='nit'):
            return {'source_file': source, 'sections': spans, 'section_titles': ['Title'] * len(spans),
                    'score': score, 'document_type': document_type}

        matches = [
            # Section III: a minor heading over the same chunks as its major one lists the range twice
            match('NIT.pdf', ['0-1', '0-1'], 0.50), match('NIT.pdf', ['0-1', '0-1'], 0.48),
            match('NIT.pdf', ['3-5'], 0.60), match('NIT.pdf', ['3-5'], 0.59), match('NIT.pdf', ['3-5'], 0.58),
            # TenderTiger's pre-qualification text: sent whole anyway, so it must not take a section's place
            match('TenderTiger Pre-Qualification', ['0-2'], 0.90, 'prequalification'),
            match('TenderTiger Pre-Qualification', ['0-2'], 0.89, 'prequalification'),
        ]
        picked = self.index._pick_sections([('qualification section', 'query')], [matches], room=10_000,
                                           chunk_size=300)
        self.assertEqual([(source, first, last) for _, source, first, last, *_ in picked], [('NIT.pdf', 3, 5)])

    def test_a_section_larger_than_its_share_of_the_budget_is_not_taken(self):
        index = service(self.client, dimension=512, chunk_size=300, chunk_overlap=40, top_k=4, max_chars=4000,
                        section_share=0.1)  # 400 characters: no section of two chunks fits
        retrieval = index.retrieve('1', {'scope': ['drainage works']}, section_queries=QUALIFICATION)
        self.assertEqual(retrieval.whole_sections, [])
        self.assertTrue(retrieval.chunks)


class ChosenSectionsTest(unittest.TestCase):
    """A model chooses the sections to take whole by their headings; the searches choose when it cannot."""

    def setUp(self):
        self.client = FakePinecone(dimension=512)
        self.index = service(self.client, dimension=512, chunk_size=300, chunk_overlap=40, top_k=4, max_chars=4000)
        self.index.rebuild_namespace('1', [
            Document('TenderTiger Pre-Qualification', 'Turnover of Rs 25 lakh.', 'prequalification'),
            Document('NIT.pdf', SECTIONED_NIT, 'nit'),
            Document('BOQ.xlsx', 'SCHEDULE OF QUANTITIES\n\n' + paragraphs('Earthwork', 2), 'boq')])
        self.offered = None

    def choose(self, *titles):
        """A chooser that answers with the IDs of the sections with these titles, and keeps what it was offered."""
        def choose(sections):
            self.offered = sections
            return [section['id'] for title in titles for section in sections if section['title'] == title]
        return choose

    def chunks_of(self, retrieval, title):
        section = next(section for section in retrieval.whole_sections if section['title'] == title)
        first, last = (int(part) for part in section['chunks'].split('-'))
        return set(range(first, last + 1))

    def test_the_headings_are_listed_in_the_manifest_but_not_for_the_tendertiger_texts(self):
        sources = {source['name']: source for source in self.index.manifest('1')['sources']}
        self.assertNotIn('contents', sources['TenderTiger Pre-Qualification'])
        titles = [(tier, title) for tier, _, _, _, title in sources['NIT.pdf']['contents']]
        self.assertEqual(titles, [(1, 'NOTICE INVITING TENDER'), (1, 'SECTION 3: QUALIFICATION CRITERIA'),
                                  (2, '3.1 Financial Criteria'), (2, '3.2 Experience Criteria'),
                                  (1, 'SECTION 4: SCOPE OF WORK'), (1, 'SECTION 5: SPECIAL CONDITIONS OF CONTRACT')])

    def test_the_sections_the_model_chooses_are_taken_whole_in_its_order(self):
        retrieval = self.index.retrieve('1', {'scope': ['drainage works']}, section_queries=QUALIFICATION,
                                        choose_sections=self.choose('3.2 Experience Criteria',
                                                                    '3.1 Financial Criteria'))

        self.assertEqual([(section['title'], section['picked_by'], section['label'])
                          for section in retrieval.whole_sections],
                         [('3.2 Experience Criteria', 'model', 'qualification section'),
                          ('3.1 Financial Criteria', 'model', 'qualification section')])
        taken = {chunk['chunk_index'] for chunk in retrieval.chunks if 'qualification section' in chunk['queries']}
        self.assertEqual(taken, self.chunks_of(retrieval, '3.1 Financial Criteria')
                         | self.chunks_of(retrieval, '3.2 Experience Criteria'))
        self.assertIn('Similar works: one completed work', '\n'.join(retrieval.sections()))
        # Neither the bill of quantities nor TenderTiger's texts (sent whole anyway) are offered
        self.assertEqual({section['source_file'] for section in self.offered}, {'NIT.pdf'})

    def test_a_section_too_long_to_take_whole_is_offered_without_an_id(self):
        index = service(self.client, dimension=512, chunk_size=300, chunk_overlap=40, top_k=4, max_chars=4000,
                        section_share=0.2)  # 800 characters: two chunks at most
        retrieval = index.retrieve('1', {'scope': ['drainage works']}, section_queries=QUALIFICATION,
                                   choose_sections=self.choose('SECTION 4: SCOPE OF WORK', '3.1 Financial Criteria'))
        ids = {section['title']: section['id'] for section in self.offered}
        self.assertIsNone(ids['SECTION 4: SCOPE OF WORK'])
        self.assertRegex(ids['3.1 Financial Criteria'], r'^S\d+$')
        self.assertEqual([section['title'] for section in retrieval.whole_sections], ['3.1 Financial Criteria'])

    def test_the_searches_choose_when_the_model_fails_chooses_none_or_names_no_listed_section(self):
        def fails(sections):
            raise ConnectionError('Groq is down')

        for choose in (fails, lambda sections: [], lambda sections: ['S99', 'none']):
            with self.assertLogs('services.pinecone_service', 'INFO'):
                retrieval = self.index.retrieve('1', {'scope': ['drainage works']}, section_queries=QUALIFICATION,
                                                choose_sections=choose)
            self.assertEqual([(section['title'], section['picked_by']) for section in retrieval.whole_sections],
                             [('SECTION 3: QUALIFICATION CRITERIA', 'search')])

    def test_a_namespace_built_before_headings_were_listed_is_left_to_the_searches(self):
        manifest = self.index.manifest('1')
        for source in manifest['sources']:
            source.pop('contents', None)
        choose = mock.Mock(return_value=['S1'])
        retrieval = self.index.retrieve('1', {'scope': ['drainage works']}, manifest=manifest,
                                        section_queries=QUALIFICATION, choose_sections=choose)
        choose.assert_not_called()
        self.assertEqual([section['picked_by'] for section in retrieval.whole_sections], ['search'])


class SectionTest(unittest.TestCase):
    def test_headings_leave_out_running_headers_page_markers_contents_and_sentences(self):
        text = '\n'.join([
            'LIFE INSURANCE CORPORATION OF INDIA', '1 Notice inviting tender 3', '2 Eligibility criteria 5',
            'LIFE INSURANCE CORPORATION OF INDIA', '## Page 2', 'NOTICE INVITING TENDER', 'Sealed bids are invited.',
            'LIFE INSURANCE CORPORATION OF INDIA', 'SECTION 3: QUALIFICATION CRITERIA', 'A. WORKS EXPERIENCE:',
            'Three similar works.', '1. The bidder shall submit the documents listed below.', '### Turnover',
            '**Net worth**', '| ITEM | VALUE |', '4.2 Bid Capacity Formula', 'Downloading ....... 24.06.2026'])
        self.assertEqual([(tier, title) for _, tier, title, _ in headings(text)], [
            (1, 'NOTICE INVITING TENDER'), (1, 'SECTION 3: QUALIFICATION CRITERIA'), (2, 'A. WORKS EXPERIENCE'),
            (2, 'Turnover'), (2, 'Net worth'), (2, '4.2 Bid Capacity Formula')])

    def test_each_chunk_lists_every_section_it_overlaps_smallest_first(self):
        text = SECTIONED_NIT.strip()
        pieces = _chunks(text, 300, 40)
        places = chunk_sections(text, [span for _, _, span in pieces])

        financial = next(number for number, (chunk, _, _) in enumerate(pieces) if 'Average annual turnover' in chunk)
        place = places[financial]
        self.assertEqual(place['section'], '3.1 Financial Criteria')
        self.assertIn('SECTION 3: QUALIFICATION CRITERIA', place['section_titles'])
        self.assertEqual(len(place['sections']), len(place['section_titles']))
        sizes = [int(last) - int(first) for first, last in (span.split('-') for span in place['sections'])]
        self.assertEqual(sizes, sorted(sizes))

    def test_a_document_without_headings_is_one_section(self):
        text = paragraphs('General', 6)
        pieces = _chunks(text, 300, 40)
        places = chunk_sections(text, [span for _, _, span in pieces])
        self.assertEqual({tuple(place['sections']) for place in places}, {(f'0-{len(pieces) - 1}',)})
        self.assertNotIn('section', places[0])


class SparseEncoderTest(unittest.TestCase):
    def test_an_amount_is_one_term_however_it_is_written(self):
        for text in ('Rs. 25 lakh', '₹ 25,00,000/-', 'INR 2500000', '25 Lakhs', 'Rs.0.25 crore', '2,500,000 rupees'):
            self.assertIn('amt:2500000', sparse_encoder.terms(text), text)
        for text in ('2.5 crore', 'Rs 250 lakh', '₹2.5 Cr.'):
            self.assertIn('amt:25000000', sparse_encoder.terms(text), text)
        # A year or a count is not an amount
        self.assertFalse([term for term in sparse_encoder.terms('in the last 3 years since 2019') if ':' in term])

    def test_clauses_percentages_and_financial_years_are_terms(self):
        terms = sparse_encoder.terms('As per clause 4.2.1 and Annexure-III, the lead member holds 51 % in FY 2023-2024')
        self.assertLessEqual({'ref:clause:4.2.1', 'ref:4.2.1', 'ref:annexure:3', 'pct:51', 'fy:2023-24'}, set(terms))
        self.assertIn('ref:4.2.1', sparse_encoder.terms('4.2.1 Bid capacity'))
        self.assertIn('ref:form:a', sparse_encoder.terms('Form-A'))

    def test_common_words_are_left_out_and_a_vector_has_unit_length(self):
        self.assertEqual(set(sparse_encoder.terms('The bidder shall submit the tender documents')), {'submit'})
        self.assertIsNone(sparse_encoder.sparse_vector('the shall'))
        vector = sparse_encoder.sparse_vector('Turnover turnover, net worth of Rs 5 crore as per clause 3.1')
        self.assertEqual(len(vector['indices']), len(set(vector['indices'])))
        self.assertAlmostEqual(sum(value * value for value in vector['values']), 1.0)
        self.assertTrue(all(0 <= index < 2 ** 32 for index in vector['indices']))


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.client = FakePinecone(max_namespaces=1)
        self.index = service(self.client)
        sleep = mock.patch('services.pinecone_service.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_rate_limits_and_server_errors_are_retried(self):
        self.client.failures = {'embed': [RateLimitError('slow down', 429)],
                                'upsert': [ServiceError('unavailable', 503)]}
        with self.assertLogs('services.pinecone_service', 'WARNING') as logs:
            built = self.index.rebuild_namespace('1', [Document('NIT.pdf', NIT, 'nit')])
        self.assertGreater(built['chunks'], 0)
        self.assertEqual(len([line for line in logs.output if 'retrying' in line]), 2)

    def test_errors_that_persist_fail_and_leave_no_complete_manifest(self):
        self.index.reset_namespace('1')
        self.client.failures = {'upsert': [ServiceError('unavailable', 503)] * 3}
        with self.assertRaises(VectorStoreError), self.assertLogs('services.pinecone_service', 'WARNING'):
            self.index.ingest('1', [Document('NIT.pdf', NIT, 'nit')])
        self.assertTrue(self.index.has_namespace('1'))
        self.assertIsNone(self.index.manifest('1'))  # half-built: rebuilt next time

    def test_the_namespace_quota_is_reported_before_anything_is_indexed(self):
        self.index.rebuild_namespace('1', [Document('NIT.pdf', NIT, 'nit')])
        with self.assertRaises(QuotaExceeded) as raised:
            self.index.reset_namespace('2')
        self.assertIn("Pinecone plan's limit is reached", str(raised.exception))
        self.assertEqual(list(self.client.namespaces()), ['tcno-1'])

    def test_the_index_quota_is_reported(self):
        client = FakePinecone(max_indexes=0)
        with self.assertRaises(QuotaExceeded):
            service(client).reset_namespace('1')
        # Pinecone's own wording, without the word quota
        client = FakePinecone()
        client.failures = {'create': [ForbiddenError("You've reached the max serverless indexes allowed in project "
                                                     'Default (5). Upgrade your plan.', 403)]}
        with self.assertRaises(QuotaExceeded):
            service(client).ensure_index()

    def test_a_rate_limit_that_speaks_of_a_quota_is_retried_not_reported_as_the_plan_limit(self):
        self.client.failures = {'embed': [RateLimitError('Request failed: embedding token quota exceeded', 429)]}
        with self.assertLogs('services.pinecone_service', 'WARNING'):
            built = self.index.rebuild_namespace('1', [Document('NIT.pdf', NIT, 'nit')])
        self.assertGreater(built['chunks'], 0)

    def test_a_refused_key_is_not_retried(self):
        self.client.failures = {'create': [UnauthorizedError('Invalid API Key', 401)]}
        with self.assertRaises(VectorStoreError) as raised:
            self.index.ensure_index()
        self.assertIn('refused the API key', str(raised.exception))
        self.assertNotIsInstance(raised.exception, QuotaExceeded)

    def test_without_an_api_key_nothing_is_called(self):
        index = PineconeService(api_key=None)
        self.assertFalse(index.configured)
        self.assertFalse(index.has_namespace('1'))
        with self.assertRaises(VectorStoreNotConfigured):
            index.has_namespace('1', strict=True)
        with self.assertRaises(VectorStoreNotConfigured):
            index.rebuild_namespace('1', [])

    def test_an_old_namespace_list_is_given_at_once_while_a_fresh_one_is_listed(self):
        self.index.reset_namespace('1')
        self.assertEqual(self.index.namespaces(), {'tcno-1'})
        self.client.namespaces()['tcno-2'] = {}  # made by another process, so not known here yet
        self.index._names_at -= self.index.cache_seconds  # the list is old

        self.assertEqual(self.index.namespaces(), {'tcno-1'})  # at once, the old list
        deadline = time.monotonic() + 5
        while self.index._names_refreshing and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.index.namespaces(), {'tcno-1', 'tcno-2'})  # listed in the background meanwhile

    def test_an_unreachable_pinecone_lists_no_namespace_without_failing_the_list(self):
        self.index.reset_namespace('1')
        self.index._names = None
        self.index._handle = mock.Mock(list_namespaces=mock.Mock(side_effect=ConnectionError('offline')))
        with self.assertLogs('services.pinecone_service', 'WARNING'):
            self.assertFalse(self.index.has_namespace('1'))
            self.assertFalse(self.index.has_namespace('2'))
            self.assertEqual(self.index._handle.list_namespaces.call_count, 1)  # the failure is remembered a while
            with self.assertRaises(VectorStoreError):
                self.index.has_namespace('1', strict=True)  # retried, then raised

    def test_no_shared_index_yet_means_no_namespaces(self):
        self.assertEqual(self.index.namespaces(refresh=True), set())
        self.assertEqual(self.client.indexes, {})  # reading never creates the index


if __name__ == '__main__':
    unittest.main()
