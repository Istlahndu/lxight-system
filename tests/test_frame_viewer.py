"""Frame selection must not attribute concurrent work from a different frame."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.core import analyze
from lxtrace.workspace import workspace_environment
from test_analysis import event, session


class FrameViewerParityTests(unittest.TestCase):
    def test_explicit_frame_membership_precedes_wait_and_overlap_analysis(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        data = session([
            event("f1", 0, 10, kind="frame", category="unknown", frame_id="f1",
                  args={"application_frame_id": "mb0.layer1", "operator_id": "gate_up"}),
            event("f2", 0, 10, kind="frame", category="unknown", frame_id="f2",
                  args={"application_frame_id": "mb1.layer1", "operator_id": "down"}),
            event("f1.compute", 0, 10, frame_id="f1"),
            event("f1.request", 1, 6, kind="request", category="cx", thread="sender", frame_id="f1"),
            event("f2.wait", 2, 8, kind="wait", category="wait", frame_id="f2"),
            event("f2.compute", 3, 9, thread="other", frame_id="f2"),
            event("f2.open", 8, None, thread="other", frame_id="f2"),
        ])
        options = [
            {"frame_ids": ["f1"]}, {"frame_ids": ["mb0.layer1"]},
            {"operator_ids": ["gate_up"]}, {"frame_ids": ["f1"], "thread_ids": ["compute"]},
            {"frame_ids": ["f2"]}, {"frame_ids": ["f1.compute"]},
            {"frame_ids": ["f1"], "operator_ids": ["down"]},
        ]
        script = """const A=require('./legacy_web/analysis.js');let input='';
process.stdin.on('data',d=>input+=d);process.stdin.on('end',()=>{
const x=JSON.parse(input);process.stdout.write(JSON.stringify(x.options.map(options=>({
 result:A.analyze(x.session,0,10,options),ids:A.filteredEvents(x.session,options).events.map(e=>e.event_id)}))));});"""
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                                input=json.dumps({"session": data, "options": options}), text=True,
                                encoding="utf-8", capture_output=True, env=workspace_environment(), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        for option, value in zip(options, values):
            with self.subTest(options=option):
                py = analyze(data, 0, 10, option)
                for key in ("compute_ns", "request_ns", "overlap_ns", "wait_ns",
                            "compute_parallel_ns", "event_count", "incomplete_count", "exact"):
                    self.assertEqual(value["result"][key], py[key], key)
        for i in (0, 1, 2):
            self.assertEqual(values[i]["ids"], ["f1", "f1.compute", "f1.request"])
            self.assertEqual(values[i]["result"]["compute_ns"], 10)
            self.assertEqual(values[i]["result"]["overlap_ns"], 5)
            self.assertTrue(values[i]["result"]["exact"])
        self.assertEqual(values[3]["result"]["request_ns"], 0)
        self.assertFalse(values[4]["result"]["exact"])
        self.assertEqual(values[5]["ids"], [])
        self.assertEqual(values[6]["ids"], [])


if __name__ == "__main__":
    unittest.main()
