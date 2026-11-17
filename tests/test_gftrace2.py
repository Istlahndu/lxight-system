"""Native GFTRACE2 evidence, state attribution, integer clocks and raw retention."""
import base64
import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path

from lxtrace.gftrace import K, load_gftrace2, read_gftrace2
from lxtrace import resource as R

BASE = 120_000_000_000_001
FREQ = 100_000_000
POOL = [(0, 1, 3, -1), (40, 3, 6, 0), (50, 4, 6, 0), (50, 23, 3, 0),
        (60, 23, 4, 0), (80, 24, 4, 0), (90, 3, 10, 0), (100, 4, 10, 0), (100, 2, 3, -1)]
DRIVER = [(0, 9, 1, 0), (10, 15, 3, 0), (90, 16, 3, 0), (100, 10, 1, 0)]
META = "run tA T 100 tc 100 B 1 freq 100000000\nstage 6 gate_up\nstage 10 ffn_exit\nframe 0 counting 1 parts 2 tokens 100\npmu 0x7201,0x0008\n"


class GFTrace2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "trace-tA.bin"
        self.path.with_suffix(".txt").write_text(META, encoding="utf-8")

    def write(self, buffers=None, frequency=FREQ, origin=BASE, reserved=0, tail=b""):
        if buffers is None:
            readings = [(i * 150, i * 5, i * 20, 0, 0, 0, 0, 0, 0) for i in range(len(POOL))]
            buffers = [(0, 0, 0, POOL, readings)]
        raw = bytearray(struct.pack("<8sQii", b"GFTRACE2", frequency, len(buffers), reserved))
        for die, dom, thr, events, readings in buffers:
            nv = 9 if readings is not None else 0
            raw.extend(struct.pack("<6i", die, dom, thr, len(events), nv, 0))
            raw.extend(b"".join(struct.pack("<QHHi", origin + tick, kind, code, aux) for tick, kind, code, aux in events))
            if nv:
                raw.extend(b"".join(struct.pack("<9Q", *row) for row in readings))
        raw.extend(tail)
        self.path.write_bytes(raw)
        return bytes(raw)

    @staticmethod
    def windows(s):
        return [r for r in s["records"] if r["representation"] == "counter_window"]

    def test_chained_modes_next_ending_barrier_and_exact_large_origin(self):
        self.write()
        s = load_gftrace2(self.path)
        intervals = [r for r in s["records"] if r["source_id"] == "gf.sw"]
        self.assertEqual([(r["begin_ns"], r["end_ns"], r["args"]["mode"], r["args"]["stage"]) for r in intervals],
                         [(0, 400, "busy", "gate_up"), (400, 500, "bar", "gate_up"), (500, 600, "deqg", "ffn_exit"),
                          (600, 800, "mmg", "ffn_exit"), (800, 900, "busy", "ffn_exit"), (900, 1000, "bar", "ffn_exit")])
        self.assertEqual(s["clocks"][0]["origin_ticks"], BASE)
        self.assertEqual(s["clocks"][0]["native_clock_id"], "CNTVCT_EL0")
        self.assertEqual(s["raw_time_range_ticks"], [BASE, BASE + 100])
        self.assertEqual(intervals[3]["raw_begin_ticks"], BASE + 60)
        self.assertEqual(intervals[3]["raw_event_indices"], [4, 5])
        self.assertFalse(any(r["type"] == "logical_cpu" for r in s["resources"]))

    def test_raw_input_and_all_cumulative_rows_are_lossless_with_u64_counts(self):
        counts = [(2**63 + i * 50, 2**62 + i, 0, 0, 0, 0, 0, 0, 0) for i in range(len(POOL))]
        raw = self.write([(0, 0, 0, POOL, counts)])
        s = load_gftrace2(self.path)
        self.assertEqual(base64.b64decode(s["raw_files"]["raw/gftrace2/trace.bin.base64"]), raw)
        self.assertEqual(read_gftrace2(self.path)[1][0]["v"], counts)
        win = next(r for r in self.windows(s) if r["event"] == "0x0011")
        self.assertIsInstance(win["counter_begin"], str)
        self.assertEqual(int(win["counter_begin"]), 2**63)
        ref = win["raw_refs"][0]
        i0 = win["raw_event_indices"][0]
        self.assertEqual(struct.unpack_from("<9Q", raw, ref["reading_byte_offsets"][0]), counts[i0])

    def test_counter_deltas_windows_and_aggregates_conserve_counts(self):
        self.write()
        s = load_gftrace2(self.path)
        for event, total in (("0x0011", 1200), ("0x7201", 40), ("0x0008", 160)):
            windows = [r for r in self.windows(s) if r["event"] == event]
            self.assertEqual(sum(r["raw"] for r in windows), total)
            self.assertEqual(sum(r["value"] for r in s["records"] if r["representation"] == "aggregate" and r["event"] == event), total)
        mmg = next(r for r in s["records"] if r.get("label") == "ffn_exit|mmg" and r.get("event") == "0x7201")
        self.assertEqual(mmg["value"], 5)
        self.assertEqual((mmg["begin_ns"], mmg["end_ns"]), (600, 800))
        self.assertEqual((mmg["enabled_ns"], mmg["running_ns"]), (200, 200))
        self.assertFalse(any(r.get("semantics_status") == "verified" for r in self.windows(s)))

    def test_no_windows_keeps_all_raw_readings_and_aggregates(self):
        self.write()
        full, summary = load_gftrace2(self.path), load_gftrace2(self.path, windows=False)
        self.assertEqual(summary["raw_files"], full["raw_files"])
        self.assertFalse(self.windows(summary))
        self.assertEqual([r for r in full["records"] if r["representation"] == "aggregate"],
                         [r for r in summary["records"] if r["representation"] == "aggregate"])

    def test_ticks_only_zero_arrays_are_not_hardware_counter_evidence(self):
        self.path.with_suffix(".txt").write_text(META.rsplit("pmu", 1)[0] + "pmu ticks\n", encoding="utf-8")
        raw = self.write([(0, 0, 0, POOL, [(0,) * 9] * len(POOL))])
        s = load_gftrace2(self.path)
        self.assertEqual({r["layer"] for r in s["records"]}, {"software"})
        self.assertEqual({r["source_id"] for r in s["sources"]}, {"gf.sw"})
        self.assertEqual(base64.b64decode(s["raw_files"]["raw/gftrace2/trace.bin.base64"]), raw)
        self.assertEqual(s["provenance"][0]["pmu_state"], "ticks_only")
        self.write()
        with self.assertRaisesRegex(ValueError, "conflict"):
            load_gftrace2(self.path)

    def test_unknown_pmu_columns_remain_unknown_without_inventing_codes(self):
        self.path.with_suffix(".txt").unlink()
        self.write()
        s = load_gftrace2(self.path)
        events = {r["event"] for r in s["records"] if r["representation"] == "aggregate"}
        self.assertEqual(events, {"0x0011", "unknown.column.1", "unknown.column.2"})
        self.assertTrue(any("metadata is missing" in x for x in s["quality"]["warnings"]))

    def test_zero_duration_counter_increments_are_retained(self):
        events = [(0, 1, 3, -1), (10, 23, 3, 0), (10, 23, 4, 0), (20, 24, 4, 0), (30, 2, 3, -1)]
        counts = [(0,) * 9, (10,) + (0,) * 8, (17,) + (0,) * 8, (27,) + (0,) * 8, (37,) + (0,) * 8]
        self.write([(0, 0, 0, events, counts)])
        s = load_gftrace2(self.path)
        window = next(r for r in self.windows(s) if r["label"] == "tail|deqg")
        self.assertEqual((window["begin_ns"], window["end_ns"], window["value"]), (100, 100, 7))
        self.assertEqual(sum(r["value"] for r in s["records"] if r["representation"] == "aggregate" and r["event"] == "0x0011"), 37)

    def test_native_fractional_tick_conversion_preserves_raw_boundaries(self):
        freq = 333_333_333
        self.path.with_suffix(".txt").write_text(META.replace(str(FREQ), str(freq)), encoding="utf-8")
        self.write(frequency=freq)
        s = load_gftrace2(self.path)
        for r in s["records"]:
            if r["representation"] in ("interval", "counter_window"):
                self.assertEqual(r["begin_ns"], (r["raw_begin_ticks"] - BASE) * 1_000_000_000 // freq)
                self.assertEqual(r["end_ns"], (r["raw_end_ticks"] - BASE) * 1_000_000_000 // freq)
        self.assertEqual(s["clocks"][0]["origin_remainder_numerator"], BASE * 1_000_000_000 % freq)

    def test_driver_nested_scopes_do_not_duplicate_counters_and_keep_frame_metadata(self):
        readings = [(i * 100, i * 4, i * 10, 0, 0, 0, 0, 0, 0) for i in range(len(DRIVER))]
        self.write([(0, -1, -1, DRIVER, readings)])
        s = load_gftrace2(self.path)
        frame = next(r for r in s["records"] if r.get("kind") == "frame")
        self.assertEqual((frame["args"]["counting"], frame["args"]["parts"], frame["args"]["tokens"]), (True, 2, 100))
        self.assertEqual(sum(r["value"] for r in self.windows(s) if r["event"] == "0x0011"), 300)
        self.assertEqual(sum(r["value"] for r in s["records"] if r["representation"] == "aggregate" and r["event"] == "0x0011"), 300)
        layer = next(r for r in s["records"] if r.get("args", {}).get("kind") == "LAY")
        self.assertEqual(layer["parent_id"], frame["record_id"])

    def test_wait_inside_substep_returns_to_same_mode(self):
        events = [(0, 1, 3, -1), (10, 23, 1, 0), (20, 7, 4, 0), (30, 8, 4, 0), (40, 24, 1, 0), (50, 2, 3, -1)]
        self.write([(0, 0, 0, events, None)])
        s = load_gftrace2(self.path)
        self.assertEqual([r["args"]["mode"] for r in s["records"]], ["busy", "push", "spin", "push", "busy"])

    def test_identity_text_metadata_and_explicit_mapping_are_preserved(self):
        self.path.with_suffix(".txt").write_text(META + "host_id node\nboot_id boot\nrun_id run\ncounter_domain_id nodeboot\n", encoding="utf-8")
        self.write()
        s = load_gftrace2(self.path, mapping={"threads": {"0:0:0": {"cpu": 17, "verified": True}}})
        self.assertEqual((s["host_id"], s["boot_id"], s["run_id"], s["clocks"][0]["counter_domain_id"]), ("node", "boot", "run", "nodeboot"))
        self.assertEqual(next(r for r in s["resources"] if r["type"] == "thread")["declared_mapping"]["cpu"], 17)
        self.assertEqual(s["allocation"]["status"], "unverified")

    def test_archive_roundtrip_keeps_original_binary_and_cumulative_readings(self):
        self.write()
        s = load_gftrace2(self.path)
        archive = self.root / "capture.lxtrace"
        R.write_session(s, archive)
        back = R.load_session(archive)
        self.assertEqual(back["records"], s["records"])
        self.assertEqual({name: value["content"] if isinstance(value, dict) else value
                          for name, value in back["raw_files"].items()}, s["raw_files"])
        self.assertEqual(back["clocks"], s["clocks"])

    def test_public_cli_import_raw_report_export_and_summary_archive(self):
        from lxtrace.cli import main

        raw = self.write()
        archive, summary = self.root / "full.lxtrace", self.root / "summary.lxtrace"
        html, exported = self.root / "report.html", self.root / "export.json"
        commands = [
            ["import", "--type", "gftrace", "--input", self.path, "--output", archive],
            ["import", "--type", "gftrace", "--input", self.path, "--no-windows", "--output", summary],
            ["report", self.path, "--output", html],
            ["export", archive, "--format", "chrome-trace", "--output", exported],
        ]
        for command in commands:
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(list(map(str, command))), 0, errors.getvalue())
        full, reduced = R.load_session(archive), R.load_session(summary)
        self.assertEqual(full["schema_version"], R.SCHEMA)
        self.assertTrue(self.windows(full))
        self.assertFalse(self.windows(reduced))
        for session in (full, reduced):
            entry = session["raw_files"]["raw/gftrace2/trace.bin.base64"]
            self.assertEqual(base64.b64decode(entry["content"]), raw)
        self.assertEqual([r for r in full["records"] if r["representation"] == "aggregate"],
                         [r for r in reduced["records"] if r["representation"] == "aggregate"])
        self.assertIn("<html", html.read_text(encoding="utf-8").lower())
        trace = json.loads(exported.read_text(encoding="utf-8"))
        self.assertEqual(trace["lxtrace_native"]["records"], full["records"])
        self.assertTrue(all(event["ph"] == "i" for event in trace["traceEvents"]
                            if event["args"]["representation"] == "counter_window"))

    def test_metadata_cannot_rename_native_cntvct_clock(self):
        self.write()
        with self.assertRaisesRegex(ValueError, "cannot rename the native clock"):
            load_gftrace2(self.path, {"native_clock_id": "CLOCK_MONOTONIC"})

    def test_rejects_truncated_reserved_duplicate_and_decreasing_binary(self):
        raw = self.write()
        for broken in (raw[:20], raw[:-1], raw + b"x", raw[:20] + struct.pack("<i", 1) + raw[24:],
                       raw[:40] + struct.pack("<i", 3) + raw[44:]):
            with self.subTest(length=len(broken)):
                self.path.write_bytes(broken)
                with self.assertRaises(ValueError):
                    load_gftrace2(self.path)
        for buffers in ([(0, 0, 0, POOL, None)] * 2,
                        [(0, 0, 0, POOL[:2] + [(-1, *POOL[2][1:])] + POOL[3:], None)],
                        [(0, 0, 0, POOL, [(10 - i,) + (0,) * 8 for i in range(len(POOL))])]):
            self.write(buffers)
            with self.assertRaises(ValueError):
                load_gftrace2(self.path)

    def test_rejects_unclosed_and_crossing_software_scopes(self):
        cases = [POOL[:-1], [POOL[0], POOL[0]] + POOL[1:], [(0, 2, 3, -1)],
                 [POOL[0], (5, 8, 1, 0)] + POOL[1:], [POOL[0], (5, 24, 4, 0)] + POOL[1:],
                 [POOL[0], (5, 7, 1, 0), (6, 23, 3, 0), (7, 8, 1, 0)] + POOL[1:]]
        for events in cases:
            self.write([(0, 0, 0, events, None)])
            with self.subTest(events=events), self.assertRaises(ValueError):
                load_gftrace2(self.path)
        self.write([(0, -1, -1, DRIVER[:-1], None)])
        with self.assertRaises(ValueError):
            load_gftrace2(self.path)

    def test_rejects_bad_metadata_even_for_unobserved_frames(self):
        self.write()
        cases = [[], {"frames": []}, {"frames": {"99": {"counting": "false"}}}, {"frames": {"99": {"tokens": -1}}},
                 {"pmu": "0x0008,0x0008"}, {"pmu": "0x0011"}, {"frequency_hz": 1000}, {"host_id": 3}]
        for metadata in cases:
            meta_path = self.root / "metadata.json"
            meta_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                load_gftrace2(self.path, meta_path)


if __name__ == "__main__":
    unittest.main()
