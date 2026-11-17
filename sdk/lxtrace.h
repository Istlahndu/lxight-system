#pragma once
#include <stdint.h>

/* C-compatible API. No function throws; loss and protocol failures never abort the application.
 * LXTRACE_OUTPUT selects a JSONL file; LXTRACE_DISABLED=1 or no output path makes calls no-ops.
 * args_json is caller-owned JSON object text, <= 767 UTF-8 bytes, nesting <= 64.
 * Non-finite numbers, malformed text or oversized arguments are replaced with
 * {"invalid_args":true} and counted as protocol errors; the event is retained.
 * Tensor/message payloads are not captured.
 * Synchronous ranges are strictly nested on the calling thread; async handles may close elsewhere.
 */
#ifdef __cplusplus
extern "C" {
#endif
typedef uint64_t lx_handle;
/* A frame context is process-local. It may be copied with a task to another
 * thread in the same process; it is not a cross-process or persistent ID. */
typedef struct lx_context { lx_handle frame; uint64_t process_id; } lx_context;
void lx_init(const char *app_name, const char *build_id);
void lx_thread_role(const char *name, const char *role);
lx_handle lx_frame_begin(const char *frame_id, int driver_calls_complete);
/* Extended frame boundary for framework adapters.  args_json must be a JSON
 * object; its fields are copied into the frame metadata (for example
 * microbatch, layer, operator_id, and parent_application_frame_id).  The
 * ordinary lx_frame_begin() remains source-compatible and supplies only the
 * required application frame fields.
 */
lx_handle lx_frame_begin_ex(const char *frame_id, int driver_calls_complete, const char *args_json);
lx_context lx_context_capture(void);
/* Returns 0 when attached, -1 for a foreign process or non-frame handle.
 * Restore the returned previous context after all synchronous child ranges
 * have closed.  An attached frame may outlive the producer's own call span;
 * the context is membership metadata, not a fabricated parent execution span.
 */
int lx_context_attach(lx_context context, lx_context *previous);
lx_handle lx_range_begin(const char *name, const char *category, lx_handle parent, const char *args_json);
lx_handle lx_wait_begin(const char *name, const char *object);
lx_handle lx_exchange_begin(const char *name, const char *args_json);
void lx_range_end(lx_handle handle);
void lx_frame_end(lx_handle handle);
lx_handle lx_async_begin(const char *name, const char *correlation_id, const char *peer, uint64_t logical_bytes);
/* transport describes the application's mechanism, not hardware transfer evidence.
 * The original lx_async_begin() retains its software_queue transport default.
 */
lx_handle lx_async_begin_transport(const char *name, const char *correlation_id, const char *peer, uint64_t logical_bytes, const char *transport);
void lx_async_complete_observed(lx_handle handle);
void lx_instant(const char *name, const char *args_json);
void lx_counter(const char *name, double value);
/* Freeze the recorded event set and write a cumulative quality snapshot.
 * Unclosed recorded ranges/requests, loss and protocol errors make the capture
 * unclean. Later event calls are rejected and counted as late/dropped records;
 * changed quality is appended immediately, including during later destructors.
 * A snapshot only describes observations up to that snapshot. Abnormal process
 * exit or a failed output write cannot be made durable by this buffered API.
 */
void lx_shutdown(void);
#ifdef __cplusplus
}
namespace lxtrace {
class ContextScope {
    lx_context previous_;
    bool active_;
public:
    explicit ContextScope(lx_context context) : previous_{0,0}, active_(lx_context_attach(context, &previous_) == 0) {}
    ~ContextScope() { if (active_) lx_context_attach(previous_, 0); }
    ContextScope(const ContextScope&) = delete;
    ContextScope& operator=(const ContextScope&) = delete;
    bool active() const { return active_; }
};
class Frame {
    lx_handle handle_;
public:
    Frame(const char *frame_id, int driver_calls_complete = 0, const char *args = "{}")
        : handle_(lx_frame_begin_ex(frame_id, driver_calls_complete, args)) {}
    ~Frame() { lx_frame_end(handle_); }
    Frame(const Frame&) = delete;
    Frame& operator=(const Frame&) = delete;
    lx_handle handle() const { return handle_; }
};
class Range {
    lx_handle handle_;
public:
    Range(const char *name, const char *category, const char *args = "{}") : handle_(lx_range_begin(name, category, 0, args)) {}
    ~Range() { lx_range_end(handle_); }
    Range(const Range&) = delete;
    Range& operator=(const Range&) = delete;
};
class Wait {
    lx_handle handle_;
public:
    Wait(const char *name, const char *object) : handle_(lx_wait_begin(name, object)) {}
    ~Wait() { lx_range_end(handle_); }
    Wait(const Wait&) = delete;
    Wait& operator=(const Wait&) = delete;
};
}
#endif
