"""Tests for services/pinecone_service.py, against the in-memory FakePinecone (no network)."""
import unittest
from unittest import mock

from pinecone import ForbiddenError, RateLimitError, ServiceError, UnauthorizedError

from pinecone_fake import FakePinecone
from services.pinecone_service import (MANIFEST_ID, Document, NamespaceIncomplete, PineconeService, QuotaExceeded,
                                       VectorStoreError, VectorStoreNotConfigured, chunk_id, chunk_text, document_type)

NIT = ('Eligibility criteria. The bidder must have a minimum average annual turnover of Rs 10 crore in the last three '
       'financial years.\n\nSimilar work: one completed work of 40 crore in the last seven years.\n\n'
       'Joint venture: a JV of up to 3 members is allowed; the lead member holds at least 51%.')
BOQ = 'Item 1: Earthwork 500 cum.\n\nItem 2: Concrete 200 cum.\n\nEstimated cost: Rs 98 crore.'


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
