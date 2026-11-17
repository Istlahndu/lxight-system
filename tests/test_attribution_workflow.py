"""Production CLI boundaries, input protection and aggregate archive semantics."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from lxtrace.cli import main
from lxtrace.attribution.evidence import to_resource_session
from lxtrace.attribution.workflow import (analysis_link, bin_nanoseconds,
                                           output_paths, parse_count_inputs, read_json)
from lxtrace.resource import analyze_resources, load_session, write_session
from lxtrace.workspace import temporary_directory


class AttributionWorkflowTests(unittest.TestCase):
    def test_standalone_cli_preserves_source_reference_and_separate_sidecar(self):
        with temporary_directory() as directory:
            root = Path(directory)
            counts = {"label": "same-window", "source": "application summary",
                      "frame": {"threads": 304, "seconds": 1, "compute_cycles": 155000000000,
                                "mopa": 38750000000, "flop": 19840000000000}}
            source = root / "counts.json"
            source.write_text(json.dumps(counts), encoding="utf-8")
            reference = root / "reference.json"
            reference.write_text('{"mfu":99}', encoding="utf-8")
            output = root / "html" / "report.html"
            sidecar = root / "data" / "analysis #1.json"
            command = ["production-report", "--mfu-counts", str(source), "--output", str(output),
                       "--analysis-output", str(sidecar), "--reference", str(reference)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(command), 0)
            analysis = read_json(sidecar)
            self.assertAlmostEqual(analysis["mfu"]["frame"]["mfu"], 100 * 19840000000000 / (608 * 1550000000 * 512))
            self.assertEqual(analysis["mfu"]["input_counts"], counts)
            self.assertEqual(analysis["external_reference"]["status"], "external_reference_not_recomputed")
            self.assertEqual(analysis["analysis_file"], "../data/analysis%20%231.json")
            self.assertEqual(analysis["segments"], [])
            self.assertFalse(analysis["hardware_interval"])
            before = output.read_bytes(), sidecar.read_bytes(), source.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(command), 2)
                self.assertEqual(main(command + ["--count", "tA=unused.bin"]), 2)
            self.assertEqual(before, (output.read_bytes(), sidecar.read_bytes(), source.read_bytes()))

    def test_output_guards_cover_inputs_metadata_and_containing_directories(self):
        with temporary_directory() as directory:
            root = Path(directory)
            source = root / "capture" / "trace.bin"
            source.parent.mkdir()
            source.write_bytes(b"original")
            source.with_suffix(".txt").write_text("metadata", encoding="utf-8")
            for bad in (source, source.with_suffix(".txt"), source.parent,
                        source / "nested.html"):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    output_paths(bad, root / "analysis.json", [source])
            for out, sidecar, extra in ((root / "x", root / "x", ()),
                                         (root / "x", root / "x" / "y", ()),
                                         (root / "x", root / "y", (root / "y",))):
                with self.assertRaises(ValueError):
                    output_paths(out, sidecar, [source], extra)
            self.assertEqual(source.read_bytes(), b"original")

    def test_nonfinite_inputs_and_bad_bin_sizes_fail_before_writing(self):
        with temporary_directory() as directory:
            root = Path(directory)
            source = root / "counts.json"
            output = root / "report.html"
            for raw in ('{"frame":NaN}', '{"frame":1e999}', '{"frame":Infinity}', '[]', '{"frame":[]}'):
                source.write_text(raw, encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["production-report", "--mfu-counts", str(source),
                                           "--output", str(output)]), 2)
                self.assertFalse(output.exists())
            for value in (0, -1, float("nan"), float("inf"), 1e308, 1e-12):
                with self.assertRaises(ValueError):
                    bin_nanoseconds(value)
            self.assertEqual(bin_nanoseconds(.2), 200000)
            self.assertEqual(list(parse_count_inputs(["first=a.bin", "second=b.bin"])), ["first", "second"])
            for values in ([], ["a"], ["=a"], ["a="], ["a=x", "a=y"]):
                with self.assertRaises(ValueError):
                    parse_count_inputs(values)

    def test_cross_volume_link_is_not_fabricated(self):
        with patch("lxtrace.attribution.workflow.os.path.relpath", side_effect=ValueError):
            self.assertIsNone(analysis_link("report.html", "analysis.json"))

    def test_aggregate_export_roundtrip_cannot_claim_hardware_overlap(self):
        analysis = {"schema_version": "lxtrace.production-analysis.v1",
                    "timeline": {"time_range": [10, 1000]}, "warnings": ["subset"],
                    "coverage": {"observed_pool_threads": 1, "full_pool_coverage": False},
                    "stages": {"gemm": {"matrix_first": {"mat": 60, "comm_x": 40},
                                         "bounds": {"mem_lo": 0, "mem_hi": 20}}},
                    "provenance": [{"path": "external.bin", "sha256": "0" * 64}]}
        before = copy.deepcopy(analysis)
        session = to_resource_session(analysis)
        other = copy.deepcopy(analysis)
        other["hardware"] = {"revision": "different calibration"}
        self.assertNotEqual(to_resource_session(other)["session_id"], session["session_id"])
        self.assertEqual(analysis, before)
        self.assertTrue(all(r["representation"] == "aggregate" and not r["hardware_interval"]
                            and r["semantics_status"].startswith("model_calibrated:")
                            and "begin_ns" not in r and "end_ns" not in r for r in session["records"]))
        with temporary_directory() as directory:
            path = Path(directory) / "model.lxtrace"
            write_session(session, path)
            recovered = load_session(path)
        result = analyze_resources(recovered)
        self.assertEqual(result["hardware_overlap"]["status"], "unavailable")
        self.assertIsNone(result["hardware_overlap"]["overlap_ns"])
        categories = {c["category"]: c for c in result["categories"]}
        self.assertEqual(categories["mat"]["status"], "aggregate_only")
        self.assertEqual(categories["cx"]["status"], "aggregate_only")
        self.assertIsNone(result["target_cpu_ns"])
        self.assertEqual(recovered["quality"]["attribution_evidence"], "aggregate_estimates_only")


if __name__ == "__main__":
    unittest.main()
