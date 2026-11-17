#pragma once
#include <stdint.h>

/* Optional Linux per-thread PMU reader. Independent of the JSONL SDK.
 * Each handle belongs to the process and thread that opened it; it is not
 * inherited as a usable reader across fork. Calls never throw or alter system
 * policy. No root access is requested. The application chooses its RAW event
 * encodings; an accepted encoding alone does not validate its hardware meaning.
 *
 * Values and multiplex times are exact uint64 readings, without scaling.
 * Counters are user-only, non-inheriting, pid=0/cpu=-1. A cycles leader and up
 * to eight RAW members are enabled together and read in one group syscall; these
 * readings are counter-window evidence, not hardware activity boundaries.
 */
#ifdef __cplusplus
extern "C" {
#endif

#define LX_PMU_MAX_EVENTS 9
#define LX_PMU_NO_EVENT UINT32_MAX

enum lx_pmu_status {
    LX_PMU_OK = 0,
    LX_PMU_DISABLED = 1,
    LX_PMU_NOT_RUNNING = 2,
    LX_PMU_INVALID = -1,
    LX_PMU_DENIED = -2,
    LX_PMU_UNSUPPORTED = -3,
    LX_PMU_IO = -4,
    LX_PMU_WRONG_THREAD = -5,
    LX_PMU_FORKED = -6,
    LX_PMU_NO_MEMORY = -7
};

/* Linux PERF_TYPE_* values. The supported group is HARDWARE cycles followed
 * by distinct RAW 16-bit codes, with config1/config2 zero. */
enum lx_pmu_event_type {
    LX_PMU_TYPE_HARDWARE = 0,
    LX_PMU_TYPE_SOFTWARE = 1,
    LX_PMU_TYPE_RAW = 4
};

typedef struct lx_pmu_event {
    uint32_t type;
    uint64_t config;
    uint64_t config1;
    uint64_t config2;
} lx_pmu_event;

typedef struct lx_pmu_config {
    const lx_pmu_event *events;
    uint32_t event_count;
    int disabled;
} lx_pmu_config;

typedef struct lx_pmu_reading {
    lx_pmu_event event;
    uint64_t value;
    uint64_t time_enabled;
    uint64_t time_running;
    int status; /* LX_PMU_OK or LX_PMU_NOT_RUNNING; never substitute a count. */
} lx_pmu_reading;

typedef struct lx_pmu_snapshot {
    uint64_t pid;
    uint64_t tid;
    uint64_t read_begin_ns; /* CLOCK_MONOTONIC, outer single group read window. */
    uint64_t read_end_ns;
    uint32_t event_count; /* Zero on error, unsupported, or disabled. */
    lx_pmu_reading readings[LX_PMU_MAX_EVENTS];
} lx_pmu_snapshot;

typedef struct lx_pmu_error {
    int status;
    int system_errno;
    uint32_t event_index; /* LX_PMU_NO_EVENT if not event-specific. */
    char operation[32];
    char message[192];
} lx_pmu_error;

typedef struct lx_pmu lx_pmu;

/* events[0] must be HARDWARE cycles (config=0); remaining events are at most
 * eight distinct RAW codes <= 0xffff. A cycles-only group is allowed.
 * Open/reset/enable the group, rolling back every fd on failure. The config
 * and event array are copied. disabled returns LX_PMU_DISABLED with *out=NULL,
 * without probing the kernel. A failed open never returns a partial handle. */
int lx_pmu_open(const lx_pmu_config *config, lx_pmu **out, lx_pmu_error *error);

/* No allocation. On failure the complete snapshot is cleared, so stale or
 * partial values cannot masquerade as new observations. LX_PMU_NOT_RUNNING
 * retains successful reads but at least one event has time_running==0; this
 * cannot be treated as a measured zero or a scalable count. No extrapolation
 * is performed. A NULL handle returns LX_PMU_DISABLED. */
int lx_pmu_read(lx_pmu *handle, lx_pmu_snapshot *snapshot, lx_pmu_error *error);

/* NULL-safe. May release an inherited child handle without changing the
 * parent's enable state. Never race close against another call on the handle.
 * Callers must close handles explicitly; no global registry/atfork lock exists. */
void lx_pmu_close(lx_pmu *handle);

#ifdef __cplusplus
}
#endif
