/* Generic executor integration example, independent of a model or framework. */
#include "lxtrace_compact.h"
#include <thread>
#include <vector>
#include <cstdio>

int main() {
    const int capture=lx_compact_init_env(65536,10);
    if(capture<0) { std::fprintf(stderr,"compact capture initialization failed\n"); return 2; }
    uint32_t frame=0, compute=0, wait=0;
    if(!capture) {
        frame=lx_compact_site("request.prefill","unknown","{\"kind\":\"frame\"}");
        compute=lx_compact_site("executor.compute","mat","{}");
        wait=lx_compact_site("executor.synchronize","wait","{\"kind\":\"wait\"}");
    }
    std::vector<std::thread> workers;
    for(int rank=0;rank<4;++rank) workers.emplace_back([=] {
        if(!capture) { char name[40]; std::snprintf(name,sizeof name,"executor.%d",rank); lx_compact_thread(name); }
        lx_compact_event(frame,LX_COMPACT_BEGIN,0,0);
        for(int step=0;step<20;++step) {
            lx_compact_event(compute,LX_COMPACT_BEGIN,(uint16_t)step,0);
            volatile double value=1;
            for(int i=0;i<10000;++i)value=value*1.000001+0.000001;
            lx_compact_event(compute,LX_COMPACT_END,(uint16_t)step,0);
            lx_compact_event(wait,LX_COMPACT_BEGIN,0,0);
            std::this_thread::yield();
            lx_compact_event(wait,LX_COMPACT_END,0,0);
        }
        lx_compact_event(frame,LX_COMPACT_END,0,0);
    });
    for(auto &worker:workers)worker.join();
    lx_compact_shutdown();
}
