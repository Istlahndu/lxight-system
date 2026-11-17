#include "lxtrace_pmu.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <new>

#if defined(__linux__) && !defined(LXTRACE_PMU_FORCE_UNSUPPORTED)
#define LXTRACE_PMU_LINUX 1
#include <linux/perf_event.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#endif

struct lx_pmu {
    uint64_t pid;
    uint64_t tid;
    uint32_t event_count;
    lx_pmu_event events[LX_PMU_MAX_EVENTS];
    int fds[LX_PMU_MAX_EVENTS];
};

namespace {
void clear_error(lx_pmu_error *error) {
    if (error) {
        std::memset(error, 0, sizeof(*error));
        error->event_index = LX_PMU_NO_EVENT;
    }
}

int fail(lx_pmu_error *error, int status, int number, uint32_t index,
         const char *operation, const char *detail) {
    if (error) {
        clear_error(error);
        error->status = status;
        error->system_errno = number;
        error->event_index = index;
        std::snprintf(error->operation, sizeof(error->operation), "%s", operation);
        std::snprintf(error->message, sizeof(error->message), "%s%s%s", detail,
                      number ? ": " : "", number ? std::strerror(number) : "");
    }
    return status;
}

#if defined(LXTRACE_PMU_LINUX)
int os_status(int number) {
    if (number == EACCES || number == EPERM) return LX_PMU_DENIED;
    if (number == ENOSYS || number == ENOENT || number == ENODEV || number == EOPNOTSUPP)
        return LX_PMU_UNSUPPORTED;
    return LX_PMU_IO;
}

int monotonic_ns(uint64_t *value) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
    *value = static_cast<uint64_t>(now.tv_sec) * 1000000000ULL + static_cast<uint64_t>(now.tv_nsec);
    return 0;
}
#endif
}

extern "C" int lx_pmu_open(const lx_pmu_config *config, lx_pmu **out, lx_pmu_error *error) {
    clear_error(error);
    if (out) *out = 0;
    if (!out || !config)
        return fail(error, LX_PMU_INVALID, EINVAL, LX_PMU_NO_EVENT, "configuration", "config and out are required");
    if (config->disabled) return LX_PMU_DISABLED;
    if (!config->events || config->event_count == 0 || config->event_count > LX_PMU_MAX_EVENTS)
        return fail(error, LX_PMU_INVALID, EINVAL, LX_PMU_NO_EVENT, "configuration", "event_count must be 1..LX_PMU_MAX_EVENTS with events");
    for (uint32_t i = 0; i < config->event_count; ++i) {
        const lx_pmu_event &event = config->events[i];
        if (event.config1 || event.config2 || (i == 0 ?
            event.type != LX_PMU_TYPE_HARDWARE || event.config != 0 :
            event.type != LX_PMU_TYPE_RAW || event.config > 0xffff))
            return fail(error, LX_PMU_INVALID, EINVAL, i, "configuration", "group requires cycles leader and up to eight 16-bit RAW events");
        for (uint32_t j = 1; j < i; ++j)
            if (event.config == config->events[j].config)
                return fail(error, LX_PMU_INVALID, EINVAL, i, "configuration", "RAW event codes must be distinct");
    }
#if !defined(LXTRACE_PMU_LINUX) || !defined(SYS_perf_event_open) || !defined(SYS_gettid)
    return fail(error, LX_PMU_UNSUPPORTED, ENOSYS, LX_PMU_NO_EVENT, "perf_event_open", "per-thread PMU requires supported Linux syscalls");
#else
    lx_pmu *handle = new (std::nothrow) lx_pmu();
    if (!handle)
        return fail(error, LX_PMU_NO_MEMORY, ENOMEM, LX_PMU_NO_EVENT, "allocation", "PMU handle allocation failed");
    handle->pid = static_cast<uint64_t>(getpid());
    handle->tid = static_cast<uint64_t>(syscall(SYS_gettid));
    handle->event_count = config->event_count;
    for (uint32_t i = 0; i < LX_PMU_MAX_EVENTS; ++i) handle->fds[i] = -1;
    for (uint32_t i = 0; i < handle->event_count; ++i) {
        handle->events[i] = config->events[i];
        struct perf_event_attr attr;
        std::memset(&attr, 0, sizeof(attr));
        attr.size = sizeof(attr);
        attr.type = handle->events[i].type;
        attr.config = handle->events[i].config;
        attr.config1 = handle->events[i].config1;
        attr.config2 = handle->events[i].config2;
        attr.disabled = i == 0;
        attr.exclude_kernel = 1;
        attr.exclude_hv = 1;
        attr.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
        const long fd = syscall(SYS_perf_event_open, &attr, 0, -1,
                                i ? handle->fds[0] : -1, PERF_FLAG_FD_CLOEXEC);
        if (fd < 0) {
            const int number = errno;
            lx_pmu_close(handle);
            return fail(error, os_status(number), number, i, "perf_event_open", "cannot open requested per-thread event");
        }
        handle->fds[i] = static_cast<int>(fd);
    }
    {
        if (ioctl(handle->fds[0], PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP) != 0) {
            const int number = errno;
            lx_pmu_close(handle);
            return fail(error, os_status(number), number, 0, "PERF_EVENT_IOC_RESET", "cannot reset requested group");
        }
    }
    {
        if (ioctl(handle->fds[0], PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP) != 0) {
            const int number = errno;
            lx_pmu_close(handle);
            return fail(error, os_status(number), number, 0, "PERF_EVENT_IOC_ENABLE", "cannot enable requested group");
        }
    }
    *out = handle;
    return LX_PMU_OK;
#endif
}

