#pragma once
#include <stdint.h>

/* Optional independent compact backend. Compile lxtrace_compact.cpp with
 * C++11 and pthread; no change to the existing lxtrace.h ABI or JSONL format.
 *
 * Init/site/thread registration and metadata are cold paths. Register every
 * producer thread before calling event/pmu. The hot paths use only a bounded
 * per-thread SPSC ring: no mutex, heap allocation or file I/O. A failed enqueue
 * returns -1 and records an attempted-sequence/timestamp loss bounding range. Each ring
 * has one producer (the registered thread) and one background consumer.
 *
 * Periodic fflush+fsync (_commit on Windows) preserves complete checksummed
 * chunks for prefix recovery after abnormal exit. It does not promise that
 * in-memory or partially written trailing records survive. Missing final
 * quality means incomplete capture. No root or system policy changes.
 *
 * Fork after init disables the inherited backend in the child until exec.
 * Always join producers before shutdown; attempts after close are rejected.
 * Registration is limited to 1024 lifetime threads. Rings stay allocated until
 * process exit so that late calls are safe; budget ring_records*16*threads bytes.
 * Each thread supports 128 nested scopes; scope misuse marks quality partial.
 */
#ifdef __cplusplus
extern "C" {
#endif

enum lx_compact_kind { LX_COMPACT_BEGIN=1, LX_COMPACT_END=2, LX_COMPACT_INSTANT=3 };

/* Exclusive output creation. ring_records is 16-byte slots per thread,
 * 64..1048576; flush_ms is 1..60000. Returns 0, or -1 without throwing.
 * The output path is literal; give each process its own unique file. */
int lx_compact_init(const char *output_path, uint32_t ring_records, uint32_t flush_ms);
/* Collector integration: reads LXTRACE_COMPACT_OUTPUT and expands only %p into
 * this process's PID. Returns 1 when unset or LXTRACE_DISABLED=1 (no capture),
 * 0 on initialization, -1 on error. Explicit init above keeps paths literal. */
int lx_compact_init_env(uint32_t ring_records, uint32_t flush_ms);

/* Site IDs are process-local. Call once per static site on a cold path.
 * Args are retained as escaped raw text for loss-aware importer validation.
 * Invalid/oversized args must not corrupt a chunk or remove a range. */
uint32_t lx_compact_site(const char *name, const char *category, const char *args_json);
int lx_compact_metadata(const char *json_object);
int lx_compact_thread(const char *name);

/* timestamp_ns=0 reads CLOCK_MONOTONIC (steady_clock on Windows); a nonzero
 * value must be an actual timestamp in that same clock, not a PMU counter.
 * BEGIN/END are strictly nested within one thread and use the same site ID.
 * Aux is application-defined uint16 metadata, not an async correlation ID. */
int lx_compact_event(uint32_t site, uint16_t kind, uint16_t aux, uint64_t timestamp_ns);

/* Exact uint64 PMU snapshot extension. count<=16; all arrays have count entries.
 * Codes/configuration belong in cold metadata. The whole snapshot is published
 * atomically to the ring or rejected as one loss attempt. No scaling, source
 * invention or hardware activity boundaries are performed here. */
int lx_compact_pmu(uint32_t site, uint16_t kind, uint16_t aux,
                   uint64_t timestamp_ns, uint32_t count,
                   const uint64_t *values, const uint64_t *enabled,
                   const uint64_t *running);
/* A measured PMU read window, including its read uncertainty. timestamp_ns and
 * read_end_ns must be nonzero actual CLOCK_MONOTONIC/steady-clock boundaries;
 * read_end_ns >= timestamp_ns. The point API above uses equal boundaries. */
int lx_compact_pmu_window(uint32_t site, uint16_t kind, uint16_t aux,
                          uint64_t timestamp_ns, uint64_t read_end_ns,
                          uint32_t count, const uint64_t *values,
                          const uint64_t *enabled, const uint64_t *running);

/* Cold synchronous drain + durable flush. Shutdown may be called repeatedly;
 * changed late/loss quality is appended as another cumulative footer. */
int lx_compact_flush(void);
void lx_compact_shutdown(void);

#ifdef __cplusplus
}
#endif
