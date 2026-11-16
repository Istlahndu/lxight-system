#include "lxtrace.h"
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

/* A real CPU software pipeline. Matrix/vector labels express application intent only.
 * The software queue simulates an async exchange; it is never device-transfer evidence.
 * Worker association is explicit via args.application_frame_id, not inferred from wall overlap.
 */
int main(int argc,char **argv) {
    bool open=false,stress=false;
    for(int i=1;i<argc;++i) { open|=!std::strcmp(argv[i],"--open"); stress|=!std::strcmp(argv[i],"--stress"); }
    lx_init("LX2 deterministic software pipeline","lxtrace-sample-v1-cxx11");
    lx_thread_role("帧驱动 · pipeline","frame_driver");
    const int frames=stress?100:5, workers=4;
    std::atomic<uint64_t> checksum(0);
    for(int frame=0;frame<frames;++frame) {
        char frame_name[64]; std::snprintf(frame_name,sizeof frame_name,"decode.step.%d",frame);
        lx_handle f=lx_frame_begin(frame_name,1);
        lx_instant("frame.input", "{\"batch\":1,\"context\":128,\"dtype\":\"u64\"}");
        lx_handle req=0;
        { lxtrace::Range submit("queue.submit","vec"); lx_handle x=lx_exchange_begin("queue.push", "{\"bytes\":4096,\"peer\":\"software-completion-thread\"}"); req=lx_async_begin("activation.exchange","activation-buffer","software-completion-thread",4096); lx_range_end(x); }
        std::thread completion([req] { lx_thread_role("软件队列 · completion","completion"); std::this_thread::sleep_for(std::chrono::milliseconds(4)); lx_instant("queue.completion_observed","{}"); lx_async_complete_observed(req); });
        lx_handle pool=lx_range_begin("layer.0 / worker pool","unknown",f,"{\"call_boundary\":true}");
        std::vector<std::thread> threads;
        for(int worker=0;worker<workers;++worker) threads.emplace_back([worker,frame,&checksum] {
            char name[64],args[256]; std::snprintf(name,sizeof name,"计算线程 %d",worker);
            lx_thread_role(name,"worker");
            std::snprintf(args,sizeof args,"{\"application_frame_id\":\"decode.step.%d\",\"worker_id\":%d,\"shape\":[128,128],\"dtype\":\"u64\",\"implementation\":\"portable-cxx-loop\"}",frame,worker);
            lx_handle whole=lx_range_begin("layer.0.gemm <demo>&\"","mat",0,args);
            uint64_t sum=0;
            for(int i=0;i<80000;++i) sum+=(uint64_t)((i*17+worker*13+frame)%997);
            { lxtrace::Wait wait("annotated queue wait","activation-buffer"); std::this_thread::sleep_for(std::chrono::milliseconds(worker==0?5:1)); }
            { lxtrace::Range vector("layer.0.norm","vec"); for(int i=0;i<60000;++i) sum^=(sum<<1)+(uint64_t)i+worker; }
            checksum.fetch_add(sum,std::memory_order_relaxed);
            lx_counter("work.items",140000); lx_range_end(whole);
        });
        { lxtrace::Wait wait("join workers","worker_pool"); for(size_t i=0;i<threads.size();++i) threads[i].join(); }
        lx_range_end(pool);
        { lxtrace::Wait wait("observe exchange","activation-buffer"); completion.join(); }
        { lxtrace::Range head("head.sample","vec"); std::this_thread::sleep_for(std::chrono::milliseconds(1)); }
        lx_frame_end(f);
    }
    if(open) { lx_range_begin("unclosed.range","mat",0,"{}"); lx_async_begin("uncompleted.request","reuse-handle","software-queue",64); }
    std::printf("business_checksum=%llu frames=%d\n",(unsigned long long)checksum.load(),frames);
    lx_shutdown();
    return 0;
}
