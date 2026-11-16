"""CLI boundary and offline interchange acceptance tests for LX2 Trace D0."""
import json
import subprocess
import struct
import sys
import unittest
from pathlib import Path

from test_analysis import event, session
from lxtrace.core import analyze
from lxtrace.importers import load_gftrace, load_sdk
from lxtrace.workspace import temporary_directory, workspace_environment

ROOT = Path(__file__).resolve().parents[1]


class CliAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.tmp = Path(self.temp.name)
        self.input = self.tmp / "session.json"
        self.input.write_text(json.dumps(session([
            event("compute", 0, 10_000_000),
            event("wait", 2_000_000, 8_000_000, kind="wait", category="wait"),
            event("request", 2_000_000, 8_000_000, kind="request", category="cx"),
        ], time_range=[0, 10_000_000])), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "lxtrace", *map(str, args)],
                              cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=30,
                              env=workspace_environment(dict(PYTHONIOENCODING="utf-8")))

    def test_schedule_fails_without_launching_business_program(self):
        marker = self.tmp / "must-not-run.txt"
        code = "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('ran')"
        r = self.cli("record", "--profile", "schedule", "--output", self.tmp / "schedule.json",
                     "--", sys.executable, "-c", code)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("schedule", (r.stdout + r.stderr).lower())

    def test_analyze_explicit_half_open_selection_json(self):
        output = self.tmp / "selected.json"
        r = self.cli("analyze", self.input, "--from", "0ms", "--to", "10ms", "--output", output)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        selected = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(selected["overlap_ns"], 0)
        self.assertEqual(selected["compute_ns"], 4_000_000)

    def test_csv_and_trace_exports_are_offline_files(self):
        csv = self.tmp / "selected.csv"
        r = self.cli("analyze", self.input, "--from", "0ms", "--to", "10ms", "--format", "csv", "--output", csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("compute", csv.read_text(encoding="utf-8").lower())
        trace = self.tmp / "trace.json"
        r = self.cli("export", self.input, "--format", "chrome-trace", "--output", trace)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        exported = json.loads(trace.read_text(encoding="utf-8"))
        self.assertIn("traceEvents", exported)

    def test_invalid_gftrace_is_rejected(self):
        bad = self.tmp / "bad.bin"
        bad.write_bytes(b"GFTRACE1\x00")
        r = self.cli("import", "--type", "gftrace", "--input", bad, "--output", self.tmp / "bad.json")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_incompatible_session_schema_is_rejected(self):
        self.input.write_text('{"schema_version":"lxtrace.session.v999","events":[]}', encoding="utf-8")
        r = self.cli("analyze", self.input, "--from", "0ms", "--to", "10ms", "--output", self.tmp / "bad-schema.json")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_synthetic_fixture_origin_is_explicit(self):
        output = self.tmp / "synthetic.json"
        r = self.cli("synthetic", "--count", "100000", "--output", output)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(data["data_origin"], "synthetic")
        self.assertGreaterEqual(len(data["events"]), 100000)
        self.assertNotIn("cpu_residency", {e["kind"] for e in data["events"]})


class ImporterAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.tmp = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def gftrace(self, records, frequency=1000, suffix=b""):
        path = self.tmp / "fixture.bin"
        raw = struct.pack("<8sQii", b"GFTRACE1", frequency, 1, 0)
        raw += struct.pack("<iiii", 0, 0, 0, len(records))
        raw += b"".join(struct.pack("<QHHi", *r) for r in records)
        path.write_bytes(raw + suffix)
        return path

    def sdk(self, records, extra_line=None):
        path = self.tmp / "sdk.jsonl"
        rows = [{"type": "meta", "schema_version": "lxtrace.sdk.v1", "clock_id": "CLOCK_MONOTONIC", "app_name": "fixture", "build_id": "unit"}]
        rows += records
        text = "\n".join(json.dumps(row) for row in rows) + "\n"
        if extra_line:
            text += extra_line + "\n"
        path.write_text(text, encoding="utf-8")
        return path

    def test_gftrace_tick_conversion_and_logical_mapping(self):
        path = self.gftrace([(1000, 1, 3, -1), (1040, 7, 1, 0), (1050, 8, 1, 0), (1100, 2, 3, -1)])
        s = load_gftrace(path)
        self.assertEqual(s["data_origin"], "imported")
        self.assertEqual(s["clock"]["frequency_hz"], 1000)
        call = next(e for e in s["events"] if e["kind"] == "range")
        self.assertEqual((call["begin_ts"], call["end_ts"]), (0, 100_000_000))
        self.assertEqual(s["threads"][0]["physical_mapping_status"], "unknown")
        self.assertIsNone(s["threads"][0]["tid"])
        self.assertNotIn("static_mapping", s["threads"][0])
        self.assertEqual(len(s["provenance"][0]["sha256"]), 64)

    def test_frozen_gftrace_fixture_discloses_synthetic_original_source(self):
        s = load_gftrace(ROOT / "fixtures" / "gftrace-demo.bin")
        self.assertEqual(s["data_origin"], "imported")
        self.assertEqual(s.get("original_data_origin"), "synthetic")
        self.assertIn("synthetic", s["app"]["name"])

    def test_gftrace_push_is_api_not_synthetic_request(self):
        s = load_gftrace(self.gftrace([(1000, 19, 0, 1), (1010, 20, 0, 1)]))
        self.assertEqual(s["events"][0]["kind"], "exchange")
        self.assertFalse(any(e["kind"] == "request" for e in s["events"]))

    def test_gftrace_missing_end_and_unknown_kind_are_preserved(self):
        s = load_gftrace(self.gftrace([(1000, 1, 3, -1), (1010, 65534, 7, 3)]))
        call = next(e for e in s["events"] if e["kind"] == "range")
        self.assertIsNone(call["end_ts"])
        self.assertEqual(call["status"], "incomplete")
        self.assertTrue(any(e["kind"] == "instant" and "unknown" in e["name"] for e in s["events"]))

    def test_gftrace_rejects_bad_frequency_order_and_trailing_bytes(self):
        for records, frequency, suffix in [([], 0, b""), ([(2, 1, 0, 0), (1, 2, 0, 0)], 1000, b""), ([], 1000, b"x")]:
            with self.subTest(records=records, frequency=frequency, suffix=suffix):
                with self.assertRaises(ValueError):
                    load_gftrace(self.gftrace(records, frequency, suffix))

    def test_sdk_cross_thread_sync_end_is_rejected_but_async_allowed(self):
        records = [
            {"type": "begin", "event_id": "p:1", "thread_id": "A", "pid": 1, "tid": 1, "kind": "range", "category": "mat", "name": "c", "ts": 100},
            {"type": "end", "event_id": "p:1", "tid": 2, "ts": 200},
            {"type": "begin", "event_id": "p:2", "thread_id": "A", "pid": 1, "tid": 1, "kind": "request", "category": "cx", "name": "r", "ts": 110},
            {"type": "end", "event_id": "p:2", "tid": 2, "ts": 210},
        ]
        s = load_sdk(self.sdk(records))
        by_id = {e["event_id"]: e for e in s["events"]}
        self.assertIsNone(by_id["p:1"]["end_ts"])
        self.assertEqual(by_id["p:2"]["end_ts"], 110)
        self.assertEqual(by_id["p:2"]["args"]["completion_tid"], 2)
        self.assertGreater(s["quality"]["protocol_errors"], 0)

    def test_sdk_malformed_record_does_not_report_exact_coverage(self):
        records = [
            {"type": "begin", "event_id": "p:1", "thread_id": "A", "pid": 1, "tid": 1, "kind": "range", "category": "mat", "name": "c", "ts": 100},
            {"type": "end", "event_id": "p:1", "tid": 1, "ts": 200},
            {"type": "quality", "clean_shutdown": True, "dropped_records": 0, "protocol_errors": 0},
        ]
        s = load_sdk(self.sdk(records, '{"type":"begin", bad JSON'))
        r = analyze(s, 0, 100)
        self.assertFalse(r["exact"])
        self.assertTrue(s["quality"]["warnings"])

    def test_sdk_continuity_requires_complete_pairs_and_no_protocol_errors(self):
        begin = {"type": "begin", "event_id": "p:1", "thread_id": "A", "pid": 1,
                 "tid": 1, "kind": "request", "category": "cx", "ts": 100}
        end = {"type": "end", "event_id": "p:1", "tid": 2, "ts": 200}
        clean = {"type": "quality", "clean_shutdown": True, "dropped_records": 0,
                 "protocol_errors": 0}
        for records, incomplete, errors, continuity in [
            ([begin, end, clean], 0, 0, "complete_recorded_pairs"),
            ([begin, clean], 1, 0, "unknown"),
            ([begin, end, end, clean], 0, 1, "unknown"),
            ([begin, end, dict(clean, protocol_errors=1)], 0, 1, "unknown"),
        ]:
            with self.subTest(incomplete=incomplete, errors=errors, records=len(records)):
                s = load_sdk(self.sdk(records))
                self.assertEqual(s["quality"]["incomplete_events"], incomplete)
                self.assertEqual(s["quality"]["protocol_errors"], errors)
                self.assertEqual(s["quality"]["source_continuity"], continuity)

    def test_sdk_request_reuse_correlations_are_distinct_instances(self):
        records = []
        for index in (1, 2):
            records += [
                {"type": "begin", "event_id": "p:" + str(index), "thread_id": "A", "pid": 1, "tid": 1, "kind": "request", "category": "cx", "name": "r", "ts": 100 * index, "args": {"correlation_id": "reused-handle"}},
                {"type": "end", "event_id": "p:" + str(index), "tid": 1, "ts": 100 * index + 10},
            ]
        s = load_sdk(self.sdk(records))
        ids = [e["args"]["request_id"] for e in s["events"]]
        self.assertEqual(len(set(ids)), 2)

    def test_sdk_crossing_sync_ranges_remain_incomplete(self):
        records = [
            {"type": "begin", "event_id": "outer", "thread_id": "A", "pid": 1, "tid": 1, "kind": "range", "category": "mat", "ts": 100},
            {"type": "begin", "event_id": "inner", "thread_id": "A", "pid": 1, "tid": 1, "kind": "wait", "category": "wait", "ts": 110, "parent_id": "outer"},
            {"type": "end", "event_id": "outer", "tid": 1, "ts": 120},
            {"type": "end", "event_id": "inner", "tid": 1, "ts": 130},
        ]
        s = load_sdk(self.sdk(records))
        self.assertIsNone(next(e for e in s["events"] if e["event_id"] == "outer")["end_ts"])
        self.assertGreater(s["quality"]["protocol_errors"], 0)


if __name__ == "__main__":
    unittest.main()
