"""Real Linux SDK fork isolation: fail closed after init, normal after exec."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from lxtrace.core import has_loss
from lxtrace.importers import load_sdk
from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]
SOURCE = r'''
#include "lxtrace.h"
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <sys/wait.h>
#include <unistd.h>

int wait_child(pid_t child) {
    int status=0;
    if(child<0 || waitpid(child,&status,0)!=child) return 80;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 81;
}
void own_capture(const char *name) {
    lx_init(name,"fork-regression");
    lx_thread_role("main","worker");
    lx_handle frame=lx_frame_begin(name,0);
    lx_handle range=lx_range_begin("own.compute","mat",0,"{}");
    lx_range_end(range);
    lx_frame_end(frame);
    lx_shutdown();
}
int inherited_child(lx_handle frame,lx_handle request,lx_context context) {
    // Exercise every SDK entry point; inherited handles and TLS must not be used.
    lx_init("child.must.not.capture","fork-regression");
    lx_thread_role("child.must.not.capture","worker");
    if(lx_frame_begin("child.frame",0)) return 10;
    if(lx_frame_begin_ex("child.frame.ex",0,"{\"layer\":1}")) return 11;
    if(lx_range_begin("child.range","mat",frame,"{}")) return 12;
    if(lx_wait_begin("child.wait","queue")) return 13;
    if(lx_exchange_begin("child.exchange","{}")) return 14;
    if(lx_async_begin("child.request","c","p",4)) return 15;
    if(lx_async_begin_transport("child.request2","c","p",4,"socket")) return 16;
    lx_context empty=lx_context_capture(), previous={77,88};
    if(empty.frame || empty.process_id) return 17;
    if(lx_context_attach(context,&previous)!=-1) return 18;
    if(previous.frame!=77 || previous.process_id!=88) return 19;
    lx_range_end(frame);
    lx_frame_end(frame);
    lx_async_complete_observed(request);
    lx_instant("child.instant","{}");
    lx_counter("child.counter",1.0);
    lx_shutdown();
    return 0;
}
int main(int argc,char **argv) {
    if(argc<2) return 90;
    if(!std::strcmp(argv[1],"exec.worker")) {
        own_capture("exec.child");
        return 0;
    }
    if(!std::strcmp(argv[1],"preinit")) {
        pid_t child=fork();
        if(!child) { own_capture("preinit.child"); _exit(0); }
        own_capture("preinit.parent");
        return wait_child(child);
    }
    lx_init("fork.parent","fork-regression");
    lx_thread_role("main","frame_driver");
    lx_handle before=lx_range_begin("parent.before_fork","mat",0,"{}");
    lx_range_end(before);
    lx_handle frame=lx_frame_begin("parent.frame",0);
    lx_context context=lx_context_capture();
    lx_handle request=lx_async_begin("parent.request","c","p",4);
    int result=0;
    if(!std::strcmp(argv[1],"threaded")) {
        std::atomic<bool> ready(false), done(false);
        std::thread worker([&] {
            lx_thread_role("concurrent.worker","worker");
            ready.store(true);
            for(int i=0; !done.load() && i<32768; ++i) {
                lx_handle h=lx_range_begin("worker.compute","vec",0,"{}");
                lx_range_end(h);
                std::this_thread::sleep_for(std::chrono::microseconds(20));
            }
        });
        while(!ready.load()) std::this_thread::yield();
        for(int i=0; i<12 && !result; ++i) {
            pid_t child=fork();
            if(!child) _exit(inherited_child(frame,request,context));
            result=wait_child(child);
        }
        done.store(true);
        worker.join();
    } else {
        pid_t child=fork();
        if(!child) {
            if(!std::strcmp(argv[1],"exec")) {
                execl(argv[0],argv[0],"exec.worker",static_cast<char*>(0));
                _exit(91);
            }
            // Also exercise the inherited atexit(lx_shutdown) hook.
            std::exit(inherited_child(frame,request,context));
        }
        result=wait_child(child);
    }
    lx_async_complete_observed(request);
    lx_frame_end(frame);
    lx_shutdown();
    return result;
}
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux fork integration")
class SdkForkAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_compiler = Path("/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++")
        compiler = str(server_compiler) if server_compiler.is_file() else shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("No C++ compiler for SDK fork acceptance")
        cls.temporary = temporary_directory()
        cls.directory = Path(cls.temporary.name)
        source = cls.directory / "sdk_fork.cpp"
        source.write_text(SOURCE, encoding="utf-8")
        executable = cls.directory / "sdk_fork"
        command = [compiler, "-std=c++11", "-O2", "-pthread", "-Isdk", "sdk/lxtrace.cpp",
                   str(source), "-o", str(executable)]
        if Path(compiler) == server_compiler:
            command.append("-Wl,-rpath,/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64")
        build = subprocess.run(command, cwd=ROOT, env=workspace_environment(),
                               text=True, capture_output=True, timeout=90)
        if build.returncode:
            cls.temporary.cleanup()
            raise AssertionError(build.stdout + build.stderr)
        cls.captures = {}
        try:
            for mode in ("initialized", "threaded", "exec", "preinit"):
                run = subprocess.run([str(executable), mode], cwd=ROOT,
                                     env=workspace_environment({
                                         "LXTRACE_OUTPUT": str(cls.directory / (mode + "-%p.jsonl")),
                                         "LXTRACE_DISABLED": "0", "LXTRACE_CAPACITY": "131072"}),
                                     text=True, capture_output=True, timeout=20)
                if run.returncode:
                    raise AssertionError("%s: exit %d\n%s%s" %
                                         (mode, run.returncode, run.stdout, run.stderr))
                cls.captures[mode] = [(path, load_sdk(path))
                                      for path in cls.directory.glob(mode + "-*.jsonl")]
        except BaseException:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def assert_pid_isolation(self, path, session):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        pid = rows[0]["pid"]
        self.assertTrue(session["events"])
        for row in rows:
            if "pid" in row:
                self.assertEqual(row["pid"], pid)
            if "thread_id" in row:
                self.assertTrue(row["thread_id"].startswith(str(pid) + ":"))
        return session["quality"]

    def test_initialized_fork_disables_child_and_marks_parent_partial(self):
        for mode in ("initialized", "threaded"):
            with self.subTest(mode=mode):
                captures = self.captures[mode]
                self.assertEqual(len(captures), 1, "fork child must not flush inherited capture")
                path, session = captures[0]
                quality = self.assert_pid_isolation(path, session)
                self.assertEqual(session["app"]["name"], "fork.parent")
                self.assertFalse(any("child." in event["name"] for event in session["events"]))
                self.assertEqual(quality["unsupported_fork_count"], 1)
                self.assertTrue(quality["unsupported_fork_count_is_lower_bound"])
                self.assertEqual(quality["fork_capture_policy"], "child_disabled_until_exec")
                self.assertEqual(quality["fork_tracking_status"], "available")
                self.assertEqual(quality["protocol_errors"], 1)
                self.assertEqual(quality["dropped_records"], 0)
                self.assertFalse(quality["clean_shutdown"])
                self.assertEqual(quality["source_continuity"], "unknown")
                self.assertTrue(has_loss(session))
                self.assertTrue(any("child instrumentation is disabled until exec" in warning
                                    for warning in quality["warnings"]))

    def test_exec_starts_independent_child_capture(self):
        captures = self.captures["exec"]
        self.assertEqual(len(captures), 2)
        by_app = {session["app"]["name"]: (path, session) for path, session in captures}
        self.assertEqual(set(by_app), {"fork.parent", "exec.child"})
        parent_quality = self.assert_pid_isolation(*by_app["fork.parent"])
        self.assertFalse(parent_quality["clean_shutdown"])
        child_path, child = by_app["exec.child"]
        quality = self.assert_pid_isolation(child_path, child)
        self.assertEqual(quality["unsupported_fork_count"], 0)
        self.assertEqual(quality["protocol_errors"], 0)
        self.assertTrue(quality["clean_shutdown"])
        self.assertEqual(quality["source_continuity"], "complete_recorded_pairs")
        self.assertFalse(any(event["name"] == "parent.before_fork" for event in child["events"]))

    def test_fork_before_sdk_first_use_captures_both_processes(self):
        captures = self.captures["preinit"]
        self.assertEqual(len(captures), 2)
        self.assertEqual({session["app"]["name"] for _, session in captures},
                         {"preinit.parent", "preinit.child"})
        for path, session in captures:
            quality = self.assert_pid_isolation(path, session)
            self.assertEqual(quality["unsupported_fork_count"], 0)
            self.assertEqual(quality["protocol_errors"], 0)
            self.assertTrue(quality["clean_shutdown"])
            self.assertEqual(quality["source_continuity"], "complete_recorded_pairs")


if __name__ == "__main__":
    unittest.main()
