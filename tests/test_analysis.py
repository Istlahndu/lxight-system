"""D0 mathematical and evidence-boundary acceptance tests; no cluster required."""
import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.core import analyze, export_trace, finalize_session, intersection, subtract, union, write_report
from lxtrace.workspace import temporary_directory, workspace_environment


def event(eid, start, end, thread="A", kind="range", category="mat", **extra):
    out = {
        "event_id": eid, "thread_id": thread, "kind": kind, "category": category,
        "name": eid, "begin_ts": start, "end_ts": end, "parent_id": None,
        "frame_id": None, "source_id": "test", "evidence": "observed",
        "status": "complete" if end is not None else "incomplete", "args": {},
    }
    out.update(extra)
    return out


def session(events, **overrides):
    out = {
        "schema_version": "lxtrace.session.v1", "session_id": "unit-fixture",
        "data_origin": "synthetic", "execution_mode": "user_only",
        "app": {"name": "test", "build_id": "unit", "workload_config": {}},
        "clock": {"id": "CLOCK_MONOTONIC", "unit": "ns", "origin_ns": 0, "resolution_ns": 1},
        "time_range": [0, 20],
        "threads": [{"thread_id": t, "pid": 1, "tid": i + 1, "name": t, "role": "worker"}
                    for i, t in enumerate(sorted({e["thread_id"] for e in events}))],
        "events": events, "quality": {}, "topology": {}, "provenance": [],
        "sources": [{"source_id": "test", "last_reliable_ts": 20}],
        "capability_manifest": [{"feature_id": "scheduler.capture", "availability": "unverified", "reason_code": "not_deployed"}],
    }
    out.update(overrides)
    return out


def normalized(intervals):
    return [list(i) for i in intervals]


class IntervalMathTests(unittest.TestCase):
    def test_union_unsorted_nested_duplicate_and_touching(self):
        self.assertEqual(normalized(union([(8, 10), (1, 4), (2, 3), (4, 8), (1, 4)])), [[1, 10]])

    def test_empty_interval_sets(self):
        self.assertEqual(normalized(union([])), [])
        self.assertEqual(normalized(intersection([], [(0, 1)])), [])
        self.assertEqual(normalized(subtract([(0, 1)], [])), [[0, 1]])

    def test_half_open_touch_is_no_overlap(self):
        self.assertEqual(normalized(intersection([(0, 3)], [(3, 5)])), [])

    def test_intersection_disjoint_pieces(self):
        self.assertEqual(normalized(intersection([(0, 5), (8, 12)], [(2, 10)])), [[2, 5], [8, 10]])

    def test_subtract_overlapping_masks(self):
        self.assertEqual(normalized(subtract([(0, 10)], [(2, 4), (3, 8)])), [[0, 2], [8, 10]])


