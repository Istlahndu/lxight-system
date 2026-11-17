"""The official streaming route bounds memory and never fabricates coverage."""
import hashlib
import json
import struct
import tracemalloc
import unittest
from pathlib import Path

from lxtrace.compact import EVENT, load_compact
from lxtrace.compact_stream import _emit, inspect_compact, select_compact, iter_compact_chunks
from lxtrace.workspace import temporary_directory


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def write_capture(path, middle=(), footer=True):
    rows = [(1, 0, 1, encoded(dict(schema_version='lxtrace.compact.v1', pid=9,
             clock_id='CLOCK_MONOTONIC', record_bytes=16, run_id='run', host_id='node', boot_id='boot'))),
            (2, 0, 1, encoded(dict(site_id=1, name='matrix', category='mat', args_json='{}'))),
            (3, 1, 1, encoded(dict(thread_id=1, tid=18, name='first'))),
            (3, 2, 1, encoded(dict(thread_id=2, tid=18, name='second-generation')))]
    rows.extend(middle)
    if footer:
        rows.append((6, 0, 1, encoded(quality())))
    with path.open('xb') as stream:
        for index, (kind, thread, count, payload) in enumerate(rows, 1):
            _emit(stream, index, kind, thread, count, payload)


def quality(revision=1, **extra):
    return dict(quality_semantics='cumulative_snapshot', quality_revision=revision,
                dropped_records=0, protocol_errors=0, incomplete_events=0,
                clean_shutdown=True, **extra)


def event_pair(thread=1, base=100):
    return (4, thread, 2, EVENT.pack(base, 1, 1, 7) + EVENT.pack(base + 50, 1, 2, 8))


class CompactStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'capture.lxc'

    def test_summary_tracks_instances_counts_and_boundaries(self):
        write_capture(self.path, [event_pair(), event_pair(2, 200)])
        result = inspect_compact(self.path)
        self.assertTrue(result['quality']['clean_shutdown'])
        self.assertEqual((result['event_count'], result['begin_count'], result['end_count']), (4, 2, 2))
        self.assertEqual(result['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result['valid_bytes'], self.path.stat().st_size)
        self.assertEqual([t['thread_id'] for t in result['threads']], [1, 2])
        self.assertEqual([t['tid'] for t in result['threads']], [18, 18])
        self.assertEqual([t['name'] for t in result['threads']], ['first', 'second-generation'])
        self.assertEqual(result['threads'][1]['last_timestamp_ns'], 250)
        self.assertEqual(result['threads'][1]['records'], 2)

    def test_complete_thread_selection_retains_source_counters_scope(self):
        write_capture(self.path, [event_pair(), event_pair(2, 200)])
        original = self.path.read_bytes()
        output = self.root / 'selected.lxc'
        result = select_compact(self.path, output, [2])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(result['thread_count'], 1)
        self.assertEqual(result['event_count'], 2)
        self.assertTrue(result['quality']['clean_shutdown'])
        self.assertEqual(result['quality']['footer_counters_scope'], 'unfiltered_source_capture')
        self.assertEqual(result['quality']['full_track_coverage'], 'unknown')
        self.assertEqual(result['selection']['source_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(result['selection']['selected_thread_ids'], [2])
        native = load_compact(output)
        self.assertEqual(len(native['records']), 1)
        metadata = native['quality']['compact_metadata']
        self.assertTrue(any('compact_selection' in item for item in metadata))
        chunks = list(iter_compact_chunks(output))
        self.assertEqual([c['sequence'] for c in chunks], list(range(1, len(chunks) + 1)))
        self.assertEqual(chunks[-1]['kind'], 6)
        self.assertEqual(json.loads(chunks[-1]['payload']), quality())

    def test_invalid_selection_arguments_never_create_output(self):
        write_capture(self.path, [event_pair()])
        for ids in ([], [0], [True], [1, 1], ['1'], [3]):
            output = self.root / ('bad-' + str(len(list(self.root.iterdir()))) + '.lxc')
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                select_compact(self.path, output, ids)
            self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            select_compact(self.path, self.path, [1])
        output = self.root / 'existing.lxc'
        output.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            select_compact(self.path, output, [1])
        self.assertEqual(output.read_bytes(), b'keep')

    def test_corrupt_suffix_is_partial_and_selectable_as_prefix(self):
        write_capture(self.path, [event_pair()], footer=False)
        prefix = self.path.stat().st_size
        with self.path.open('ab') as stream:
            stream.write(b'incomplete-header')
        result = inspect_compact(self.path)
        self.assertFalse(result['quality']['clean_shutdown'])
        self.assertEqual(result['valid_bytes'], prefix)
        output = self.root / 'partial-view.lxc'
        selected = select_compact(self.path, output, [1])
        self.assertFalse(selected['quality']['clean_shutdown'])
        self.assertTrue(selected['selection']['source_capture_partial'])
        self.assertFalse(load_compact(output)['quality']['clean_shutdown'])

    def test_open_non_lifo_and_time_regression_are_reported(self):
        middle = [(4, 1, 3, EVENT.pack(100, 1, 2, 0) + EVENT.pack(90, 1, 1, 0) + EVENT.pack(80, 1, 3, 0))]
        write_capture(self.path, middle)
        result = inspect_compact(self.path)
        self.assertFalse(result['quality']['clean_shutdown'])
        self.assertEqual(result['quality']['incomplete_events'], 1)
        self.assertGreaterEqual(result['quality']['protocol_errors'], 3)

    def test_event_between_shutdown_footers_stays_partial(self):
        write_capture(self.path, [(6, 0, 1, encoded(quality())), event_pair(),
                                 (6, 0, 1, encoded(quality(2)))], footer=False)
        result = inspect_compact(self.path)
        self.assertFalse(result['quality']['clean_shutdown'])
        self.assertEqual(result['footer_count'], 2)
        self.assertTrue(any('follows' in text for text in result['warnings']))

    def test_exact_pmu_snapshot_and_regression_detection(self):
        large = 2**60 + 13
        def pmu(ts, kind, value):
            return (7, 1, 1, EVENT.pack(ts, 1, kind, 4) + struct.pack('<IIQ', 1, 0, ts + 3)
                    + struct.pack('<QQQ', value, ts, ts))
        meta = encoded({'metadata_json': json.dumps({'pmu_events': [{'type': 4, 'config': 0x7201, 'name': 'MOPA'}]})})
        write_capture(self.path, [(8, 0, 1, meta), pmu(100, 1, large), pmu(200, 2, large + 9)])
        result = inspect_compact(self.path)
        self.assertTrue(result['quality']['clean_shutdown'])
        self.assertEqual(result['pmu_read_count'], 2)
        self.assertEqual(result['threads'][0]['first_pmu_read']['values'][0]['value'], large)
        self.assertEqual(result['threads'][0]['last_pmu_read']['values'][0]['value'], large + 9)
        self.assertEqual(result['threads'][0]['first_pmu_read']['read_end_ns'], 103)
        self.assertEqual(result['pmu_events'][0]['config'], 0x7201)
        self.path.unlink()
        write_capture(self.path, [pmu(100, 1, large), pmu(200, 2, 10)])
        self.assertFalse(inspect_compact(self.path)['quality']['clean_shutdown'])

    def test_requested_pmu_failure_is_partial_even_with_clean_software_footer(self):
        meta = encoded({'metadata_json': json.dumps({'engine_pmu_thread': {'status': -1}})})
        write_capture(self.path, [event_pair(), (8, 0, 1, meta)])
        result = inspect_compact(self.path)
        self.assertFalse(result['quality']['clean_shutdown'])
        self.assertTrue(any('PMU unavailable' in text for text in result['warnings']))

    def test_selecting_good_track_does_not_erase_bad_unselected_source_quality(self):
        write_capture(self.path, [event_pair(), (4, 2, 1, EVENT.pack(200, 1, 2, 0))])
        output = self.root / 'good-track-from-partial-source.lxc'
        selected = select_compact(self.path, output, [1])
        self.assertFalse(selected['quality']['clean_shutdown'])
        native = load_compact(output)
        self.assertFalse(native['quality']['clean_shutdown'])
        self.assertEqual(native['quality']['capture_scope'], 'selected_threads_only')
        self.assertEqual(native['quality']['footer_counters_scope'], 'unfiltered_source_capture')
        self.assertTrue(any(p.get('kind') == 'compact_thread_selection' for p in native['provenance']))

    def test_loss_counts_and_producer_incomplete_are_retained(self):
        loss = struct.pack('<8Q', 2, 2, 3, 120, 130, 4, 0, 2)
        write_capture(self.path, [event_pair(), (5, 1, 1, loss)])
        result = inspect_compact(self.path)
        self.assertEqual(result['quality']['dropped_records'], 2)
        self.assertFalse(result['quality']['clean_shutdown'])
        self.assertEqual(result['quality']['loss_bounds'][0]['last_seq'], 3)

    def test_checksum_and_semantic_failure_preserve_safe_prefix(self):
        write_capture(self.path, [event_pair(), (4, 2, 1, EVENT.pack(200, 99, 3, 0))])
        result = inspect_compact(self.path)
        self.assertFalse(result['quality']['clean_shutdown'])
        self.assertLess(result['semantic_valid_bytes'], result['valid_bytes'])
        output = self.root / 'safe.lxc'
        selected = select_compact(self.path, output, [1])
        self.assertEqual(selected['event_count'], 2)
        self.assertFalse(selected['quality']['clean_shutdown'])
        data = bytearray(self.path.read_bytes())
        data[-10] ^= 1
        self.path.write_bytes(data)
        self.assertFalse(inspect_compact(self.path)['quality']['clean_shutdown'])

    def test_streaming_memory_does_not_scale_with_event_count(self):
        # 300,000 records: 4.8 MB on disk, only one 32 KiB event chunk live.
        write_capture(self.path, footer=False)
        with self.path.open('ab') as stream:
            seq = 4
            for block in range(150):
                payload = b''.join(EVENT.pack(block * 2000 + i, 1, 3, 0) for i in range(2000))
                seq += 1
                _emit(stream, seq, 4, 1, 2000, payload)
            _emit(stream, seq + 1, 6, 0, 1, encoded(quality()))
        tracemalloc.start()
        try:
            result = inspect_compact(self.path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result['event_count'], 300000)
        self.assertTrue(result['quality']['clean_shutdown'])
        # Allow allocator/filesystem differences between CPython 3.11/3.12.
        # This remains below the 4.8 MB source, before any decoded event tuples.
        self.assertLess(peak, 4 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
