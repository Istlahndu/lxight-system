"""Independent clock, identity and input-protection checks for reports."""
import copy
import hashlib
import json
import unittest
from pathlib import Path

from lxtrace import resource
from lxtrace.report import merge_sessions, write_unified_report
from lxtrace.workspace import temporary_directory


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def legacy(rank, origin=1000000, run_id="shared-run"):
    thread = "7:7:1"  # Deliberately reused IDs exercise the namespace mapping.
    frame = dict(event_id="7:frame", thread_id=thread, pid=7, tid=7,
                 kind="frame", category="unknown", name="rank%d.frame" % rank,
                 begin_ts=0, end_ts=100, parent_id=None, frame_id="7:frame",
                 source_id="sdk", args=dict(rank=rank, application_frame_id="frame"))
    child = dict(event_id="7:child", thread_id=thread, pid=7, tid=7,
                 kind="request", category="cx", name="rank%d.copy" % rank,
                 begin_ts=20, end_ts=60, parent_id="7:frame", frame_id="7:frame",
                 source_id="sdk", args=dict(rank=rank, request_id="7:child"))
    text = json.dumps(dict(rank=rank, origin=origin)) + "\n"
    return dict(schema_version="lxtrace.session.v1", session_id="rank%d" % rank,
                run_id=run_id, data_origin="application", host_id="node1", boot_id="boot1",
                clock=dict(id="CLOCK_MONOTONIC", unit="ns", origin_ns=origin,
                           resolution_ns=None, calibration_error_ns=None),
                time_range=[0, 100], threads=[dict(thread_id=thread, pid=7, tid=7, name="rank%d" % rank)],
                events=[frame, child], sources=[dict(source_id="sdk", clock_id="CLOCK_MONOTONIC")],
                quality=dict(dropped_records=0, protocol_errors=0, incomplete_events=0,
                             clean_shutdown=True), capability_manifest=[],
                provenance=[dict(sha256=digest(text))], raw_files={"capture/records.jsonl": text})


