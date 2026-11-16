"""A real tiny MLP capture validates per-layer frames without synthetic overlap."""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.core import finalize_session
from lxtrace.importers import load_sdk
from lxtrace.workspace import temporary_directory, workspace_environment
from mlp_demo import summarize_run


ROOT = Path(__file__).resolve().parents[1]


class MlpLayerFrameCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_compiler = Path("/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++")
        compiler = str(server_compiler) if server_compiler.exists() else shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("C++ compiler unavailable")
        cls.temporary = temporary_directory()
        cls.directory = Path(cls.temporary.name)
        cls.executable = cls.directory / ("mlp.exe" if os.name == "nt" else "mlp")
        command = [compiler, "-std=c++11", "-O2", "-ffp-contract=off", "-pthread", "-Isdk",
                   "sdk/lxtrace.cpp", "examples/mlp_pipeline.cpp", "-o", str(cls.executable)]
        server_lib = server_compiler.parent.parent / "lib64"
        if server_lib.exists() and str(server_compiler) == compiler:
            command.append("-Wl,-rpath," + str(server_lib))
        subprocess.run(command, cwd=ROOT, env=workspace_environment(), check=True,
                       capture_output=True, text=True, encoding="utf-8", timeout=90)
        cls.runs = {}
        for mode in ("serial", "pipeline"):
            raw = cls.directory / (mode + ".jsonl")
            process = subprocess.run([str(cls.executable), mode, "--microbatches", "3", "--batch", "2",
                                      "--input", "17", "--hidden", "23", "--output", "11"],
                                     cwd=ROOT, env=workspace_environment(dict(LXTRACE_OUTPUT=str(raw),
                                         LXTRACE_DISABLED="0", LXTRACE_CAPACITY="32768")),
                                     check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
            business = json.loads(next(line[7:] for line in process.stdout.splitlines() if line.startswith("RESULT ")))
            data = finalize_session(load_sdk(raw))
            cls.runs[mode] = (data, business)
        disabled_raw = cls.directory / "disabled.jsonl"
        process = subprocess.run([str(cls.executable), "pipeline", "--microbatches", "3", "--batch", "2",
                                  "--input", "17", "--hidden", "23", "--output", "11"],
                                 cwd=ROOT, env=workspace_environment(dict(LXTRACE_OUTPUT=str(disabled_raw),
                                     LXTRACE_DISABLED="1")), check=True, capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
        cls.disabled = json.loads(next(line[7:] for line in process.stdout.splitlines() if line.startswith("RESULT ")))
        cls.disabled_capture_exists = disabled_raw.exists()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_layer_frames_have_distinct_identity_and_category_boundaries(self):
        for mode, (data, business) in self.runs.items():
            with self.subTest(mode=mode):
                frames = [e for e in data["events"] if e["kind"] == "frame"]
                layers = [e for e in frames if e["args"].get("frame_kind") == "layer"]
                self.assertEqual(len(frames), 7)
                self.assertEqual(len(layers), 6)
                global_frame = next(e for e in frames if e["name"] == "mlp.inference")
                identities = {(e["args"]["microbatch"], e["args"]["layer"]) for e in layers}
                self.assertEqual(identities, {(m, layer) for m in range(3) for layer in (1, 2)})
                for frame in layers:
                    self.assertEqual(frame["parent_id"], global_frame["event_id"])
                    members = [e for e in data["events"] if e.get("frame_id") == frame["event_id"] and
                               e["kind"] not in ("frame", "request")]
                    self.assertEqual(sum(e["category"] == "mat" for e in members), 1)
                    self.assertEqual(sum(e["category"] == "vec" for e in members), 1)
                    self.assertEqual(sum(e["kind"] == "exchange" for e in members), 1)
                    self.assertGreaterEqual(sum(e["kind"] == "wait" for e in members), 1)
                    for event in members:
                        self.assertGreaterEqual(event["begin_ts"], frame["begin_ts"])
                        self.assertLessEqual(event["end_ts"], frame["end_ts"])
                self.assertEqual(data["quality"]["dropped_records"], 0)
                self.assertEqual(data["quality"]["protocol_errors"], 0)
                self.assertEqual(data["quality"]["incomplete_events"], 0)
                self.assertTrue(business["reference_valid"])

    def test_frame_summary_keeps_real_intervals_and_unavailable_hardware_unknown(self):
        for mode, (data, business) in self.runs.items():
            with self.subTest(mode=mode):
                summary = summarize_run(data, business)
                self.assertEqual(len(summary["frames"]), 6)
                for frame in summary["frames"]:
                    for category in ("mat", "vec", "cx", "wait"):
                        evidence = frame["categories"][category]
                        self.assertEqual(evidence["status"], "recorded")
                        self.assertTrue(evidence["intervals"])
                        self.assertTrue(all(e["end_ts"] >= e["begin_ts"] for e in evidence["intervals"]))
                    for category in ("memory_exposed", "frontend_branch"):
                        self.assertEqual(frame["categories"][category]["status"], "unavailable")
                        self.assertIsNone(frame["categories"][category]["wall_ns"])
                    self.assertEqual(frame["categories"]["cc"]["status"], "not_recorded")
        self.assertEqual(self.runs["serial"][1]["output_checksum"], self.runs["pipeline"][1]["output_checksum"])

    def test_disabled_adapter_preserves_real_model_output_and_writes_no_capture(self):
        self.assertFalse(self.disabled_capture_exists)
        self.assertTrue(self.disabled["reference_valid"])
        self.assertEqual(self.disabled["output_checksum"], self.runs["pipeline"][1]["output_checksum"])
