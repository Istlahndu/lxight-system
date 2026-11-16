#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "lxtrace.h"
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>
#include <pthread.h>
#include <sched.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

// Linux-only lesson: real local socket bytes, deliberately paced for readability.
// Affinity is a static thread setting, never a continuous CPU-residency trace.
namespace {
const size_t BYTES = 256 * 1024, CHUNK = 8 * 1024;
const int BATCHES = 3;
const unsigned ITERATIONS = 6000000;
std::mutex mutex_;
std::condition_variable changed;
int generation = -1;
int workers_ready = 0;
bool done = false, sender_done = false, stopping = false;
lx_handle request = 0;
uint64_t received_checksum = 0;
int sockets_[2];

void fail(const char *what) {
    std::fprintf(stderr, "%s: %s\n", what, std::strerror(errno));
    std::_Exit(3);
}
double cpu_ms() {
    timespec t;
    if (clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t)) fail("thread CPU clock");
    return t.tv_sec * 1000.0 + t.tv_nsec / 1000000.0;
}
void bind_and_name(const char *name, const char *role, int target) {
    int error = EINVAL, query_error = EINVAL, effective = -1;
    if (target >= 0) {
        cpu_set_t wanted;
        CPU_ZERO(&wanted); CPU_SET(target, &wanted);
        error = pthread_setaffinity_np(pthread_self(), sizeof wanted, &wanted);
        cpu_set_t actual;
        CPU_ZERO(&actual);
        query_error = pthread_getaffinity_np(pthread_self(), sizeof actual, &actual);
        if (!error && !query_error && CPU_COUNT(&actual) == 1 && CPU_ISSET(target, &actual)) effective = target;
    }
    char label[128], args[256];
    if (effective >= 0) std::snprintf(label, sizeof label, "%s · 绑CPU %d", name, effective);
    else std::snprintf(label, sizeof label, "%s · 未确认绑核", name);
    lx_thread_role(label, role);
    std::snprintf(args, sizeof args,
        "{\"requested_cpu\":%d,\"verified_single_cpu\":%d,\"setaffinity_error\":%d,\"getaffinity_error\":%d,\"scope\":\"static_thread_affinity_only\"}",
        target, effective, error, query_error);
    lx_instant("线程静态绑核设置", args);
}
unsigned char value_at(int batch, size_t offset) {
    return static_cast<unsigned char>((offset * 17 + batch * 29) % 251);
}
void args_for(char *out, size_t size, int batch, const char *extra = "") {
    std::snprintf(out, size,
        "{\"application_frame_id\":\"batch.%d\",\"batch\":%d,\"transport\":\"local_unix_socketpair\"%s}",
        batch, batch, extra);
}
int next_job(int previous, lx_handle &req) {
    std::unique_lock<std::mutex> lock(mutex_);
    lx_handle waiting = lx_wait_begin("等待下一批任务", "lesson job condition");
    changed.wait(lock, [previous] { return stopping || generation > previous; });
    lx_range_end(waiting);
    if (stopping) return -1;
    req = request;
    return generation;
}
void wait_complete() {
    lx_handle waiting = lx_wait_begin("等待本批通信完成", "validated socket receive");
    std::unique_lock<std::mutex> lock(mutex_);
    changed.wait(lock, [] { return done; });
    lx_range_end(waiting);
}
void announce_ready() {
    { std::lock_guard<std::mutex> lock(mutex_); ++workers_ready; }
    changed.notify_all();
}
}

