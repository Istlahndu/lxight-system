"""Real multi-process C ABI recording, automatic merge/report and failure paths."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from lxtrace.resource import load_session
from lxtrace.software import record_software
from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]
SOURCE = r'''
#include "lxtrace.h"
#include <chrono>
#include <cstring>
#include <fstream>
#include <thread>
int main(int argc, char **argv) {
    const char *name=argc>1?argv[1]:"single";
    bool release_timed_out=false;
    lx_init("software multiprocess acceptance", "software-record-v1");
    lx_thread_role(name,"frame_driver");
    {
        lxtrace::Frame frame("same-request",0);
        lxtrace::Range work(name,"mat");
        if(!std::strcmp(name,"slow") && argc>2) {
            const std::string release=std::string(argv[2])+".release";
            { std::ofstream ready(std::string(argv[2])+".ready"); ready << "waiting"; }
            const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(120);
            while(true) {
                std::ifstream released(release);
                if(released.good()) break;
                if(std::chrono::steady_clock::now()>=deadline) { release_timed_out=true; break; }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(40));
        }
    }
    lx_shutdown();
    if(argc>2) { std::ofstream marker(argv[2]); marker << (release_timed_out?"release_timeout":"finished"); }
    if(release_timed_out) return 9;
    return !std::strcmp(name,"failed")?7:0;
}
'''


class SoftwarePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.directory = Path(self.temporary.name)
        self.marker = self.directory / "business-started.txt"
        self.program = [sys.executable, "-c",
                        "from pathlib import Path; Path(%r).write_text('started')" % str(self.marker)]

    def tearDown(self):
        self.temporary.cleanup()

    def test_existing_session_and_html_rejected_before_business_launch(self):
        out = self.directory / "existing.lxtrace"
        out.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            record_software(out, self.program)
        out = self.directory / "report-only.lxtrace"
        out.with_suffix(".html").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            record_software(out, self.program)
        self.assertFalse(self.marker.exists())
        self.assertEqual(out.with_suffix(".html").read_text(encoding="utf-8"), "keep")
        self.assertFalse(list(self.directory.glob("*.capture")))

    def test_duration_rejected_without_launch_or_capture_side_effect(self):
        with self.assertRaisesRegex(ValueError, "does not support --duration"):
            record_software(self.directory / "duration.lxtrace", self.program, duration=1000000)
        self.assertFalse(self.marker.exists())
        self.assertFalse(list(self.directory.glob("*.capture")))

    def test_non_sdk_program_fails_explicitly_and_retains_manifest(self):
        out = self.directory / "no-sdk.lxtrace"
        with self.assertRaisesRegex(ValueError, "no usable per-process output"):
            record_software(out, self.program)
        manifests = list(self.directory.glob("*.capture/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "no_usable_sdk_output")
        self.assertEqual(manifest["launcher_exit_code"], 0)
        self.assertEqual(manifest["raw_files"], [])
        self.assertTrue(self.marker.is_file())
        self.assertFalse(out.exists())
        self.assertFalse(out.with_suffix(".html").exists())


class SoftwareSdkEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_compiler = Path("/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++")
        compiler = str(server_compiler) if server_compiler.is_file() else shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("No g++ compiler for real SDK process recording")
        cls.build_temporary = temporary_directory()
        build_directory = Path(cls.build_temporary.name)
        source = build_directory / "software_worker.cpp"
        source.write_text(SOURCE, encoding="utf-8")
        cls.executable = build_directory / ("software_worker.exe" if os.name == "nt" else "software_worker")
        command = [compiler, "-std=c++11", "-O2", "-pthread", "-Isdk", "sdk/lxtrace.cpp",
                   str(source), "-o", str(cls.executable)]
        if Path(compiler) == server_compiler:
            command.append("-Wl,-rpath,/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64")
        build = subprocess.run(command, cwd=ROOT, env=workspace_environment(),
                               capture_output=True, text=True, timeout=90)
        if build.returncode:
            cls.build_temporary.cleanup()
            raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build_temporary.cleanup()

    def setUp(self):
        self.temporary = temporary_directory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_pid_template_expansion_preserves_other_percent_text_and_identity(self):
        template = self.directory / "literal%q-%p-again%p.jsonl"
        process = subprocess.run([str(self.executable), "template"], cwd=ROOT,
                                 env=workspace_environment({"LXTRACE_OUTPUT": str(template),
                                                            "LXTRACE_RUN_ID": "run-template-proof",
                                                            "LXTRACE_DISABLED": "0"}),
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        paths = list(self.directory.glob("literal%q-*.jsonl"))
        self.assertEqual(len(paths), 1)
        meta = json.loads(paths[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(paths[0].name, "literal%%q-%s-again%s.jsonl" % (meta["pid"], meta["pid"]))
        self.assertEqual(meta["run_id"], "run-template-proof")
        if sys.platform.startswith("linux"):
            self.assertEqual(meta["host_id"], os.uname().nodename)
            self.assertEqual(meta["boot_id"], Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        elif os.name == "nt":
            self.assertIsNone(meta["host_id"])
            self.assertIsNone(meta["boot_id"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Cross-process clock proof is Linux monotonic host/boot identity")
    def test_two_worker_launch_produces_one_native_archive_html_and_all_raw(self):
        launcher = ("import subprocess,sys; "
                    "children=[subprocess.Popen([sys.argv[1],label]) for label in ('rank0','rank1')]; "
                    "codes=[child.wait() for child in children]; sys.exit(max(codes))")
        output = self.directory / "two-rank.lxtrace"
        session, code = record_software(output, [sys.executable, "-c", launcher, str(self.executable)])
        self.assertEqual(code, 0)
        restored = load_session(output)
        self.assertEqual(restored["schema_version"], "lxtrace.resource-session.v2")
        self.assertEqual(restored["data_origin"], "live")
        self.assertEqual(restored["run_id"], session["run_id"])
        self.assertTrue(output.with_suffix(".html").is_file())
        self.assertTrue(restored["quality"]["clean_shutdown"])
        self.assertFalse(restored["quality"]["capture_partial"])
        self.assertEqual(restored["capture"]["imported_process_count"], 2)
        self.assertTrue(restored["capture"]["process_group"]["all_observed_members_exited"])
        raw = list(self.directory.glob("*.capture/raw/*.jsonl"))
        self.assertEqual(len(raw), 2)
        metas = [json.loads(path.read_text(encoding="utf-8").splitlines()[0]) for path in raw]
        self.assertEqual(len({meta["pid"] for meta in metas}), 2)
        self.assertEqual({meta["run_id"] for meta in metas}, {session["run_id"]})
        self.assertEqual({meta["boot_id"] for meta in metas}, {Path("/proc/sys/kernel/random/boot_id").read_text().strip()})
        records = [record for record in restored["records"] if record.get("name") in ("rank0", "rank1")]
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["resource_id"] for record in records}), 2)
        self.assertEqual(len({record["record_id"] for record in records}), 2)
        self.assertTrue(all(record["layer"] == "software" for record in records))
        self.assertTrue(all(record.get("end_ns") is not None for record in records))
        self.assertTrue(any(name.endswith(".jsonl") for name in restored["raw_files"]))
        self.assertTrue(any(name.endswith("capture-manifest.json") for name in restored["raw_files"]))
        self.assertTrue(all(raw_ref.startswith("raw/") for record in records for raw_ref in record["raw_refs"]))
        html = output.with_suffix(".html").read_text(encoding="utf-8")
        self.assertIn("rank0", html)
        self.assertIn("rank1", html)

    def test_single_rank_nonzero_exit_still_writes_report_and_marks_nonclean(self):
        output = self.directory / "failed.lxtrace"
        session, code = record_software(output, [str(self.executable), "failed"])
        self.assertEqual(code, 7)
        self.assertFalse(session["quality"]["clean_shutdown"])
        self.assertTrue(session["quality"]["capture_partial"])
        self.assertEqual(session["capture"]["launcher_exit_code"], 7)
        self.assertEqual(session["capture"]["exit_code"], 7)
        self.assertTrue(output.with_suffix(".html").is_file())
        self.assertTrue(any(record.get("name") == "failed" for record in load_session(output)["records"]))

    def test_invalid_worker_raw_is_preserved_while_valid_worker_still_gets_report(self):
        launcher = ("import os,subprocess,sys; from pathlib import Path; "
                    "bad=Path(os.environ['LXTRACE_OUTPUT'].replace('%p',str(os.getpid()))); "
                    "bad.write_text('not valid SDK JSONL\\n',encoding='utf-8'); "
                    "sys.exit(subprocess.call([sys.argv[1],'valid-worker']))")
        output = self.directory / "invalid-worker.lxtrace"
        session, code = record_software(output, [sys.executable, "-c", launcher, str(self.executable)])
        self.assertEqual(code, 0)
        self.assertTrue(session["quality"]["capture_partial"])
        self.assertFalse(session["quality"]["clean_shutdown"])
        self.assertEqual(session["capture"]["imported_process_count"], 1)
        rejected = [item for item in session["capture"]["raw_files"] if item["status"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("missing supported", rejected[0]["error"])
        restored = load_session(output)
        self.assertTrue(any(record.get("name") == "valid-worker" for record in restored["records"]))
        self.assertTrue(any("not valid SDK JSONL" in (value["content"] if isinstance(value, dict) else value)
                            for value in restored["raw_files"].values()))
        self.assertTrue(output.with_suffix(".html").is_file())

    def test_racing_html_writer_is_not_overwritten(self):
        from lxtrace.resource import write_report
        output = self.directory / "html-race.lxtrace"
        html = output.with_suffix(".html")

        def competing_writer(session, path, **options):
            self.assertIs(options.get("overwrite"), False)
            Path(path).write_text("independent report must survive", encoding="utf-8")
            return write_report(session, path, **options)

        with mock.patch("lxtrace.resource.write_report", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                record_software(output, [str(self.executable), "race-worker"])
        self.assertEqual(html.read_text(encoding="utf-8"), "independent report must survive")
        manifests = list(self.directory.glob("*.capture/manifest.json"))
        self.assertEqual(json.loads(manifests[0].read_text(encoding="utf-8"))["status"], "report_failed")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group membership inspection")
    def test_unawaited_business_child_is_not_killed_and_snapshot_is_partial(self):
        marker = self.directory / "slow-child-finished.txt"
        release = Path(str(marker) + ".release")
        launcher = """import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.argv[1], 'slow', sys.argv[2]])
