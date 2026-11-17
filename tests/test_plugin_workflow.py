"""Public CLI plugin selection across synthetic native capture formats."""
import contextlib
import io
import json
import struct
import unittest
from pathlib import Path

from lxtrace.cli import main
from lxtrace.plugins import load_model
from lxtrace.resource import load_session
from lxtrace.workspace import temporary_directory


FREQ = 100_000_000
ORIGIN = 2**60 + 17


class PluginWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_cli(self, command, expected=0):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = main(list(map(str, command)))
        self.assertEqual(result, expected, err.getvalue())
        return out.getvalue(), err.getvalue()

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def plugin(self):
        model = load_model('generic-transformer')
        model.update(profile_id='custom-decoder', model='Custom decoder test', expected_pool_threads=1)
        model['trace']['stages'] = {'0': 'encoder', '1': 'plugin_projection', '2': 'redistribute'}
        model['trace']['sites'] = {'141': {'mode': 'affine', 'category': 'mat'},
                                   '142': {'mode': 'activation', 'category': 'vec'},
                                   '143': {'mode': 'shuffle', 'category': 'cx'}}
        model['stage_families']['encoder']['stages'] = ['encoder', 'plugin_projection']
        model['presentation']['stage_labels'] = {'plugin_projection': 'Plugin projection label'}
        return self.write_json('custom-model.json', model)

    def trace(self, name, *, version=2, counting=False, custom=False, metadata_stage=None, sites=None):
        matrix, vector, exchange = sites or ((141, 142, 143) if custom else (41, 42, 43))
        events = [(0, 1, 0, 3), (10, 23, matrix, 0), (30, 23, vector, 0),
                  (50, 23, exchange, 0), (70, 24, exchange, 0),
                  (80, 3, 1, 0), (90, 4, 1, 0), (100, 2, 0, 3)]
        driver = [(0, 9, 1, 0), (100, 10, 1, 0)]
        buffers = [(0, -1, -1, driver), (0, 0, 0, events)]
        path = self.root / (name + '.bin')
        raw = bytearray(struct.pack('<8sQii', ('GFTRACE%d' % version).encode(), FREQ, len(buffers), 0))
        for die, dom, thr, entries in buffers:
            if version == 2:
                raw.extend(struct.pack('<6i', die, dom, thr, len(entries), 9 if counting else 0, 0))
            else:
                raw.extend(struct.pack('<4i', die, dom, thr, len(entries)))
            raw.extend(b''.join(struct.pack('<QHHi', ORIGIN+t, kind, code, aux) for t, kind, code, aux in entries))
            if version == 2 and counting:
                raw.extend(b''.join(struct.pack('<9Q', n*100, n*10, n*20, 0, 0, 0, 0, 0, 0)
                                    for n in range(len(entries))))
        path.write_bytes(raw)
        metadata = ('run %s T 3 B 2 dies 1 domains 1 freq %d\nlayer 0 synthetic\n'
                    'frame 0 counting 1 parts 1 tokens 3\npmu %s\n' %
                    (name, FREQ, '0x7201,0x0008' if counting else 'ticks'))
        if metadata_stage:
            metadata += 'stage 1 ' + metadata_stage + '\n'
        path.with_suffix('.txt').write_text(metadata, encoding='utf-8')
        return path

    def production(self, *, model='generic-transformer', custom=False, extra=(), name='production'):
        timing = self.trace(name+'-tT', custom=custom)
        counts = self.trace(name+'-tA', counting=True, custom=custom)
        output = self.root / (name+'.html')
        command = ['production-report', '--timing', timing, '--count', 'tA='+str(counts),
                   '--model', model, '--output', output, *extra]
        self.run_cli(command)
        return json.loads(output.with_suffix('.analysis.json').read_text(encoding='utf-8')), output

    def test_plugin_catalog_and_model_flop_cli_hand_calculation(self):
        out, _ = self.run_cli(['plugins'])
        catalog = json.loads(out)
        self.assertIn('generic-transformer', {m['profile_id'] for m in catalog['models']})
        self.assertTrue(catalog['hardware'])
        config = self.write_json('config.json', {'hidden_size': 8, 'num_hidden_layers': 2})
        workload = self.write_json('workload.json', {'T': 3, 'B': 2})
        output = self.root/'flop.json'
        self.run_cli(['model-flops', '--model', 'generic-transformer', '--config', config,
                      '--workload', workload, '--output', output])
        result = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(result['total'], 1920)
        self.assertEqual(result['phases'], {'projection': 1536, 'encoder': 384})
        self.assertEqual(result['window_correspondence'], 'not_proven_by_counter_alone')

    def test_production_builtin_and_custom_plugins_change_attribution_and_descriptor(self):
        for custom in (False, True):
            with self.subTest(custom=custom):
                model = self.plugin() if custom else 'generic-transformer'
                analysis, output = self.production(model=model, custom=custom, name='p'+str(custom))
                expected = 'plugin_projection' if custom else 'projection'
                self.assertIn(expected, analysis['stages'])
                self.assertEqual({e['mode'] for e in analysis['entries'] if e['mode'] not in ('busy', 'bar', 'idle')},
                                 {'affine', 'activation', 'shuffle'})
                self.assertGreater(analysis['totals']['matrix_first']['comm_x'], 0)
                self.assertFalse(analysis['presentation']['views']['dense_enabled'])
                self.assertEqual(analysis['presentation']['stages'][expected]['color'], '#336699')
                self.assertFalse(analysis['hardware_interval'])
                self.assertIn('Custom decoder test' if custom else 'Generic transformer', output.read_text(encoding='utf-8'))
                self.assertEqual(analysis['model']['profile_id'], 'custom-decoder' if custom else 'generic-transformer')

    def test_both_native_formats_import_report_and_metadata_stage_precedence(self):
        model = self.plugin()
        for version in (1, 2):
            with self.subTest(version=version):
                source = self.trace('v%d'%version, version=version, custom=True, metadata_stage='runtime_projection')
                archive = self.root / ('v%d.lxtrace'%version)
                html = self.root / ('v%d.html'%version)
                self.run_cli(['import', '--type', 'gftrace', '--input', source, '--model', model, '--output', archive])
                self.run_cli(['report', source, '--model', model, '--output', html])
                session = load_session(archive)
                self.assertTrue({'mat', 'vec', 'cx'} <= {r['category'] for r in session['records']})
                if version == 2:
                    steps = [r for r in session['records'] if r['source_id'] == 'gf.sw' and 'mode' in r.get('args',{})]
                    self.assertIn('runtime_projection', {r['args']['stage'] for r in steps})
                    self.assertEqual({r['args']['mode'] for r in steps}, {'busy', 'bar', 'affine', 'activation', 'shuffle'})
                else:
                    args = [r['args'] for r in session['records']]
                    self.assertTrue(any(a.get('substep_mode') == 'affine' for a in args))
                    self.assertTrue(any(a.get('wait_object') == 'runtime_projection' for a in args))
                self.assertIn('<html', html.read_text(encoding='utf-8').lower())

    def test_format_restrictions_are_respected_by_import_report_and_production(self):
        for version in (1, 2):
            model = load_model('generic-transformer')
            model['trace']['format'] = ['GFTRACE%d' % (3-version)]
            plugin = self.write_json('restrict%d.json'%version, model)
            source = self.trace('restricted%d'%version, version=version)
            for command in (['import','--type','gftrace','--input',source,'--model',plugin],
                            ['report',source,'--model',plugin]):
                output = self.root / ('bad%d-%d.html'%(version,len(command)))
                self.run_cli([*command,'--output',output], 2)
                self.assertFalse(output.exists())
        timing = self.trace('restricted-tT')
        counts = self.trace('restricted-tA', counting=True)
        output = self.root/'bad-production.html'
        self.run_cli(['production-report','--timing',timing,'--count','tA='+str(counts),
                      '--model',plugin,'--output',output],2)
        self.assertFalse(output.exists())

    def test_declared_compute_categories_are_consistent_across_native_formats(self):
        for category in ('mat', 'vec', 'mem', 'cc', 'fe', 'unknown'):
            model = load_model('generic-transformer')
            model['trace']['sites']['41']['category'] = category
            model['trace']['mode_categories']['affine'] = category
            plugin = self.write_json('category-' + category + '.json', model)
            for version in (1, 2):
                with self.subTest(category=category, version=version):
                    source = self.trace('category-%s-v%d' % (category, version), version=version)
                    archive = source.with_suffix('.lxtrace')
                    self.run_cli(['import', '--type', 'gftrace', '--input', source,
                                  '--model', plugin, '--output', archive])
                    records = [r for r in load_session(archive)['records']
                               if r.get('args', {}).get('mode', r.get('args', {}).get('substep_mode')) == 'affine']
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]['category'], category)
                    self.assertEqual(records[0]['layer'], 'software')
                    self.assertEqual(records[0]['representation'], 'interval')

    def test_legacy_glm_without_trace_keeps_native_import_and_alignment(self):
        legacy = load_model('glm5f')
        legacy.pop('trace')
        legacy.pop('flop')
        legacy.pop('presentation')
        legacy['stage_families'] = {k: v['stages'] for k,v in legacy['stage_families'].items()}
        model = self.write_json('legacy-glm-profile.json',legacy)
        timing = self.trace('legacy-tT',sites=(4,3,1))
        counts = self.trace('legacy-tA',counting=True,sites=(4,3,1))
        archive, output = self.root/'legacy.lxtrace', self.root/'legacy.html'
        self.run_cli(['import','--type','gftrace','--input',timing,'--model',model,'--output',archive])
        self.assertTrue({'mat','vec','cx'} <= {r['category'] for r in load_session(archive)['records']})
        self.run_cli(['production-report','--timing',timing,'--count','tA='+str(counts),
                      '--model',model,'--output',output])
        analysis=json.loads(output.with_suffix('.analysis.json').read_text(encoding='utf-8'))
        self.assertTrue({'mmg','deqg','push'} <= {e['mode'] for e in analysis['entries']})
        self.assertEqual(analysis['model']['trace_compatibility']['basis'],'shipped_glm5f_legacy_v1')

    def test_declared_numerator_does_not_certify_trace_window_or_enable_mfu(self):
        config = self.write_json('model-config.json', {'hidden_size':8,'num_hidden_layers':2})
        workload = self.write_json('flop-workload.json', {'T':3,'B':2})
        model = self.plugin()
        analysis, output = self.production(model=model, custom=True,
            extra=['--model-config',config,'--flop-workload',workload])
        self.assertEqual(analysis['model_flops']['total'],1920)
        self.assertEqual(analysis['model_flops']['window_correspondence'],'not_proven_by_counter_alone')
        self.assertEqual(analysis['mfu']['status'],'unavailable')
        self.assertTrue(any('does not enable MFU' in w for w in analysis['warnings']))
        kinds={p['kind'] for p in analysis['provenance'] if 'kind' in p}
        self.assertTrue({'model_plugin','model_configuration','flop_workload'} <= kinds)
        self.assertIn('1920',output.read_text(encoding='utf-8'))

    def test_config_workload_pairing_and_input_protection(self):
        timing=self.trace('guard-tT')
        counts=self.trace('guard-tA',counting=True)
        plugin=self.plugin()
        config=self.write_json('guard-config.json',{'hidden_size':8,'num_hidden_layers':2})
        workload=self.write_json('guard-work.json',{'T':3,'B':2})
        source_paths=[timing,timing.with_suffix('.txt'),counts,counts.with_suffix('.txt'),plugin,config,workload]
        before={p:p.read_bytes() for p in source_paths}
        base=['production-report','--timing',timing,'--count','tA='+str(counts),'--model',plugin]
        for flag,path in (('--model-config',config),('--flop-workload',workload)):
            output=self.root/('missing-'+flag[2:]+'.html')
            self.run_cli([*base,flag,path,'--output',output],2)
            self.assertFalse(output.exists())
        for path in source_paths:
            self.run_cli([*base,'--model-config',config,'--flop-workload',workload,'--output',path],2)
        for path in (plugin,config,workload):
            self.run_cli(['model-flops','--model',plugin,'--config',config,'--workload',workload,'--output',path],2)
        self.assertEqual(before,{p:p.read_bytes() for p in source_paths})


if __name__ == '__main__':
    unittest.main()
