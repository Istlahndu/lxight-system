"""Checksummed SDK recovery, loss boundaries and exact boundary PMU evidence."""
import json
import struct
import unittest
import zlib
from pathlib import Path

from lxtrace.compact import HEADER, MAGIC, TRAILER, EVENT, load_compact
from lxtrace.report import write_unified_report
from lxtrace.report import merge_sessions
from lxtrace.resource import analyze_resources
from lxtrace.workspace import temporary_directory


def capture(extra=(), footer=True):
    def obj(value):
        return json.dumps(value).encode()
    chunks = [
        (1, 0, 1, obj(dict(schema_version='lxtrace.compact.v1', pid=9, clock_id='CLOCK_MONOTONIC', run_id='run'))),
        (2, 0, 1, obj(dict(site_id=1, name='matrix', category='mat', args_json='{}'))),
        (3, 1, 1, obj(dict(thread_id=1, tid=18, name='worker'))),
    ]
    chunks.extend(extra)
    if footer:
        chunks.append((6, 0, 1, obj(dict(quality_semantics='cumulative_snapshot', quality_revision=1,
            dropped_records=0, protocol_errors=0, clean_shutdown=True))))
    result = b''
    for seq, (kind, thread, count, payload) in enumerate(chunks, 1):
        h = HEADER.pack(MAGIC, 1, kind, seq, thread, count, len(payload), 0)
        crc = zlib.crc32(payload, zlib.crc32(h[:28]))
        result += h[:28] + struct.pack('<I', crc) + payload + struct.pack('<II', TRAILER, crc)
    return result


class CompactImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.path = Path(self.tmp.name) / 'sample.lxc'

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, data):
        self.path.write_bytes(data)
        return load_compact(self.path)

    def test_normal_report_autodetects_and_retains_binary(self):
        event = EVENT.pack(100, 1, 1, 0) + EVENT.pack(250, 1, 2, 0)
        s = self.load(capture([(4, 1, 2, event)]))
        self.assertTrue(s['quality']['clean_shutdown'])
        self.assertEqual((s['records'][0]['begin_ns'], s['records'][0]['end_ns']), (0, 150))
        self.assertIn('compact/capture.lxc.base64', s['raw_files'])
        output = self.path.with_suffix('.html')
        write_unified_report([self.path], output)
        self.assertTrue(output.is_file())

    def test_pipeline_joins_only_contained_prefill_worker_intervals(self):
        from lxtrace.pipeline import pipeline_data

        def site(site_id, name, category, args):
            return 2, 0, 1, json.dumps(dict(site_id=site_id, name=name, category=category,
                                            args_json=json.dumps(args))).encode()

        worker = (3, 2, 1, json.dumps(dict(thread_id=2, tid=19, name='die0.domain1.cpu38')).encode())
        driver = (3, 3, 1, json.dumps(dict(thread_id=3, tid=20, name='die.0.driver')).encode())
        driver_records = [(100, 2, 1, 5), (110, 3, 1, 5), (180, 3, 2, 5), (200, 2, 2, 5)]
        worker_records = [(120, 4, 1, 5), (160, 4, 2, 5), (185, 4, 1, 5), (195, 4, 2, 5)]
        chunks = [site(2, 'prefill.frame', 'frame', {'kind': 'frame', 'scope': 'die_driver'}),
                  site(3, 'prefill.layer.15', 'unknown', {'layer': 15}),
                  site(4, 'L15 gate_up', 'engine.stage', {'layer': 15, 'stage': 'gate_up'}), worker, driver]
        chunks.extend((4, thread, len(records), b''.join(EVENT.pack(*r) for r in records))
                      for thread, records in ((2, worker_records), (3, driver_records)))
        data = pipeline_data(self.load(capture(chunks)))
        frame = next(f for f in data['frames'] if f['observed'])
        group, = frame['groups']
        self.assertEqual((group['label'], group['source']), ('L15', 'die0.domain1.cpu38'))
        stage, = group['records']
        self.assertEqual(stage[1:4], ['moe', 10, 40])
        unframed = next(f for f in data['frames'] if not f['observed'])
        self.assertEqual((unframed['begin_ns'], unframed['end_ns']), (85, 95))
        self.assertEqual(data['counts'], dict(intervals=4, frames=1))

    def test_crash_and_corruption_never_fabricate_end(self):
        prefix = capture([(4, 1, 1, EVENT.pack(100, 1, 1, 0))], footer=False)
        for data in (prefix, prefix + b'partial', prefix + b'\0'*64):
            with self.subTest(size=len(data)):
                s = self.load(data)
                self.assertFalse(s['quality']['clean_shutdown'])
                self.assertIsNone(s['records'][0]['end_ns'])
                self.assertEqual(s['quality']['compact_valid_prefix_bytes'], len(prefix))

    def test_checksum_error_stops_before_bad_events(self):
        content = bytearray(capture([(4, 1, 2, EVENT.pack(100, 1, 1, 0) + EVENT.pack(250, 1, 2, 0))]))
        # Corrupt the last event payload, invalidating its entire chunk.
        from lxtrace.compact import read_chunks
        self.path.write_bytes(content)
        chunks, _, _ = read_chunks(self.path)
        content[chunks[3]['offset'] + 32] ^= 1
        s = self.load(content)
        self.assertEqual(s['records'], [])
        self.assertFalse(s['quality']['clean_shutdown'])

    def test_loss_bounds_prevent_false_pairing_repeated_sites(self):
        events = EVENT.pack(100, 1, 1, 0) + EVENT.pack(250, 1, 2, 0)
        loss = struct.pack('<8Q', 2, 2, 3, 150, 200, 4, 0, 2)
        s = self.load(capture([(4, 1, 2, events), (5, 1, 1, loss)]))
        self.assertIsNone(s['records'][0]['end_ns'])
        self.assertEqual(s['quality']['dropped_records'], 2)
        self.assertFalse(s['quality']['clean_shutdown'])

    def test_boundary_uint64_counts_and_multiplexing_remain_counter_evidence(self):
        large = 2**60 + 13
        pmu = lambda t, k, v, en, run: (EVENT.pack(t, 1, k, 0) + struct.pack('<IIQ', 1, 0, t+4)
                                       + struct.pack('<QQQ', v, en, run))
        meta = json.dumps({'metadata_json': json.dumps({'pmu_events': [{'type': 4, 'config': 0x7201, 'name': 'MOPA'}]})}).encode()
        s = self.load(capture([(8, 0, 1, meta), (7, 1, 1, pmu(100, 1, large, 80, 40)),
                               (7, 1, 1, pmu(250, 2, large+11, 180, 90))]))
        reads = [r for r in s['records'] if r['representation'] == 'sample']
        self.assertEqual(reads[0]['readings'][0]['value'], large)
        windows = analyze_resources(s)['counters']
        self.assertEqual(windows[0]['raw_count'], 11)
        self.assertEqual(windows[0]['selection_status'], 'multiplexed_estimate')
        self.assertFalse(windows[0]['included_in_totals'])
        matrix = next(c for c in analyze_resources(s)['categories'] if c['category'] == 'mat')
        self.assertEqual(matrix['status'], 'software_only')

    def test_invalid_metadata_preserves_range_but_is_not_clean(self):
        chunks = [(2, 0, 1, json.dumps(dict(site_id=2, name='bad', category='vec', args_json='{"v":NaN}')).encode()),
                  (4, 1, 2, EVENT.pack(100, 2, 1, 0) + EVENT.pack(250, 2, 2, 0))]
        s = self.load(capture(chunks))
        self.assertEqual(len(s['records']), 1)
        self.assertTrue(s['records'][0]['args']['invalid_args'])
        self.assertFalse(s['quality']['clean_shutdown'])

    def test_same_run_compact_pmu_merge_retains_read_windows_and_raw_refs(self):
        pmu = lambda t, k, v: EVENT.pack(t, 1, k, 0) + struct.pack('<IIQ', 1, 0, t+4) + struct.pack('<QQQ', v, t, t)
        data = capture([(7, 1, 1, pmu(100, 1, 1000)), (7, 1, 1, pmu(250, 2, 1100))])
        first = self.load(data)
        first.update(host_id='node', boot_id='boot')
        first['session_id'] = 'first'
        from lxtrace.resource import write_session
        a = self.path.with_name('a.json')
        b = self.path.with_name('b.json')
        write_session(first, a)
        second = self.load(capture([(7, 1, 1, pmu(300, 1, 2000)), (7, 1, 1, pmu(450, 2, 2100))]))
        second.update(host_id='node', boot_id='boot')
        second['session_id'] = 'second'
        write_session(second, b)
        merged = merge_sessions([a, b])
        reads = [r for r in merged['records'] if r['representation'] == 'sample']
        self.assertEqual([r['read_end_ns'] for r in reads], [4, 154, 204, 354])
        for record in merged['records']:
            for ref in record['raw_refs']:
                self.assertIn(ref.split('#')[0], merged['raw_files'])
        second['run_id'] = 'another'
        write_session(second, b)
        with self.assertRaisesRegex(ValueError, 'same run_id'):
            merge_sessions([a, b], run_id='override')

    def test_loss_between_pmu_reads_omits_derived_attribution_window(self):
        pmu = lambda t, k, v: EVENT.pack(t, 1, k, 0) + struct.pack('<IIQ', 1, 0, t+4) + struct.pack('<QQQ', v, t, t)
        s = self.load(capture([(7, 1, 1, pmu(100, 1, 1000)), (7, 1, 1, pmu(250, 2, 1100)),
                               (5, 1, 1, struct.pack('<8Q', 1, 2, 2, 150, 150, 3, 0, 12))]))
        self.assertEqual(len([r for r in s['records'] if r['representation'] == 'sample']), 2)
        self.assertFalse(any(r['representation'] == 'counter_window' for r in s['records']))
        self.assertFalse(s['quality']['clean_shutdown'])

    def test_later_clean_footer_cannot_erase_post_shutdown_events(self):
        footer = json.dumps(dict(quality_semantics='cumulative_snapshot', quality_revision=1,
                                 dropped_records=0, protocol_errors=0, clean_shutdown=True)).encode()
        s = self.load(capture([(6, 0, 1, footer),
                               (4, 1, 2, EVENT.pack(100, 1, 1, 0) + EVENT.pack(250, 1, 2, 0))]))
        self.assertFalse(s['quality']['clean_shutdown'])
        self.assertTrue(s['quality']['capture_partial'])
        self.assertTrue(any('events follow' in w for w in s['quality']['warnings']))

    def test_large_input_rejected_before_normalization_without_silent_sampling(self):
        events = b''.join(EVENT.pack(t, 1, 3, 0) for t in (100, 200, 300))
        self.path.write_bytes(capture([(4, 1, 3, events)]))
        with self.assertRaisesRegex(ValueError, 'compact-info and compact-select'):
            load_compact(self.path, max_records=2)
        self.assertEqual(len(load_compact(self.path, max_records=3)['records']), 3)
        with self.assertRaisesRegex(ValueError, 'byte budget'):
            load_compact(self.path, max_bytes=16)

    def test_inter_capture_delta_is_retained_without_false_scope_attribution(self):
        pmu = lambda t, k, v: EVENT.pack(t, 1, k, 0) + struct.pack('<IIQ', 1, 0, t+4) + struct.pack('<QQQ', v, t, t)
        s = self.load(capture([(7, 1, 1, pmu(t, k, v)) for t, k, v in
                               ((100, 1, 1000), (200, 2, 1100), (1000, 1, 5000), (1100, 2, 5200))]))
        windows = [r for r in s['records'] if r['representation'] == 'counter_window']
        self.assertEqual([w['raw_count'] for w in windows], [100, 3900, 200])
        self.assertEqual([w['attribution_scope'] for w in windows],
                         ['matching_software_range', 'unassigned_cumulative_window', 'matching_software_range'])
        self.assertIsNone(windows[1]['software_scope_id'])
        self.assertEqual(windows[1]['frame_attribution'], 'unproven')


if __name__ == '__main__':
    unittest.main()