class SemanticAnalysisTests(unittest.TestCase):
    def test_at20_same_thread_wait_exclusion(self):
        s = session([event("compute", 0, 10), event("wait", 2, 8, kind="wait", category="wait"),
                     event("r1", 2, 8, kind="request", category="cx")])
        r = analyze(s, 0, 10)
        self.assertEqual(r["compute_raw_ns"], 10)
        self.assertEqual(r["compute_ns"], 4)
        self.assertEqual(r["request_ns"], 6)
        self.assertEqual(r["overlap_ns"], 0)

    def test_at20_wait_does_not_subtract_another_threads_compute(self):
        s = session([event("compute", 0, 10), event("wait", 2, 8, kind="wait", category="wait"),
                     event("B_compute", 2, 8, thread="B"), event("r1", 2, 8, kind="request", category="cx")])
        r = analyze(s, 0, 10)
        self.assertEqual(r["compute_ns"], 10)
        self.assertEqual(r["overlap_ns"], 6)
        self.assertEqual(r["request_overlap_ratio"], 1)
        self.assertAlmostEqual(r["compute_overlap_ratio"], 0.6)

    def test_exchange_api_excluded_but_request_lifetime_not_excluded(self):
        s = session([event("c", 0, 10), event("api", 1, 3, kind="exchange", category="cx"),
                     event("r", 1, 8, kind="request", category="cx")])
        r = analyze(s, 0, 10)
        self.assertEqual(r["compute_ns"], 8)
        self.assertEqual(r["overlap_ns"], 5)

    def test_requests_and_nested_compute_count_union_once(self):
        s = session([event("c", 1, 7), event("nested", 2, 6, parent_id="c"),
                     event("r", 3, 8, kind="request", category="cx"),
                     event("r2", 4, 9, thread="B", kind="request", category="cx")])
        r = analyze(s, 0, 10)
        self.assertEqual(r["compute_ns"], 6)
        self.assertEqual(r["request_ns"], 6)
        self.assertEqual(r["overlap_ns"], 4)

    def test_query_clips_all_intervals(self):
        s = session([event("c", 0, 10), event("r", 5, 15, kind="request", category="cx")])
        r = analyze(s, 7, 9)
        self.assertEqual(r["wall_ns"], 2)
        self.assertEqual(r["compute_ns"], 2)
        self.assertEqual(r["request_ns"], 2)
        self.assertEqual(r["overlap_ns"], 2)

    def test_zero_denominators_are_na(self):
        r = analyze(session([event("marker", 2, 2, kind="instant", category="unknown")]), 0, 10)
        self.assertEqual(r["overlap_ns"], 0)
        self.assertIsNone(r["request_overlap_ratio"])
        self.assertIsNone(r["compute_overlap_ratio"])

    def test_query_does_not_mutate_source_or_depend_on_previous_window(self):
        s = session([event("c", 0, 10), event("open", 4, None, kind="request", category="cx")])
        before = copy.deepcopy(s)
        first = analyze(s, 2, 8)
        analyze(s, 0, 20)
        self.assertEqual(first, analyze(s, 2, 8))
        self.assertEqual(s, before)

    def test_same_event_imported_twice_deduplicates_and_conflicts_fail(self):
        e = event("unique", 0, 10)
        out = finalize_session(session([e, copy.deepcopy(e)]))
        self.assertEqual(len(out["events"]), 1)
        self.assertEqual(analyze(out, 0, 10)["compute_ns"], 10)
        conflict = copy.deepcopy(e)
        conflict["end_ts"] = 12
        with self.assertRaises(ValueError):
            finalize_session(session([e, conflict]))

    def test_own_wall_uses_same_thread_child_union_only(self):
        s = session([event("p", 0, 10), event("c1", 2, 6, parent_id="p"),
                     event("c2", 4, 8, parent_id="p"),
                     event("remote", 0, 10, thread="B", parent_id="p"),
                     event("async", 0, 10, kind="request", category="cx", parent_id="p")])
        r = analyze(s, 0, 10)
        p = next(e for e in r["events"] if e["event_id"] == "p")
        self.assertEqual(p["inclusive_wall_ns"], 10)
        self.assertEqual(p["self_wall_ns"], 4)

    def test_open_compute_is_not_closed_at_reliable_source_time(self):
        r = analyze(session([event("open", 2, None)]), 0, 20)
        self.assertEqual(r["incomplete_count"], 1)
        self.assertEqual(r["compute_ns"], 0)
        self.assertFalse(r["exact"])
        self.assertTrue(r["known_subset_only"])
        self.assertIsNone(r["compute_overlap_ratio"])
        self.assertTrue(r["warnings"])

    def test_open_request_is_not_counted_as_confirmed_overlap(self):
        r = analyze(session([event("c", 0, 10), event("open", 2, None, kind="request", category="cx")]), 0, 10)
        self.assertEqual(r["overlap_ns"], 0)
        self.assertEqual(r["request_ns"], 0)
        self.assertFalse(r["exact"])
        self.assertTrue(r["known_subset_only"])
        self.assertIsNone(r["request_overlap_ratio"])
        self.assertEqual(r["compute_raw_ns"], 10)
        self.assertEqual(r["compute_ns"], 10)
        self.assertEqual(r["incomplete_count"], 1)
        self.assertTrue(r["warnings"])

    def test_open_wait_masks_parent_semantic_compute_unknown_suffix(self):
        r = analyze(session([event("c", 0, 10), event("w", 2, None, kind="wait", category="wait"),
                             event("r", 2, 8, kind="request", category="cx")]), 0, 10)
        self.assertEqual(r["compute_raw_ns"], 10)
        self.assertEqual(r["compute_ns"], 2)
        self.assertEqual(r["overlap_ns"], 0)
        self.assertFalse(r["exact"])
        self.assertIsNone(r["request_overlap_ratio"])

    def test_loss_does_not_silently_yield_unqualified_complete_metrics(self):
        s = session([event("c", 0, 10), event("r", 2, 8, kind="request", category="cx")],
                    quality={"dropped_records": 3, "clean_shutdown": False})
        r = analyze(s, 0, 10)
        self.assertTrue(r["warnings"])
        self.assertNotEqual(r.get("exclusion_coverage"), "complete")
        self.assertFalse(r["exact"])
        self.assertTrue(r["known_subset_only"])
        self.assertIsNone(r["request_overlap_ratio"])
        self.assertEqual(r["compute_raw_ns"], 10)
        self.assertEqual(r["compute_ns"], 0)
        self.assertEqual(r["overlap_ns"], 0)
        self.assertEqual(r["request_ns"], 6)

    def test_protocol_errors_mask_confirmed_compute_without_fabricating_request_end(self):
        s = session([event("c", 0, 10), event("r", 2, 8, kind="request", category="cx"),
                     event("open", 3, None, kind="request", category="cx")],
                    quality={"protocol_errors": 1})
        r = analyze(s, 0, 10)
        self.assertEqual(r["compute_raw_ns"], 10)
        self.assertEqual(r["compute_ns"], 0)
        self.assertEqual(r["request_ns"], 6)
        self.assertEqual(r["overlap_ns"], 0)
        self.assertIsNone(r["request_overlap_ratio"])
        self.assertIsNone(s["events"][-1]["end_ts"])

    def test_source_loss_is_also_unknown_coverage(self):
        s = session([event("c", 0, 10), event("r", 2, 8, kind="request", category="cx")],
                    sources=[{"source_id": "test", "dropped_records": 3, "last_reliable_ts": 20}])
        r = analyze(s, 0, 10)
        self.assertFalse(r["exact"])
        self.assertTrue(r["known_subset_only"])
        self.assertIsNone(r["request_overlap_ratio"])
        self.assertEqual(r["compute_raw_ns"], 10)
        self.assertEqual(r["compute_ns"], 0)
        self.assertEqual(r["overlap_ns"], 0)
        self.assertEqual(r["request_ns"], 6)

    def test_parent_own_wall_is_unknown_with_open_same_thread_child(self):
        s = session([event("parent", 0, 10), event("child", 2, None, parent_id="parent")])
        r = analyze(s, 0, 10)
        parent = next(e for e in r["events"] if e["event_id"] == "parent")
        self.assertEqual(parent["inclusive_wall_ns"], 10)
        self.assertIsNone(parent["self_wall_ns"])

    def test_thread_filter_precedes_wait_exclusion(self):
        s = session([event("c", 0, 10, thread="B"), event("wait", 2, 8, kind="wait", category="wait"),
                     event("r", 2, 8, thread="B", kind="request", category="cx")])
        r = analyze(s, 0, 10, {"thread_ids": ["B"]})
        self.assertEqual(r["compute_ns"], 10)
        self.assertEqual(r["overlap_ns"], 6)


