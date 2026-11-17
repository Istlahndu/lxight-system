"""Hardware event meanings are independent of model names and rendering."""
import copy
import json
import unittest
from pathlib import Path

from lxtrace.attribution.hardware import (EVENT_ROLES, REQUIRED_CODES,
    canonical_counts, event_code, load_hardware, required_codes,
    split_compute, split_dense, split_issue, unit_load)
from lxtrace.workspace import temporary_directory


class HardwareProfileTests(unittest.TestCase):
    def setUp(self):
        self.hw = load_hardware()
        self.raw = {code: 0 for code in EVENT_ROLES.values()}
        self.raw.update({'0x7201': 20, '0x7005': 50, '0x7007': 30, '0x7008': 10,
                         '0x0017': 10, '0x6055': 2, '0x6057': 1,
                         '0x8006': 100, '0x8084': 50, '0x0008': 150})

    def test_three_views_use_selected_raw_dictionary_and_preserve_input(self):
        changed = copy.deepcopy(self.hw)
        changed['profile_id'] = 'other-chip-compatible-semantics'
        remapped = {}
        for i, (role, old_code) in enumerate(EVENT_ROLES.items()):
            code = '0x%04x' % (0xa000 + i)
            changed['events'][role]['code'] = code
            remapped[code] = self.raw[old_code]
        before = copy.deepcopy(remapped)
        for fn in (split_compute, split_issue, unit_load):
            self.assertEqual(fn({'cycles': 100, 'counters': self.raw}, self.hw),
                             fn({'cycles': 100, 'counters': remapped}, changed))
        self.assertEqual(remapped, before)
        self.assertEqual(canonical_counts(remapped, changed), self.raw)
        self.assertEqual(event_code(changed, '0x7201'), '0xa000')
        self.assertEqual(event_code(changed, 'matrix_instruction'), '0xa000')
        self.assertEqual(required_codes(changed)['matrix_first'][0], '0xa000')

    def test_dictionary_permutations_do_not_confuse_input_and_semantic_codes(self):
        changed = copy.deepcopy(self.hw)
        a, b = 'matrix_instruction', 'load_stall'
        changed['events'][a]['code'], changed['events'][b]['code'] = (
            changed['events'][b]['code'], changed['events'][a]['code'])
        raw = dict(self.raw)
        raw['0x7201'], raw['0x7005'] = raw['0x7005'], raw['0x7201']
        self.assertEqual(split_compute({'cycles': 100, 'codes': raw}, changed),
                         split_compute({'cycles': 100, 'codes': self.raw}, self.hw))
        self.assertEqual(canonical_counts({'0x7201': 7}, changed), {'0x7005': 7})

    def test_old_hardware_json_keeps_default_semantics_and_numeric_parameters(self):
        old = copy.deepcopy(self.hw)
        old.pop('events')
        old.pop('decomposition')
        loaded = load_hardware(old)
        self.assertEqual(loaded['event_mapping_basis'], 'legacy_lx2_semantics')
        self.assertEqual(required_codes(loaded), REQUIRED_CODES)
        self.assertEqual(split_compute({'cycles': 100, 'codes': self.raw}, old),
                         split_compute({'cycles': 100, 'codes': self.raw}, self.hw))
        old['facts']['cycles_per_mopa'] = 1
        self.assertEqual(split_compute({'cycles': 100, 'codes': self.raw}, old)[0]['mat'], 20)

    def test_unsupported_algorithm_and_incomplete_dictionary_fail_explicitly(self):
        changed = copy.deepcopy(self.hw)
        changed['decomposition'] = 'unknown-chip-formula'
        with self.assertRaisesRegex(ValueError, 'unsupported hardware decomposition'):
            load_hardware(changed)
        for bad in ({}, {'matrix_instruction': {'code': '0x7201'}}, [], None):
            changed = copy.deepcopy(self.hw)
            changed['events'] = bad
            with self.assertRaisesRegex(ValueError, 'semantic roles'):
                load_hardware(changed)

    def test_invalid_aliasing_or_encodings_and_unknown_roles_are_rejected(self):
        for code in (True, -1, 65536, '7201', '0xno', {}, 1.5):
            with self.subTest(code=code):
                changed = copy.deepcopy(self.hw)
                changed['events']['matrix_instruction']['code'] = code
                with self.assertRaises(ValueError):
                    load_hardware(changed)
        changed = copy.deepcopy(self.hw)
        changed['events']['matrix_instruction']['code'] = '0x7005'
        with self.assertRaisesRegex(ValueError, 'distinct'):
            load_hardware(changed)
        with self.assertRaisesRegex(ValueError, 'unknown hardware event'):
            event_code(self.hw, '0xbeef')

    def test_custom_profile_file_changes_hardware_without_model_or_renderer(self):
        with temporary_directory() as directory:
            path = Path(directory) / 'hardware.json'
            changed = copy.deepcopy(self.hw)
            changed['facts']['sve_per_cycle'] = 4
            path.write_text(json.dumps(changed), encoding='utf-8')
            result = unit_load({'cycles': 100, 'codes': self.raw}, path)
            self.assertEqual(result['sve'], .125)
            self.assertEqual(load_hardware(path)['facts']['cores'], 608)

    def test_detail_algorithm_uses_plugin_scope_and_hardware_raw_encodings(self):
        cfg = {'stage': 'projection', 'matrix_steps': {'tile': 8}}
        codes = list(EVENT_ROLES.values())
        rows = {}
        for mode, cycles in (('tile', 10000), ('vector', 2000)):
            raw = dict(self.raw)
            raw.update({'0x7201': 1000 if mode == 'tile' else 0, '0x7005': 4000,
                        '0x7007': 2500, '0x7008': 2000, '0x0008': 5000})
            rows['projection|' + mode] = dict(raw, cycles=cycles, ticks=cycles, ccyc_tA=cycles * 1.1,
                                              **{'raw_' + code: value for code, value in raw.items()})
        rows['projection|sync'] = dict(cycles=100, ticks=100)
        baseline = split_dense(rows, {'tA': codes}, self.hw, cfg, ['sync'])
        self.assertEqual(baseline['status'], 'available')
        before = copy.deepcopy(rows)
        # A permutation reuses existing RAW keys with different semantics. It
        # catches accidental second normalization as well as fixed-code lookups.
        changed = copy.deepcopy(self.hw)
        actual = codes[1:] + codes[:1]
        mapping = dict(zip(codes, actual))
        for role, old_code in EVENT_ROLES.items():
            changed['events'][role]['code'] = mapping[old_code]
        mapped_rows = {}
        for key, row in rows.items():
            mapped_rows[key] = {(mapping[k] if k in mapping else
                                 'raw_' + mapping[k[4:]] if k.startswith('raw_') else k): v
                                for k, v in row.items()}
        result = split_dense(mapped_rows, {'tA': actual}, changed, cfg, ['sync'])
        reverse = {v: k for k, v in mapping.items()}
        for step in result['steps'].values():
            for field in ('rates', 'counts', 'raw_counts'):
                if field in step:
                    self.assertTrue(set(step[field]) <= set(actual))
                    step[field] = {reverse[k]: v for k, v in step[field].items()}
        self.assertEqual(result, baseline)
        self.assertEqual(rows, before)
        self.assertEqual(baseline['steps']['tile']['k'], 5)
        self.assertEqual(baseline['total']['wait'], 100)

    def test_detail_without_config_does_not_assume_a_model_specific_stage(self):
        self.assertEqual(split_dense({'dense_gemm|mmg': {'cycles': 1}}, hardware=self.hw),
                         {'status': 'unavailable', 'reason': 'detail_not_configured'})
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            split_dense({}, hardware=self.hw, dense_config={'stage': 'projection', 'matrix_steps': {'x': 0}})


if __name__ == '__main__':
    unittest.main()