class UnifiedReportTests(unittest.TestCase):
    def setUp(self):
        self.scratch = temporary_directory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def save(self, name, session):
        path = self.root / name
        path.write_text(json.dumps(session), encoding="utf-8")
        return path

    def test_four_ranks_keep_clock_offsets_and_all_internal_links(self):
        offsets = [0, 350, 50, 350]
        inputs = [self.save("rank%d.json" % rank, legacy(rank, 1000000 + offset))
                  for rank, offset in enumerate(offsets)]
        merged = merge_sessions(inputs)
        self.assertEqual(merged["time_range"], [0, 450])
        self.assertEqual(len(merged["records"]), 8)
        self.assertEqual(len({r["record_id"] for r in merged["records"]}), 8)
        for rank, offset in enumerate(offsets):
            prefix = "input%d:" % rank
            records = {r["kind"]: r for r in merged["records"] if r["record_id"].startswith(prefix)}
            frame, request = records["frame"], records["request"]
            self.assertEqual((frame["begin_ns"], frame["end_ns"]), (offset, offset + 100))
            self.assertEqual((request["begin_ns"], request["end_ns"]), (offset + 20, offset + 60))
            self.assertEqual(request["frame_id"], frame["record_id"])
            self.assertEqual(request["parent_id"], frame["record_id"])
            self.assertEqual(request["args"]["request_id"], request["record_id"])
            self.assertEqual(request["thread_instance_id"], prefix + "7:7:1")
            self.assertEqual(request["resource_id"], prefix + "thread:7:7:1")
            self.assertIsNone(request["uncertainty_ns"])
        self.assertTrue(all(c["error_ns"] is None for c in merged["clocks"]))
        self.assertIsNone(resource.analyze_resources(merged)["target_cpu_ns"])
        self.assertEqual(resource.analyze_resources(merged)["hardware_overlap"]["status"], "unavailable")

    def test_native_roundtrip_preserves_raw_bytes_hash_and_refs(self):
        paths = [self.save("rank%d.json" % r, legacy(r, 1000000 + r)) for r in range(2)]
        merged = merge_sessions(paths)
        archive = self.root / "merged.lxtrace"
        resource.write_session(merged, archive)
        loaded = resource.load_session(archive)
        self.assertEqual(loaded["archive"]["integrity"], "verified")
        for rank in range(2):
            embedded = archive / "raw" / "inputs" / ("input%d" % rank) / "input" / "session.json"
            self.assertEqual(embedded.read_bytes(), paths[rank].read_bytes())
            raw = archive / "raw" / "inputs" / ("input%d" % rank) / "capture" / "records.jsonl"
            expected = legacy(rank, 1000000 + rank)["raw_files"]["capture/records.jsonl"]
            self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), digest(expected))
        for record in loaded["records"]:
            self.assertTrue((archive / record["raw_refs"][0].split("#")[0]).is_file())

    def test_rejects_cross_host_boot_unknown_clock_and_nonidentity_transform(self):
        first = self.save("first.json", legacy(0))
        for field, value in (("host_id", "elsewhere"), ("boot_id", "reboot")):
            other = legacy(1, 1000001)
            other[field] = value
            path = self.save("other.json", other)
            with self.assertRaisesRegex(ValueError, field):
                merge_sessions([first, path], run_id="declared-group")
        other = legacy(1, 1000001)
        other["clock"]["id"] = "unknown"
        with self.assertRaisesRegex(ValueError, "CLOCK_MONOTONIC"):
            merge_sessions([first, self.save("other.json", other)])
        other = resource.migrate_v1(legacy(1, 1000001))
        other.update(host_id="node1", boot_id="boot1")
        other["clocks"][0]["transform"] = dict(scale=2, offset_ns=-1000001)
        with self.assertRaisesRegex(ValueError, "transform"):
            merge_sessions([first, self.save("other.json", other)])

    def test_legacy_run_group_needs_explicit_declaration_and_is_recorded(self):
        paths = [self.save("rank%d.json" % r, legacy(r, 1000000 + r, "old-run%d" % r)) for r in range(2)]
        with self.assertRaisesRegex(ValueError, "run_id"):
            merge_sessions(paths)
        merged = merge_sessions(paths, run_id="reviewed-group")
        self.assertEqual(merged["run_id"], "reviewed-group")
        self.assertEqual(merged["provenance"][0]["grouping"], "explicit_user_run_id")

    def test_directory_prefers_v2_and_deduplicates_legacy_aliases(self):
        paths = []
        for rank in range(2):
            old = legacy(rank, 1000000 + rank)
            paths.append(self.save("rank%d.json" % rank, old))
            resource.write_session(resource.migrate_v1(old), self.root / ("rank%d.lxtrace" % rank))
        merged = merge_sessions(self.root)
        self.assertEqual(len(merged["records"]), 4)
        self.assertTrue(all(i["path"].endswith(".lxtrace") for i in merged["provenance"][0]["inputs"]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            merge_sessions([paths[0], paths[0]])

    def test_explicit_alias_and_partially_repeated_capture_are_rejected(self):
        old = legacy(0)
        first = self.save("first.json", old)
        archive = self.root / "first.lxtrace"
        resource.write_session(resource.migrate_v1(old), archive)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            merge_sessions([first, archive])
        changed = copy.deepcopy(old)
        changed.update(session_id="wrapped-again", provenance=[], raw_files={})
        changed["events"] = changed["events"][:1]
        with self.assertRaisesRegex(ValueError, "overlapping|repeated"):
            merge_sessions([first, self.save("partial.json", changed)])

    def test_remerge_uses_current_shared_origin_and_conflicting_ids_fail(self):
        first = self.save("rank0.json", legacy(0, 1000000))
        second = self.save("rank1.json", legacy(1, 1000100))
        merged = merge_sessions([first, second])
        archive = self.root / "two.lxtrace"
        resource.write_session(merged, archive)
        third = self.save("rank2.json", legacy(2, 1000200))
        final = merge_sessions([archive, third])
        self.assertEqual(final["time_range"], [0, 300])
        starts = {r["name"]: r["begin_ns"] for r in final["records"] if r["kind"] == "frame"}
        self.assertEqual(starts, {"rank0.frame": 0, "rank1.frame": 100, "rank2.frame": 200})
        conflict = legacy(3, 1000300)
        conflict["session_id"] = "rank0"
        with self.assertRaisesRegex(ValueError, "conflicting session_id"):
            merge_sessions([first, self.save("conflict.json", conflict)])

    def test_single_any_v2_but_multi_os_or_pmu_rejected(self):
        single = resource.demo_session("parallel")
        path = self.save("os.json", single)
        self.assertEqual(merge_sessions(path)["records"], single["records"])
        other = self.save("software.json", legacy(0))
        with self.assertRaisesRegex(ValueError, "software sources only"):
            merge_sessions([path, other], run_id="group")

    def test_raw_sdk_metadata_used_and_old_single_raw_still_reportable(self):
        paths = []
        for rank in range(2):
            origin = 1000000 + rank
            rows = [dict(type="meta", schema_version="lxtrace.sdk.v1", clock_id="CLOCK_MONOTONIC",
                         host_id="node1", boot_id="boot1", run_id="raw-run", pid=rank+1),
                    dict(type="thread", thread_id=str(rank), pid=rank+1, tid=rank+1),
                    dict(type="begin", event_id=str(rank), thread_id=str(rank), pid=rank+1, tid=rank+1,
                         ts=origin, kind="range", name="compute", category="mat", args={}),
                    dict(type="end", event_id=str(rank), tid=rank+1, ts=origin+100),
                    dict(type="quality", clean_shutdown=True)]
            path = self.root / ("rank%d.jsonl" % rank)
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            paths.append(path)
        merged = merge_sessions(paths)
        self.assertEqual(merged["run_id"], "raw-run")
        self.assertEqual(merged["time_range"], [0, 101])
        text = paths[0].read_text()
        rows = [json.loads(line) for line in text.splitlines()]
        for key in ("host_id", "boot_id", "run_id"):
            rows[0].pop(key)
        paths[0].write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertEqual(len(merge_sessions(paths[0])["records"]), 1)
        with self.assertRaisesRegex(ValueError, "host_id"):
            merge_sessions(paths, run_id="explicit")

    def test_quality_open_ends_and_clock_uncertainty_are_retained(self):
        paths = []
        for rank in range(2):
            old = legacy(rank, 1000000 + rank)
            if rank:
                old["events"][1]["end_ts"] = None
                old["quality"].update(dropped_records=2, incomplete_events=1, clean_shutdown=False)
            paths.append(self.save("rank%d.json" % rank, old))
        merged = merge_sessions(paths)
        self.assertEqual(merged["quality"]["dropped_records"], 2)
        self.assertFalse(merged["quality"]["clean_shutdown"])
        self.assertEqual(merged["quality"]["per_source"]["input1:sdk"]["quality"]["incomplete_events"], 1)
        self.assertIsNone(next(r for r in merged["records"] if r["record_id"] == "input1:7:child")["end_ns"])

    def test_fixed_web_template_and_existing_or_input_outputs_are_protected(self):
        source = self.save("source.json", legacy(0))
        output, archive = self.root / "report.html", self.root / "report.lxtrace"
        merged = write_unified_report(source, output, session_output=archive)
        html = output.read_text(encoding="utf-8")
        self.assertIn('id="timelineCanvas"', html)
        self.assertIn('data-lxtrace-session', html)
        self.assertEqual(resource.load_session(archive)["records"], merged["records"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            write_unified_report(source, output, session_output=self.root / "not-created.lxtrace")
        self.assertFalse((self.root / "not-created.lxtrace").exists())
        with self.assertRaisesRegex(ValueError, "input evidence"):
            write_unified_report(source, source)
        with self.assertRaisesRegex(ValueError, "input evidence"):
            write_unified_report(archive, archive / "rewrite.html")
        with self.assertRaisesRegex(ValueError, "already exists"):
            write_unified_report(source, self.root / "new.html", session_output=archive)


if __name__ == "__main__":
    unittest.main()
