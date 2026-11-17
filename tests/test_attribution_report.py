"""Aggregate report conservation, evidence boundaries and offline packaging."""
import copy
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.attribution.report import build_timeline, report_data, write_report
from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]


def analysis():
    return {"schema_version": "lxtrace.production-analysis.v1", "model": {"name": "Example model"},
            "configuration": {"batch": 2, "tokens": 4096},
            "totals": {"cycles": 600, "matrix_first": {"mat": 280, "vec": 120, "mem": 100, "wait": 100},
                       "issue": {"mopa": 200, "other": 300, "wait": 100}, "unit_load": {"mat": .68}},
            "stages": {}, "dense": {"status": "unavailable", "reason": "no dense"},
            "mfu": {"status": "unavailable", "reason": "incomplete_pool_coverage"},
            "windows": {"0": [0, 400000]},
            "entries": [{"entry_id": "x", "raw": {"0x7201": 123}}],
            "segments": [
                dict(entry_id="x", thread_id="a", die=0, stage="gemm", begin_ns=0, end_ns=300000,
                     cycles=300, matrix_first={"mat": 180, "vec": 120}, issue={"mopa": 100, "other": 200},
                     unit_load={"mat": .8}, unit_denominator_cycles=300),
                dict(entry_id="y", thread_id="b", die=0, stage="gemm", begin_ns=100000, end_ns=300000,
                     cycles=200, matrix_first={"mat": 100, "mem": 100}, issue={"mopa": 100, "other": 100},
                     unit_load={"mat": .5}, unit_denominator_cycles=200),
                dict(entry_id="z", thread_id="a", die=0, stage="barrier", begin_ns=300000, end_ns=400000,
                     cycles=100, matrix_first={"wait": 100}, issue={"wait": 100},
                     unit_load={"mat": 0}, unit_denominator_cycles=0)],
            "drivers": [dict(die=0, kind="frame", name="frame.1", begin_ns=0, end_ns=1000000),
                        dict(die=1, kind="frame", name="frame.1", begin_ns=0, end_ns=1000000)],
            "alignment": {"observed_pool_threads": 2, "expected_pool_threads": 8,
                          "full_pool_coverage": False, "alignment": {"basis_counts": {"block_mode": 2}},
                          "run_counts": {"large": "kept in sidecar"}},
            "provenance": [{"path": "trace-tT.bin", "sha256": "example"}],
            "warnings": ["Sample coverage is incomplete."]}


