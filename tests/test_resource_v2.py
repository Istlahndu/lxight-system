"""v2 regressions for evidence boundaries, allocation and integrity."""
import copy
import contextlib
import csv
import hashlib
import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from lxtrace.resource import (SCHEMA, analyze_resources, demo_session, load_session,
                              migrate_v1, validate_session, write_report, write_session)
from lxtrace.workspace import temporary_directory, workspace_environment


class ResourceAccountingTests(unittest.TestCase):
    def test_equal_cpu_time_parallel_and_serial_have_different_wall_union(self):
        parallel, serial = (analyze_resources(demo_session(case)) for case in ("parallel", "serial"))
        self.assertEqual(parallel["target_cpu_ns"], 8000000)
        self.assertEqual(serial["target_cpu_ns"], 8000000)
        self.assertEqual(parallel["target_wall_ns"], 4000000)
        self.assertEqual(serial["target_wall_ns"], 8000000)
        self.assertEqual(parallel["peak_concurrency"], 2)
        self.assertEqual(serial["peak_concurrency"], 1)

    def test_noncontiguous_cpu_ids_migration_and_conservation(self):
        s = demo_session()
        before = copy.deepcopy(s)
        a = analyze_resources(s)
        self.assertEqual(s, before)
        self.assertEqual(a["cpu_ids"], [2, 6, 14, 30])
        self.assertEqual(a["capacity_cpu_ns"], 48000000)
        self.assertEqual(a["target_cpu_ns"], 20000000)
        self.assertEqual(a["target_wall_ns"], 9000000)
        self.assertEqual(a["peak_concurrency"], 3)
        self.assertEqual(sum(a[state+"_cpu_ns"] for state in ("target", "other", "idle", "unknown")), a["capacity_cpu_ns"])
        for cpu in a["per_cpu"]:
            self.assertEqual(sum(cpu[state+"_cpu_ns"] for state in ("target", "other", "idle", "unknown")), cpu["capacity_cpu_ns"])
        # A migrates at exactly 4ms; its residency does not duplicate on both CPUs.
        self.assertEqual(analyze_resources(s, 4000000, 5000000, "cpu:2")["target_cpu_ns"], 0)
        self.assertEqual(analyze_resources(s, 4000000, 5000000, "cpu:14")["target_cpu_ns"], 1000000)

    def test_dynamic_allocation_changes_capacity_not_wall_clock(self):
        a = analyze_resources(demo_session("sparse"))
        self.assertEqual(a["capacity_cpu_ns"], 36000000)
        self.assertEqual(a["unallocated_cpu_ns"], 12000000)
        self.assertEqual(a["unknown_cpu_ns"], 2000000)
        self.assertEqual(sum(a[state+"_cpu_ns"] for state in ("target", "other", "idle", "unknown")), 36000000)

    def test_unverified_affinity_never_becomes_authorized_capacity(self):
        s = demo_session()
        s["allocation"]["status"] = "unverified"
        a = analyze_resources(s)
        self.assertIsNone(a["capacity_cpu_ns"])
        self.assertIsNone(a["utilization"])
        self.assertIsNone(a["coverage_ratio"])

    def test_located_loss_masks_only_affected_cpu_and_window(self):
        s = demo_session("loss")
        a = analyze_resources(s)
        self.assertEqual(a["unknown_cpu_ns"], 2000000)
        self.assertEqual(a["target_cpu_ns"], 18000000)
        self.assertEqual(analyze_resources(s, resource_id="cpu:2")["unknown_cpu_ns"], 0)

    def test_unlocated_loss_does_not_extend_surviving_states(self):
        s = demo_session()
        s["sources"][0]["loss"] = 1
        a = analyze_resources(s)
        self.assertEqual(a["target_cpu_ns"], 0)
        self.assertEqual(a["idle_cpu_ns"], 0)
        self.assertEqual(a["unknown_cpu_ns"], a["capacity_cpu_ns"])

    def test_open_scheduler_record_is_unknown_after_its_start(self):
        a = analyze_resources(demo_session("open"), resource_id="cpu:2")
        self.assertEqual(a["target_cpu_ns"], 3000000)
        self.assertEqual(a["unknown_cpu_ns"], 5000000)
        self.assertEqual(a["per_cpu"][0]["intervals"]["unknown"], [(7000000, 12000000)])

    def test_conflicting_cpu_states_are_unknown(self):
        s = demo_session()
        r = copy.deepcopy(s["records"][1])
        r.update(record_id="conflict", state="idle", begin_ns=2000000, end_ns=3000000)
        s["records"].append(r)
        a = analyze_resources(s, resource_id="cpu:2")
        self.assertEqual(a["unknown_cpu_ns"], 1000000)

    def test_query_outside_allocation_does_not_expand_capacity(self):
        a = analyze_resources(demo_session(), 0, 20000000)
        self.assertEqual(a["capacity_cpu_ns"], 48000000)
        self.assertEqual(a["unallocated_cpu_ns"], 32000000)


