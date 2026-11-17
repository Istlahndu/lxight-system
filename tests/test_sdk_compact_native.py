"""Compile the optional producer and verify its durable on-disk contract."""
import json
import os
import shutil
import struct
import subprocess
import unittest
import zlib
from pathlib import Path

from lxtrace.workspace import temporary_directory, workspace_environment


PRODUCER = r'''
#include "lxtrace_compact.h"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>
#if defined(_WIN32)
#include <process.h>
#else
#include <unistd.h>
#include <sys/wait.h>
#endif
int main(int argc,char **argv) {
    if(argc<3)return 2;
    const char *mode=argv[1];
    const unsigned cap=(!std::strcmp(mode,"loss")||!std::strcmp(mode,"threadlimit"))?64:4096;
    if(!std::strcmp(mode,"env")){if(lx_compact_init_env(cap,5))return 3;}
    else if(!std::strcmp(mode,"disabled")){return lx_compact_init_env(cap,5)==1?0:3;}
    else if(lx_compact_init(argv[2],cap,5))return 3;
    const uint32_t site=lx_compact_site("矩阵块","mat","{\"tag\":1}");
    if(!site||lx_compact_metadata("{\"counter_codes\":[29185]}"))return 4;
    if(!std::strcmp(mode,"threadlimit")) {
        unsigned rejected=0;
        for(unsigned i=0;i<1025;++i){std::thread worker([&]{if(lx_compact_thread("short-lived")<0)++rejected;});worker.join();}
        if(rejected!=1)return 10;
    } else if(!std::strcmp(mode,"multi")) {
        std::vector<std::thread> workers;
        for(int t=0;t<4;++t)workers.push_back(std::thread([site,t]{
            if(lx_compact_thread("worker")<0)std::abort();
            for(int i=0;i<400;++i)if(lx_compact_event(site,3,(uint16_t)t,100000+t*10000+i))std::abort();
        }));
        for(size_t i=0;i<workers.size();++i)workers[i].join();
    } else {
        if(lx_compact_thread("main")<0)return 5;
        if(!std::strcmp(mode,"loss")) {
            unsigned lost=0;
            for(unsigned i=0;i<200000;++i)if(lx_compact_event(site,3,0,100+i))++lost;
            std::printf("%u\n",lost);
        } else if(!std::strcmp(mode,"pmu")) {
            uint64_t v[16],e[16],r[16];
            for(int i=0;i<16;++i){v[i]=9007199254740993ull+i;e[i]=700+i;r[i]=600+i;}
            for(unsigned i=0;i<200;++i){
                while(lx_compact_pmu_window(site,3,9,100+i,101+i,16,v,e,r))lx_compact_flush();
                if(i%7==0)lx_compact_flush();
            }
        } else if(!std::strcmp(mode,"json")) {
            const char *bad[]={"{oops}","[]","{\"n\":NaN}","{\"n\":1e999}","{\"u\":\"\\ud800\"}","{\"x\":1} trailing"};
            for(unsigned i=0;i<sizeof bad/sizeof bad[0];++i){
                const uint32_t id=lx_compact_site("invalid","mat",bad[i]);
                if(!id||lx_compact_event(id,1,0,100+i*10)||lx_compact_event(id,2,0,105+i*10))return 11;
            }
            if(lx_compact_metadata("{bad}")!=-1)return 12;
        } else if(!std::strcmp(mode,"incomplete")) {
            lx_compact_event(site,1,0,100);
        } else if(!std::strcmp(mode,"misnested")) {
            if(lx_compact_event(site,2,0,100)!=-1)return 13;
        } else if(!std::strcmp(mode,"bad")) {
            const char invalid[]={char(0xff),0};
            const uint32_t bad=lx_compact_site("bad","mat",invalid);
            lx_compact_event(bad,1,0,100);lx_compact_event(bad,2,0,200);
        } else if(!std::strcmp(mode,"fork")) {
#if !defined(_WIN32)
            lx_compact_event(site,3,0,100);lx_compact_flush();
            const pid_t child=fork();
            if(child==0){const bool rejected=lx_compact_event(site,3,0,200)==-1&&lx_compact_thread("child")==-1;lx_compact_shutdown();_exit(rejected?0:8);}
            int status=0;if(child<0||waitpid(child,&status,0)!=child||!WIFEXITED(status)||WEXITSTATUS(status))return 8;
#else
            return 77;
#endif
        } else {
            lx_compact_event(site,1,0,100);lx_compact_event(site,2,0,200);
            if(!std::strcmp(mode,"crash")) {
                // Only the background worker makes these records durable.
                std::this_thread::sleep_for(std::chrono::milliseconds(100));_exit(0);
            }
        }
    }
    lx_compact_shutdown();
    if(!std::strcmp(mode,"late")){if(lx_compact_event(site,3,0,300)!=-1)return 9;lx_compact_shutdown();}
    if(!std::strcmp(mode,"shutdown")){
        std::thread a([]{lx_compact_shutdown();}),b([]{lx_compact_shutdown();});a.join();b.join();
    }
    return 0;
}
'''


