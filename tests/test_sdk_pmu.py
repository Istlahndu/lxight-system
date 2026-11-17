"""Optional PMU API: portable fallback, exact kernel contract, real denial path."""
import json
import os
import platform
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.workspace import temporary_directory, workspace_environment


ROOT = Path(__file__).resolve().parents[1]
PORTABLE = r'''
#include "lxtrace_pmu.h"
#include <cassert>
#include <cstring>
int main() {
    lx_pmu *h = reinterpret_cast<lx_pmu*>(1);
    lx_pmu_error e;
    lx_pmu_snapshot s;
    lx_pmu_event event = {LX_PMU_TYPE_HARDWARE, 0, 0, 0};
    lx_pmu_config c = {&event, 1, 1};
    assert(lx_pmu_open(&c, &h, &e) == LX_PMU_DISABLED && h == 0);
    assert(e.status == 0 && e.system_errno == 0 && e.event_index == LX_PMU_NO_EVENT);
    std::memset(&s, 0xff, sizeof(s));
    assert(lx_pmu_read(0, &s, &e) == LX_PMU_DISABLED && s.event_count == 0 && s.pid == 0);
    c.disabled = 0;
    assert(lx_pmu_open(&c, &h, &e) == LX_PMU_UNSUPPORTED && h == 0);
    assert(e.status == LX_PMU_UNSUPPORTED && e.system_errno != 0);
    assert(lx_pmu_open(0, &h, &e) == LX_PMU_INVALID && h == 0);
    assert(lx_pmu_open(&c, 0, &e) == LX_PMU_INVALID);
    c.event_count = LX_PMU_MAX_EVENTS + 1;
    assert(lx_pmu_open(&c, &h, &e) == LX_PMU_INVALID && h == 0);
    c.event_count = 0;
    assert(lx_pmu_open(&c, &h, &e) == LX_PMU_INVALID && h == 0);
    lx_pmu_event bad[]={{LX_PMU_TYPE_RAW,0x7201,0,0}, {LX_PMU_TYPE_RAW,0x10000,0,0}};
    c.events=bad; c.event_count=1;
    assert(lx_pmu_open(&c,&h,&e)==LX_PMU_INVALID && e.event_index==0);
    bad[0].type=LX_PMU_TYPE_HARDWARE;bad[0].config=0;c.event_count=2;
    assert(lx_pmu_open(&c,&h,&e)==LX_PMU_INVALID && e.event_index==1);
    bad[1].config=0x7201;bad[1].config1=1;
    assert(lx_pmu_open(&c,&h,&e)==LX_PMU_INVALID && e.event_index==1);
    lx_pmu_event duplicate[]={{LX_PMU_TYPE_HARDWARE,0,0,0},{LX_PMU_TYPE_RAW,0x7201,0,0},{LX_PMU_TYPE_RAW,0x7201,0,0}};
    c.events=duplicate;c.event_count=3;
    assert(lx_pmu_open(&c,&h,&e)==LX_PMU_INVALID && e.event_index==2);
    assert(lx_pmu_read(0, 0, &e) == LX_PMU_INVALID);
    lx_pmu_close(0);
    return 0;
}
'''