class EvidenceContractTests(unittest.TestCase):
    def test_exact_duplicates_deduplicate_before_default_fields_are_added(self):
        s = demo_session()
        r = copy.deepcopy(s["records"][0])
        r.pop("raw_refs")
        r.pop("correlation_ids")
        s["records"] = [r, copy.deepcopy(r)]
        self.assertEqual(len(validate_session(s)["records"]), 1)
        s["records"][1]["state"] = "other"
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            validate_session(s)

    def test_partial_counter_windows_are_not_prorated(self):
        a = analyze_resources(demo_session(), 2000000, 7000000)
        self.assertEqual(len(a["counters"]), 2)
        self.assertTrue(all(c["selection_value"] is None and not c["included_in_totals"] for c in a["counters"]))
        a = analyze_resources(demo_session(), 0, 6000000)
        self.assertEqual(a["counters"][0]["selection_value"], 10000000)
        self.assertEqual(len(a["samples"]), 1)
        self.assertEqual(a["samples"][0]["timestamp_ns"], 5500000)

    def test_counters_and_software_never_prove_hardware_overlap(self):
        a = analyze_resources(demo_session("counters"))
        self.assertEqual(a["hardware_overlap"]["status"], "unavailable")
        self.assertIsNone(a["hardware_overlap"]["overlap_ns"])
        self.assertEqual(next(c for c in a["categories"] if c["category"] == "mat")["status"], "counter_only")

    def test_aggregate_percentages_have_no_time_intervals(self):
        a = analyze_resources(demo_session("aggregate"))
        self.assertEqual(a["target_cpu_ns"], 0)
        self.assertEqual(a["unknown_cpu_ns"], a["capacity_cpu_ns"])
        self.assertEqual(a["hardware_overlap"]["status"], "unavailable")
        self.assertEqual(next(c for c in a["categories"] if c["category"] == "mat")["status"], "aggregate_only")

    def test_uncalibrated_clocks_block_cross_source_overlap(self):
        a = analyze_resources(demo_session("uncalibrated"))
        self.assertEqual(a["hardware_overlap"]["reason"], "clocks_not_aligned")
        self.assertIsNone(a["hardware_overlap"]["overlap_ns"])

    def test_clocks_must_share_one_alignment_target_and_known_errors(self):
        for mutation in ("different_target", "unknown_error", "missing_error", "missing_record_error"):
            s = demo_session()
            if mutation == "different_target":
                s["clocks"][1]["aligned_to"] = "a-different-session"
            elif mutation == "unknown_error":
                s["clocks"][1]["error_ns"] = None
            elif mutation == "missing_error":
                s["clocks"][1].pop("error_ns")
            else:
                next(r for r in s["records"] if r["source_id"] == "device").pop("uncertainty_ns")
            with self.subTest(mutation=mutation):
                overlap = analyze_resources(s)["hardware_overlap"]
                self.assertEqual(overlap["status"], "unavailable")
                self.assertIsNone(overlap["lower_bound_ns"])

    def test_hardware_quality_loss_does_not_leave_exact_overlap(self):
        for quality in ({"dropped_records": 1}, {"unknown_windows": [{"source_id": "device", "resource_id": "dma:0", "begin_ns": 4000000, "end_ns": 6000000}]}):
            s = demo_session()
            s["quality"] = quality
            with self.subTest(quality=quality):
                overlap = analyze_resources(s)["hardware_overlap"]
                self.assertEqual(overlap["status"], "unavailable")
                self.assertEqual(overlap["reason"], "hardware_source_loss")

    def test_uncertainty_produces_bounds_not_false_precision(self):
        s = demo_session()
        a = analyze_resources(s, resource_id="domain:D0")
        o = a["hardware_overlap"]
        self.assertEqual(o["attribution_scope"], "selected_resource_set_concurrency_only")
        self.assertEqual(o["status"], "bounded")
        self.assertEqual(o["overlap_ns"], 4000000)
        self.assertLess(o["lower_bound_ns"], o["overlap_ns"])
        self.assertGreater(o["upper_bound_ns"], o["overlap_ns"])
        for r in s["records"]:
            if r["source_id"] == "device":
                r["uncertainty_ns"] = 10000000
        self.assertEqual(analyze_resources(s)["hardware_overlap"]["lower_bound_ns"], 0)

    def test_legacy_migration_never_creates_cpu_or_hardware(self):
        old = {"schema_version": "lxtrace.session.v1", "session_id": "old", "data_origin": "application", "time_range": [0, 10],
               "threads": [{"thread_id": "T"}], "events": [{"event_id": "mat", "thread_id": "T", "kind": "range", "category": "mat", "begin_ts": 1, "end_ts": 9}]}
        s = migrate_v1(old)
        self.assertEqual(s["data_origin"], "imported")
        self.assertEqual(s["records"][0]["layer"], "software")
        self.assertFalse(any(r["type"] == "logical_cpu" for r in s["resources"]))
        self.assertEqual(analyze_resources(s)["hardware_overlap"]["status"], "unavailable")

    def test_aggregate_does_not_acquire_activity_boundaries(self):
        s = demo_session()
        s["records"][0].update(representation="aggregate", layer="hardware")
        with self.assertRaisesRegex(ValueError, "aggregate evidence"):
            validate_session(s)