int main(int argc, char **argv) {
    const bool pipeline = argc == 2 && !std::strcmp(argv[1], "pipeline");
    if (argc != 2 || (!pipeline && std::strcmp(argv[1], "serial"))) {
        std::fprintf(stderr, "usage: overlap_first_steps serial|pipeline\n");
        return 2;
    }
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    std::vector<int> cpus;
    if (!sched_getaffinity(0, sizeof allowed, &allowed))
        for (int c = 0; c < CPU_SETSIZE; ++c) if (CPU_ISSET(c, &allowed)) cpus.push_back(c);
    const auto target = [&cpus](size_t i) { return cpus.empty() ? -1 : cpus[i % cpus.size()]; };
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets_)) fail("socketpair");
    timeval timeout = {3, 0};
    for (int s : sockets_) {
        if (setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof timeout)
                || setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout)) fail("socket timeout");
    }
    std::vector<std::vector<unsigned char> > payloads(BATCHES, std::vector<unsigned char>(BYTES));
    for (int b = 0; b < BATCHES; ++b)
        for (size_t i = 0; i < BYTES; ++i) payloads[b][i] = value_at(b, i);
    lx_init(pipeline ? "流水线实验 · 计算与本地通信并行" : "串行对照 · 先通信再计算", "socket-overlap-lesson-v1");
    bind_and_name("计算线程", "worker", target(0));
    std::thread sender([&] {
        bind_and_name("发送线程", "sender", target(1));
        announce_ready();
        int previous = -1;
        for (;;) {
            lx_handle req;
            int batch = next_job(previous, req);
            if (batch < 0) break;
            previous = batch;
            char args[384], label[128];
            args_for(args, sizeof args, batch, ",\"teaching_pacing_ms_per_chunk\":1,\"scope\":\"software_stage_including_waits\"");
            std::snprintf(label, sizeof label, "第%d批 · 发送阶段（含节拍）", batch + 1);
            lx_handle stage = lx_range_begin(label, "unknown", 0, args);
            for (size_t offset = 0; offset < BYTES; offset += CHUNK) {
                // Only this wrapper measures send() wall time; it may also block.
                lx_handle api = lx_exchange_begin("send() · 8 KiB", "{\"api_wall_time\":true,\"hardware_busy\":false}");
                size_t sent = 0;
                while (sent < CHUNK) {
                    ssize_t n = send(sockets_[0], &payloads[batch][offset + sent], CHUNK - sent, MSG_NOSIGNAL);
                    if (n < 0 && errno == EINTR) continue;
                    if (n <= 0) fail("send");
                    sent += static_cast<size_t>(n);
                }
                lx_range_end(api);
                if (offset + CHUNK < BYTES) {
                    lxtrace::Wait wait("教学节拍 · sleep 1ms", "deliberate lesson pacing");
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                }
            }
            lx_range_end(stage);
            { std::lock_guard<std::mutex> lock(mutex_); sender_done = true; }
            changed.notify_all();
        }
    });
    std::thread receiver([&] {
        bind_and_name("接收线程", "receiver", target(2));
        std::vector<unsigned char> buffer(CHUNK);
        announce_ready();
        int previous = -1;
        for (;;) {
            lx_handle req;
            int batch = next_job(previous, req);
            if (batch < 0) break;
            previous = batch;
            char args[384], label[128];
            args_for(args, sizeof args, batch, ",\"api_wall_time\":true,\"includes_blocking_and_validation\":true,\"hardware_busy\":false");
            std::snprintf(label, sizeof label, "第%d批 · recv() + 校验（含阻塞）", batch + 1);
            lx_handle api = lx_exchange_begin(label, args);
            uint64_t sum = 0;
            for (size_t offset = 0; offset < BYTES;) {
                ssize_t n = recv(sockets_[1], buffer.data(), buffer.size(), 0);
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) fail("recv");
                for (ssize_t i = 0; i < n; ++i) {
                    if (offset + i >= BYTES || buffer[i] != value_at(batch, offset + i)) {
                        errno = EIO; fail("payload validation");
                    }
                    sum += buffer[i];
                }
                offset += static_cast<size_t>(n);
            }
            lx_range_end(api);
            {
                lxtrace::Wait wait("确认发送阶段已结束", "sender finished");
                std::unique_lock<std::mutex> lock(mutex_);
                changed.wait(lock, [] { return sender_done; });
            }
            // Complete only after all bytes were actually received and validated.
            lx_async_complete_observed(req);
            { std::lock_guard<std::mutex> lock(mutex_); received_checksum += sum; done = true; }
            changed.notify_all();
        }
    });
    {
        lxtrace::Wait wait("等待通信线程就绪", "both own workers ready");
        std::unique_lock<std::mutex> lock(mutex_);
        changed.wait(lock, [] { return workers_ready == 2; });
    }
    uint64_t compute_checksum = 17;
    const auto start = std::chrono::steady_clock::now();
    const double cpu_start = cpu_ms();
    for (int batch = 0; batch < BATCHES; ++batch) {
        char frame_name[64], label[128];
        std::snprintf(frame_name, sizeof frame_name, "batch.%d", batch);
        lx_handle frame = lx_frame_begin(frame_name, 0);
        std::snprintf(label, sizeof label, "第%d批 · 通信请求", batch + 1);
        lx_handle req = lx_async_begin_transport(label, frame_name, "local socket receiver", BYTES, "local_unix_socketpair");
        { std::lock_guard<std::mutex> lock(mutex_); request = req; generation = batch; done = false; sender_done = false; }
        changed.notify_all();
        if (!pipeline) wait_complete();
        std::snprintf(label, sizeof label, "第%d批 · 整数计算", batch + 1);
        lx_handle compute = lx_range_begin(label, "vec", frame, "{\"iterations\":6000000,\"operation\":\"fixed_integer_arithmetic\"}");
        for (unsigned i = 0; i < ITERATIONS; ++i) {
            compute_checksum = compute_checksum * 6364136223846793005ULL + 1442695040888963407ULL;
            compute_checksum ^= compute_checksum >> 17;
        }
        lx_range_end(compute);
        if (pipeline) wait_complete();
        lx_frame_end(frame);
    }
    const double used_cpu = cpu_ms() - cpu_start;
    const double elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
    { std::lock_guard<std::mutex> lock(mutex_); stopping = true; }
    changed.notify_all();
    { lxtrace::Wait wait("回收发送与接收线程", "own lesson threads"); sender.join(); receiver.join(); }
    close(sockets_[0]); close(sockets_[1]);
    lx_counter("independent_compute_thread_cpu_ms", used_cpu);
    lx_counter("independent_work_wall_ms", elapsed);
    std::printf("RESULT {\"mode\":\"%s\",\"elapsed_wall_ms\":%.3f,\"compute_thread_cpu_ms\":%.3f,\"compute_checksum\":\"%llu\",\"receive_checksum\":\"%llu\",\"bytes_received\":%llu,\"batches\":%d,\"iterations_per_batch\":%u,\"pacing_ms\":1}\n",
        pipeline ? "pipeline" : "serial", elapsed, used_cpu,
        static_cast<unsigned long long>(compute_checksum), static_cast<unsigned long long>(received_checksum),
        static_cast<unsigned long long>(BYTES * BATCHES), BATCHES, ITERATIONS);
    lx_shutdown();
    return 0;
}
