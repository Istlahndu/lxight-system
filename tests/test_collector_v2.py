"""External collection contracts: evidence limits, handshake, failure and quota."""
import copy
import ctypes
import errno
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from lxtrace import collector
from lxtrace.workspace import temporary_directory


def snapshot():
    return dict(host_id="fixture", boot_id="fixture-boot", job_id=None,
                resources=[dict(resource_id="node:fixture", type="node", parent_id=None,
                                name="fixture", mapping_source="fixture"),
                           dict(resource_id="logical_cpu:7", type="logical_cpu", parent_id="node:fixture",
                                logical_cpu=7, name="CPU 7", mapping_source="fixture")],
                allocation=dict(status="unverified", cpu_ids=[7], source="affinity"),
                clocks=[dict(clock_id="CLOCK_MONOTONIC", unit="ns", resolution_ns=1, origin_ns=0)],
                capability_manifest=[collector._cap("schedule", reason="implementation_gap"),
                                     collector._cap("core_pmu", availability="denied", reason="access_denied"),
                                     collector._cap("proc_sample", availability="available", implementation_status="implemented"),
                                     collector._cap("task_pmu.cycles:u", availability="available", implementation_status="implemented"),
                                     collector._cap("task_pmu.instructions:u", availability="available", implementation_status="implemented")])


class FakeProcess:
    pid = 123

    def __init__(self, args, **kwargs):
        Path(args[2]).write_text("123", encoding="ascii")
        self.polls = 0
        self.terminated = False

    def poll(self):
        self.polls += 1
        return None if self.polls <= 1 else 17

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 17


class CollectorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.tmp = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_required_sources_fail_before_workload_and_preserve_evidence(self):
        out = self.tmp / "blocked.lxtrace"
        with patch.object(collector, "doctor", return_value=snapshot()), patch.object(collector.subprocess, "Popen") as spawn:
            with self.assertRaises(collector.CollectorBlocked):
                collector.record_external(out, ["must-not-launch"], profile="resource")
        spawn.assert_not_called()
        evidence = json.loads(Path(str(out) + ".capability.json").read_text(encoding="utf-8"))
        self.assertFalse(evidence["workload_started"])
        self.assertIn("schedule", evidence["reason"])
        self.assertIn("core_pmu", evidence["reason"])
        self.assertFalse(out.exists())

    def test_shared_uid_attach_does_not_become_authorized_by_job_text(self):
        out = self.tmp / "attach.lxtrace"
        with patch.object(collector, "doctor", return_value=snapshot()), patch.object(collector.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(collector.CollectorBlocked, "trusted collector"):
                collector.record_external(out, attach=123, job_id="user-supplied")
        spawn.assert_not_called()

    def test_config_cannot_weaken_required_resource_sources(self):
        out = self.tmp / "weak.lxtrace"
        with patch.object(collector, "doctor", return_value=snapshot()), patch.object(collector.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(collector.CollectorBlocked, "cannot be weakened"):
                collector.record_external(out, ["must-not-launch"], config={"required_sources": []})
        spawn.assert_not_called()

    def test_invalid_sampling_config_fails_before_start(self):
        with patch.object(collector.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "sample_interval"):
                collector.record_external(self.tmp / "bad.lxtrace", ["x"], config={"sample_interval_ms": 0})
        spawn.assert_not_called()

    def test_proc_parser_handles_parentheses_and_thread_instance_start(self):
        fields = ["S"] + ["0"] * 49
        fields[11], fields[12], fields[19], fields[36] = "41", "7", "123456", "75"
        parsed = collector._parse_proc_stat("53 (name with ) parentheses) " + " ".join(fields))
        self.assertEqual(parsed, dict(tid=53, comm="name with ) parentheses", state="S", utime_ticks=41,
                                      stime_ticks=7, start_ticks=123456, cpu=75))

    def test_probe_distinguishes_permission_from_unimplemented_platform(self):
        with patch.object(collector, "_event_open", side_effect=OSError(errno.EACCES, "denied")):
            self.assertEqual(collector._probe_event(0)["availability"], "denied")
        with patch.object(collector, "_event_open", side_effect=OSError(errno.ENOSYS, "absent")):
            self.assertEqual(collector._probe_event(0)["availability"], "unsupported")

    def test_probe_disabled_event_never_enables_other_task_collection(self):
        calls = []

        class Lib:
            @staticmethod
            def syscall(number, attr, pid, cpu, group, flags):
                calls.append((number.value, ctypes.string_at(attr, 128), pid.value, cpu.value))
                return 51

        with patch.object(collector, "LINUX", True), patch.object(collector.platform, "machine", return_value="aarch64"), patch.object(collector.ctypes, "CDLL", return_value=Lib()):
            fd = collector._event_open(0, pid=-1, cpu=7)
        self.assertEqual(fd, 51)
        self.assertEqual(calls[0][0], 241)
        self.assertEqual(calls[0][2:], (-1, 7))
        attr_flags = struct.unpack_from("=Q", calls[0][1], 40)[0]
        self.assertTrue(attr_flags & 1)  # disabled
        self.assertTrue(attr_flags & (1 << 5))  # exclude kernel
        self.assertFalse(attr_flags & (1 << 12))  # no enable_on_exec on probe

    def test_proc_capture_keeps_samples_as_points_and_exit_code(self):
        out = self.tmp / "proc.lxtrace"
        values = [dict(tid=123, comm="x", state="R", utime_ticks=2, stime_ticks=0, start_ticks=30, cpu=7)]
        with patch.object(collector, "LINUX", True), patch.object(collector, "doctor", return_value=snapshot()), patch.object(collector.subprocess, "Popen", FakeProcess), patch.object(collector, "_proc_stat", return_value=values), patch.object(collector.os, "sysconf", return_value=100, create=True):
            session, exit_code = collector.record_external(out, [sys.executable, "-c", "pass"], profile="proc-sample", config={"sample_interval_ms": 1})
        self.assertEqual(exit_code, 17)
        self.assertEqual(session["capture"]["exit_code"], 17)
        self.assertEqual(session["data_origin"], "live")
        self.assertEqual(session["allocation"]["status"], "unverified")
        self.assertTrue(session["records"])
        self.assertTrue(all(r["representation"] == "sample" for r in session["records"]))
        self.assertTrue(all(r["begin_ns"] is None and r["end_ns"] is None for r in session["records"]))
        self.assertEqual(session["quality"]["scheduler_coverage"], 0)
        self.assertTrue((out / "raw/proc-sample/samples.jsonl").exists())

    def test_task_pmu_preserves_raw_and_multiplex_times_without_busy_intervals(self):
        out = self.tmp / "pmu.lxtrace"
        counters = [dict(raw=100, enabled_ns=40, running_ns=20, scaled=200, multiplex_ratio=.5),
                    dict(raw=70, enabled_ns=40, running_ns=20, scaled=140, multiplex_ratio=.5)]
        with patch.object(collector, "LINUX", True), patch.object(collector, "doctor", return_value=snapshot()), patch.object(collector.subprocess, "Popen", FakeProcess), patch.object(collector, "_proc_stat", return_value=[]), patch.object(collector, "_event_open", side_effect=[51, 52]), patch.object(collector, "_read_counter", side_effect=counters), patch.object(collector.os, "close"):
            session, exit_code = collector.record_external(out, ["fixture"], profile="task-pmu")
        self.assertEqual(exit_code, 17)
        self.assertEqual([r["raw"] for r in session["records"]], [100, 70])
        self.assertEqual(session["records"][0]["enabled_ns"], 40)
        self.assertEqual(session["records"][0]["running_ns"], 20)
        self.assertEqual(session["records"][0]["scaled"], 200)
        self.assertIsNone(session["records"][0]["uncertainty_ns"])
        self.assertTrue(all(r["representation"] == "counter_window" for r in session["records"]))
        self.assertIn("process", session["sources"][0]["scope"])
        self.assertEqual(session["quality"]["h1_status"], "blocked")

    def test_permission_change_after_probe_does_not_start_workload(self):
        out = self.tmp / "pmu-race.lxtrace"
        processes = []

        def spawn(*args, **kwargs):
            process = FakeProcess(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(collector, "LINUX", True), patch.object(collector, "doctor", return_value=snapshot()), patch.object(collector.subprocess, "Popen", side_effect=spawn), patch.object(collector, "_proc_stat", return_value=[]), patch.object(collector, "_event_open", side_effect=OSError(errno.EACCES, "policy changed")):
            with self.assertRaises(collector.CollectorBlocked):
                collector.record_external(out, ["fixture"], profile="task-pmu")
        self.assertTrue(processes[0].terminated)
        evidence = json.loads(Path(str(out) + ".capability.json").read_text(encoding="utf-8"))
        event = next(c for c in evidence["capability"]["capability_manifest"] if c["feature_id"] == "task_pmu.cycles:u")
        self.assertEqual(event["failure_stage"], "capture_event_open")
        self.assertEqual(event["errno"], errno.EACCES)
        self.assertFalse(out.exists())

    def test_quota_stops_collection_without_terminating_workload(self):
        class Running(FakeProcess):
            def poll(self):
                return None

        out = self.tmp / "quota.lxtrace"
        values = [dict(tid=123, comm="x", state="R", utime_ticks=2, stime_ticks=0, start_ticks=30, cpu=7)]
        instances = []

        def spawn(*args, **kwargs):
            process = Running(*args, **kwargs)
            instances.append(process)
            return process

        with patch.object(collector, "LINUX", True), patch.object(collector, "doctor", return_value=snapshot()), patch.object(collector.subprocess, "Popen", side_effect=spawn), patch.object(collector, "_proc_stat", return_value=values), patch.object(collector.os, "sysconf", return_value=100, create=True):
            session, exit_code = collector.record_external(out, ["fixture"], profile="proc-sample", config={"max_samples": 1})
        self.assertEqual(exit_code, 0)
        self.assertIsNone(session["capture"]["exit_code"])
        self.assertTrue(session["capture"]["workload_running"])
        self.assertFalse(instances[0].terminated)
        self.assertIn("sample_quota", session["capture"]["stop_reason"])


if __name__ == "__main__":
    unittest.main()
