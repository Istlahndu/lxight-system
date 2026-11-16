"""Real SDK interval preservation; these annotations do not prove device activity."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.importers import load_sdk
from lxtrace.resource import migrate_v1
from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]
SOURCE = r'''
#include "lxtrace.h"
#include <atomic>
#include <thread>
volatile unsigned long long checksum=1;
void compute(const char *name) {
    lx_handle h=lx_range_begin(name,"mat",0,"{}");
    for(int i=0; i<10000; ++i) checksum=checksum*6364136223846793005ULL+1;
    lx_range_end(h);
}
int main() {
    lx_init("SDK overlap regression","v2");
    lx_handle frame=lx_frame_begin("same.frame",0);
    lx_handle a=lx_async_begin("same.a","shared-correlation","peer",4);
    lx_handle b=lx_async_begin("same.b","shared-correlation","peer",4);
    compute("same.compute");
    // Requests cross; synchronous LIFO restrictions must not be applied.
    lx_async_complete_observed(a);
    lx_async_complete_observed(b);
    lx_frame_end(frame);

    frame=lx_frame_begin("cross.frame",0);
    lx_context context=lx_context_capture();
    lx_handle request=lx_async_begin("cross.request","c","peer",4);
    std::atomic<bool> ready(false), finish(false);
    std::thread progress([&] {
        lx_thread_role("progress","communication");
        lxtrace::ContextScope task(context);
        lx_handle exchange=lx_exchange_begin("cross.exchange","{}");
        ready.store(true);
        while(!finish.load()) std::this_thread::yield();
        lx_range_end(exchange);
        lx_async_complete_observed(request);
    });
    while(!ready.load()) std::this_thread::yield();
    compute("cross.compute");
    lx_frame_end(frame); // Context is membership; it must not clip async spans.
    finish.store(true);
    progress.join();

    frame=lx_frame_begin("serial.frame",0);
    request=lx_async_begin("serial.request","c","peer",4);
    lx_async_complete_observed(request);
    compute("serial.compute");
    lx_frame_end(frame);
    lx_shutdown();
}
'''


class SdkOverlapAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_compiler = Path("/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++")
        compiler = str(server_compiler) if server_compiler.is_file() else shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("No C++ compiler for SDK overlap acceptance")
        cls.temporary = temporary_directory()
        directory = Path(cls.temporary.name)
        source = directory / "sdk_overlap.cpp"
        source.write_text(SOURCE, encoding="utf-8")
        executable = directory / ("sdk_overlap.exe" if os.name == "nt" else "sdk_overlap")
        command = [compiler, "-std=c++11", "-O2", "-pthread", "-Isdk", "sdk/lxtrace.cpp",
                   str(source), "-o", str(executable)]
        if Path(compiler) == server_compiler:
            command.append("-Wl,-rpath,/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64")
        try:
            build = subprocess.run(command, cwd=ROOT, env=workspace_environment(),
                                   text=True, capture_output=True, timeout=90)
            if build.returncode:
                raise AssertionError(build.stdout + build.stderr)
            raw = directory / "capture.jsonl"
            run = subprocess.run([str(executable)], cwd=ROOT,
                                 env=workspace_environment({"LXTRACE_OUTPUT": str(raw),
                                                            "LXTRACE_DISABLED": "0"}),
                                 text=True, capture_output=True, timeout=15)
            if run.returncode:
                raise AssertionError(run.stdout + run.stderr)
            cls.session = load_sdk(raw)
            cls.events = {event["name"]: event for event in cls.session["events"]}
        except BaseException:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_crossing_async_requests_keep_independent_ids_and_overlap(self):
        a, b, compute = (self.events[name] for name in ("same.a", "same.b", "same.compute"))
        self.assertNotEqual(a["args"]["request_id"], b["args"]["request_id"])
        self.assertEqual(a["args"]["correlation_id"], b["args"]["correlation_id"])
        self.assertLessEqual(a["begin_ts"], b["begin_ts"])
        self.assertLessEqual(b["begin_ts"], compute["begin_ts"])
        self.assertLess(compute["begin_ts"], compute["end_ts"])
        self.assertLessEqual(compute["end_ts"], a["end_ts"])
        self.assertLessEqual(a["end_ts"], b["end_ts"])
        self.assertEqual(a["args"]["completion_tid"], a["tid"])

    def test_cross_thread_completion_and_frame_membership_do_not_clip(self):
        request, exchange, compute, frame = (self.events[name] for name in
                                             ("cross.request", "cross.exchange", "cross.compute", "cross.frame"))
        self.assertNotEqual(request["args"]["completion_tid"], request["tid"])
        self.assertEqual(request["frame_id"], frame["event_id"])
        self.assertEqual(exchange["frame_id"], frame["event_id"])
        for event in (request, exchange):
            self.assertLessEqual(event["begin_ts"], compute["begin_ts"])
            self.assertLessEqual(compute["end_ts"], frame["end_ts"])
            self.assertLessEqual(frame["end_ts"], event["end_ts"])

    def test_serial_control_and_v2_migration_preserve_all_boundaries(self):
        self.assertLessEqual(self.events["serial.request"]["end_ts"],
                             self.events["serial.compute"]["begin_ts"])
        for record in migrate_v1(self.session)["records"]:
            event = self.events[record["name"]]
            self.assertEqual((record["begin_ns"], record["end_ns"]),
                             (event["begin_ts"], event["end_ts"]))
        quality = self.session["quality"]
        self.assertEqual(quality["protocol_errors"], 0)
        self.assertEqual(quality["dropped_records"], 0)
        self.assertEqual(quality["incomplete_events"], 0)
        self.assertEqual(quality["source_continuity"], "complete_recorded_pairs")


if __name__ == "__main__":
    unittest.main()