class ArchiveTests(unittest.TestCase):
    def test_explicit_recovery_rebuilds_missing_index_and_derived_but_not_raw(self):
        with temporary_directory() as tmp:
            original, rebuilt = Path(tmp)/"original.lxtrace", Path(tmp)/"rebuilt.lxtrace"
            s = demo_session()
            s["raw_files"] = {"source/events.jsonl": "source-data\n"}
            write_session(s, original)
            (original/"index/resources.json").unlink()
            (original/"derived/analysis.json").unlink()
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_session(original)
            recovered = load_session(original, allow_missing_derived=True)
            self.assertEqual(set(recovered["archive"]["missing_derived"]), {"index/resources.json", "derived/analysis.json"})
            write_session(recovered, rebuilt)
            loaded = load_session(rebuilt)
            self.assertEqual(analyze_resources(loaded)["target_cpu_ns"], 20000000)
            result = json.loads((rebuilt/"derived/analysis.json").read_text(encoding="utf-8"))
            index = json.loads((rebuilt/"index/resources.json").read_text(encoding="utf-8"))
            self.assertEqual(result["analysis_rules_version"], index["analysis_rules_version"])
            self.assertIn("index_version", index)
            self.assertFalse((original/"index/resources.json").exists())
            (original/"raw/source/events.jsonl").unlink()
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_session(original, allow_missing_derived=True)

    def test_untrusted_source_path_is_rejected_as_data(self):
        s = demo_session()
        s["raw_files"] = {"capture.txt": {"source_path": "outside-workspace-shared-project/private.txt"}}
        with temporary_directory() as tmp:
            with self.assertRaisesRegex(ValueError, "source_path is unsupported"):
                write_session(s, Path(tmp)/"never-created.lxtrace")
            self.assertFalse((Path(tmp)/"never-created.lxtrace").exists())

    def test_raw_utf8_bytes_survive_archive_load_and_rewrite(self):
        with temporary_directory() as tmp:
            first, second = Path(tmp)/"a.lxtrace", Path(tmp)/"b.lxtrace"
            s = demo_session()
            s["raw_files"] = {"source/events.jsonl": "first\r\nsecond\n中文\r\n"}
            write_session(s, first)
            loaded = load_session(first)
            self.assertNotIn("source_path", json.dumps(loaded["raw_files"]))
            write_session(loaded, second)
            self.assertEqual((first/"raw/source/events.jsonl").read_bytes(), (second/"raw/source/events.jsonl").read_bytes())

    def test_binary_raw_is_explicitly_unsupported_after_integrity_check(self):
        with temporary_directory() as tmp:
            p = Path(tmp)/"binary.lxtrace"
            s = demo_session()
            s["raw_files"] = {"source/binary.bin": "placeholder"}
            write_session(s, p)
            raw = p/"raw/source/binary.bin"
            raw.write_bytes(b"\xff\xfe\x00\x01")
            manifest_path = p/"manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["raw/source/binary.bin"] = hashlib.sha256(raw.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "binary raw member is unsupported"):
                load_session(p)

    def test_archive_round_trip_hashes_raw_and_rejects_corruption(self):
        with temporary_directory() as tmp:
            p = Path(tmp)/"session.lxtrace"
            s = demo_session()
            s["raw_files"] = {"scheduler/raw.jsonl": '{"switch":1}\n'}
            write_session(s, p)
            loaded = load_session(p)
            self.assertEqual(loaded["schema_version"], SCHEMA)
            self.assertEqual(loaded["archive"]["integrity"], "verified")
            self.assertEqual(analyze_resources(loaded)["target_cpu_ns"], 20000000)
            self.assertEqual((p/"raw/scheduler/raw.jsonl").read_text(encoding="utf-8"), '{"switch":1}\n')
            (p/"raw/scheduler/raw.jsonl").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_session(p)

    def test_archive_rejects_path_escape_and_existing_output(self):
        with temporary_directory() as tmp:
            p = Path(tmp)/"session.lxtrace"
            s = demo_session()
            s["raw_files"] = {"../escape": "never written"}
            with self.assertRaises(ValueError):
                write_session(s, p)
            self.assertFalse((Path(tmp)/"escape").exists())
            self.assertFalse(p.exists())
            write_session(demo_session(), p)
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_session(demo_session(), p)

    def test_json_report_and_safe_payload(self):
        with temporary_directory() as tmp:
            s = demo_session()
            s["app"]["name"] = '</script><img src=x onerror=alert(1)>'
            p = Path(tmp)/"session.json"
            write_session(s, p)
            self.assertEqual(load_session(p)["app"], s["app"])
            p = Path(tmp)/"report.html"
            write_report(s, p)
            page = p.read_text(encoding="utf-8")
            self.assertIn("window.LXTRACE_SESSION=", page)
            self.assertIn("\\u003c/script>", page)
            self.assertNotIn('<script src=', page)
            self.assertNotIn('href="style.css"', page)

    def test_demo_legacy_report_uses_its_saved_archive_on_rerun(self):
        import demo_v2
        with temporary_directory() as tmp:
            root = Path(tmp)
            (root/"web").mkdir()
            (root/"artifacts").mkdir()
            old = dict(schema_version="lxtrace.session.v1", session_id="legacy-demo", run_id="original", data_origin="application",
                       time_range=[0, 10], events=[], threads=[], app={"name": "original-archived-input"})
            source = root/"artifacts/demo.lxtrace"
            source.write_text(json.dumps(old), encoding="utf-8")
            with patch.object(demo_v2, "ROOT", root), patch("sys.argv", ["demo_v2.py", "--output", str(root/"out")]), contextlib.redirect_stdout(io.StringIO()):
                demo_v2.main()
                old["app"]["name"] = "changed-input-must-not-replace-evidence"
                source.write_text(json.dumps(old), encoding="utf-8")
                demo_v2.main()
            report = (root/"out/legacy-import.html").read_text(encoding="utf-8")
            self.assertIn("original-archived-input", report)
            self.assertNotIn("changed-input-must-not-replace-evidence", report)