extern "C" int lx_pmu_read(lx_pmu *handle, lx_pmu_snapshot *snapshot, lx_pmu_error *error) {
    clear_error(error);
    if (!snapshot)
        return fail(error, LX_PMU_INVALID, EINVAL, LX_PMU_NO_EVENT, "read", "snapshot is required");
    std::memset(snapshot, 0, sizeof(*snapshot));
    if (!handle) return LX_PMU_DISABLED;
#if !defined(LXTRACE_PMU_LINUX) || !defined(SYS_perf_event_open) || !defined(SYS_gettid)
    return fail(error, LX_PMU_UNSUPPORTED, ENOSYS, LX_PMU_NO_EVENT, "read", "per-thread PMU requires supported Linux syscalls");
#else
    if (handle->pid != static_cast<uint64_t>(getpid()))
        return fail(error, LX_PMU_FORKED, 0, LX_PMU_NO_EVENT, "owner", "inherited PMU handle must be reopened after fork");
    if (handle->tid != static_cast<uint64_t>(syscall(SYS_gettid)))
        return fail(error, LX_PMU_WRONG_THREAD, 0, LX_PMU_NO_EVENT, "owner", "PMU handle belongs to another thread");
    lx_pmu_snapshot next = {};
    next.pid = handle->pid;
    next.tid = handle->tid;
    next.event_count = handle->event_count;
    if (monotonic_ns(&next.read_begin_ns) != 0)
        return fail(error, LX_PMU_IO, errno, LX_PMU_NO_EVENT, "clock_gettime", "cannot timestamp counter read");
    int status = LX_PMU_OK;
    uint64_t raw[3 + LX_PMU_MAX_EVENTS] = {};
    const size_t bytes = (3 + handle->event_count) * sizeof(uint64_t);
    ssize_t got;
    do { got = read(handle->fds[0], raw, bytes); } while (got < 0 && errno == EINTR);
    if (got != static_cast<ssize_t>(bytes)) {
            const int number = got < 0 ? errno : EIO;
            return fail(error, os_status(number), number, 0, "read", "counter group read was incomplete or failed");
        }
    if (raw[0] != handle->event_count || raw[2] > raw[1])
        return fail(error, LX_PMU_IO, EPROTO, 0, "read", "group size mismatch or time_running exceeds time_enabled");
    for (uint32_t i = 0; i < handle->event_count; ++i) {
        lx_pmu_reading &reading = next.readings[i];
        reading.event = handle->events[i];
        reading.value = raw[3 + i];
        reading.time_enabled = raw[1];
        reading.time_running = raw[2];
        reading.status = raw[2] ? LX_PMU_OK : LX_PMU_NOT_RUNNING;
        if (!raw[2]) status = LX_PMU_NOT_RUNNING;
    }
    if (monotonic_ns(&next.read_end_ns) != 0)
        return fail(error, LX_PMU_IO, errno, LX_PMU_NO_EVENT, "clock_gettime", "cannot timestamp counter read");
    *snapshot = next;
    return status;
#endif
}

extern "C" void lx_pmu_close(lx_pmu *handle) {
    if (!handle) return;
#if defined(LXTRACE_PMU_LINUX)
    // Do not disable: an inherited child fd refers to the parent's event.
    for (uint32_t i = 0; i < handle->event_count; ++i)
        if (handle->fds[i] >= 0) close(handle->fds[i]);
#endif
    delete handle;
}
