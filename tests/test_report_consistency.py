"""Reports share one viewer; synthetic inputs exercise different observed outcomes."""
import copy
import json
import re
import unittest
from pathlib import Path

from lxtrace.core import analyze, finalize_session, write_report
from lxtrace.workspace import temporary_directory
from pipeline_lesson import export_run_reports, verify_pair


SESSION_SCRIPT = re.compile(
    br"<script\s+data-lxtrace-session\s*>\s*window\.LXTRACE_SESSION=(.*?);\s*</script>",
    re.DOTALL,
)


def event(eid, start, end, kind="range", category="vec", thread="compute", **extra):
    value = dict(event_id=eid, thread_id=thread, kind=kind, category=category,
                 name=eid, begin_ts=start, end_ts=end, parent_id=None, frame_id=None,
                 source_id="synthetic-report-test", evidence="observed", args={})
    value.update(extra)
    return value


def fixture(name, events, quality=None):
    threads = sorted({e["thread_id"] for e in events})
    return finalize_session(dict(
        schema_version="lxtrace.session.v1", session_id="synthetic-" + name,
        run_id="synthetic-" + name, data_origin="synthetic", execution_mode="user_only",
        app=dict(name="Synthetic report fixture <&> \u2028 " + name,
                 build_id="report-consistency-test", workload_config={}),
        clock=dict(id="synthetic-monotonic", unit="ns", origin_ns=0, resolution_ns=1),
        time_range=[0, 60],
        threads=[dict(thread_id=t, pid=1, tid=i + 1, name=t, role="worker")
                 for i, t in enumerate(threads)],
        events=events, quality=quality or {},
        sources=[dict(source_id="synthetic-report-test", kind="synthetic_fixture")],
        provenance=[dict(description="Synthetic test data; not a server capture")],
    ))


def cpu_fixture():
    return fixture("cpu-no-requests", [
        event("first-compute", 1, 11),
        event("sleep", 11, 31, kind="wait", category="wait"),
        event("second-compute", 31, 51),
    ])


def lesson_run(mode, overlap):
    events = []
    for batch in range(3):
        base = batch * 20
        frame_id = "frame-" + str(batch)
        request_id = "request-" + str(batch)
        events.extend([
            event(frame_id, base, base + 18, kind="frame", category="unknown",
                  frame_id=frame_id,
                  args=dict(application_frame_id="batch." + str(batch),
                            driver_calls_complete=False)),
            event(request_id, base + 1, base + 7, kind="request", category="cx",
                  parent_id=frame_id, frame_id=frame_id,
                  args=dict(transport="local_unix_socketpair", bytes=262144,
                            completion_tid=2, correlation_id="batch." + str(batch))),
            event("compute-" + str(batch), base + (2 if overlap else 8),
                  base + (7 if overlap else 13), parent_id=frame_id, frame_id=frame_id),
            event("receive-" + str(batch), base + 1, base + 7,
                  kind="exchange", category="cx", thread="receiver", frame_id=frame_id,
                  args=dict(includes_blocking_and_validation=True)),
            event("send-" + str(batch), base + 1, base + 6,
                  category="unknown", thread="sender", frame_id=frame_id),
        ])
    data = fixture(mode, events)
    business = dict(mode=mode, compute_checksum="synthetic-compute-checksum",
                    receive_checksum="synthetic-receive-checksum", bytes_received=786432,
                    iterations_per_batch=6000000, batches=3, elapsed_wall_ms=0.00006)
    return dict(session=data, analysis=analyze(data, 0, 60), business=business)


class ReportConsistencyTests(unittest.TestCase):
    def unpack_report(self, path):
        report = Path(path).read_bytes()
        matches = list(SESSION_SCRIPT.finditer(report))
        self.assertEqual(len(matches), 1, "A report must embed exactly one unchanged session")
        payload = json.loads(matches[0].group(1).decode("utf-8"))
        shell = SESSION_SCRIPT.sub(b"<!--SESSION-PAYLOAD-->", report)
        self.assertIsNone(re.search(br"<svg(?:\s|>)", report, re.IGNORECASE),
                          "A report must use the viewer rather than an extra sample chart")
        return shell, payload

    def test_cpu_serial_pipeline_and_loss_use_identical_viewer_without_payload_changes(self):
        cpu = cpu_fixture()
        serial = lesson_run("serial", overlap=False)["session"]
        pipeline = lesson_run("pipeline", overlap=True)["session"]
        loss = copy.deepcopy(pipeline)
        loss["session_id"] = loss["run_id"] = "synthetic-loss"
        loss["quality"]["dropped_records"] = 1
        self.assertIsNone(analyze(cpu, 0, 60)["request_overlap_ratio"])
        self.assertEqual(analyze(serial, 0, 60)["overlap_ns"], 0)
        self.assertGreater(analyze(pipeline, 0, 60)["overlap_ns"], 0)
        self.assertFalse(analyze(loss, 0, 60)["exact"])

        reports = dict(cpu=cpu, serial=serial, pipeline=pipeline, loss=loss)
        original = copy.deepcopy(reports)
        with temporary_directory() as temporary:
            shells = []
            for name, data in reports.items():
                with self.subTest(session=name):
                    path = Path(temporary) / (name + ".html")
                    write_report(data, path)
                    shell, payload = self.unpack_report(path)
                    self.assertEqual(payload, original[name])
                    shells.append(shell)
            self.assertTrue(all(shell == shells[0] for shell in shells[1:]))
        self.assertEqual(reports, original, "Report export must not alter capture data")

    def test_lesson_exports_same_viewer_and_index_is_pipeline_session(self):
        runs = dict(serial=lesson_run("serial", overlap=False),
                    pipeline=lesson_run("pipeline", overlap=True))
        original = copy.deepcopy(runs)
        with temporary_directory() as temporary:
            directory = Path(temporary)
            export_run_reports(runs, directory)
            shells = []
            for name, source_mode in (("serial", "serial"), ("pipeline", "pipeline"),
                                      ("index", "pipeline")):
                with self.subTest(report=name):
                    shell, payload = self.unpack_report(directory / (name + ".html"))
                    self.assertEqual(payload, original[source_mode]["session"])
                    shells.append(shell)
            self.assertTrue(all(shell == shells[0] for shell in shells[1:]))
        self.assertEqual(runs, original)

    def test_zero_pipeline_overlap_is_recorded_and_exported_as_observed(self):
        runs = dict(serial=lesson_run("serial", overlap=False),
                    pipeline=lesson_run("pipeline", overlap=False))
        original = copy.deepcopy(runs)
        checks = verify_pair(runs)
        self.assertIs(checks["same_work"], True)
        self.assertIs(checks["serial_overlap_zero"], True)
        self.assertIs(checks["pipeline_overlap_positive"], False)
        with temporary_directory() as temporary:
            directory = Path(temporary)
            export_run_reports(runs, directory)
            for name in ("pipeline", "index"):
                _, payload = self.unpack_report(directory / (name + ".html"))
                self.assertEqual(payload, original["pipeline"]["session"])
                self.assertEqual(analyze(payload, 0, 60)["overlap_ns"], 0)
        self.assertEqual(runs, original, "A zero-overlap result must not be rewritten")


if __name__ == "__main__":
    unittest.main()
