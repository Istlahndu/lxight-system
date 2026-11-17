"""Node-counter clocks merge by integer ticks, never by a renamed OS clock."""
import copy
import struct
import unittest
from pathlib import Path

from lxtrace import resource
from lxtrace.report import merge_sessions
from lxtrace.workspace import temporary_directory


BASE = 120_000_000_000_000


def session(name, origin, frequency=3, hardware=False, raw=True):
    ns = lambda delta: delta * 1_000_000_000 // frequency
    record = dict(record_id='span', resource_id='thread', source_id='source',
                  layer='hardware' if hardware else 'software', category='pmu' if hardware else 'mat',
                  representation='counter_window' if hardware else 'interval',
                  begin_ns=ns(2), end_ns=ns(3), name=name)
    if raw:
        record.update(raw_begin_ticks=origin+2, raw_end_ticks=origin+3)
    if hardware:
        record.update(raw=90, scaled=90, enabled_ns=ns(3)-ns(2), running_ns=ns(3)-ns(2))
    return dict(schema_version=resource.SCHEMA, session_id=name, run_id='shared-run',
                host_id='node', boot_id='boot', data_origin='imported', time_range=[0, ns(4)],
                resources=[dict(resource_id='thread', type='thread', parent_id=None)],
                sources=[dict(source_id='source', source_layer=record['layer'], clock_id='counter')],
                clocks=[dict(clock_id='counter', native_clock_id='CNTVCT_EL0', unit='ns',
                             frequency_hz=frequency, origin_ticks=origin, last_ticks=origin+4, error_ns=None)],
                records=[record], provenance=[], capability_manifest=[], quality={})


class CNTVCTMergeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = temporary_directory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def save(self, value):
        path = self.root / (value['session_id'] + '.json')
        resource.write_session(value, path)
        return path

    def test_nondivisible_frequency_recomputes_from_raw_ticks_and_remerges(self):
        paths = [self.save(session('a', BASE)), self.save(session('b', BASE+1))]
        merged = merge_sessions(paths)
        times = {r['name']: (r['begin_ns'], r['end_ns']) for r in merged['records']}
        self.assertEqual(times['b'], (1_000_000_000, 1_333_333_333))
        self.assertTrue(all(c['native_clock_id'] == 'CNTVCT_EL0' for c in merged['clocks']))
        self.assertTrue(all(c['error_ns'] is None for c in merged['clocks']))
        self.assertEqual({c['origin_ticks'] for c in merged['clocks']}, {BASE})
        merged_path = self.save(merged)
        third = self.save(session('c', BASE-1))
        again = merge_sessions([merged_path, third])
        b = next(r for r in again['records'] if r['name'] == 'b')
        self.assertEqual((b['begin_ns'], b['end_ns']), (1_333_333_333, 1_666_666_666))

    def test_integer_ns_can_reconstruct_unique_legacy_ticks(self):
        paths = [self.save(session('a', BASE, raw=False)), self.save(session('b', BASE+1, raw=False))]
        b = next(r for r in merge_sessions(paths)['records'] if r['name'] == 'b')
        self.assertEqual(b['begin_ns'], 1_000_000_000)

    def test_same_run_counter_windows_preserve_values_and_original_evidence(self):
        first, second = session('a', BASE, hardware=True), session('b', BASE+1, hardware=True)
        paths = [self.save(first), self.save(second)]
        before = [p.read_bytes() for p in paths]
        result = merge_sessions(paths)
        self.assertEqual([r['raw'] for r in result['records']], [90, 90])
        self.assertTrue(all(r['representation'] == 'counter_window' for r in result['records']))
        self.assertEqual([p.read_bytes() for p in paths], before)
        other = copy.deepcopy(second)
        other.update(session_id='separate', run_id='counting-run-B')
        with self.assertRaisesRegex(ValueError, 'same run_id'):
            merge_sessions([paths[0], self.save(other)], run_id='explicit')

    def test_aggregate_labels_and_structured_raw_refs_are_not_capture_identities(self):
        values = [session('a', BASE, hardware=True), session('b', BASE+1, hardware=True)]
        for value in values:
            value['raw_files'] = {'raw/readings.base64': 'AAAA'}
            value['records'][0]['raw_refs'] = [dict(path='raw/readings.base64', encoding='base64', event_indices=[0, 1])]
            value['records'].append(dict(record_id='same-aggregate-label', resource_id='thread', source_id='source',
                                         layer='hardware', category='pmu', representation='aggregate', value=90, label='layer|busy'))
        result = merge_sessions([self.save(v) for v in values])
        self.assertEqual(sum(r.get('value', 0) for r in result['records'] if r['representation'] == 'aggregate'), 180)
        for r in result['records']:
            for ref in r['raw_refs']:
                self.assertIn(ref['path'], result['raw_files'])
                self.assertEqual(ref['event_indices'], [0, 1])
                self.assertEqual(ref['encoding'], 'base64')

    def test_counter_merge_rejects_other_hosts_boots_frequencies_and_domains(self):
        first = self.save(session('a', BASE))
        for field, value in [('host_id', 'elsewhere'), ('boot_id', 'reboot')]:
            other = session('b', BASE+1)
            other[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                merge_sessions([first, self.save(other)])
        for field, value in [('frequency_hz', 4), ('counter_domain_id', 'other-domain')]:
            other = session('b', BASE+1)
            other['clocks'][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'frequency_hz|domain'):
                merge_sessions([first, self.save(other)])

    def test_grouping_cannot_calibrate_cntvct_against_monotonic(self):
        first = self.save(session('a', BASE))
        other = session('b', BASE+1)
        other['clocks'] = [dict(clock_id='counter', native_clock_id='CLOCK_MONOTONIC', origin_ns=1)]
        with self.assertRaisesRegex(ValueError, 'calibration'):
            merge_sessions([first, self.save(other)], run_id='explicit')

    def test_raw_ticks_must_agree_and_high_frequency_requires_raw_ticks(self):
        first = self.save(session('a', BASE))
        other = session('b', BASE+1)
        other['records'][0]['raw_begin_ticks'] += 1
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            merge_sessions([first, self.save(other)])
        first = self.save(session('fast-a', BASE, frequency=2_000_000_000))
        other = session('fast-b', BASE+1, frequency=2_000_000_000, raw=False)
        with self.assertRaisesRegex(ValueError, 'raw ticks'):
            merge_sessions([first, self.save(other)])

    def test_high_frequency_merges_when_raw_ticks_cover_records_and_bounds(self):
        paths = [self.save(session('fast-a', BASE, frequency=2_000_000_000)),
                 self.save(session('fast-b', BASE+1, frequency=2_000_000_000))]
        result = merge_sessions(paths)
        self.assertEqual(result['time_range'], [0, 2])
        self.assertEqual(len(result['records']), 2)

    def test_gftrace1_bins_and_shared_metadata_merge_without_external_conversion(self):
        paths = []
        for rank in range(2):
            path = self.root / ('rank%d.bin' % rank)
            path.write_bytes(struct.pack('<8sQii4i', b'GFTRACE1', 100_000_000, 1, 0, rank, 0, 0, 2)
                             + struct.pack('<QHHiQHHi', BASE+rank, 1, 3, -1, BASE+rank+100, 2, 3, -1))
            path.with_suffix('.txt').write_text('host_id node\nboot_id boot\nrun_id shared\n', encoding='utf-8')
            paths.append(path)
        result = merge_sessions(paths)
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(result['time_range'], [0, 1010])
        self.assertIsNone(result['quality']['dropped_records'])
        self.assertTrue(all(c['native_clock_id'] == 'CNTVCT_EL0' for c in result['clocks']))


if __name__ == '__main__':
    unittest.main()