class SemanticSelectorTests(unittest.TestCase):
    def test_frame_and_operator_filters_use_explicit_metadata(self):
        f1 = event("f1", 0, 10, kind="frame", category="unknown", frame_id="f1",
                   args={"application_frame_id": "mb0.layer1", "operator_id": "gate_up"})
        op1 = event("op1", 1, 4, frame_id="f1")
        f2 = event("f2", 10, 20, kind="frame", category="unknown", frame_id="f2",
                   args={"application_frame_id": "mb1.layer1"})
        op2 = event("op2", 11, 19, frame_id="f2", args={"operator_id": "gate_up"})
        data = session([f1, op1, f2, op2], time_range=[0, 20])
        by_frame = analyze(data, 0, 20, {"frame_ids": ["f1"]})
        self.assertEqual(by_frame["compute_raw_ns"], 3)
        self.assertEqual(by_frame["event_count"], 2)
        by_label = analyze(data, 0, 20, {"frame_ids": ["mb1.layer1"]})
        self.assertEqual(by_label["compute_raw_ns"], 8)
        by_operator = analyze(data, 0, 20, {"operator_ids": ["gate_up"]})
        self.assertEqual(by_operator["compute_raw_ns"], 11)
        self.assertEqual(by_operator["event_count"], 3)
        self.assertEqual(by_operator["operator_ids"], ["gate_up"])
        self.assertEqual(by_label["frame_ids"], ["f2", "mb1.layer1"])
        self.assertEqual(analyze(data, 0, 20, {"frame_ids": ["op1"]})["event_count"], 0)

    def test_missing_frame_label_is_not_stringified_as_a_selector(self):
        data = session([event("unlabelled", 0, 10, kind="frame", category="unknown", frame_id="unlabelled"),
                        event("work", 1, 4, frame_id="unlabelled")])
        self.assertEqual(analyze(data, 0, 20, {"frame_ids": ["None"]})["event_count"], 0)


