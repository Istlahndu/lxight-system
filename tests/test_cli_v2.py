"""Public CLI integration over native archives and evidence-preserving exports."""
import json
import csv
import subprocess
import sys
import unittest
from pathlib import Path
from lxtrace.resource import load_session, write_session, demo_session
from lxtrace.workspace import temporary_directory, workspace_environment
from lxtrace.cli import export_csv

ROOT = Path(__file__).resolve().parents[1]


class ResourceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.out = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def cli(self,*args):
        return subprocess.run([sys.executable,'-m','lxtrace',*map(str,args)],cwd=str(ROOT),
                              capture_output=True,text=True,encoding='utf-8',timeout=30,
                              env=workspace_environment(dict(PYTHONIOENCODING='utf-8')))

    def test_archive_demo_analyze_and_csv_metadata(self):
        archive = self.out/'parallel.lxtrace'
        p = self.cli('demo','--case','parallel','--output',archive)
        self.assertEqual(p.returncode,0,p.stderr)
        p = self.cli('analyze',archive,'--output',self.out/'result.json')
        self.assertEqual(p.returncode,0,p.stderr)
        result = json.loads((self.out/'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['target_cpu_ns'],8000000)
        self.assertEqual(result['target_wall_ns'],4000000)
        self.assertEqual(result['peak_target_cpus'],2)
        p = self.cli('analyze',archive,'--format','csv','--output',self.out/'result.csv')
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertTrue((self.out/'result.csv.metadata.json').is_file())

    def test_csv_formula_text_is_literal_but_numbers_and_metadata_are_preserved(self):
        dangerous = ['=HYPERLINK("https://invalid.example","label")', '+SUM(1,2)', '-1+2', '@SUM(A1)',
                     '  =1+1', '\t=1+1', '\r=1+1', '\n=1+1', '\ufeff=1+1']
        labels = dangerous + ['plain text', '中文, "quoted"', '123']
        result = {'per_cpu': [{'resource_id': label, 'logical_cpu': i, 'signed_number': -2.5}
                              for i, label in enumerate(labels)]}
        output = self.out/'safe.csv'
        export_csv(result, output)
        with output.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r['resource_id'] for r in rows], ["'"+v for v in dangerous]+labels[len(dangerous):])
        self.assertTrue(all(r['signed_number'] == '-2.5' for r in rows))
        self.assertEqual(json.loads(Path(str(output)+'.metadata.json').read_text(encoding='utf-8')), result)

    def test_unknown_schema_is_not_guessed(self):
        bad = self.out/'bad.json'
        bad.write_text('{"schema_version":"lxtrace.resource-session.v999"}',encoding='utf-8')
        p = self.cli('analyze',bad,'--output',self.out/'result.json')
        self.assertNotEqual(p.returncode,0)
        self.assertFalse((self.out/'result.json').exists())

    def test_rebuild_missing_index_verifies_and_preserves_evidence(self):
        source=self.out/'source.lxtrace'
        write_session(demo_session('parallel'),source)
        (source/'index/resources.json').unlink()
        rebuilt=self.out/'rebuilt.lxtrace'
        result=self.cli('rebuild',source,'--output',rebuilt)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((rebuilt/'index/resources.json').is_file())
        self.assertEqual(load_session(rebuilt)['records'],demo_session('parallel')['records'])

    def test_trace_preserves_native_and_counter_window_is_not_activity(self):
        archive=self.out/'resource.lxtrace'
        write_session(demo_session(),archive)
        output=self.out/'trace.json'
        p=self.cli('export',archive,'--format','chrome-trace','--output',output)
        self.assertEqual(p.returncode,0,p.stderr)
        trace=json.loads(output.read_text(encoding='utf-8'))
        self.assertTrue(trace['format_losses'])
        self.assertIn('resources',trace['lxtrace_native'])
        for event in trace['traceEvents']:
            if event['args']['representation']=='counter_window': self.assertEqual(event['ph'],'i')

    def test_schedule_import_runs_reconstruction_and_keeps_raw(self):
        base=demo_session('parallel')
        cpus=[r for r in base['resources'] if r['type']=='logical_cpu']
        cpu=cpus[0]['logical_cpu']
        base['target_instances']=['T']; base['reliable_stop']=True
        meta=self.out/'meta.json'; meta.write_text(json.dumps(base),encoding='utf-8')
        raw=self.out/'events.jsonl'
        raw.write_text(json.dumps(dict(timestamp_ns=1000000,cpu=cpu,event='switch',next_instance='T',next_tid=7))+'\n'+
                       json.dumps(dict(timestamp_ns=4000000,cpu=cpu,event='switch',prev_instance='T',next_instance=None,next_tid=0))+'\n',encoding='utf-8')
        archive=self.out/'imported.lxtrace'
        p=self.cli('import','--type','schedule','--input',raw,'--metadata',meta,'--output',archive)
        self.assertEqual(p.returncode,0,p.stderr)
        s=load_session(archive)
        self.assertEqual(s['data_origin'],'imported')
        self.assertTrue(any(r['state']=='idle' for r in s['records']))
        self.assertTrue((archive/'raw/schedule/events.jsonl').is_file())
