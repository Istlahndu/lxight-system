"""Opt-in external RAW capture preserves the old default and blocks before exec."""
import ctypes
import errno
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from lxtrace import collector
from lxtrace.workspace import temporary_directory


def capability():
    return dict(host_id="raw-fixture", boot_id="boot", job_id=None,
                resources=[dict(resource_id="node", type="node", parent_id=None, name="fixture")],
                allocation=dict(status="unverified", cpu_ids=[]),
                clocks=[dict(clock_id="CLOCK_MONOTONIC", unit="ns", origin_ns=0)],
                capability_manifest=[collector._cap("task_pmu." + name, availability="available",
                                                   implementation_status="implemented")
                                     for name in ("cycles:u", "instructions:u")])


class GateProcess:
    pid = 321

    def __init__(self, args, **options):
        Path(args[2]).write_text(str(self.pid), encoding="ascii")
        self.go = Path(args[3])
        self.polls = 0
        self.terminated = False

    def poll(self):
        self.polls += 1
        return None if self.polls <= 1 else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class CollectorRawTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_default_event_contract_is_unchanged(self):
        self.assertEqual(collector._task_events("task-pmu", {}), [(name, value, 0) for name, value in collector.EVENTS])
        self.assertEqual(collector._task_events("task-pmu", {"raw_events": []}), [("cycles:u", 0, 0)])

    def test_codes_are_canonical_distinct_16_bit_and_limited_to_eight(self):
        result = collector._task_events("task-pmu", {"raw_events": ["0x7201", 0x7005, "0X0008"]})
        self.assertEqual(result, [("cycles:u", 0, 0), ("raw:0x7201:u", 0x7201, 4),
                                  ("raw:0x7005:u", 0x7005, 4), ("raw:0x0008:u", 8, 4)])
        invalid = (None, "0x7201", [True], [-1], [65536], [3.0], ["7201"], ["0x"], ["0x+1"],
                   ["0x0010", 16], list(range(9)))
        for value in invalid:
            with self.subTest(value=value), patch.object(collector, "doctor") as doctor:
                with self.assertRaises(ValueError):
                    collector.record_external(self.folder / "invalid", ["never"], profile="task-pmu", config={"raw_events": value})
                doctor.assert_not_called()
        with self.assertRaises(ValueError):
            collector._task_events("proc-sample", {"raw_events": []})

    def test_perf_attribute_uses_RAW_type_and_exact_encoding(self):
        seen = {}

        class Kernel:
            def syscall(self, number, attr, pid, cpu, group, flags):
                seen["attr"] = ctypes.string_at(attr, 128)
                seen["pid"], seen["cpu"] = pid.value, cpu.value
                return 75

        with patch.object(collector, "LINUX", True), patch.object(collector.platform, "machine", return_value="aarch64"), patch.object(collector.ctypes, "CDLL", return_value=Kernel()):
            self.assertEqual(collector._event_open(0x7201, pid=12, inherit=True, enable_on_exec=True, event_type=4), 75)
        typ, size, config = struct.unpack_from("=IIQ", seen["attr"])
        self.assertEqual((typ, size, config), (4, 128, 0x7201))
        self.assertEqual((seen["pid"], seen["cpu"]), (12, -1))

    def test_RAW_probe_denial_does_not_create_workload(self):
        out = self.folder / "denied.lxtrace"
        probe = dict(availability="denied", errno=errno.EACCES, reason_code="policy", failure_stage="perf_event_open")
        with patch.object(collector, "doctor", return_value=capability()), patch.object(collector, "_probe_event", return_value=probe) as raw_probe, patch.object(collector.subprocess, "Popen") as spawn:
            with self.assertRaises(collector.CollectorBlocked):
                collector.record_external(out, ["never"], profile="task-pmu", config={"raw_events": ["0x7201"]})
        spawn.assert_not_called()
        raw_probe.assert_called_once_with(0x7201, event_type=4)
        evidence = json.loads(Path(str(out) + ".capability.json").read_text(encoding="utf-8"))
        self.assertFalse(evidence["workload_started"])
        raw = next(row for row in evidence["capability"]["capability_manifest"] if row["feature_id"] == "task_pmu.raw:0x7201:u")
        self.assertEqual((raw["availability"], raw["errno"]), ("denied", errno.EACCES))

    def test_RAW_race_denial_closes_fds_and_never_releases_exec_gate(self):
        out = self.folder / "race.lxtrace"
        processes = []

        def spawn(*args, **kwargs):
            p = GateProcess(*args, **kwargs)
            processes.append(p)
            return p

        probe = dict(availability="available", errno=None, reason_code="ok", failure_stage=None)
        with patch.object(collector, "LINUX", True), patch.object(collector, "doctor", return_value=capability()), patch.object(collector, "_probe_event", return_value=probe), patch.object(collector.subprocess, "Popen", side_effect=spawn), patch.object(collector, "_proc_stat", return_value=[]), patch.object(collector, "_event_open", side_effect=[75, OSError(errno.EPERM, "changed")]), patch.object(collector.os, "close") as close:
            with self.assertRaises(collector.CollectorBlocked):
                collector.record_external(out, ["never"], profile="task-pmu", config={"raw_events": [0x7201]})
        self.assertTrue(processes[0].terminated)
        self.assertFalse(processes[0].go.exists())
        self.assertEqual(close.call_args_list.count(((75,), {})), 1)
        evidence = json.loads(Path(str(out) + ".capability.json").read_text(encoding="utf-8"))
        raw = next(row for row in evidence["capability"]["capability_manifest"] if row["feature_id"] == "task_pmu.raw:0x7201:u")
        self.assertEqual(raw["failure_stage"], "capture_event_open")

    def test_success_keeps_exact_raw_times_and_process_tree_scope(self):
        out = self.folder / "raw.lxtrace"
        probe = dict(availability="available", errno=None, reason_code="ok", failure_stage=None)
        counters = [dict(raw=9007199254740993, enabled_ns=400, running_ns=200, scaled=18014398509481986, multiplex_ratio=.5),
                    dict(raw=731, enabled_ns=400, running_ns=200, scaled=1462, multiplex_ratio=.5)]
        with patch.object(collector, "LINUX", True), patch.object(collector, "doctor", return_value=capability()), patch.object(collector, "_probe_event", return_value=probe), patch.object(collector.subprocess, "Popen", GateProcess), patch.object(collector, "_proc_stat", return_value=[]), patch.object(collector, "_event_open", side_effect=[75, 76]) as opened, patch.object(collector, "_read_counter", side_effect=counters), patch.object(collector.os, "close"):
            session, exit_code = collector.record_external(out, ["fixture"], profile="task-pmu", config={"raw_events": ["0x7201"]})
        self.assertEqual(exit_code, 0)
        self.assertEqual([row["event"] for row in session["records"]], ["cycles:u", "raw:0x7201:u"])
        self.assertEqual(session["records"][0]["raw"], 9007199254740993)
        self.assertEqual(session["records"][1]["event_type"], 4)
        self.assertEqual(session["records"][1]["event_config"], 0x7201)
        self.assertEqual(session["records"][1]["running_ns"], 200)
        self.assertIn("not_per_thread_or_per_core", session["sources"][0]["attribution_scope"])
        self.assertTrue(all(row["representation"] == "counter_window" for row in session["records"]))
        self.assertEqual(opened.call_args_list[1].kwargs, dict(pid=321, inherit=True, enable_on_exec=True, group_fd=75, event_type=4))
        self.assertEqual(session["required_sources"], ["task_pmu.cycles:u", "task_pmu.raw:0x7201:u"])


if __name__ == "__main__":
    unittest.main()
