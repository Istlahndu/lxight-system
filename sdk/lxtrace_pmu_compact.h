#pragma once
#include "lxtrace_pmu.h"
#include "lxtrace_compact.h"

/* Optional bridge. Caller opens its per-thread group and records its event
 * dictionary once with lx_compact_metadata. A read failure returns the PMU
 * status and never substitutes a zero count or an apparently successful event.
 * A successful read returns 0 when published, -100 if the ring rejected it.
 * No allocation or shared mutex is used by this helper. */
static inline int lx_compact_boundary(lx_pmu *reader, uint32_t site,
                                      uint16_t kind, uint16_t aux,
                                      lx_pmu_error *error) {
    lx_pmu_snapshot snapshot;
    int result=lx_pmu_read(reader,&snapshot,error);
    if(result!=LX_PMU_OK && result!=LX_PMU_NOT_RUNNING)return result;
    uint64_t values[LX_PMU_MAX_EVENTS],enabled[LX_PMU_MAX_EVENTS],running[LX_PMU_MAX_EVENTS];
    for(uint32_t i=0;i<snapshot.event_count;++i) {
        values[i]=snapshot.readings[i].value;
        enabled[i]=snapshot.readings[i].time_enabled;
        running[i]=snapshot.readings[i].time_running;
    }
    return lx_compact_pmu_window(site,kind,aux,snapshot.read_begin_ns,snapshot.read_end_ns,
                                 snapshot.event_count,values,enabled,running)?-100:0;
}
