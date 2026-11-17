"""Presentation descriptors change labels and colors without changing math."""
import copy
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.attribution.report import build_timeline, report_data, write_report
from lxtrace.workspace import temporary_directory, workspace_environment

ROOT = Path(__file__).resolve().parents[1]


def descriptor(label, color, matrix_label, dense=True):
    return {
        'stages': {'phase_x': {'label': label, 'family': 'compute'},
                   'phase_y': {'label': 'Auxiliary', 'family': 'compute', 'color': '#abcdef'}},
        'families': {'compute': {'label': label + ' family', 'color': color}},
        'modes': {'kernel': {'label': label + ' kernel', 'color': color}},
        'palettes': {'issue': {'mopa': [matrix_label + ' issue', color]},
                     'units': {'sve': ['Custom vector unit', color]}},
        'labels': {'matrix_instruction': matrix_label},
        'metric_labels': {'k': 'Custom instruction ratio'},
        'views': {'timeline_title': label + ' timeline', 'dense_title': label + ' detail',
                  'dense_enabled': dense},
    }


def uniform_analysis(presentation):
    return {
        'schema_version': 'lxtrace.production-analysis.v1',
        'model': {'model': 'Application supplied model'},
        'presentation': presentation,
        'totals': {'cycles': 100, 'matrix_first': {'mat': 60, 'wait': 40}},
        'segments': [dict(entry_id='e1', thread_id='worker1', die=0, stage='phase_x',
                          begin_ns=0, end_ns=100, cycles=100,
                          matrix_first={'mat': 60, 'wait': 40}, issue={'mopa': 50, 'other': 50},
                          unit_load={'mat': .6}, unit_denominator_cycles=100)],
        'stages': {}, 'dense': {'status': 'unavailable'}, 'mfu': {'status': 'unavailable'},
    }


class PluginReportPackagingTests(unittest.TestCase):
    def test_two_descriptors_keep_numeric_projection_and_payload_separate(self):
        first = uniform_analysis(descriptor('Decoder A', '#123456', 'Matrix A'))
        second = uniform_analysis(descriptor('Encoder B', '#765432', 'Tensor B', dense=False))
        first['timeline'], second['timeline'] = build_timeline(first, 10), build_timeline(second, 10)
        self.assertEqual(first['timeline'], second['timeline'])
        with temporary_directory() as directory:
            for i, source in enumerate((first, second)):
                before = copy.deepcopy(source)
                path = write_report(source, Path(directory) / ('report%d.html' % i))
                page = path.read_text(encoding='utf-8')
                payload = re.search(r'<script type="application/json" id="attribution-data">(.*?)</script>', page, re.S)
                data = json.loads(payload[1])
                self.assertEqual(data['presentation'], source['presentation'])
                self.assertEqual(data['timeline'], report_data(source)['timeline'])
                self.assertEqual(source, before)
                self.assertIn('id="densePanel"', page)
                self.assertIn('id="timelineTitle"', page)
                self.assertIn('P=A.presentation(D)', page)

    def test_descriptor_text_is_escaped_and_report_does_not_apply_model_rules(self):
        source = uniform_analysis(descriptor('</script><img onerror="bad()">', '#123456', 'Tensor'))
        source['timeline'] = build_timeline(source)
        with temporary_directory() as directory:
            page = write_report(source, Path(directory) / 'report.html').read_text(encoding='utf-8')
        self.assertNotIn(source['presentation']['stages']['phase_x']['label'], page)
        self.assertIn('\\u003c/script>', page)
        self.assertEqual(report_data(source)['presentation'], source['presentation'])
        renderer = '\n'.join((ROOT / 'web/attribution' / name).read_text(encoding='utf-8')
                             for name in ('analysis.js', 'viewer.js', 'index.html'))
        for model_rule in ('glm5f', 'dense_gemm', 'kda_proj', 'dsa_qkv', 'mmg', 'mmd'):
            self.assertNotIn(model_rule, renderer)
        self.assertNotIn('MOPA', renderer)
        self.assertNotIn('SVE', renderer)
        self.assertNotIn('.innerHTML', renderer)


