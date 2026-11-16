#pragma once
#include "lxtrace.h"

/* Optional executor bridge: wrap existing callbacks at scheduling boundaries.
 * Model kernels do not need to include this header or call the tracing SDK.
 * When LXTRACE_OUTPUT is absent, the callback still runs and the SDK is a no-op.
 * Categories describe the callback's software intent, not hardware-unit busy.
 */
namespace lxtrace {
class Exchange {
    lx_handle handle_;
public:
    Exchange(const char *name, const char *metadata = "{}")
        : handle_(lx_exchange_begin(name, metadata)) {}
    ~Exchange() { lx_range_end(handle_); }
    Exchange(const Exchange&) = delete;
    Exchange& operator=(const Exchange&) = delete;
};

class ExecutionAdapter {
public:
    template<class Callback>
    auto frame(const char *id, const char *metadata, Callback&& callback) const -> decltype(callback()) {
        Frame scope(id, 0, metadata);
        return callback();
    }
    template<class Callback>
    auto operation(const char *name, const char *category, const char *metadata,
                   Callback&& callback) const -> decltype(callback()) {
        Range scope(name, category, metadata);
        return callback();
    }
    template<class Callback>
    auto exchange(const char *name, const char *metadata, Callback&& callback) const -> decltype(callback()) {
        Exchange scope(name, metadata);
        return callback();
    }
    template<class Callback>
    auto wait(const char *name, const char *object, Callback&& callback) const -> decltype(callback()) {
        Wait scope(name, object);
        return callback();
    }
    lx_handle submit(const char *name, const char *correlation, const char *peer,
                     uint64_t bytes, const char *transport) const {
        return lx_async_begin_transport(name, correlation, peer, bytes, transport);
    }
    void complete(lx_handle request) const { lx_async_complete_observed(request); }
};
}
