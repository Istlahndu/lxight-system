"""Real SDK frame metadata and explicit worker context propagation."""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.importers import load_sdk
from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]
SOURCE = r'''
#include "lxtrace.h"
#include <cstring>
#include <thread>
int main(int argc,char **argv) {
    lx_init("frame adapter acceptance", "sdk-frame-v2");
    lx_thread_role("driver", "frame_driver");
    lx_handle root=lx_frame_begin("inference",0);
    lx_context context=lx_context_capture();
    {
        lxtrace::Frame layer("mb0.layer1",0,"{\"microbatch\":0,\"layer\":1,\"operator_id\":\"gate_up\",\"application_frame_id\":\"forbidden-override\",\"driver_calls_complete\":true}");
        lxtrace::Range mat("layer1.matmul","mat");
    }
    { lxtrace::Range after("after.nested.frame","vec"); }
    if(argc>1&&!std::strcmp(argv[1],"invalid")) {
        lx_handle range=lx_range_begin("not.a.frame","vec",0,"{}");
        lx_context invalid={range,context.process_id};
        if(lx_context_attach(invalid,0)!=-1) return 3;
        invalid=context;invalid.process_id++;
        if(lx_context_attach(invalid,0)!=-1) return 4;
        lx_range_end(range);
    }
    std::thread worker([context] {
        lx_thread_role("executor", "worker");
        {
            lxtrace::ContextScope task(context);
            if(!task.active()) return;
            lxtrace::Range work("worker.explicit.context","vec");
        }
        { lxtrace::Range outside("worker.restored.context","vec"); }
        {
            lxtrace::Frame layer("mb0.layer2",0,"{\"parent_application_frame_id\":\"inference\",\"microbatch\":0,\"layer\":2}");
            lxtrace::Wait wait("layer2.wait","activation queue");
        }
    });
    worker.join();
    lx_frame_end(root);
    lx_shutdown();
    return 0;
}
'''


class SdkFrameAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_compiler = Path("/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++")
        compiler = str(server_compiler) if server_compiler.is_file() else shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("No C++ compiler for real SDK frame acceptance")
        cls.temporary = temporary_directory()
        cls.directory = Path(cls.temporary.name)
        source = cls.directory / "frame_adapter.cpp"
        source.write_text(SOURCE, encoding="utf-8")
        cls.executable = cls.directory / ("frame_adapter.exe" if os.name == "nt" else "frame_adapter")
        command = [compiler, "-std=c++11", "-O2", "-pthread", "-Isdk", "sdk/lxtrace.cpp",
                   str(source), "-o", str(cls.executable)]
        if Path(compiler) == server_compiler:
            command.append("-Wl,-rpath,/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64")
        build = subprocess.run(command, cwd=ROOT, env=workspace_environment(),
                               text=True, capture_output=True, timeout=90)
        if build.returncode:
            cls.temporary.cleanup()
            raise AssertionError(build.stdout + build.stderr)
        cls.sessions = {}
        for mode in ("normal", "invalid"):
            raw = cls.directory / (mode + ".jsonl")
            run = subprocess.run([str(cls.executable), mode], cwd=ROOT,
                                 env=workspace_environment({"LXTRACE_OUTPUT": str(raw), "LXTRACE_DISABLED": "0"}),
                                 text=True, capture_output=True, timeout=15)
            if run.returncode:
                cls.temporary.cleanup()
                raise AssertionError(run.stdout + run.stderr)
            cls.sessions[mode] = load_sdk(raw)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_metadata_and_nested_frame_restore(self):
        data = self.sessions["normal"]
        by_name = {e["name"]: e for e in data["events"]}
        root, layer = by_name["inference"], by_name["mb0.layer1"]
        self.assertEqual(layer["args"]["application_frame_id"], "mb0.layer1")
        self.assertFalse(layer["args"]["driver_calls_complete"])
        self.assertEqual(layer["args"]["operator_id"], "gate_up")
        self.assertEqual(layer["args"]["microbatch"], 0)
        self.assertEqual(by_name["layer1.matmul"]["frame_id"], layer["event_id"])
        self.assertEqual(by_name["after.nested.frame"]["frame_id"], root["event_id"])
        self.assertEqual(data["quality"]["protocol_errors"], 0)
        self.assertEqual(data["quality"]["incomplete_events"], 0)

    def test_explicit_worker_context_and_cross_thread_parent(self):
        by_name = {e["name"]: e for e in self.sessions["normal"]["events"]}
        root = by_name["inference"]
        self.assertEqual(by_name["worker.explicit.context"]["frame_id"], root["event_id"])
        self.assertIsNone(by_name["worker.restored.context"]["frame_id"])
        self.assertEqual(by_name["mb0.layer2"]["parent_id"], root["event_id"])
        self.assertEqual(by_name["layer2.wait"]["frame_id"], by_name["mb0.layer2"]["event_id"])

    def test_foreign_context_and_non_frame_handle_are_rejected(self):
        self.assertEqual(self.sessions["invalid"]["quality"]["protocol_errors"], 2)
        self.assertEqual(self.sessions["invalid"]["quality"]["incomplete_events"], 0)


if __name__ == "__main__":
    unittest.main()