@unittest.skipUnless(shutil.which('node'), 'Node unavailable; presentation descriptor behavior')
class PluginPresentationBrowserTests(unittest.TestCase):
    def javascript(self, body, *values):
        script = "const A=require('./web/attribution/analysis.js');\n" + body
        run = subprocess.run([shutil.which('node'), '-e', script, *map(json.dumps, values)], cwd=ROOT,
                             text=True, capture_output=True, encoding='utf-8', timeout=30,
                             env=workspace_environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_same_renderer_resolves_two_plugins_and_family_colors(self):
        first = descriptor('Decoder A', '#123456', 'Matrix A')
        second = descriptor('Encoder B', '#765432', 'Tensor B', dense=False)
        result = self.javascript("""
const inputs=process.argv.slice(1).map(x=>A.presentation({presentation:JSON.parse(x)}));
process.stdout.write(JSON.stringify(inputs.map(P=>({stage:A.stageStyle(P,'phase_x'),
explicit:A.stageStyle(P,'phase_y'),mode:A.modeLabel(P,'kernel'),
unknown:A.stageStyle(P,'unregistered'),issue:P.palettes.issue.mopa,
vector:P.palettes.units.sve,instruction:P.labels.matrix_instruction,
detail:P.views.dense_enabled,metric:P.metric_labels.k}))));
""", first, second)
        for actual, label, color, matrix, dense in zip(result, ('Decoder A', 'Encoder B'),
                                                     ('#123456', '#765432'), ('Matrix A', 'Tensor B'),
                                                     (True, False)):
            self.assertEqual(actual['stage']['label'], label)
            self.assertEqual(actual['stage']['color'], color)
            self.assertEqual(actual['stage']['family_label'], label + ' family')
            self.assertEqual(actual['explicit']['color'], '#abcdef')
            self.assertEqual(actual['mode'], label + ' kernel')
            self.assertEqual(actual['unknown']['label'], 'unregistered')
            self.assertEqual(actual['unknown']['color'], '#93a8b5')
            self.assertEqual(actual['issue'], [matrix + ' issue', color])
            self.assertEqual(actual['vector'], ['Custom vector unit', color])
            self.assertEqual(actual['instruction'], matrix)
            self.assertEqual(actual['detail'], dense)
            self.assertEqual(actual['metric'], 'Custom instruction ratio')

    def test_legacy_or_missing_presentation_keeps_generic_defaults(self):
        result = self.javascript("""
const P=A.presentation({model:{mode_labels:{old:'Historical label'}}}),N=A.presentation({presentation:null});
process.stdout.write(JSON.stringify({mode:A.modeLabel(P,'old'),unknown:A.modeLabel(P,'new'),
stage:A.stageStyle(P,'old'),label:N.labels.matrix_instruction,unit:N.palettes.units.sve,
counts:A.classes({matrix_first:{mat:2,wait:3}},'matrix_first')}));
""")
        self.assertEqual(result['mode'], 'Historical label')
        self.assertEqual(result['unknown'], 'new')
        self.assertEqual(result['stage']['label'], 'old')
        self.assertEqual(result['label'], '矩阵指令')
        self.assertEqual(result['unit'][0], '向量')
        self.assertEqual(result['counts'], {'mat': .4, 'wait': .6})

    def viewer(self, source, body):
        # Execute the real viewer against a minimal DOM/canvas surface. Geometry
        # and browser layout are covered separately; this verifies data wiring.
        return self.javascript(r"""
const fs=require('fs'),vm=require('vm'),paint=[];
class N {
 constructor(){this.childNodes=[];this.style={};this.value='';this.hidden=false;this.clientWidth=1000;this.scrollWidth=1000;this.scrollLeft=0;this.events={};this.dataset={};this.classList={add(){},toggle(){}};this.text='';}
 append(...items){this.childNodes.push(...items);}
 replaceChildren(...items){this.childNodes=[...items];this.text='';}
 set textContent(value){this.text=String(value);this.childNodes=[];}
 get textContent(){return this.text+this.childNodes.map(x=>typeof x==='string'?x:x.textContent).join('');}
 addEventListener(name,fn){this.events[name]=fn;}
 getBoundingClientRect(){return {left:0,top:0};}
 focus(){} setPointerCapture(){}
 getContext(){return new Proxy({},{get(_,key){if(key==='fillText')return value=>paint.push(value);return ()=>{};},set(){return true;}});}
}
const nodes=Object.fromEntries([...fs.readFileSync('web/attribution/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new N()]));
nodes['attribution-data']=new N();nodes['attribution-data'].textContent=process.argv[1];
const tabs=['matrix_first','issue','units'].map(mode=>{const n=new N();n.dataset.mode=mode;return n;});
const context={window:{AttributionReport:A,devicePixelRatio:1,innerWidth:1200,innerHeight:800},document:{getElementById:id=>{if(!nodes[id])throw Error('missing '+id);return nodes[id];},createElement:()=>new N(),createTextNode:text=>({textContent:String(text)}),querySelectorAll:()=>tabs},requestAnimationFrame:fn=>fn(),ResizeObserver:class{observe(){}},console};
vm.runInNewContext(fs.readFileSync('web/attribution/viewer.js','utf8'),context);
""" + body, source)

    def test_viewer_uses_resolved_labels_colors_and_optional_detail(self):
        source = uniform_analysis(descriptor('Custom phase', '#123456', 'Tensor', dense=False))
        source['stages'] = {'phase_x': source['totals']}
        source['timeline'] = build_timeline(source, 10)
        result = self.viewer(source, r"""
nodes.timelineCanvas.events.pointermove({clientX:200,clientY:60});
process.stdout.write(JSON.stringify({title:nodes.timelineTitle.textContent,detailHidden:nodes.densePanel.hidden,
phase:nodes.stageRows.childNodes[0].childNodes[0].textContent,
color:nodes.stageRows.childNodes[0].childNodes[0].style.borderLeft,
tooltip:nodes.timelineTooltip.textContent,paint}));
""")
        self.assertEqual(result['title'], 'Custom phase timeline')
        self.assertTrue(result['detailHidden'])
        self.assertEqual(result['phase'], 'Custom phase')
        self.assertEqual(result['color'], '3px solid #123456')
        self.assertIn('Custom phase', result['tooltip'])
        self.assertIn('Custom phase', result['paint'])

    def test_model_flop_numerator_is_visible_without_enabling_unproven_mfu(self):
        source = uniform_analysis(descriptor('Custom phase', '#123456', 'Tensor', dense=False))
        source['timeline'] = build_timeline(source, 10)
        source['mfu'] = {'status': 'unavailable', 'reason': 'incomplete_pool_coverage',
                         'frame': {'mfu': None}, 'waterfall': []}
        source['model_flops'] = {
            'status': 'available', 'total': 123456789, 'definition': '声明的模型运算量',
            'adapter': 'matmul_components.v1',
            'configuration': {'T': 4099, 'B': 8, 'layers': [0, 44]},
            'phases': {'projection': 123456789}, 'model_config_sha256': 'a' * 64,
            'window_correspondence': 'not_proven_by_counter_alone',
        }
        result = self.viewer(source, r"""
process.stdout.write(JSON.stringify({notice:nodes.mfuNotice.textContent,status:nodes.mfuStatus.textContent,
factors:nodes.mfuFactors.textContent,waterfallHidden:nodes.mfuWaterfall.hidden,
metrics:nodes.metrics.textContent,mfu:JSON.parse(nodes.mfuData.textContent)}));
""")
        self.assertIn('模型插件 FLOP 分子', result['notice'])
        self.assertIn('123,456,789 FLOP', result['notice'])
        self.assertIn('声明的模型运算量', result['notice'])
        self.assertIn('"T": 4099', result['notice'])
        self.assertIn('"B": 8', result['notice'])
        self.assertIn('"projection": 123456789', result['notice'])
        self.assertIn('a' * 64, result['notice'])
        self.assertIn('not_proven_by_counter_alone', result['notice'])
        self.assertIn('不能据此启用全机 MFU', result['notice'])
        self.assertEqual(result['status'], '不可用')
        self.assertEqual(result['factors'].count('N/A'), 4)
        self.assertTrue(result['waterfallHidden'])
        self.assertIn('全机 MFUN/A', result['metrics'])
        self.assertEqual(result['mfu']['status'], 'unavailable')
        self.assertIsNone(result['mfu']['frame']['mfu'])


if __name__ == '__main__':
    unittest.main()