ready = Path(sys.argv[2] + '.ready')
deadline = time.monotonic() + 120
while not ready.exists() and time.monotonic() < deadline:
    if child.poll() is not None:
        sys.exit(10)
    time.sleep(0.05)
if not ready.exists():
    sys.exit(11)
sys.exit(subprocess.call([sys.argv[1], 'immediate']))
"""
        output = self.directory / "partial.lxtrace"
        try:
            session, code = record_software(output, [sys.executable, "-c", launcher,
                                                      str(self.executable), str(marker)])
            self.assertEqual(code, 0)
            self.assertFalse(marker.exists(), "Child must wait for the explicit release")
            self.assertTrue(session["quality"]["capture_partial"])
            self.assertFalse(session["quality"]["clean_shutdown"])
            self.assertTrue(session["capture"]["process_group"]["live_members_at_snapshot"])
        finally:
            # Release even when recording or an assertion fails. The business
            # child flushes its SDK normally, rather than being terminated.
            release.write_text("release", encoding="utf-8")
            deadline = time.monotonic() + 120
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.is_file(), "Business child must finish normally after release")
            self.assertEqual(marker.read_text(encoding="utf-8"), "finished")
        self.assertEqual(len(list(self.directory.glob("*.capture/raw/*.jsonl"))), 2)


if __name__ == "__main__":
    unittest.main()
