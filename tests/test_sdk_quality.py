"""SDK quality snapshots must preserve anomalies through import and reporting."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.importers import load_sdk
from lxtrace import resource
from lxtrace.workspace import temporary_directory, workspace_environment


class SdkQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.begin = dict(type="begin", event_id="1:1", thread_id="1:2:1", pid=1, tid=2,
                          kind="range", category="mat", name="compute", ts=100, args={})
        self.end = dict(type="end", event_id="1:1", tid=2, ts=200)

    def load(self, rows, bad_line=None):
        path = self.root / "capture.jsonl"
        meta = dict(type="meta", schema_version="lxtrace.sdk.v1", pid=1, clock_id="CLOCK_MONOTONIC")
        path.write_text("\n".join(json.dumps(row) for row in [meta] + rows) + "\n" + (bad_line or ""), encoding="utf-8")
        return load_sdk(path)

    @staticmethod
    def snapshot(revision, dropped=0, errors=0, late=0, incomplete=0, clean=True):
        return dict(type="quality", quality_semantics="cumulative_snapshot", quality_revision=revision,
                    dropped_records=dropped, protocol_errors=errors, late_records=late,
                    incomplete_events=incomplete, clean_shutdown=clean)

    def test_cumulative_snapshots_do_not_double_count_and_keep_history(self):
        rows = [self.begin, self.end, self.snapshot(1, dropped=2, errors=1, clean=False),
                self.snapshot(2, dropped=5, errors=2, late=3, clean=False)]
        quality = self.load(rows)["quality"]
        self.assertEqual((quality["dropped_records"], quality["protocol_errors"], quality["late_records"]), (5, 2, 3))
        self.assertEqual(quality["quality_history"], rows[-2:])
        self.assertEqual(quality["quality_snapshot_count"], 2)
        self.assertFalse(quality["clean_shutdown"])
        self.assertTrue(quality["capture_partial"])
        self.assertEqual(quality["source_continuity"], "unknown")

    def test_later_clean_row_cannot_erase_earlier_abnormal_shutdown(self):
        quality = self.load([self.begin, self.end, self.snapshot(1, clean=False), self.snapshot(2)])["quality"]
        self.assertFalse(quality["clean_shutdown"])
        self.assertTrue(quality["capture_partial"])

    def test_legacy_delta_rows_stay_additive_but_quality_is_conservative(self):
        quality = self.load([self.begin, self.end,
                             dict(type="quality", dropped_records=2, protocol_errors=1, clean_shutdown=False),
                             dict(type="quality", dropped_records=3, protocol_errors=2, clean_shutdown=True)])["quality"]
        self.assertEqual((quality["dropped_records"], quality["protocol_errors"]), (5, 3))
        self.assertFalse(quality["clean_shutdown"])

    def test_bad_input_and_open_events_override_producer_clean_claim(self):
        for rows, bad_line in (([self.begin, self.snapshot(1)], None),
                               ([self.begin, self.end, self.snapshot(1)], "{bad\n")):
            with self.subTest(rows=rows):
                quality = self.load(rows, bad_line)["quality"]
                self.assertFalse(quality["clean_shutdown"])
                self.assertTrue(quality["capture_partial"])
                self.assertEqual(quality["source_continuity"], "unknown")

    def test_nonmonotonic_quality_is_retained_as_lower_bound(self):
        quality = self.load([self.begin, self.end, self.snapshot(2, dropped=4),
                             self.snapshot(1, dropped=1)])["quality"]
        self.assertEqual(quality["dropped_records"], 4)
        self.assertGreater(quality["protocol_errors"], 0)
        self.assertTrue(quality["quality_counts_are_lower_bounds"])
        self.assertFalse(quality["clean_shutdown"])

    def test_reported_unclosed_ranges_and_missing_final_quality_remain_unknown(self):
        quality = self.load([self.begin, self.end, self.snapshot(1, incomplete=2)])["quality"]
        self.assertEqual(quality["reported_incomplete_events"], 2)
        self.assertEqual(quality["incomplete_events"], 2)
        self.assertFalse(quality["clean_shutdown"])
        quality = self.load([self.begin, self.end])["quality"]
        self.assertTrue(quality["capture_partial"])
        self.assertEqual(quality["source_continuity"], "unknown")

    def test_noninterchangeable_args_are_marked_without_losing_ranges(self):
        for args in ([], {"value": float("inf")}, {"value": float("nan")}, {"value": "\ud800"}):
            with self.subTest(args=args):
                session = self.load([dict(self.begin, args=args), self.end, self.snapshot(1)])
                self.assertEqual(session["events"][0]["args"], {"invalid_args": True})
                self.assertEqual(session["events"][0]["status"], "complete")
                self.assertEqual(session["quality"]["protocol_errors"], 1)
                resource.validate_session(resource.migrate_v1(session))

    def test_native_archive_and_standard_html_retain_abnormal_quality(self):
        session = self.load([self.begin, self.end, self.snapshot(1),
                             self.snapshot(2, dropped=8, late=8, clean=False)])
        native = resource.migrate_v1(session)
        archive = self.root / "capture.lxtrace"
        resource.write_session(native, archive)
        back = resource.load_session(archive)
        self.assertEqual(back["quality"], native["quality"])
        self.assertEqual(back["records"], native["records"])
        html = self.root / "capture.html"
        resource.write_report(back, html)
        text = html.read_text(encoding="utf-8")
        self.assertIn('"late_records": 8', text)
        self.assertIn('"clean_shutdown": false', text)
        self.assertIn("record attempts after shutdown", text)

    def test_unified_report_keeps_per_process_history_and_late_total(self):
        from lxtrace.report import merge_sessions

        paths = []
        for pid in (1, 2):
            meta = dict(type="meta", schema_version="lxtrace.sdk.v1", pid=pid,
                        clock_id="CLOCK_MONOTONIC", host_id="host", boot_id="boot", run_id="same-run")
            begin = dict(self.begin, event_id="%d:1" % pid, thread_id="%d:2:1" % pid, pid=pid, ts=100 * pid)
            end = dict(self.end, event_id=begin["event_id"], ts=100 * pid + 50)
            quality = self.snapshot(1, dropped=8 if pid == 1 else 0, late=8 if pid == 1 else 0, clean=pid == 2)
            path = self.root / ("process%d.jsonl" % pid)
            path.write_text("\n".join(json.dumps(row) for row in (meta, begin, end, quality)) + "\n", encoding="utf-8")
            paths.append(path)
        merged = merge_sessions(paths)
        self.assertEqual(merged["quality"]["late_records"], 8)
        self.assertEqual(merged["quality"]["dropped_records"], 8)
        self.assertTrue(merged["quality"]["capture_partial"])
        self.assertFalse(merged["quality"]["clean_shutdown"])
        self.assertEqual(len(merged["quality"]["per_input"]["input0"]["quality_history"]), 1)

    def test_analysis_json_and_browser_expose_capture_quality(self):
        native = resource.migrate_v1(self.load([self.begin, self.end, self.snapshot(1),
                                               self.snapshot(2, dropped=3, late=3, clean=False)]))
        analysis = resource.analyze_resources(native)
        self.assertEqual(analysis["capture_quality"], native["quality"])
        self.assertTrue(any("record attempts after shutdown" in w for w in analysis["warnings"]))
        self.assertTrue(any("complete coverage" in w for w in analysis["warnings"]))
        if shutil.which("node"):
            script = "const A=require('./web/analysis.js');const s=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(A.analyze(s,...s.time_range)));"
            run = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(native),
                                 text=True, encoding="utf-8", capture_output=True,
                                 cwd=Path(__file__).resolve().parents[1], timeout=30,
                                 env=workspace_environment())
            self.assertEqual(run.returncode, 0, run.stderr)
            browser = json.loads(run.stdout)
            self.assertEqual(browser["capture_quality"], native["quality"])
            self.assertTrue(any("record attempts after shutdown" in w for w in browser["warnings"]))
            self.assertTrue(any("采集质量存在异常" in w for w in browser["warnings"]))


if __name__ == "__main__":
    unittest.main()
