"""Distinct-thread semantic concurrency, independent of request lifetimes.

All intervals below are synthetic mathematical fixtures, not hardware timings.
"""
import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.core import analyze, parallel_intervals
from lxtrace.workspace import workspace_environment
from test_analysis import event, normalized, session


class ParallelIntervalTests(unittest.TestCase):
    def test_requires_two_distinct_threads_and_unions_nested_ranges(self):
        self.assertEqual(parallel_intervals([]), [])
        self.assertEqual(parallel_intervals([[(0, 10), (2, 8), (0, 10)]]), [])
        self.assertEqual(normalized(parallel_intervals([
            [(0, 10), (2, 8), (0, 10)], [(4, 7)]])), [[4, 7]])

    def test_three_threads_form_one_continuous_union_without_double_counting(self):
        intervals = [[(0, 10)], [(2, 6)], [(4, 8)]]
        original = copy.deepcopy(intervals)
        self.assertEqual(normalized(parallel_intervals(intervals)), [[2, 8]])
        self.assertEqual(intervals, original)

    def test_touching_endpoints_are_half_open(self):
        self.assertEqual(parallel_intervals([[(0, 5)], [(5, 10)]]), [])
        self.assertEqual(normalized(parallel_intervals([
            [(0, 10)], [(0, 5)], [(5, 10)]])), [[0, 10]])


class ParallelAnalysisTests(unittest.TestCase):
    def assert_parallel(self, data, expected, start=0, end=10, options=None):
        original = copy.deepcopy(data)
        result = analyze(data, start, end, options)
        self.assertEqual(normalized(result["intervals"]["parallel"]), expected)
        self.assertEqual(result["compute_parallel_ns"], sum(b - a for a, b in expected))
        self.assertEqual(data, original)
        return result

    def test_no_request_needed_for_pipeline_compute_overlap(self):
        data = session([
            event("layer1.mb1", 0, 6, thread="stage1"),
            event("layer1.nested", 1, 5, thread="stage1", parent_id="layer1.mb1"),
            event("layer2.mb0", 2, 8, thread="stage2"),
        ])
        result = self.assert_parallel(data, [[2, 6]])
        self.assertEqual(result["request_ns"], 0)
        self.assertEqual(result["overlap_ns"], 0)
        self.assertEqual(result["compute_ns"], 8)

    def test_wait_and_exchange_excluded_only_on_their_own_thread(self):
        data = session([
            event("A.compute", 0, 10),
            event("A.wait", 2, 8, kind="wait", category="wait"),
            event("B.compute", 0, 10, thread="B"),
            event("B.exchange", 1, 3, thread="B", kind="exchange", category="cx"),
            event("C.compute", 4, 6, thread="C"),
        ])
        result = self.assert_parallel(data, [[0, 1], [4, 6], [8, 10]])
        self.assertEqual(result["compute_ns"], 9)

    def test_filter_and_selection_apply_before_counting_distinct_threads(self):
        data = session([
            event("A", 0, 10),
            event("B", 2, 8, thread="B"),
            event("C", 6, 10, thread="C"),
        ])
        self.assert_parallel(data, [[3, 9]], start=3, end=9)
        self.assert_parallel(data, [], options={"thread_ids": ["A"]})
        self.assert_parallel(data, [[6, 8]], options={"thread_ids": ["B", "C"]})

    def test_loss_or_protocol_errors_invalidate_confirmed_parallel_lower_bound(self):
        for override in (
            {"quality": {"dropped_records": 1}},
            {"quality": {"protocol_errors": 1}},
            {"sources": [{"source_id": "test", "lost_events": 1}]},
        ):
            with self.subTest(override=override):
                data = session([event("A", 0, 10), event("B", 0, 10, thread="B")], **override)
                result = self.assert_parallel(data, [])
                self.assertFalse(result["exact"])
                self.assertEqual(result["compute_ns"], 0)

    def test_open_wait_masks_unknown_suffix_without_hiding_other_thread_pair(self):
        data = session([
            event("A", 0, 10),
            event("A.open_wait", 5, None, kind="wait", category="wait"),
            event("B", 0, 10, thread="B"),
            event("C", 7, 9, thread="C"),
        ])
        result = self.assert_parallel(data, [[0, 5], [7, 9]])
        self.assertFalse(result["exact"])

    def test_open_compute_is_not_extended_to_capture_end(self):
        data = session([
            event("A.open", 0, None),
            event("B", 0, 10, thread="B"),
        ])
        result = self.assert_parallel(data, [])
        self.assertFalse(result["exact"])


class ParallelViewerParityTests(unittest.TestCase):
    def test_python_and_javascript_agree_for_concurrency_and_evidence_edges(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable; JS parity is verified on the development workstation")
        fixtures = [
            session([event("A", 0, 10), event("A.nested", 1, 9),
                     event("B", 2, 8, thread="B"), event("C", 6, 10, thread="C")]),
            session([event("A", 0, 10), event("wait", 3, 7, kind="wait", category="wait"),
                     event("B", 1, 9, thread="B"),
                     event("exchange", 6, 8, thread="B", kind="exchange", category="cx")]),
            session([event("A", 0, 10), event("wait", 5, None, kind="wait", category="wait"),
                     event("B", 0, 10, thread="B"), event("C", 7, 9, thread="C")]),
            session([event("A", 0, 5), event("B", 5, 10, thread="B")]),
            session([event("A", 0, 10), event("B", 0, 10, thread="B")], quality={"protocol_errors": 1}),
            session([event("A", 0, 10), event("B", 0, 10, thread="B")], quality={"dropped_records": 1}),
        ]
        queries = [(0, 10, {}), (3, 9, {}), (0, 10, {"thread_ids": ["A"]}),
                   (0, 10, {"thread_ids": ["B", "C"]})]
        inputs = [dict(session=data, start=a, end=b, options=options)
                  for data in fixtures for a, b, options in queries]
        script = """const A=require('./legacy_web/analysis.js');let input='';
process.stdin.on('data', d=>input+=d);
process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(input).map(
  x=>A.analyze(x.session,x.start,x.end,x.options)))));"""
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                                input=json.dumps(inputs), text=True, encoding="utf-8",
                                capture_output=True, env=workspace_environment(), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        for index, (query, js) in enumerate(zip(inputs, json.loads(result.stdout))):
            py = analyze(query["session"], query["start"], query["end"], query["options"])
            with self.subTest(case=index):
                self.assertEqual(js["compute_parallel_ns"], py["compute_parallel_ns"])
                self.assertEqual(js["intervals"]["parallel"], normalized(py["intervals"]["parallel"]))
                self.assertEqual(js["compute_ns"], py["compute_ns"])
                self.assertEqual(js["exact"], py["exact"])


if __name__ == "__main__":
    unittest.main()
