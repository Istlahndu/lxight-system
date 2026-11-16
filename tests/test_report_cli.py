"""Public report commands share one offline viewer and reject unsafe launches."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from lxtrace.core import finalize_session
from lxtrace.resource import load_session
from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]
SESSION_SCRIPT = re.compile(
    r"<script\s+data-lxtrace-session\s*>\s*window\.LXTRACE_SESSION=(.*?);\s*</script>",
    re.DOTALL,
)


class ReportCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.out = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "lxtrace", *map(str, arguments)],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            timeout=30, env=workspace_environment({"PYTHONIOENCODING": "utf-8"}),
        )

    def write_rank(self, rank, directory=None):
        """Two synthetic SDK sessions use local times on one absolute clock."""
        directory = self.out if directory is None else directory
        directory.mkdir(parents=True, exist_ok=True)
        events = []
        specifications = [
            ("frame", 0, 100, "frame", "unknown"),
            ("matmul", 10, 30, "range", "mat"),
            ("activation", 30, 40, "range", "vec"),
            ("exchange", 40, 60, "exchange", "cx"),
            ("request", 40, 80, "request", "cx"),
            ("wait", 60, 80, "wait", "wait"),
        ]
        for name, begin, end, kind, category in specifications:
            events.append({
                # Deliberately reuse IDs across ranks: merging must preserve both.
                "event_id": name, "thread_id": "main", "kind": kind,
                "category": category, "name": "rank%d.%s" % (rank, name),
                "begin_ts": begin, "end_ts": end,
                "parent_id": None if kind == "frame" else "frame",
                "frame_id": "frame", "source_id": "sdk-fixture",
                "evidence": "observed", "args": {"rank": rank},
            })
        events[0]["args"]["driver_calls_complete"] = False
        session = finalize_session({
            "schema_version": "lxtrace.session.v1",
            "session_id": "synthetic-report-cli-rank%d" % rank,
            "run_id": "synthetic-report-cli-shared-run",
            "host_id": "synthetic-same-host", "boot_id": "synthetic-same-boot",
            "data_origin": "synthetic", "execution_mode": "user_only",
            "app": {"name": "Report CLI fixture", "build_id": "test-only",
                    "workload_config": {"rank": rank}},
            "clock": {"id": "CLOCK_MONOTONIC", "unit": "ns",
                      "origin_ns": 8_000_000_000 + rank * 40, "resolution_ns": 1},
            "time_range": [0, 100],
            "threads": [{"thread_id": "main", "pid": 3000 + rank,
                         "tid": 3000 + rank, "name": "rank%d-main" % rank,
                         "role": "frame_driver"}],
            "events": events,
            "sources": [{"source_id": "sdk-fixture", "kind": "sdk"}],
            "quality": {"clean_shutdown": True, "dropped_records": 0,
                        "protocol_errors": 0},
            "provenance": [{"description": "Synthetic SDK CLI test input"}],
        })
        path = directory / ("rank%d.lxtrace" % rank)
        path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        return path

    def unpack_report(self, path):
        page = path.read_text(encoding="utf-8")
        matches = list(SESSION_SCRIPT.finditer(page))
        self.assertEqual(len(matches), 1, "HTML must contain exactly one session payload")
        payload = json.loads(matches[0].group(1))
        self.assertEqual(payload["schema_version"], "lxtrace.resource-session.v2")
        shell = SESSION_SCRIPT.sub("<!--SESSION-PAYLOAD-->", page)
        # timeline.js is inlined, alongside the standard viewer, for offline use.
        self.assertIn("root.LXTimeline=api", shell)
        self.assertIn("softwareTracks", shell)
        self.assertIn('id="zoomSelection"', shell)
        self.assertIn('id="timelineHorizontal"', shell)
        self.assertIn('aria-label="时间线水平滚动条"', shell)
        self.assertNotRegex(shell, r"<script\b[^>]*\bsrc\s*=")
        self.assertNotRegex(shell, r"<link\b[^>]*\bhref\s*=")
        return shell, payload

    def assert_two_rank_records(self, session):
        self.assertEqual(len(session["records"]), 12)
        names = {record["name"] for record in session["records"]}
        self.assertTrue({"rank0.frame", "rank1.frame", "rank0.matmul",
                         "rank1.matmul", "rank0.request", "rank1.wait"} <= names)
        records = {record["name"]: record for record in session["records"]}
        self.assertEqual(records["rank1.matmul"]["begin_ns"] -
                         records["rank0.matmul"]["begin_ns"], 40)
        self.assertEqual(records["rank1.frame"]["begin_ns"] -
                         records["rank0.frame"]["begin_ns"], 40,
                         "frames must retain their absolute-clock offset")
        self.assertEqual(records["rank0.matmul"]["end_ns"] -
                         records["rank0.matmul"]["begin_ns"], 20)
        self.assertEqual(len({record["record_id"] for record in session["records"]}), 12)
        self.assertEqual(len({record["resource_id"] for record in session["records"]}), 2)

    def test_report_multiple_inputs_produces_one_timeline_and_native_session(self):
        rank0, rank1 = self.write_rank(0), self.write_rank(1)
        html = self.out / "report.html"
        archive = self.out / "merged.lxtrace"
        result = self.cli("report", rank1, rank0, "--output", html,
                          "--session-output", archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, payload = self.unpack_report(html)
        self.assert_two_rank_records(payload)
        stored = load_session(archive)
        self.assert_two_rank_records(stored)
        self.assertEqual(stored["records"], payload["records"])

    def test_report_discovers_same_run_sessions_in_directory(self):
        inputs = self.out / "inputs"
        self.write_rank(0, inputs)
        self.write_rank(1, inputs)
        html = self.out / "discovered.html"
        result = self.cli("report", inputs, "--output", html)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, payload = self.unpack_report(html)
        self.assert_two_rank_records(payload)

    def test_older_sessions_require_an_explicit_run_group(self):
        sources = [self.write_rank(rank) for rank in range(2)]
        for path in sources:
            old = json.loads(path.read_text(encoding="utf-8"))
            old.pop("run_id")
            path.write_text(json.dumps(old), encoding="utf-8")
        rejected = self.out / "ungrouped.html"
        result = self.cli("report", *sources, "--output", rejected)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("run", result.stderr.lower())
        self.assertFalse(rejected.exists())
        grouped = self.out / "grouped.html"
        result = self.cli("report", *sources, "--output", grouped,
                          "--run-id", "explicit-test-group")
        self.assertEqual(result.returncode, 0, result.stderr)
        _, payload = self.unpack_report(grouped)
        self.assert_two_rank_records(payload)
        self.assertEqual(payload["run_id"], "explicit-test-group")

    def test_single_session_report_and_v1_export_use_the_same_web_template(self):
        source = self.write_rank(0)
        report, exported = self.out / "single.html", self.out / "export.html"
        result = self.cli("report", source, "--output", report)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("export", source, "--format", "html", "--output", exported)
        self.assertEqual(result.returncode, 0, result.stderr)
        report_shell, report_session = self.unpack_report(report)
        export_shell, export_session = self.unpack_report(exported)
        self.assertEqual(report_shell, export_shell)
        self.assertEqual(len(report_session["records"]), 6)
        self.assertEqual(len(export_session["records"]), 6)
        expected = {"rank0." + name for name in
                    ("frame", "matmul", "activation", "exchange", "request", "wait")}
        self.assertEqual({record["name"] for record in report_session["records"]}, expected)
        self.assertEqual({record["name"] for record in export_session["records"]}, expected)

    def test_bundle_viewer_includes_timeline_and_migrates_v1_payload(self):
        source = self.write_rank(0)
        bundle = self.out / "viewer"
        result = self.cli("bundle-viewer", source, "--output", bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((bundle / "timeline.js").read_bytes(),
                         (ROOT / "web" / "timeline.js").read_bytes())
        self.assertRegex((bundle / "index.html").read_text(encoding="utf-8"),
                         r'<script\b[^>]*src=["\']timeline\.js["\']')
        session = json.loads((bundle / "demo-session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["schema_version"], "lxtrace.resource-session.v2")
        self.assertEqual(len(session["records"]), 6)

    def business_command(self, marker):
        code = "from pathlib import Path; Path(%r).write_text('started', encoding='utf-8')" % str(marker)
        return [sys.executable, "-c", code]

    def test_basic_attach_is_rejected_before_business_starts(self):
        marker, output = self.out / "attach-started.txt", self.out / "attach.lxtrace"
        result = self.cli("record", "--profile", "basic", "--attach", "12345",
                          "--output", output, "--", *self.business_command(marker))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--attach", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(output.exists())

    def test_basic_duration_is_rejected_before_business_starts(self):
        marker, output = self.out / "duration-started.txt", self.out / "duration.lxtrace"
        result = self.cli("record", "--profile", "basic", "--duration", "1ms",
                          "--output", output, "--", *self.business_command(marker))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("duration", result.stderr.lower())
        self.assertFalse(marker.exists())
        self.assertFalse(output.exists())

    def test_existing_html_is_rejected_before_external_business_starts(self):
        for explicit in (False, True):
            with self.subTest(explicit_html=explicit):
                stem = "explicit" if explicit else "default"
                output = self.out / (stem + ".lxtrace")
                html = self.out / ("custom.html" if explicit else stem + ".html")
                marker = self.out / (stem + "-started.txt")
                html.write_text("keep this report", encoding="utf-8")
                arguments = ["record", "--profile", "proc-sample", "--output", output]
                if explicit:
                    arguments += ["--html", html]
                result = self.cli(*arguments, "--", *self.business_command(marker))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("already exists", result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse(output.exists())
                self.assertEqual(html.read_text(encoding="utf-8"), "keep this report")


if __name__ == "__main__":
    unittest.main()
