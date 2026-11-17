"""Model semantics and FLOP adapters; independent of cluster and private traces."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from lxtrace.plugins import calculate_flops, list_models, load_model, presentation
from lxtrace.plugins import glm5f_flops as G


def glm_config():
    """Published GLM-5.3-Flash dimensions, not a private checkpoint fixture."""
    return {'text_config': {
        'num_hidden_layers': 45, 'hidden_size': 4096,
        'layer_types': [G.DSA if n % 4 == 3 else G.KDA for n in range(45)],
        'mlp_layer_types': ['dense'] * 3 + ['sparse'] * 42,
        'indexer_types': ['full'] * 45,
        'linear_attn_config': {'num_heads': 64, 'head_dim': 128, 'short_conv_kernel_size': 4,
                               'kda_layers': [n for n in range(45) if n % 4 != 3],
                               'full_attn_layers': list(range(3, 45, 4))},
        'first_k_dense_replace': 3, 'num_attention_heads': 64, 'num_key_value_heads': 64,
        'qk_head_dim': 256, 'qk_nope_head_dim': 256, 'qk_rope_head_dim': 0, 'v_head_dim': 256,
        'q_lora_rank': 1536, 'kv_lora_rank': 512,
        'index_n_heads': 32, 'index_head_dim': 128, 'index_kpool': 4, 'index_topk': 2048,
        'index_kpool_always_select_tail': True, 'mhc': True,
        'moe_intermediate_size': 2048, 'n_routed_experts': 288, 'num_experts_per_tok': 8,
        'n_shared_experts': 1, 'intermediate_size': 12288,
        'vocab_size': 154880, 'hc_mult': 4, 'hc_sinkhorn_iters': 20,
    }}


class ModelPluginTests(unittest.TestCase):
    def test_default_glm_semantics_and_independent_copies(self):
        a, b = load_model(), load_model('glm5f')
        self.assertEqual(a, b)
        self.assertEqual(a['trace']['sites']['4'], {'mode': 'mmg', 'category': 'mat'})
        self.assertEqual(a['trace']['sites']['3'], {'mode': 'deqg', 'category': 'vec'})
        self.assertEqual(a['trace']['sites']['1']['category'], 'cx')
        self.assertIn('dense_gemm', a['trace']['stages'].values())
        a['trace']['sites']['6']['category'] = 'cx'
        self.assertEqual(b['trace']['sites']['6']['category'], 'mat')

    def test_legacy_v1_profile_remains_loadable(self):
        old = dict(schema_version='lxtrace.attribution-model.v1', profile_id='legacy',
                   compute_modes=['busy'], exchange_modes=['copy'], wait_modes=['bar'], idle_modes=['idle'],
                   stage_families={'compute': ['layer']})
        model = load_model(old)
        self.assertEqual(model['flop']['adapter'], 'none')
        self.assertEqual(model['trace']['sites'], {})
        self.assertEqual(model['mode_categories']['copy'], 'cx')
        self.assertEqual(presentation(model)['stages']['layer']['family'], 'compute')
        self.assertNotIn('trace', old)

    def test_legacy_glm_profile_gets_only_declared_compatibility_semantics(self):
        original = load_model('glm5f')
        for identity in ({'profile_id': 'glm5f'}, {'profile_id': 'custom-glm', 'model': 'GLM-5.3-Flash'}):
            old = copy.deepcopy(original)
            old.pop('trace')
            old.update(identity)
            restored = load_model(old)
            self.assertEqual(restored['trace'], original['trace'])
            self.assertEqual(restored['trace_compatibility']['basis'], 'shipped_glm5f_legacy_v1')
        other = copy.deepcopy(original)
        other.pop('trace')
        other.update(profile_id='different-model', model='Different architecture')
        self.assertEqual(load_model(other)['trace']['sites'], {})

    def test_swapped_plugin_changes_semantics_and_display_without_glm_rules(self):
        model = load_model('generic-transformer')
        self.assertEqual(model['trace']['sites']['41']['mode'], 'affine')
        self.assertNotIn('mmg', model['mode_categories'])
        view = presentation(model)
        self.assertEqual(view['stages']['projection']['label'], 'Projection stage')
        self.assertEqual(view['stages']['projection']['color'], '#336699')
        self.assertFalse(view['dense']['enabled'])
        self.assertFalse(view['views']['dense_enabled'])
        self.assertNotIn('dense_gemm', view['stages'])

    def test_presentation_uses_hardware_terms_and_model_overrides(self):
        model = load_model('generic-transformer')
        model['presentation'].update(labels={'matrix_instruction': 'Product op'},
                                     metric_labels={'k': 'Instructions / product'},
                                     palettes={'units': {'sve': ['Portable vector', '#123456']}})
        view = presentation(model, {'display': {'instruction_label': 'Hardware op', 'vector_label': 'SIMD',
                                               'unit_labels': {'sve': 'Chip vector'}},
                                    'presentation': {'palettes': {'units': {'sve': ['SVE', '#000000'],
                                                                            'mat': ['Matrix', '#abcdef']}}}})
        self.assertEqual(view['labels']['matrix_instruction'], 'Product op')
        self.assertEqual(view['labels']['vector_unit'], 'SIMD')
        self.assertEqual(view['palettes']['units']['sve'], ['Portable vector', '#123456'])
        self.assertEqual(view['palettes']['units']['mat'], ['Matrix', '#abcdef'])
        self.assertEqual(view['metric_labels']['k'], 'Instructions / product')

    def test_catalog_is_declarative_and_custom_file_takes_priority(self):
        self.assertEqual({x['profile_id'] for x in list_models()}, {'glm5f', 'generic-transformer'})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'glm5f'
            path.write_text(json.dumps(load_model('generic-transformer')), encoding='utf-8')
            self.assertEqual(load_model(path)['profile_id'], 'generic-transformer')
            with self.assertRaises(OSError):
                load_model(Path(tmp) / 'not-a-plugin.json')

    def test_invalid_declarations_fail_before_import_or_plot(self):
        edits = [
            lambda p: p['exchange_modes'].append('affine'),
            lambda p: p['compute_modes'].append('affine'),
            lambda p: p['compute_modes'].remove('busy'),
            lambda p: p['wait_modes'].remove('spin'),
            lambda p: p['compute_modes'].append('invalid|mode'),
            lambda p: p['trace']['stages'].update({'0': 'invalid|stage'}),
            lambda p: p['trace']['sites']['41'].update(mode='not_declared'),
            lambda p: p['trace']['sites']['41'].update(category='cx'),
            lambda p: p['trace']['sites']['41'].update(category=[]),
            lambda p: p['trace']['sites']['41'].update(mode=[]),
            lambda p: p['trace']['mode_categories'].update(busy='cx'),
            lambda p: p['trace']['mode_categories'].update(bar='mat'),
            lambda p: p['trace']['mode_categories'].update(idle='wait'),
            lambda p: p['trace']['mode_categories'].update(affine='cx') or p['trace']['sites']['41'].update(category='cx'),
            lambda p: p['trace']['sites'].update({'041': {'mode': 'affine', 'category': 'mat'}}),
            lambda p: p['trace']['stages'].update({'65536': 'invalid'}),
            lambda p: p['trace'].update(format=['python-module']),
            lambda p: p['flop'].update(adapter='module:function'),
            lambda p: p['flop'].update(adapter=[]),
            lambda p: p['stage_families']['encoder'].update(color='url(javascript:x)'),
            lambda p: p['stage_families']['transport']['stages'].append('projection'),
            lambda p: p['presentation'].update(stage_labels=['invalid']),
            lambda p: p['presentation'].update(views={'dense_enabled': 'false'}),
            lambda p: p['presentation'].update(palettes={'units': {'mat': ['M', 'red']}}),
            lambda p: p.update(expected_pool_threads=True),
            lambda p: p.update(not_finite=float('nan')),
        ]
        for edit in edits:
            model = load_model('generic-transformer')
            edit(model)
            with self.subTest(edit=edits.index(edit)), self.assertRaises(ValueError):
                load_model(model)

    def test_generic_flop_hand_calculation_and_scope(self):
        config = {'num_hidden_layers': 2, 'hidden_size': 8}
        result = calculate_flops('generic-transformer', config, {'T': 3, 'B': 2})
        # Two layers: 2*B*T*H*H matrix + 4*B*T*H activation, per layer.
        self.assertEqual(result['phases'], {'projection': 1536, 'encoder': 384})
        self.assertEqual(result['total'], 1920)
        self.assertEqual(result['configuration']['layers'], [0, 1])
        self.assertEqual(result['window_correspondence'], 'not_proven_by_counter_alone')
        smaller = calculate_flops('generic-transformer', config, {'T': 3, 'B': 2, 'layers': [1, 1]})
        self.assertEqual(smaller['total'], 960)
        larger = calculate_flops('generic-transformer', dict(config, hidden_size=16), {'T': 3, 'B': 2})
        self.assertEqual(larger['total'], 2 * (2 * 6 * 16 * 16 + 4 * 6 * 16))
        self.assertNotEqual(result['model_config_sha256'], larger['model_config_sha256'])

    def test_flop_config_path_and_dict_give_same_result(self):
        config = {'num_hidden_layers': 2, 'hidden_size': 8}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'config.json').write_text(json.dumps(config), encoding='utf-8')
            self.assertEqual(calculate_flops('generic-transformer', tmp, {'T': 3}),
                             calculate_flops('generic-transformer', config, {'T': 3}))

    def test_unavailable_flop_is_explicit(self):
        for adapter in ('none', 'external_summary'):
            model = load_model('generic-transformer')
            model['flop'] = {'adapter': adapter}
            result = calculate_flops(model)
            self.assertEqual(result['status'], 'unavailable')
            self.assertIsNone(result['total'])

    def test_flop_workload_and_formula_references_are_validated(self):
        config = {'num_hidden_layers': 2, 'hidden_size': 8}
        for work in ({'T': 0}, {'T': True}, {'T': 1, 'B': 0}, {'T': 1, 'T_c': -1},
                     {'T': 1, 'layers': [0, 2]}, {'T': 1, 'layers': [1, 0]}):
            with self.subTest(work=work), self.assertRaises(ValueError):
                calculate_flops('generic-transformer', config, work)
        for value in ('__import__("os").system("x")', 'config.hidden_size*2', 'hidden_size', 1.5, True):
            model = load_model('generic-transformer')
            model['flop']['components'][0]['k'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate_flops(model, config, {'T': 1})
        with self.assertRaises(ValueError):
            calculate_flops('generic-transformer', dict(config, hidden_size='8'), {'T': 1})
        with self.assertRaises(ValueError):
            calculate_flops('generic-transformer', {'text_config': None}, {'T': 1})


class GlmFlopTests(unittest.TestCase):
    def test_layer_types_and_context_free_reference_values(self):
        shape = G.load_shape(glm_config())
        self.assertEqual((len(shape.kda_layers), len(shape.dsa_layers)), (34, 11))
        self.assertEqual(shape.dense_layers, [0, 1, 2])
        def per_token(layer):
            result = {}
            for term in G.layer_terms(shape, layer):
                result[term.row] = result.get(term.row, 0) + term.flop
            return result
        kda, dsa = per_token(8), per_token(11)
        self.assertEqual(kda['kda_proj'], 275251200)
        self.assertEqual(kda['kda_core'], 64 * 144619)
        self.assertEqual(kda['kda_elem'], 294912 + 57344 + 49344 + 65536)
        self.assertEqual(dsa['dsa_proj'], 234881024 + 8192)
        self.assertEqual(dsa['idx_proj'], 14942208 + 1792)
        self.assertEqual(kda['router'], 2360472)
        self.assertEqual(kda['routed'], 402849792)
        self.assertEqual(kda['shared'], 50348032 + 4096)
        self.assertEqual(per_token(0)['dense_mlp'], 302088192)
        self.assertEqual(G.head_flops(shape), 1268826112)
        self.assertEqual(G.pool_score_flop(shape), 8320)
        self.assertEqual(G.key_flop(shape), 65920)

    def test_long_context_reference_totals_and_stage_partition(self):
        shape = G.load_shape(glm_config())
        for T, want in ((4096, 33733e6), (16384, 34152e6), (65536, 34784e6)):
            lines = G.count(shape, T)
            self.assertAlmostEqual(G.total(lines) / T, want, delta=0.0005e9)
            self.assertAlmostEqual(sum(G.by_stage(lines).values()), G.total(lines), delta=1)

    def test_chunked_context_and_batch_scaling(self):
        config = glm_config()
        whole = calculate_flops('glm5f', config, {'T': 4099, 'layers': [11, 11]})
        a = calculate_flops('glm5f', config, {'T': 2000, 'layers': [11, 11]})
        b = calculate_flops('glm5f', config, {'T': 2099, 'T_c': 2000, 'layers': [11, 11]})
        self.assertEqual(whole['total'], a['total'] + b['total'])
        batch = calculate_flops('glm5f', config, {'T': 4099, 'B': 8, 'layers': [11, 11]})
        self.assertEqual(batch['total'], whole['total'] * 8)
        self.assertNotIn('head', whole['phases'])
        last = calculate_flops('glm5f', config, {'T': 7, 'B': 2, 'layers': [44, 44]})
        self.assertEqual(last['phases']['head'], 2 * G.head_flops(G.load_shape(config)))

    def test_unsupported_glm_variants_fail_instead_of_miscounting(self):
        for field, value in (('num_experts_per_tok', 6), ('n_routed_experts', 256),
                             ('first_k_dense_replace', 2), ('qk_rope_head_dim', 64),
                             ('hidden_size', -1), ('hidden_size', True), ('mhc', False)):
            config = glm_config()
            config['text_config'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                calculate_flops('glm5f', config, {'T': 1})
        bad = glm_config()
        bad['text_config']['index_kpool'] = 0
        with self.assertRaises(ValueError):
            calculate_flops('glm5f', bad, {'T': 1})


if __name__ == '__main__':
    unittest.main()