class BrowserParityTests(unittest.TestCase):
    def test_browser_csv_formula_text_is_literal_with_correct_quoting(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable; browser CSV guard requires workstation validation")
        dangerous = ['=HYPERLINK("https://invalid.example","label")', '+SUM(1,2)', '-1+2', '@SUM(A1)',
                     '  =1+1', '\t=1+1', '\r=1+1', '\n=1+1', '\ufeff=1+1']
        values = dangerous + ['中文, "quoted"', '123', -2.5, 0, None]
        script = "const a=require('./web/analysis.js');let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>process.stdout.write(JSON.parse(s).map(a.csvCell).join(',')));"
        proc = subprocess.run([node, "-e", script], input=json.dumps(values).encode("utf-8"),
                              capture_output=True, cwd=Path(__file__).resolve().parents[1], env=workspace_environment(), timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        parsed = next(csv.reader(io.StringIO(proc.stdout.decode("utf-8"), newline='')))
        self.assertEqual(parsed, ["'"+v for v in dangerous]+['中文, "quoted"', '123', '-2.5', '0', ''])

    def test_python_and_browser_conserve_the_same_resource_evidence(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable; browser parity requires workstation validation")
        sessions = [demo_session(case) for case in
                    ("resource", "parallel", "serial", "sparse", "loss", "open", "counters", "aggregate", "uncalibrated")]
        unverified = demo_session()
        unverified["allocation"]["status"] = "unverified"
        sessions.append(unverified)
        unlocated = demo_session()
        unlocated["sources"][0]["loss"] = 2
        sessions.append(unlocated)
        conflict = demo_session()
        record = copy.deepcopy(conflict["records"][1])
        record.update(record_id="different-target-instance", thread_instance_id="other-target-instance", begin_ns=2000000, end_ns=3000000)
        conflict["records"].append(record)
        sessions.append(conflict)
        for mutation in ("different_target", "unknown_error", "missing_error", "missing_record_error", "hardware_loss"):
            s = demo_session()
            if mutation == "different_target":
                s["clocks"][1]["aligned_to"] = "another-target"
            elif mutation == "unknown_error":
                s["clocks"][1]["error_ns"] = None
            elif mutation == "missing_error":
                s["clocks"][1].pop("error_ns")
            elif mutation == "missing_record_error":
                next(r for r in s["records"] if r["source_id"] == "device").pop("uncertainty_ns")
            else:
                s["quality"]["unknown_windows"] = [{"source_id": "device", "begin_ns": 4000000, "end_ns": 6000000}]
            sessions.append(s)
        inputs = []
        for s in sessions:
            for start, end, resource_id in ((0, s["time_range"][1], None), (2000000, 7000000, "domain:D0"), (3000000, 5000000, "cpu:2")):
                inputs.append(dict(session=s, start=start, end=end, options={"resource_id": resource_id} if resource_id else {}))
        script = "const a=require('./web/analysis.js');let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s).map(x=>a.analyze(x.session,x.start,x.end,x.options)))));"
        proc = subprocess.run([node, "-e", script], input=json.dumps(inputs), text=True, encoding="utf-8",
                              capture_output=True, cwd=Path(__file__).resolve().parents[1], env=workspace_environment(), timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = json.loads(proc.stdout)
        fields = ("wall_ns", "target_cpu_ns", "other_cpu_ns", "idle_cpu_ns", "unknown_cpu_ns", "capacity_cpu_ns",
                  "target_wall_ns", "peak_concurrency", "mean_target_cpus", "coverage_ratio", "utilization", "completion_skew_ns")
        for i, (query, actual) in enumerate(zip(inputs, results)):
            expected = analyze_resources(query["session"], query["start"], query["end"], query["options"].get("resource_id"))
            for field in fields:
                with self.subTest(case=i, field=field):
                    self.assertEqual(expected[field], actual[field])
            for field in ("status", "compute_ns", "transfer_ns", "overlap_ns", "lower_bound_ns", "upper_bound_ns"):
                with self.subTest(case=i, hardware_field=field):
                    self.assertEqual(expected["hardware_overlap"][field], actual["hardware_overlap"][field])
            with self.subTest(case=i, field="counter windows"):
                self.assertEqual([r["selection_value"] for r in expected["counters"]], [r["selected_value"] for r in actual["counters"]])


if __name__ == "__main__":
    unittest.main()