class AttributionReportTests(unittest.TestCase):
    def test_bins_conserve_cycle_estimates_and_keep_compute_unit_denominator(self):
        source = analysis()
        before = copy.deepcopy(source)
        timeline = build_timeline(source)
        self.assertEqual(source, before)
        bins = timeline["dies"][0]["bins"]
        self.assertEqual([(b["begin_ns"], b["end_ns"]) for b in bins], [(0, 200000), (200000, 400000)])
        for key, expected in source["totals"]["matrix_first"].items():
            self.assertAlmostEqual(sum(b["matrix_first"].get(key, 0) for b in bins), expected)
        self.assertAlmostEqual(sum(b["cycles"] for b in bins), 600)
        self.assertAlmostEqual(bins[0]["units"]["mat"], .7)
        self.assertAlmostEqual(bins[1]["units"]["mat"], .65)
        self.assertEqual(sum(b["sampled_thread_ns"] for b in bins), 600000)
        self.assertEqual(timeline["representation"], "aggregate")
        self.assertEqual(timeline["raw_counter_selection"], "not_prorated")
        self.assertTrue(all(b["evidence"].startswith("model_calibrated:") for b in bins))

    def test_missing_pool_die_is_empty_and_default_view_uses_pool_window(self):
        source = analysis()
        source["segments"].append(dict(source["segments"][0], begin_ns=500000, end_ns=500000))
        timeline = build_timeline(source)
        self.assertEqual(timeline["time_range"], [0, 1000000])
        self.assertEqual(timeline["default_view_range"], [0, 400000])
        self.assertEqual(timeline["dies"][1]["status"], "no_observed_pool_data")
        self.assertEqual(timeline["dies"][1]["bins"], [])
        self.assertEqual(timeline["dies"][1]["observed_pool_threads"], 0)
        self.assertEqual(timeline["zero_duration_segments_not_binned"], 1)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            build_timeline(source, 0)
        source["segments"][0]["end_ns"] = -1
        with self.assertRaisesRegex(ValueError, "integer"):
            build_timeline(source)

    def test_report_is_offline_escaped_and_preserves_full_analysis_sidecar_scope(self):
        source = analysis()
        source["model"]["name"] = '</script><img src=x onerror="alert(1)">\u2028'
        source["analysis_file"] = "analysis.json"
        source["timeline"] = build_timeline(source)
        before = copy.deepcopy(source)
        with temporary_directory() as directory:
            path = write_report(source, Path(directory) / "report.html")
            html = path.read_text(encoding="utf-8")
            self.assertNotIn(source["model"]["name"], html)
            self.assertIn("\\u003c/script>", html)
            self.assertNotRegex(html, r'<(?:script|link)[^>]+(?:src|href)=["\']https?://')
            match = re.search(r'<script type="application/json" id="attribution-data">(.*?)</script>', html, re.S)
            data = json.loads(match.group(1))
            self.assertEqual(data["model"], source["model"])
            self.assertNotIn("entries", data)
            self.assertNotIn("segments", data)
            self.assertEqual(data["entry_count"], len(source["entries"]))
            self.assertEqual(data["segment_count"], len(source["segments"]))
            self.assertNotIn("run_counts", data["alignment"])
            self.assertEqual(data["timeline"], report_data(source)["timeline"])
            self.assertIn("model_calibrated", html)
            self.assertIn("放大到选区", html)
            self.assertIn('id="timelineHorizontal"', html)
            self.assertIn("外部参考 · 未由本次输入复算", html)
            self.assertEqual(source, before)
            with self.assertRaises(FileExistsError):
                write_report(source, path)
            self.assertEqual(path.read_text(encoding="utf-8"), html)

    def test_compact_report_limits_references_without_deleting_sidecar_evidence(self):
        source = analysis()
        source["timeline"] = build_timeline(source)
        bucket = source["timeline"]["dies"][0]["bins"][0]
        bucket["entry_ids"] = ["entry-%d" % i for i in range(20)]
        data = report_data(source)
        compact = data["timeline"]["dies"][0]["bins"][0]
        self.assertEqual(compact["entry_count"], 20)
        self.assertEqual(len(compact["entry_ids"]), 8)
        self.assertEqual(len(bucket["entry_ids"]), 20)
        self.assertIn("entries", source)

    def test_complete_mfu_only_report_does_not_require_time_run(self):
        source = {"schema_version": "lxtrace.production-analysis.v1", "configuration": {"label": "same-config"},
                  "mfu": {"status": "available", "frame": {"pool": .9, "in_compute": .8, "mat_busy": .7,
                                                               "eff_mopa": .6, "mfu": 30.24},
                          "waterfall": [{"label": "all", "value": 100}, {"label": "useful", "value": 30.24}],
                          "phases": [{"name": "gemm", "mfu_pts": 30.24}]}}
        source["timeline"] = build_timeline(source)
        with temporary_directory() as directory:
            path = write_report(source, Path(directory) / "mfu.html")
            html = path.read_text(encoding="utf-8")
            self.assertIn('"mfu":30.24', html)
            self.assertIn("未提供时间运行", html)
            self.assertIn('id="mfuPhaseRows"', html)


@unittest.skipUnless(shutil.which("node"), "Node unavailable; aggregate report interaction mathematics")
class AttributionBrowserMathTests(unittest.TestCase):
    def test_navigation_missing_bins_and_independent_unit_utilization(self):
        script = """
const A=require('./web/attribution/analysis.js');
const bins=[{begin_ns:0,end_ns:20},{begin_ns:40,end_ns:60}];
const dense=Array.from({length:5},(_,i)=>({begin_ns:10*i,end_ns:10*i+10,
 matrix_first:{mat:2,mem:3},issue:{mopa:1,other:4},units:{mat:i/10},unit_denominator_cycles:5,
 phase_cycles:{gemm:5}}));
process.stdout.write(JSON.stringify({zoom:A.zoom([0,100],[0,100],5,50),
clamped:A.viewport([0,100],[80,120]),gap:A.hitBin(bins,30),end:A.hitBin(bins,20),
visible:A.visibleBins(bins,10,50),units:A.classes({units:{mat:.8,sve:.8,ldst:1.2}},'units'),
unknown:A.classes({matrix_first:{mat:10,unknown:30}},'matrix_first'),
display:A.displayBins(dense,0,50,1),rawBin:A.hitBin(dense,21)}));
"""
        run = subprocess.run([shutil.which("node"), "-e", script], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", timeout=30,
                             env=workspace_environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["zoom"], [40, 60])
        self.assertEqual(result["clamped"], [60, 100])
        self.assertIsNone(result["gap"])
        self.assertIsNone(result["end"])
        self.assertEqual(len(result["visible"]), 2)
        self.assertEqual(result["units"], {"mat": .8, "sve": .8, "ldst": 1.2})
        self.assertEqual(result["unknown"], {"mat": .25, "unknown": .75})
        self.assertEqual(len(result["display"]), 1)
        self.assertEqual(result["display"][0]["matrix_first"], {"mat": 10, "mem": 15})
        self.assertAlmostEqual(result["display"][0]["units"]["mat"], .2)
        self.assertEqual((result["rawBin"]["begin_ns"], result["rawBin"]["end_ns"]), (20, 30))


if __name__ == "__main__":
    unittest.main()