FAKE = r'''
#include "lxtrace_pmu.h"
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <cassert>
#include <cerrno>
#include <cstdarg>
#include <cstdint>
#include <cstring>
#include <string>
static int opens=0, closes=0, resets=0, enables=0, reads=0;
static int deny_at=-1, bad_ioctl_at=-1, bad_enable_at=-1, short_at=-1;
static bool never_runs=false, interrupt_once=false, invalid_times=false;
static uint64_t owner_pid=101, owner_tid=202, tick=100;
extern "C" pid_t __wrap_getpid() { return (pid_t)owner_pid; }
extern "C" long __wrap_syscall(long call, ...) {
    if(call==SYS_gettid) return (long)owner_tid;
    assert(call==SYS_perf_event_open);
    va_list ap; va_start(ap,call);
    perf_event_attr *a=va_arg(ap,perf_event_attr*);
    int pid=va_arg(ap,int), cpu=va_arg(ap,int), group=va_arg(ap,int);
    unsigned long flags=va_arg(ap,unsigned long); va_end(ap);
    assert(pid==0 && cpu==-1 && group==(opens?40:-1) && flags==PERF_FLAG_FD_CLOEXEC);
    assert(a->disabled==(opens==0) && a->exclude_kernel && a->exclude_hv && !a->inherit);
    assert(!a->enable_on_exec && !a->pinned);
    assert(a->read_format==(PERF_FORMAT_GROUP|PERF_FORMAT_TOTAL_TIME_ENABLED|PERF_FORMAT_TOTAL_TIME_RUNNING));
    assert(a->type==(opens?LX_PMU_TYPE_RAW:LX_PMU_TYPE_HARDWARE) && a->config==(opens?0x7201ULL:0));
    assert(a->config1==0 && a->config2==0);
    int index=opens++;
    if(index==deny_at) { errno=EPERM; return -1; }
    return 40+index;
}
extern "C" int __wrap_close(int fd) { assert(fd>=40); ++closes; return 0; }
extern "C" int __wrap_ioctl(int fd, unsigned long request, ...) {
    assert(fd==40);
    va_list ap; va_start(ap,request); unsigned long flags=va_arg(ap,unsigned long); va_end(ap);
    assert(flags==PERF_IOC_FLAG_GROUP);
    if(request==PERF_EVENT_IOC_RESET) ++resets;
    else { assert(request==PERF_EVENT_IOC_ENABLE); ++enables; }
    if(fd-40==bad_ioctl_at) { errno=EIO; return -1; }
    if(request==PERF_EVENT_IOC_ENABLE && fd-40==bad_enable_at) { errno=EIO; return -1; }
    return 0;
}
extern "C" int __wrap_clock_gettime(clockid_t clock, timespec *out) {
    assert(clock==CLOCK_MONOTONIC); out->tv_sec=3; out->tv_nsec=(long)tick++; return 0;
}
extern "C" ssize_t __wrap_read(int fd, void *buffer, size_t length) {
    assert(fd==40 && length==40); ++reads;
    if(interrupt_once) { interrupt_once=false; errno=EINTR; return -1; }
    if(fd-40==short_at) return 8;
    uint64_t raw[5]={2,1000,never_runs?0ULL:invalid_times?1001ULL:400ULL,9007199254740993ULL,9007199254740994ULL};
    std::memcpy(buffer,raw,sizeof(raw)); return sizeof(raw);
}
int main(int argc, char **argv) {
    assert(argc==2); const std::string mode=argv[1];
    lx_pmu_event events[2]={{LX_PMU_TYPE_HARDWARE,0,0,0},{LX_PMU_TYPE_RAW,0x7201,0,0}};
    lx_pmu_config config={events,2,0}; lx_pmu *h=0; lx_pmu_error error; lx_pmu_snapshot s;
    if(mode=="denied") deny_at=1;
    if(mode=="ioctl") bad_ioctl_at=0;
    if(mode=="enable") bad_enable_at=0;
    int opened=lx_pmu_open(&config,&h,&error);
    if(mode=="denied" || mode=="ioctl" || mode=="enable") {
        assert(opened==(mode=="denied"?LX_PMU_DENIED:LX_PMU_IO) && h==0);
        assert(error.event_index==(mode=="denied"?1:0) && error.system_errno==(mode=="denied"?EPERM:EIO));
        assert(closes==(mode=="denied"?1:2)); return 0;
    }
    assert(opened==LX_PMU_OK && h && opens==2 && resets==1 && enables==1);
    events[0].config=0; // The caller's config is not retained.
    if(mode=="short") short_at=0;
    if(mode=="thread") ++owner_tid;
    if(mode=="fork") ++owner_pid;
    if(mode=="not-running") never_runs=true;
    if(mode=="invalid-times") invalid_times=true;
    if(mode=="eintr") interrupt_once=true;
    std::memset(&s,0xff,sizeof(s));
    int result=lx_pmu_read(h,&s,&error);
    if(mode=="short" || mode=="thread" || mode=="fork" || mode=="invalid-times") {
        assert(result==((mode=="short" || mode=="invalid-times")?LX_PMU_IO:mode=="thread"?LX_PMU_WRONG_THREAD:LX_PMU_FORKED));
        assert(s.event_count==0 && s.pid==0 && s.readings[0].value==0);
        if(mode!="short" && mode!="invalid-times") assert(reads==0);
    } else {
        assert(result==(never_runs?LX_PMU_NOT_RUNNING:LX_PMU_OK));
        assert(s.event_count==2 && s.pid==101 && s.tid==202);
        assert(s.read_begin_ns==3000000100ULL && s.read_end_ns==3000000101ULL);
        assert(s.readings[0].value==9007199254740993ULL && s.readings[1].value==9007199254740994ULL);
        assert(s.readings[0].event.config==0 && s.readings[1].event.config==0x7201);
        assert(s.readings[0].time_enabled==1000 && s.readings[0].time_running==(never_runs?0:400));
        assert(s.readings[0].status==(never_runs?LX_PMU_NOT_RUNNING:LX_PMU_OK));
        assert(error.status==0 && error.system_errno==0);
        assert(reads==(mode=="eintr"?2:1)); // One group read; EINTR retries only.
    }
    lx_pmu_close(h); assert(closes==2);
    // No disable ioctls, including when releasing a fork-inherited handle.
    assert(resets==1 && enables==1); return 0;
}
'''

