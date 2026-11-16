#include "lxtrace.h"
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <thread>
#include <time.h>

// This lesson runs on Linux with one application thread.
// Thread CPU time advances only while this thread uses a CPU.
// The profiler timeline, separately, records wall-clock annotation boundaries.
static double thread_cpu_ms() {
    timespec t;
    if (clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t) != 0) return -1;
    return t.tv_sec * 1000.0 + t.tv_nsec / 1000000.0;
}

static uint64_t checksum = 17;
static void calculate(double target_cpu_ms) {
    const double begin = thread_cpu_ms();
    do {
        for (unsigned i = 0; i < 10000; ++i) {
            checksum = checksum * 6364136223846793005ULL + 1442695040888963407ULL;
            checksum ^= checksum >> 17;
        }
    } while (thread_cpu_ms() - begin < target_cpu_ms);
}

int main() {
    if (thread_cpu_ms() < 0) {
        std::fprintf(stderr, "Thread CPU clock unavailable on this host.\n");
        return 2;
    }
    lx_init("CPU first steps: calculate / sleep / calculate", "cpu-first-steps-v1");
    lx_thread_role("my only worker", "worker");
    const auto wall_begin = std::chrono::steady_clock::now();
    const double cpu_begin = thread_cpu_ms();
    lx_handle frame = lx_frame_begin("one complete experiment", 0);

    std::printf("[1/3] Calculate: consume about 30 ms of this thread's CPU time.\n");
    lx_handle first = lx_range_begin("01_calculate_30ms", "vec", frame,
                                    "{\"operation\":\"integer arithmetic\",\"target_thread_cpu_ms\":30}");
    calculate(30);
    lx_range_end(first);

    std::printf("[2/3] Sleep: wait about 100 ms, doing no arithmetic.\n");
    lx_handle waiting = lx_wait_begin("02_sleep_100ms", "sleep timer");
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    lx_range_end(waiting);

    std::printf("[3/3] Calculate: consume about 60 ms of this thread's CPU time.\n");
    lx_handle last = lx_range_begin("03_calculate_60ms", "vec", frame,
                                   "{\"operation\":\"integer arithmetic\",\"target_thread_cpu_ms\":60}");
    calculate(60);
    lx_range_end(last);
    lx_frame_end(frame);
    const double used_cpu = thread_cpu_ms() - cpu_begin;
    const double elapsed_wall = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - wall_begin).count();
    lx_counter("independent_thread_cpu_ms", used_cpu);
    lx_counter("independent_elapsed_wall_ms", elapsed_wall);
    std::printf("RESULT elapsed_wall_ms=%.3f thread_cpu_ms=%.3f checksum=%llu\n",
                elapsed_wall, used_cpu, (unsigned long long)checksum);
    lx_shutdown();
    return 0;
}
