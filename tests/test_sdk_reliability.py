"""Compile the real SDK and verify JSON preservation and shutdown quality."""
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
#include <atomic>
#include <cstring>
#include <string>
#include <thread>
#include <vector>
bool late_destructor=false;
struct Late { ~Late() { if(late_destructor) lx_instant("destructor.after.sdk.atexit","{}"); } } late;
void pair(const char *name,const char *args="{}") {
    lx_handle h=lx_range_begin(name,"mat",0,args); lx_range_end(h);
}
int main(int argc,char **argv) {
    if(argc<2) return 80;
    lx_init("sdk.reliability","batch3");
    const std::string mode=argv[1];
    if(mode=="args") {
        const char *valid[]={"{}"," \n {\"v\":[true,false,null,-12.5e+2,{\"s\":\"a\\n\\t\\\\\\\"\"}]} \t",
            "{\"unicode\":\"\\u4e2d\\ud83d\\ude00\"}","{\"v\":1e308,\"tiny\":1e-9999}","{\"v\":0.00001}",
            "{\"name\":\"中文\",\"v\":1.7976931348623157e308}",
            "{\"v\":1.797693134862315807000e308}"};
        for(size_t i=0;i<sizeof valid/sizeof *valid;++i) pair(("valid."+std::to_string(i)).c_str(),valid[i]);
        std::vector<std::string> invalid={"{oops}","{\"v\":}","[]","null","", "{\"v\":01}",
            "{\"v\":NaN}","{\"v\":Infinity}","{\"v\":1e999}","{\"v\":1.}","{\"v\":+2}",
            "{\"v\":true,}","{\"v\":[1,]}","{\"v\":\"\\q\"}","{\"v\":\"\\ud800\"}",
            "{\"v\":\"\\udc00\"}","{\"v\":\"bad\nline\"}","{}\n{\"type\":\"end\"}",
            "{\"v\":\""+std::string(1,(char)0xff)+"\"}","{\"v\":\""+std::string(900,'a')+"\"}",
            "{\"v\":1.797693134862315808000e308}","{\"v\":-1.797693134862315808000e308}"};
        std::string nested="0"; for(int i=0;i<70;++i) nested="["+nested+"]";
        invalid.push_back("{\"v\":"+nested+"}");
        for(size_t i=0;i<invalid.size();++i) pair(("invalid."+std::to_string(i)).c_str(),invalid[i].c_str());
        lx_instant("invalid.instant","{oops}");
        lx_handle exchange=lx_exchange_begin("invalid.exchange","{oops}"); lx_range_end(exchange);
        lx_handle frame=lx_frame_begin_ex("invalid.frame",0,"{oops}"); lx_frame_end(frame);
        frame=lx_frame_begin_ex("valid.frame",0," \n {\"microbatch\":3,\"application_frame_id\":\"cannot.override\"} \t"); lx_frame_end(frame);
        return 0;
    }
    if(mode=="after") {
        pair("before"); lx_shutdown(); lx_shutdown();
        for(int i=0;i<37;++i) if(lx_range_begin("after","mat",0,"{}")!=0) return 81;
        lx_shutdown(); return 0;
    }
    if(mode=="open") {
        lx_frame_begin("open.frame",0); lx_range_begin("open.range","mat",0,"{}");
        lx_async_begin("open.request","c","p",1); lx_shutdown(); return 0;
    }
    if(mode=="worker") {
        std::atomic<bool> ready(false), done(false);
        std::thread worker([&] { lx_handle h=lx_range_begin("worker.open","mat",0,"{}"); ready=true;
            while(!done.load()) std::this_thread::yield(); lx_range_end(h); lx_instant("worker.after","{}"); });
        while(!ready.load()) std::this_thread::yield(); lx_shutdown(); done=true; worker.join(); return 0;
    }
    if(mode=="race") {
        std::atomic<int> ready(0); std::atomic<bool> done(false);
        std::vector<std::thread> workers;
        for(int n=0;n<4;++n) workers.emplace_back([&] { ++ready;
            for(int i=0;i<500 && !done.load();++i) pair("race.range");
            while(!done.load()) std::this_thread::yield();
            lx_instant("race.after","{}"); lx_counter("race.after.counter",1); });
        while(ready.load()!=4) std::this_thread::yield(); lx_shutdown(); done=true;
        for(auto &worker:workers) worker.join(); return 0;
    }
    if(mode=="atexit") { pair("before.atexit"); late_destructor=true; return 0; }
    if(mode=="capacity") { for(int i=0;i<20;++i) pair("capacity.range"); return 0; }
    pair("normal"); lx_shutdown(); lx_shutdown(); return 0;
}
'''


class SdkReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_compiler = Path("/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++")
        compiler = str(server_compiler) if server_compiler.is_file() else shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("No C++ compiler for real SDK reliability acceptance")
        cls.temporary = temporary_directory()
        cls.directory = Path(cls.temporary.name)
        source = cls.directory / "sdk_reliability.cpp"
        source.write_text(SOURCE, encoding="utf-8")
        executable = cls.directory / ("sdk_reliability.exe" if os.name == "nt" else "sdk_reliability")
        command = [compiler, "-std=c++11", "-O2", "-pthread", "-Isdk", "sdk/lxtrace.cpp", str(source), "-o", str(executable)]
        if Path(compiler) == server_compiler:
            command.append("-Wl,-rpath,/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64")
        try:
            built = subprocess.run(command, cwd=ROOT, env=workspace_environment(), capture_output=True, text=True, timeout=90)
            if built.returncode:
                raise AssertionError(built.stdout + built.stderr)
            cls.captures = {}
            for mode in ("normal", "args", "after", "open", "worker", "race", "atexit", "capacity"):
                path = cls.directory / (mode + ".jsonl")
                run = subprocess.run([str(executable), mode], cwd=ROOT,
                                     env=workspace_environment({"LXTRACE_OUTPUT": str(path), "LXTRACE_DISABLED": "0",
                                                                "LXTRACE_CAPACITY": "8" if mode == "capacity" else "32768"}),
                                     capture_output=True, text=True, timeout=20)
                if run.returncode:
                    raise AssertionError(mode + ": " + str(run.returncode) + run.stdout + run.stderr)
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                cls.captures[mode] = (rows, load_sdk(path))
        except BaseException:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def quality(self, mode):
        rows, session = self.captures[mode]
        snapshots = [row for row in rows if row["type"] == "quality"]
        self.assertTrue(snapshots)
        self.assertTrue(all(row["quality_semantics"] == "cumulative_snapshot" for row in snapshots))
        revisions = [row["quality_revision"] for row in snapshots]
        self.assertEqual(revisions, sorted(set(revisions)))
        for field in ("dropped_records", "protocol_errors", "late_records", "incomplete_events"):
            self.assertEqual([row[field] for row in snapshots], sorted(row[field] for row in snapshots))
        return snapshots, session["quality"]

    def test_valid_json_objects_survive_and_invalid_metadata_never_loses_ranges(self):
        _, session = self.captures["args"]
        by_name = {event["name"]: event for event in session["events"]}
        valid = [event for name, event in by_name.items() if name.startswith("valid.")]
        invalid = [event for name, event in by_name.items() if name.startswith("invalid.")]
        self.assertEqual(len(valid), 8)
        self.assertEqual(len(invalid), 26)
        self.assertTrue(all("invalid_args" not in event["args"] for event in valid))
        self.assertTrue(all(event["args"].get("invalid_args") is True for event in invalid))
        self.assertTrue(all(event["status"] == "complete" for event in valid + invalid))
        self.assertEqual(by_name["valid.frame"]["args"]["application_frame_id"], "valid.frame")
        self.assertEqual(by_name["valid.frame"]["args"]["microbatch"], 3)
        snapshots, quality = self.quality("args")
        self.assertEqual(snapshots[-1]["protocol_errors"], 26)
        self.assertEqual(quality["protocol_errors"], 26)
        self.assertEqual(quality["incomplete_events"], 0)
        self.assertFalse(quality["clean_shutdown"])

    def test_repeated_shutdown_does_not_duplicate_quality_or_cumulative_loss(self):
        snapshots, quality = self.quality("after")
        self.assertEqual(len(snapshots), 38)
        self.assertTrue(snapshots[0]["clean_shutdown"])
        self.assertEqual(snapshots[-1]["dropped_records"], 37)
        self.assertEqual(snapshots[-1]["late_records"], 37)
        self.assertEqual(quality["dropped_records"], 37)
        self.assertEqual(quality["late_records"], 37)
        self.assertFalse(quality["clean_shutdown"])
        self.assertEqual({event["name"] for event in self.captures["after"][1]["events"]}, {"before"})

    def test_unclosed_sync_and_async_handles_are_not_clean(self):
        snapshots, quality = self.quality("open")
        self.assertEqual(snapshots[-1]["incomplete_events"], 3)
        self.assertEqual(quality["incomplete_events"], 3)
        self.assertFalse(quality["clean_shutdown"])
        self.assertTrue(all(event["end_ts"] is None for event in self.captures["open"][1]["events"]))

    def test_live_worker_after_shutdown_remains_open_and_reports_late_loss(self):
        snapshots, quality = self.quality("worker")
        self.assertEqual(snapshots[0]["incomplete_events"], 1)
        self.assertFalse(snapshots[0]["clean_shutdown"])
        self.assertEqual(snapshots[-1]["late_records"], 2)
        self.assertEqual(quality["dropped_records"], 2)
        self.assertEqual(quality["incomplete_events"], 1)

    def test_shutdown_race_preserves_valid_lines_and_reports_rejected_records(self):
        snapshots, quality = self.quality("race")
        self.assertGreaterEqual(snapshots[-1]["late_records"], 8)
        self.assertEqual(quality["dropped_records"], snapshots[-1]["dropped_records"])
        self.assertEqual(quality["incomplete_events"], snapshots[-1]["incomplete_events"])
        self.assertFalse(quality["clean_shutdown"])

    def test_global_destructor_after_sdk_atexit_appends_updated_quality(self):
        snapshots, quality = self.quality("atexit")
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(snapshots[0]["clean_shutdown"])
        self.assertEqual(snapshots[-1]["late_records"], 1)
        self.assertFalse(quality["clean_shutdown"])
        self.assertEqual(quality["dropped_records"], 1)

    def test_normal_and_capacity_limited_shutdown_quality_matches_capture(self):
        snapshots, quality = self.quality("normal")
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(quality["clean_shutdown"])
        snapshots, quality = self.quality("capacity")
        self.assertGreater(snapshots[-1]["dropped_records"], 0)
        self.assertFalse(quality["clean_shutdown"])
        self.assertEqual(quality["incomplete_events"], snapshots[-1]["incomplete_events"])


if __name__ == "__main__":
    unittest.main()
