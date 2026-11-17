"""Counter coverage and selection coverage must not imply one another."""
import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.cli import export_trace
from lxtrace.resource import analyze_resources
from lxtrace.workspace import workspace_environment


def session(**fields):
    record = dict(record_id="counter", resource_id="node", source_id="pmu",
                  category="pmu", layer="hardware", representation="counter_window",
                  begin_ns=100, end_ns=900, raw=1000, unit="count")
    record.update(fields)
    return dict(schema_version="lxtrace.resource-session.v2", session_id="counter-quality",
                run_id="synthetic", data_origin="synthetic", time_range=[0, 1000],
                resources=[dict(resource_id="node", type="node", parent_id=None)],
                allocation=dict(status="unverified", cpu_ids=[]),
                sources=[dict(source_id="pmu", source_layer="hardware", clock_id="c")],
                clocks=[dict(clock_id="c")], records=[record], quality={})


class CounterQualityTests(unittest.TestCase):
    def test_multiplexed_value_is_estimate_without_mutating_raw_evidence(self):
        s = session(enabled_ns=800, running_ns=200, scaled=4000)
        original = copy.deepcopy(s)
        a = analyze_resources(s)
        c = a["counters"][0]
        self.assertEqual(c["raw"], 1000)
        self.assertEqual(c["selection_value"], 4000)
        self.assertEqual(c["selection_status"], "multiplexed_estimate")
        self.assertTrue(c["window_fully_selected"])
        self.assertFalse(c["included_in_totals"])
        self.assertTrue(any("multiplex" in w.lower() for w in a["warnings"]))
        self.assertFalse(any("Partial counter" in w for w in a["warnings"]))
        self.assertEqual(s, original)

    def test_scaled_estimate_works_with_both_field_spellings_or_raw_only(self):
        for fields in (dict(raw=1000, scaled=4000),
                       dict(raw=None, raw_count=1000, scaled_count=4000),
                       dict(raw=1000, scaled=None),
                       dict(raw=None, raw_count=1000)):
            with self.subTest(fields=fields):
                c = analyze_resources(session(enabled_ns=800, running_ns=200, **fields))["counters"][0]
                self.assertEqual(c["window_value"], 4000)
                self.assertEqual(c["selection_value"], 4000)
                self.assertFalse(c["included_in_totals"])

    def test_partial_and_open_windows_never_gain_selected_counts(self):
        for end in (900, None):
            with self.subTest(end=end):
                s = session(end_ns=end, enabled_ns=800, running_ns=200, scaled=4000)
                a = analyze_resources(s, 300, 600)
                c = a["counters"][0]
                self.assertEqual(c["counter_status"], "multiplexed_estimate")
                self.assertEqual(c["window_value"], 4000)
                self.assertEqual(c["selection_status"], "open_window" if end is None else "crosses_selection")
                self.assertIsNone(c["selection_value"])
                self.assertFalse(c["included_in_totals"])
                self.assertFalse(c["window_fully_selected"])

    def test_counter_that_never_ran_cannot_supply_a_zero_or_scaled_result(self):
        for raw in (0, 1000):
            for enabled in (0, 800):
                with self.subTest(raw=raw, enabled=enabled):
                    c = analyze_resources(session(raw=raw, enabled_ns=enabled, running_ns=0, scaled=999))["counters"][0]
                    self.assertEqual(c["selection_status"], "counter_not_running")
                    self.assertIsNone(c["window_value"])
                    self.assertIsNone(c["selection_value"])
                    self.assertFalse(c["included_in_totals"])

    def test_equal_enabled_running_and_legacy_raw_windows_keep_existing_counts(self):
        for fields in ({}, dict(enabled_ns=800, running_ns=800),
                       dict(raw=None, raw_count=1000, scaled_count=None)):
            with self.subTest(fields=fields):
                c = analyze_resources(session(**fields))["counters"][0]
                self.assertEqual(c["selection_status"], "complete_window")
                self.assertEqual(c["selection_value"], 1000)
                self.assertTrue(c["included_in_totals"])

    def test_invalid_or_partial_counter_timing_is_unavailable(self):
        for fields in (dict(enabled_ns=800), dict(running_ns=200),
                       dict(enabled_ns=800, running_ns=900),
                       dict(enabled_ns=None, running_ns=200),
                       dict(enabled_ns=800, running_ns=-1),
                       dict(enabled_ns=True, running_ns=1),
                       dict(enabled_ns="800", running_ns=200)):
            with self.subTest(fields=fields):
                a = analyze_resources(session(scaled=4000, **fields))
                c = a["counters"][0]
                self.assertEqual(c["selection_status"], "counter_timing_unavailable")
                self.assertIsNone(c["selection_value"])
                self.assertFalse(c["included_in_totals"])

    def test_cli_trace_annotation_preserves_estimate_status_and_raw_native_session(self):
        for running, status, selected in ((800, "complete_window", 1000),
                                           (200, "multiplexed_estimate", 4000),
                                           (0, "counter_not_running", None)):
            s = session(enabled_ns=800, running_ns=running)
            original = copy.deepcopy(s)
            with self.subTest(running=running):
                trace = export_trace(s)
                event = trace["traceEvents"][0]
                self.assertEqual(event["ph"], "i")
                self.assertEqual(event["args"]["selection_status"], status)
                self.assertEqual(event["args"]["selection_value"], selected)
                self.assertEqual(event["args"]["included_in_totals"], running == 800)
                self.assertEqual(trace["lxtrace_native"], original)
                self.assertEqual(s, original)

    def test_python_and_browser_counter_quality_agree(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable; counter browser parity needs Node")
        inputs = []
        for fields in ({}, dict(enabled_ns=800, running_ns=800),
                       dict(enabled_ns=800, running_ns=200, scaled=4000),
                       dict(enabled_ns=800, running_ns=200),
                       dict(enabled_ns=800, running_ns=0, scaled=4000),
                       dict(enabled_ns=0, running_ns=0),
                       dict(enabled_ns=800), dict(running_ns=200),
                       dict(enabled_ns=800, running_ns=900),
                       dict(enabled_ns=True, running_ns=1),
                       dict(enabled_ns="800", running_ns=200),
                       dict(enabled_ns=800, running_ns=200, raw=None, raw_count=1000, scaled_count=4000),
                       dict(raw=None, scaled_count=None, raw_count=1000),
                       dict(end_ns=None, enabled_ns=800, running_ns=200)):
            for start, end in ((0, 1000), (300, 600)):
                inputs.append(dict(session=session(**fields), start=start, end=end))
        script = "const a=require('./web/analysis.js');let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s).map(x=>a.analyze(x.session,x.start,x.end).counters))));"
        proc = subprocess.run([node, "-e", script], input=json.dumps(inputs), text=True,
                              encoding="utf-8", capture_output=True, timeout=20,
                              cwd=Path(__file__).resolve().parents[1], env=workspace_environment())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for i, (query, actual) in enumerate(zip(inputs, json.loads(proc.stdout))):
            expected = analyze_resources(query["session"], query["start"], query["end"])["counters"]
            for field in ("counter_status", "selection_status", "window_value", "selection_value",
                          "selected_value", "included_in_totals", "window_fully_selected"):
                with self.subTest(case=i, field=field):
                    self.assertEqual(expected[0][field], actual[0][field])

    def test_browser_trace_keeps_estimate_quality_and_raw_record(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable; browser Trace export needs Node")
        script = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm'),A=require('./web/analysis.js');
const source=fs.readFileSync('web/viewer.js','utf8'),begin=source.indexOf('function traceExport(){'),
 end=source.indexOf('\nasync function offlineHtml()',begin);
const r={record_id:'pmu:0',resource_id:'thread:0',source_id:'pmu',layer:'hardware',category:'pmu',
 representation:'counter_window',name:'cycles',begin_ns:0,end_ns:10000,raw:1000,
 enabled_ns:10000,running_ns:2500,scaled:4000};
const session={schema_version:'lxtrace.resource-session.v2',session_id:'estimate-export',data_origin:'synthetic',time_range:[0,10000],
 resources:[{resource_id:'thread:0',type:'thread'}],sources:[{source_id:'pmu',source_layer:'hardware'}],records:[r]};
const original=JSON.stringify(session);
for(const [a,b,expectedStatus,expectedValue] of [[0,10000,'multiplexed_estimate',4000],[1000,9000,'crosses_selection',null]]){
 const context={S:{session,analysis:A.analyze(session,a,b)},
  selectionExport:()=>({resources:session.resources,records:session.records}),
  origin:()=>session.data_origin,viewState:()=>({}),A};
 vm.runInNewContext(source.slice(begin,end)+';this.result=traceExport()',context);
 const event=context.result.traceEvents.find(e=>e.args?.lxtrace_record?.record_id==='pmu:0');
 assert.equal(event.ph,'i');
 assert.equal(event.args.selection_status,expectedStatus);
 assert.equal(event.args.selection_value,expectedValue);
 assert.equal(event.args.counter_status,'multiplexed_estimate');
 assert.equal(event.args.included_in_totals,false);
 assert.equal(event.args.lxtrace_record.raw,1000);
 assert.equal(event.args.lxtrace_record.scaled,4000);
 assert.equal(JSON.stringify(context.result.lxtrace.native_session),original);
 assert.equal(JSON.stringify(session),original);
}
'''
        result = subprocess.run([node, "-e", script], text=True, encoding="utf-8",
                                capture_output=True, timeout=20,
                                cwd=Path(__file__).resolve().parents[1], env=workspace_environment())
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