class EvidenceAndExportTests(unittest.TestCase):
    def finalized(self, s):
        return finalize_session(s) or s

    def test_empty_imported_capabilities_get_d0_defaults(self):
        out = self.finalized(session([event("e", 0, 10)], capability_manifest=[]))
        self.assertTrue(out["capability_manifest"])
        self.assertTrue(any(c["feature_id"] == "on_cpu" and c["availability"] != "available"
                            for c in out["capability_manifest"]))

    def test_driver_complement_requires_explicit_full_coverage(self):
        s = session([event("f", 0, 10, kind="frame", category="unknown"),
                     event("call", 2, 8, parent_id="f", frame_id="f")])
        s["threads"][0]["role"] = "frame_driver"
        out = self.finalized(s)
        self.assertFalse(any(e["kind"] == "driver_outside" for e in out["events"]))

    def test_driver_complete_frame_complement_and_idempotence(self):
        s = session([event("f", 0, 10, kind="frame", category="unknown", args={"driver_calls_complete": True}),
                     event("call", 2, 8, parent_id="f", frame_id="f")])
        s["threads"][0]["role"] = "frame_driver"
        out = self.finalized(s)
        outside = [(e["begin_ts"], e["end_ts"]) for e in out["events"] if e["kind"] == "driver_outside"]
        self.assertEqual(sorted(outside), [(0, 2), (8, 10)])
        out2 = self.finalized(copy.deepcopy(out))
        self.assertEqual(len(out["events"]), len(out2["events"]))

    def test_driver_complement_suppressed_for_incomplete_call(self):
        s = session([event("f", 0, 10, kind="frame", category="unknown", args={"driver_calls_complete": True}),
                     event("call", 2, None, parent_id="f", frame_id="f")])
        s["threads"][0]["role"] = "frame_driver"
        out = self.finalized(s)
        self.assertFalse(any(e["kind"] == "driver_outside" for e in out["events"]))

    def test_driver_complement_suppressed_for_source_loss(self):
        s = session([event("f", 0, 10, kind="frame", category="unknown", args={"driver_calls_complete": True}),
                     event("call", 2, 8, parent_id="f", frame_id="f")],
                    sources=[{"source_id": "test", "dropped_records": 1}])
        s["threads"][0]["role"] = "frame_driver"
        out = self.finalized(s)
        self.assertFalse(any(e["kind"] == "driver_outside" for e in out["events"]))

    def test_trace_export_never_closes_open_range(self):
        out = export_trace(session([event("closed", 0, 10), event("open", 2, None)]))
        events = out["traceEvents"] if isinstance(out, dict) else out
        bad = [e for e in events if e.get("name") == "open" and e.get("ph") == "X"]
        self.assertEqual(bad, [])
        serialized = json.dumps(out)
        self.assertIn("incomplete", serialized)

    def test_report_is_self_contained_and_does_not_inject_labels(self):
        malicious = "</script><script>alert('injected')</script><img src=x onerror=alert(1)>"
        s = session([event("e", 0, 10, name=malicious)])
        with temporary_directory() as tmp:
            path = Path(tmp) / "report.html"
            write_report(s, path)
            html = path.read_text(encoding="utf-8")
        self.assertNotIn(malicious, html)
        self.assertIn("LXTRACE_SESSION", html)
        self.assertIn("data-lxtrace-session", html)
        self.assertNotIn('src="viewer.js"', html)
        self.assertNotIn('href="style.css"', html)


class ViewerParityTests(unittest.TestCase):
    def test_viewer_and_cli_analyzers_agree_on_evidence_and_math(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable; browser math parity not exercised on this host")
        root = Path(__file__).resolve().parents[1]
        fixtures = [
            session([event("c", 0, 10), event("w", 2, 8, kind="wait", category="wait"),
                     event("b", 2, 8, thread="B"), event("r", 2, 8, kind="request", category="cx")]),
            session([event("c", 0, 10), event("r", 2, None, kind="request", category="cx")]),
            session([event("c", 0, 10)], quality={"dropped_records": 3}),
            session([event("c", 0, 10)], quality={"protocol_errors": 1}),
            session([event("c", 0, 10)], sources=[{"source_id": "test", "lost_events": 2}]),
            session([event("c", 0, 10), event("w", 2, None, kind="wait", category="wait"),
                     event("r", 2, 8, kind="request", category="cx")]),
        ]
        queries = [(0, 10, {}), (2, 8, {"thread_ids": ["A"]}), (4, 6, {"thread_ids": []})]
        inputs = [{"session": s, "start": a, "end": b, "options": opt} for s in fixtures for a, b, opt in queries]
        script = "const a=require('./legacy_web/analysis.js');let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(x=>a.analyze(x.session,x.start,x.end,x.options)))));"
        result = subprocess.run([node, "-e", script], input=json.dumps(inputs), text=True, encoding="utf-8",
                                capture_output=True, cwd=root, env=workspace_environment(), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        fields = ["wall_ns", "compute_raw_ns", "compute_ns", "request_ns", "overlap_ns", "wait_ns",
                  "request_overlap_ratio", "compute_overlap_ratio", "incomplete_count", "exact", "known_subset_only"]
        for index, (fixture, js) in enumerate(zip(inputs, actual)):
            py = analyze(fixture["session"], fixture["start"], fixture["end"], fixture["options"])
            for field in fields:
                with self.subTest(field=field, case=index):
                    self.assertEqual(py[field], js[field])


if __name__ == "__main__":
    unittest.main()