def chunks(path):
    raw = path.read_bytes()
    found, offset = [], 0
    while offset < len(raw):
        if len(raw) - offset < 40:
            raise AssertionError("truncated native chunk")
        magic, version, kind, seq, thread, count, size, crc = struct.unpack_from("<IHHQIIII", raw, offset)
        assert magic == 0x3143584C and version == 1
        assert seq == len(found) + 1
        payload = raw[offset + 32:offset + 32 + size]
        assert len(payload) == size
        assert zlib.crc32(raw[offset:offset + 28] + payload) == crc
        assert struct.unpack_from("<II", raw, offset + 32 + size) == (0x4543584C, crc)
        found.append(dict(kind=kind, seq=seq, thread=thread, count=count, payload=payload))
        offset += 40 + size
    return found


class NativeCompactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = os.environ.get("CXX") or shutil.which("g++")
        if not cls.compiler:
            raise unittest.SkipTest("C++11 compiler unavailable")
        cls.temp = temporary_directory()
        cls.root = Path(cls.temp.name)
        cls.addClassCleanup(cls.temp.cleanup)
        repo = Path(__file__).resolve().parents[1]
        source = cls.root / "producer.cpp"
        source.write_text(PRODUCER, encoding="utf-8")
        cls.exe = cls.root / ("producer.exe" if os.name == "nt" else "producer")
        subprocess.run([cls.compiler, "-std=c++11", "-O2", "-Wall", "-Wextra", "-pthread",
                        "-I", str(repo / "sdk"), str(source), str(repo / "sdk/lxtrace_compact.cpp"),
                        "-o", str(cls.exe)], check=True, capture_output=True, text=True,
                       env=workspace_environment(), timeout=90)

    def produce(self, mode):
        path = self.root / (mode + ".lxc")
        result = subprocess.run([str(self.exe), mode, path.name], check=True, cwd=str(self.root),
                                capture_output=True, text=True,
                                env=workspace_environment(), timeout=30)
        return chunks(path), result.stdout, path

    @staticmethod
    def json_rows(rows, kind):
        return [json.loads(row["payload"]) for row in rows if row["kind"] == kind]

    @staticmethod
    def events(rows):
        return [(row["thread"], struct.unpack_from("<QIHH", row["payload"], offset))
                for row in rows if row["kind"] == 4 for offset in range(0, len(row["payload"]), 16)]

    def test_plain_roundtrip_and_exclusive_output(self):
        rows, _, path = self.produce("plain")
        self.assertEqual(self.json_rows(rows, 1)[0]["record_bytes"], 16)
        self.assertEqual(self.json_rows(rows, 2)[0]["name"], "矩阵块")
        self.assertEqual(self.events(rows), [(1, (100, 1, 1, 0)), (1, (200, 1, 2, 0))])
        self.assertTrue(self.json_rows(rows, 6)[-1]["clean_shutdown"])
        before = path.read_bytes()
        self.assertEqual(subprocess.run([str(self.exe), "plain", path.name], cwd=str(self.root), env=workspace_environment()).returncode, 3)
        self.assertEqual(path.read_bytes(), before)

    def test_pmu_exact_integers_and_wrapped_groups(self):
        rows, _, _ = self.produce("pmu")
        snapshots = [row["payload"] for row in rows if row["kind"] == 7]
        self.assertEqual(len(snapshots), 200)
        for index, payload in enumerate(snapshots):
            self.assertEqual(struct.unpack_from("<QIHHIIQ", payload), (100 + index, 1, 3, 9, 16, 0, 101 + index))
            self.assertEqual([struct.unpack_from("<QQQ", payload, 32 + i * 24) for i in range(16)],
                             [(9007199254740993 + i, 700 + i, 600 + i) for i in range(16)])

    def test_overflow_accounts_every_attempt_and_bounds(self):
        rows, stdout, _ = self.produce("loss")
        failed = int(stdout.strip())
        loss = struct.unpack("<8Q", [row["payload"] for row in rows if row["kind"] == 5][-1])
        self.assertGreater(failed, 0)
        self.assertEqual(loss[0], failed)
        self.assertEqual(loss[5], 200000)
        self.assertEqual(loss[7] + failed, 200000)
        self.assertEqual((loss[3], loss[4]), (loss[1] + 99, loss[2] + 99))
        self.assertEqual(len(self.events(rows)), loss[7])
        self.assertFalse(self.json_rows(rows, 6)[-1]["clean_shutdown"])

    def test_threads_keep_independent_order(self):
        rows, _, _ = self.produce("multi")
        events = self.events(rows)
        self.assertEqual(len(events), 1600)
        for thread in range(1, 5):
            values = [record for tid, record in events if tid == thread]
            self.assertEqual(len(values), 400)
            aux = values[0][3]
            self.assertEqual([record[0] for record in values], list(range(100000 + aux * 10000, 100400 + aux * 10000)))

    def test_periodic_durable_prefix_without_shutdown(self):
        rows, _, _ = self.produce("crash")
        self.assertEqual(len(self.events(rows)), 2)
        self.assertFalse(self.json_rows(rows, 6))

    def test_late_calls_append_changed_quality(self):
        rows, _, _ = self.produce("late")
        footers = self.json_rows(rows, 6)
        self.assertEqual([row["late_records"] for row in footers], [0, 1])
        self.assertFalse(footers[-1]["clean_shutdown"])

    def test_invalid_utf8_cannot_corrupt_chunk_json(self):
        rows, _, _ = self.produce("bad")
        for row in rows:
            if row["kind"] in (1, 2, 3, 6, 8):
                json.loads(row["payload"])
        self.assertTrue(self.json_rows(rows, 2)[1]["invalid_text"])
        self.assertEqual(len(self.events(rows)), 2)
        self.assertGreater(self.json_rows(rows, 6)[-1]["protocol_errors"], 0)

    def test_invalid_json_repaired_before_footer(self):
        rows, _, _ = self.produce("json")
        sites = self.json_rows(rows, 2)[1:]
        self.assertEqual(len(sites), 6)
        self.assertTrue(all(json.loads(site['args_json']) == {'invalid_args': True} for site in sites))
        footer = self.json_rows(rows, 6)[-1]
        self.assertEqual(footer['protocol_errors'], 7)
        self.assertFalse(footer['clean_shutdown'])
        self.assertEqual(len(self.events(rows)), 12)

    def test_open_scopes_and_misnested_end_are_not_clean(self):
        for mode in ('incomplete', 'misnested'):
            rows, _, _ = self.produce(mode)
            footer = self.json_rows(rows, 6)[-1]
            self.assertFalse(footer['clean_shutdown'])
            self.assertGreater(footer['incomplete_events'] + footer['protocol_errors'], 0)

    def test_lifetime_thread_limit_is_explicit(self):
        rows, _, _ = self.produce('threadlimit')
        self.assertEqual(len(self.json_rows(rows, 3)), 1024)
        footer = self.json_rows(rows, 6)[-1]
        self.assertEqual(footer['max_threads'], 1024)
        self.assertFalse(footer['clean_shutdown'])
        self.assertEqual(footer['protocol_errors'], 1)

    def test_shutdown_is_idempotent_and_concurrent_safe(self):
        rows, _, _ = self.produce("shutdown")
        self.assertEqual(len(self.json_rows(rows, 6)), 1)

    def test_environment_template_and_disabled_capture(self):
        template = 'env-%p.lxc'
        result = subprocess.run([str(self.exe), 'env', 'unused'], cwd=str(self.root), check=True,
                                env=workspace_environment({'LXTRACE_COMPACT_OUTPUT': template, 'LXTRACE_DISABLED': '0'}))
        paths = list(self.root.glob('env-*.lxc'))
        self.assertEqual(len(paths), 1)
        rows = chunks(paths[0])
        self.assertEqual(paths[0].stem, 'env-' + str(self.json_rows(rows, 1)[0]['pid']))
        disabled_path = self.root / 'disabled.lxc'
        subprocess.run([str(self.exe), 'disabled', 'unused'], cwd=str(self.root), check=True,
                       env=workspace_environment({'LXTRACE_COMPACT_OUTPUT': str(disabled_path), 'LXTRACE_DISABLED': '1'}))
        self.assertFalse(disabled_path.exists())

    @unittest.skipIf(os.name == "nt", "fork is Linux only")
    def test_initialized_fork_child_is_disabled(self):
        rows, _, _ = self.produce("fork")
        self.assertEqual(len(self.events(rows)), 1)
        footer = self.json_rows(rows, 6)[-1]
        self.assertFalse(footer["clean_shutdown"])
        self.assertEqual(footer["unsupported_fork_count"], 1)


if __name__ == "__main__":
    unittest.main()
