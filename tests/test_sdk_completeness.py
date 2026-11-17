"""Shutdown anomalies must not become exact software overlap after import."""
import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.core import analyze, export_trace, finalize_session, has_loss
from lxtrace.importers import load_sdk
from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]


def scope(eid, begin=0, end=10, kind="range", category="mat", **extra):
    out = dict(event_id=eid, begin_ts=begin, end_ts=end, kind=kind, category=category,
               thread_id="driver", name=eid, source_id="sdk", parent_id=None,
               frame_id=None, status="complete", args={})
    out.update(extra)
    return out


def capture(quality=None):
    return dict(schema_version="lxtrace.session.v1", session_id="completeness",
                data_origin="synthetic", time_range=[0, 10],
                threads=[dict(thread_id="driver", role="frame_driver")], sources=[],
                quality=copy.deepcopy(quality or {}),
                events=[scope("frame", kind="frame", category="frame", frame_id="frame",
                              args={"driver_calls_complete": True}),
                        scope("compute", begin=2, end=8, parent_id="frame", frame_id="frame"),
                        scope("request", kind="request", category="cx")])


class SDKCompletenessTests(unittest.TestCase):
    def test_explicit_shutdown_anomalies_invalidate_exactness_and_driver_complement(self):
        for quality in ({"clean_shutdown": False}, {"capture_partial": True},
                        {"late_records": 1}, {"reported_incomplete_events": 2}):
            with self.subTest(quality=quality):
                finalized = finalize_session(capture(quality))
                self.assertTrue(has_loss(finalized))
                self.assertFalse(any(e["kind"] == "driver_outside" for e in finalized["events"]))
                result = analyze(finalized, 0, 10)
                self.assertFalse(result["exact"])
                self.assertTrue(result["known_subset_only"])
                self.assertTrue(result["capture_partial"])
                self.assertEqual(result["compute_raw_ns"], 6)
                self.assertEqual(result["compute_ns"], 0)
                self.assertEqual(result["request_ns"], 10)
                self.assertIsNone(result["request_overlap_ratio"])
                self.assertTrue(all(e["self_wall_ns"] is None for e in result["events"]))
                self.assertEqual(export_trace(finalized)["lxtrace"]["quality"], finalized["quality"])

    def test_omitted_legacy_shutdown_fields_keep_existing_fixture_behavior(self):
        finalized = finalize_session(capture())
        self.assertFalse(has_loss(finalized))
        self.assertTrue(analyze(finalized, 0, 10)["exact"])
        self.assertEqual(sum(e["kind"] == "driver_outside" for e in finalized["events"]), 2)

    def test_finalizing_twice_preserves_reported_and_existing_open_lower_bounds(self):
        for quality in ({"reported_incomplete_events": 2}, {"incomplete_events": 3}):
            with self.subTest(quality=quality):
                first = finalize_session(capture(quality))
                second = finalize_session(first)
                self.assertEqual(first["quality"]["incomplete_events"], max(quality.values()))
                self.assertEqual(second["quality"]["incomplete_events"], first["quality"]["incomplete_events"])
        result = analyze(finalize_session(capture({"reported_incomplete_events": 2})), 0, 10)
        self.assertEqual(result["reported_incomplete_count"], 2)
        self.assertEqual(result["incomplete_count"], 0)  # Visible records themselves have matched ends.

    def test_closed_pairs_with_missing_or_false_sdk_shutdown_quality_are_not_exact(self):
        meta = dict(type="meta", schema_version="lxtrace.sdk.v1", pid=1, clock_id="CLOCK_MONOTONIC")
        records = [dict(type="begin", event_id="1:1", thread_id="1:2:1", pid=1, tid=2,
                        kind="range", category="mat", name="closed.compute", ts=100, args={}),
                   dict(type="end", event_id="1:1", tid=2, ts=200)]
        with temporary_directory() as directory:
            path = Path(directory) / "capture.jsonl"
            for quality in (None, dict(type="quality", clean_shutdown=False,
                                       dropped_records=0, protocol_errors=0)):
                with self.subTest(quality=quality):
                    rows = [meta] + records + ([quality] if quality else [])
                    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
                    result = analyze(finalize_session(load_sdk(path)), 0, 100)
                    self.assertFalse(result["exact"])
                    self.assertTrue(result["capture_partial"])
                    self.assertEqual(result["compute_ns"], 0)


@unittest.skipUnless(shutil.which("node"), "Node unavailable; legacy browser completeness parity")
class SDKCompletenessBrowserTests(unittest.TestCase):
    def test_python_and_legacy_browser_keep_shutdown_anomalies_partial(self):
        sessions = [finalize_session(capture(q)) for q in
                    ({}, {"clean_shutdown": False}, {"capture_partial": True},
                     {"late_records": 1}, {"reported_incomplete_events": 2})]
        script = """
const A=require('./legacy_web/analysis.js');
const data=JSON.parse(require('fs').readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(data.map(s=>({analysis:A.analyze(s,0,10),
 own:A.ownWall(s,s.events.find(e=>e.event_id==='frame'),0,10)}))));
"""
        run = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(sessions),
                             text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=30,
                             env=workspace_environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        results = json.loads(run.stdout)
        for session, result in zip(sessions, results):
            python = analyze(session, 0, 10)
            with self.subTest(quality=session["quality"]):
                for key in ("exact", "known_subset_only", "capture_partial", "reported_incomplete_count",
                            "compute_raw_ns", "compute_ns", "request_ns", "overlap_ns",
                            "compute_parallel_ns", "request_overlap_ratio", "compute_overlap_ratio"):
                    self.assertEqual(result["analysis"][key], python[key], key)
                frame = next(e for e in python["events"] if e["event_id"] == "frame")
                self.assertEqual(result["own"]["self_wall_ns"], frame["self_wall_ns"])


if __name__ == "__main__":
    unittest.main()