PROBE = r'''
#include "lxtrace_pmu.h"
#include <cstdio>
int main() {
    lx_pmu_event event={LX_PMU_TYPE_HARDWARE,0,0,0}; // PERF_COUNT_HW_CPU_CYCLES
    lx_pmu_config config={&event,1,0}; lx_pmu *h=0; lx_pmu_error e; lx_pmu_snapshot s;
    int status=lx_pmu_open(&config,&h,&e);
    if(status==LX_PMU_OK) {
        volatile unsigned long long sum=0; for(unsigned long long i=0;i<1000000;++i) sum+=i;
        status=lx_pmu_read(h,&s,&e);
        if(status<0 || s.event_count!=1 || s.readings[0].time_running>s.readings[0].time_enabled) return 91;
        std::printf("{\"status\":%d,\"errno\":0,\"count\":%u,\"value\":%llu,\"running\":%llu}\n",
            status,s.event_count,(unsigned long long)s.readings[0].value,(unsigned long long)s.readings[0].time_running);
    } else {
        if(h || (status!=LX_PMU_DENIED && status!=LX_PMU_UNSUPPORTED && status!=LX_PMU_IO)) return 92;
        std::printf("{\"status\":%d,\"errno\":%d,\"count\":0}\n",status,e.system_errno);
    }
    lx_pmu_close(h); return 0;
}
'''


class SdkPmuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = Path("/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++")
        cls.compiler = str(server) if server.is_file() else shutil.which("g++")
        if not cls.compiler:
            raise unittest.SkipTest("No C++ compiler for optional PMU reader acceptance")
        cls.temp = temporary_directory()
        cls.folder = Path(cls.temp.name)
        cls.env = workspace_environment()
        cls.linux = platform.system() == "Linux"
        cls.portable = cls.build("portable", PORTABLE, ["-DLXTRACE_PMU_FORCE_UNSUPPORTED"])
        if cls.linux:
            cls.fake = cls.build("fake", FAKE, ["-Wl,--wrap=" + name for name in
                                               ("syscall", "getpid", "read", "close", "ioctl", "clock_gettime")])
            cls.probe = cls.build("probe", PROBE)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def build(cls, name, source, extra=()):
        path = cls.folder / (name + ".cpp")
        path.write_text(source, encoding="utf-8")
        output = cls.folder / (name + (".exe" if os.name == "nt" else ""))
        command = [cls.compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O2", "-Isdk",
                   "sdk/lxtrace_pmu.cpp", str(path), "-o", str(output)] + list(extra)
        if "HPCKit" in cls.compiler:
            command.append("-Wl,-rpath,/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64")
        result = subprocess.run(command, cwd=ROOT, env=cls.env, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise AssertionError("PMU compile failed: " + result.stdout + result.stderr)
        return output

    def run_fake(self, mode):
        if not self.linux:
            self.skipTest("Linux syscall contract")
        run = subprocess.run([str(self.fake), mode], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_disabled_invalid_and_unsupported_clear_outputs(self):
        run = subprocess.run([str(self.portable)], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_header_can_be_included_by_c_callers(self):
        path = self.folder / "c_caller.c"
        path.write_text('#include "lxtrace_pmu.h"\nint main(void) { lx_pmu_snapshot s = {0}; return (int)s.event_count; }\n', encoding="utf-8")
        run = subprocess.run([self.compiler, "-x", "c", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Isdk",
                              "-fsyntax-only", str(path)], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_raw_integer_and_multiplex_times_are_unscaled(self):
        self.run_fake("exact")

    def test_denial_rolls_back_all_open_fds(self):
        self.run_fake("denied")

    def test_enable_reset_failure_rolls_back_all_fds(self):
        self.run_fake("ioctl")
        self.run_fake("enable")

    def test_partial_read_never_returns_partial_snapshot(self):
        self.run_fake("short")

    def test_never_running_counter_is_explicit(self):
        self.run_fake("not-running")

    def test_impossible_multiplex_times_do_not_return_counts(self):
        self.run_fake("invalid-times")

    def test_interrupted_read_retries(self):
        self.run_fake("eintr")

    def test_another_thread_must_not_read_owner_handle(self):
        self.run_fake("thread")

    def test_fork_inherited_handle_is_rejected_without_disabling_parent(self):
        self.run_fake("fork")

    def test_real_kernel_read_or_denial_has_honest_status(self):
        if not self.linux:
            self.skipTest("Linux kernel probe")
        run = subprocess.run([str(self.probe)], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(run.stdout)
        self.assertIn(result["status"], (0, 2, -2, -3, -4))
        self.assertEqual(result["count"], 1 if result["status"] in (0, 2) else 0)
        if result["status"] < 0:
            self.assertGreater(result["errno"], 0)


if __name__ == "__main__":
    unittest.main()
