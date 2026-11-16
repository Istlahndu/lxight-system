#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "lxtrace_adapter.h"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#include <time.h>
#endif

// Two real MLP layers with a bounded activation-copy queue. There are no sleeps,
// pacing timers, fabricated memory-stall ranges or inferred hardware busy values.
namespace {
struct Shape { int microbatches=8, batch=16, input=256, hidden=512, output=256; };
struct Slot { std::vector<float> activation; int microbatch=-1; lx_handle request=0; };
struct Shared {
    std::mutex mutex;
    std::condition_variable changed;
    Slot slots[2];
    int head=0, tail=0, count=0, ready=0, released=0, completed=0;
    bool start=false;
};

double thread_cpu_ms() {
#if defined(__linux__)
    timespec t;
    if (clock_gettime(CLOCK_THREAD_CPUTIME_ID,&t)) return -1;
    return t.tv_sec*1000.0+t.tv_nsec/1000000.0;
#else
    return -1;
#endif
}
void register_thread(const char *name,const char *role,int cpu) {
    int effective=-1, set_error=-1, query_error=-1;
#if defined(__linux__)
    if (cpu>=0) {
        cpu_set_t wanted, actual;
        CPU_ZERO(&wanted); CPU_SET(cpu,&wanted); CPU_ZERO(&actual);
        set_error=pthread_setaffinity_np(pthread_self(),sizeof wanted,&wanted);
        query_error=pthread_getaffinity_np(pthread_self(),sizeof actual,&actual);
        if (!set_error&&!query_error&&CPU_COUNT(&actual)==1&&CPU_ISSET(cpu,&actual)) effective=cpu;
    }
#endif
    char label[128],args[256];
    if (effective>=0) std::snprintf(label,sizeof label,"%s · 绑CPU %d",name,effective);
    else std::snprintf(label,sizeof label,"%s · 未确认绑核",name);
    lx_thread_role(label,role);
    std::snprintf(args,sizeof args,"{\"requested_cpu\":%d,\"verified_single_cpu\":%d,\"setaffinity_error\":%d,\"getaffinity_error\":%d,\"scope\":\"static_thread_affinity_only\"}",cpu,effective,set_error,query_error);
    lx_instant("thread.static_affinity",args);
}
std::vector<int> allowed_cpus() {
    std::vector<int> result;
#if defined(__linux__)
    cpu_set_t cpus; CPU_ZERO(&cpus);
    if (!sched_getaffinity(0,sizeof cpus,&cpus))
        for (int i=0;i<CPU_SETSIZE;++i) if(CPU_ISSET(i,&cpus)) result.push_back(i);
#endif
    return result;
}
// The i-k-j loop multiplies dense row-major matrices; each output visits k in
// ascending order. All outputs are subsequently validated against long doubles.
void matmul(const float *input,const float *weights,float *out,int rows,int inner,int cols) {
    std::fill(out,out+static_cast<size_t>(rows)*cols,0.0f);
    for(int i=0;i<rows;++i)
        for(int k=0;k<inner;++k) {
            const float x=input[static_cast<size_t>(i)*inner+k];
            const float *w=weights+static_cast<size_t>(k)*cols;
            float *y=out+static_cast<size_t>(i)*cols;
            for(int j=0;j<cols;++j) y[j]+=x*w[j];
        }
}
void bias(float *output,const float *b,int rows,int cols,bool relu) {
    for(int i=0;i<rows;++i) for(int j=0;j<cols;++j) {
        float &x=output[static_cast<size_t>(i)*cols+j];
        x+=b[j]; if(relu&&x<0) x=0;
    }
}
void reference(const Shape &s,const std::vector<float> &inputs,const std::vector<float> &w1,
               const std::vector<float> &b1,const std::vector<float> &w2,const std::vector<float> &b2,
               std::vector<long double> &expected) {
    std::vector<long double> hidden(s.hidden);
    for(int mb=0;mb<s.microbatches;++mb) for(int row=0;row<s.batch;++row) {
        const float *x=inputs.data()+(static_cast<size_t>(mb)*s.batch+row)*s.input;
        for(int j=0;j<s.hidden;++j) {
            long double sum=b1[j];
            for(int k=0;k<s.input;++k) sum+=static_cast<long double>(x[k])*w1[static_cast<size_t>(k)*s.hidden+j];
            hidden[j]=std::max(0.0L,sum);
        }
        for(int j=0;j<s.output;++j) {
            long double sum=b2[j];
            for(int k=0;k<s.hidden;++k) sum+=hidden[k]*w2[static_cast<size_t>(k)*s.output+j];
            expected[(static_cast<size_t>(mb)*s.batch+row)*s.output+j]=sum;
        }
    }
}
void init_values(std::vector<float> &values,unsigned seed,float divisor) {
    uint32_t x=seed;
    for(float &v:values) { x=x*1664525U+1013904223U; v=(static_cast<int>((x>>16)%33)-16)/divisor; }
}
void event_args(char *args,size_t size,const Shape &s,int mb,int layer,const char *operation,const char *frame_id) {
    std::snprintf(args,size,"{\"application_frame_id\":\"%s\",\"microbatch\":%d,\"layer\":%d,\"operation\":\"%s\",\"dtype\":\"float32\",\"shape\":[%d,%d,%d],\"scope\":\"annotated_software_wall_time\"}",frame_id?frame_id:"mlp.inference",mb,layer,operation,s.batch,layer==1?s.input:s.hidden,layer==1?s.hidden:s.output);
}
void await_start(Shared &q) {
    std::unique_lock<std::mutex> lock(q.mutex);
    ++q.ready; q.changed.notify_all();
    lx_handle wait=lx_wait_begin("等待推理开始","driver start condition");
    q.changed.wait(lock,[&]{return q.start;});
    lx_range_end(wait);
}
bool parse_value(const char *text,int &out) {
    char *end=nullptr; long n=std::strtol(text,&end,10);
    if(!text[0]||*end||n<1||n>2048) return false;
    out=static_cast<int>(n); return true;
}
}

int main(int argc,char **argv) {
    Shape s;
    if(argc<2||(std::strcmp(argv[1],"serial")&&std::strcmp(argv[1],"pipeline"))) {
        std::fprintf(stderr,"usage: mlp_pipeline serial|pipeline [--microbatches N] [--batch N] [--input N] [--hidden N] [--output N]\n"); return 2;
    }
    const bool pipeline=!std::strcmp(argv[1],"pipeline");
    for(int i=2;i<argc;i+=2) {
        int *target=nullptr;
        if(!std::strcmp(argv[i],"--microbatches")) target=&s.microbatches;
        else if(!std::strcmp(argv[i],"--batch")) target=&s.batch;
        else if(!std::strcmp(argv[i],"--input")) target=&s.input;
        else if(!std::strcmp(argv[i],"--hidden")) target=&s.hidden;
        else if(!std::strcmp(argv[i],"--output")) target=&s.output;
        if(!target||i+1>=argc||!parse_value(argv[i+1],*target)) {std::fprintf(stderr,"invalid argument\n");return 2;}
    }
    const uint64_t macs=static_cast<uint64_t>(s.microbatches)*s.batch*s.hidden*(s.input+s.output);
    if(macs>268435456ULL||s.microbatches>128) {std::fprintf(stderr,"lesson workload too large; maximum 268435456 MACs / 128 microbatches\n");return 2;}
    std::vector<float> inputs(static_cast<size_t>(s.microbatches)*s.batch*s.input),w1(static_cast<size_t>(s.input)*s.hidden),b1(s.hidden),w2(static_cast<size_t>(s.hidden)*s.output),b2(s.output),outputs(static_cast<size_t>(s.microbatches)*s.batch*s.output);
    init_values(inputs,17,64);init_values(w1,23,256);init_values(b1,29,256);init_values(w2,31,256);init_values(b2,37,256);
    std::vector<long double> expected(outputs.size());
    // Independent, uninstrumented reference is prepared before the measured run.
    reference(s,inputs,w1,b1,w2,b2,expected);
    Shared q;
    for(Slot &slot:q.slots) slot.activation.resize(static_cast<size_t>(s.batch)*s.hidden);
    const size_t activation_bytes=static_cast<size_t>(s.batch)*s.hidden*sizeof(float);
    auto cpus=allowed_cpus();
    auto target=[&](size_t i){return cpus.empty()?-1:cpus[i%cpus.size()];};
    double stage_cpu_ms[2]={0,0};
    lxtrace::ExecutionAdapter trace;
    lx_init("两层 MLP · 微批流水线推理","mlp-layer-frames-v2");
    register_thread("推理驱动","frame_driver",target(2));
    std::thread first([&]{
        register_thread("Stage 1 · Linear + ReLU","worker",target(0));
        std::vector<float> hidden(static_cast<size_t>(s.batch)*s.hidden);
        await_start(q);
        const double cpu_start=thread_cpu_ms();
        for(int mb=0;mb<s.microbatches;++mb) {
            char args[512],name[128],correlation[64],frame_id[96],frame_args_json[256];
            std::snprintf(frame_id,sizeof frame_id,"mlp.inference.mb%d.layer1",mb);
            std::snprintf(frame_args_json,sizeof frame_args_json,"{\"frame_kind\":\"layer\",\"microbatch\":%d,\"layer\":1,\"operator_id\":\"L1.forward\",\"parent_application_frame_id\":\"mlp.inference\"}",mb);
            trace.frame(frame_id,frame_args_json,[&]{
            {std::unique_lock<std::mutex> lock(q.mutex);trace.wait("等待输入微批许可","serial dependency or pipeline admission",[&]{q.changed.wait(lock,[&]{return mb<q.released;});});}
            event_args(args,sizeof args,s,mb,1,"dense_matrix_multiply",frame_id);
            std::snprintf(name,sizeof name,"L1.GEMM · microbatch %d",mb);
            trace.operation(name,"mat",args,[&]{matmul(inputs.data()+static_cast<size_t>(mb)*s.batch*s.input,w1.data(),hidden.data(),s.batch,s.input,s.hidden);});
            event_args(args,sizeof args,s,mb,1,"bias_add_relu",frame_id);
            std::snprintf(name,sizeof name,"L1.bias + ReLU · microbatch %d",mb);
            trace.operation(name,"vec",args,[&]{bias(hidden.data(),b1.data(),s.batch,s.hidden,true);});
            std::unique_lock<std::mutex> lock(q.mutex);
            trace.wait("等待激活队列空位","bounded activation queue (capacity 2)",[&]{q.changed.wait(lock,[&]{return q.count<2;});});
            std::snprintf(correlation,sizeof correlation,"activation.%d",mb);
            std::snprintf(name,sizeof name,"L1→L2 activation · microbatch %d",mb);
            lx_handle request=trace.submit(name,correlation,"Stage 2 activation consumer",activation_bytes,"shared_memory_copy_queue");
            event_args(args,sizeof args,s,mb,1,"queue_publish_memcpy",frame_id);
            trace.exchange("发布激活 · memcpy 到队列",args,[&]{Slot &slot=q.slots[q.tail];std::memcpy(slot.activation.data(),hidden.data(),activation_bytes);slot.microbatch=mb;slot.request=request;q.tail=(q.tail+1)%2;++q.count;});
            lock.unlock();q.changed.notify_all();
            });
        }
        const double cpu_end=thread_cpu_ms();
        stage_cpu_ms[0]=(cpu_start>=0&&cpu_end>=0)?cpu_end-cpu_start:-1;
    });
    std::thread second([&]{
        register_thread("Stage 2 · Linear","worker",target(1));
        std::vector<float> hidden(static_cast<size_t>(s.batch)*s.hidden);
        await_start(q);
        const double cpu_start=thread_cpu_ms();
        for(int mb=0;mb<s.microbatches;++mb) {
            char args[512],name[128],frame_id[96],frame_args_json[256];
            std::snprintf(frame_id,sizeof frame_id,"mlp.inference.mb%d.layer2",mb);
            std::snprintf(frame_args_json,sizeof frame_args_json,"{\"frame_kind\":\"layer\",\"microbatch\":%d,\"layer\":2,\"operator_id\":\"L2.forward\",\"parent_application_frame_id\":\"mlp.inference\"}",mb);
            trace.frame(frame_id,frame_args_json,[&]{
            std::unique_lock<std::mutex> lock(q.mutex);
            trace.wait("等待上一层激活","bounded activation queue not empty",[&]{q.changed.wait(lock,[&]{return q.count>0;});});
            Slot &slot=q.slots[q.head];
            if(slot.microbatch!=mb) std::abort();
            event_args(args,sizeof args,s,mb,2,"queue_acquire_memcpy",frame_id);
            lx_handle request=slot.request;
            trace.exchange("取走激活 · memcpy 到L2输入",args,[&]{std::memcpy(hidden.data(),slot.activation.data(),activation_bytes);q.head=(q.head+1)%2;--q.count;});
            trace.complete(request);lock.unlock();q.changed.notify_all();
            event_args(args,sizeof args,s,mb,2,"dense_matrix_multiply",frame_id);
            std::snprintf(name,sizeof name,"L2.GEMM · microbatch %d",mb);
            float *out=outputs.data()+static_cast<size_t>(mb)*s.batch*s.output;
            trace.operation(name,"mat",args,[&]{matmul(hidden.data(),w2.data(),out,s.batch,s.hidden,s.output);});
            event_args(args,sizeof args,s,mb,2,"bias_add",frame_id);
            std::snprintf(name,sizeof name,"L2.bias · microbatch %d",mb);
            trace.operation(name,"vec",args,[&]{bias(out,b2.data(),s.batch,s.output,false);});
            {std::lock_guard<std::mutex> update(q.mutex);++q.completed;if(!pipeline)q.released=q.completed+1;}q.changed.notify_all();
            });
        }
        const double cpu_end=thread_cpu_ms();
        stage_cpu_ms[1]=(cpu_start>=0&&cpu_end>=0)?cpu_end-cpu_start:-1;
    });
    {std::unique_lock<std::mutex> lock(q.mutex);lxtrace::Wait wait("等待两个流水级就绪","own worker initialization");q.changed.wait(lock,[&]{return q.ready==2;});}
    lx_handle frame=lx_frame_begin("mlp.inference",1);
    const auto start=std::chrono::steady_clock::now();
    {lx_handle dispatch=lx_range_begin("发布推理输入","unknown",frame,"{\"call_boundary\":true}");{std::lock_guard<std::mutex> lock(q.mutex);q.released=pipeline?s.microbatches:1;q.start=true;}q.changed.notify_all();lx_range_end(dispatch);}
    {lx_handle observe=lx_range_begin("等待流水线输出","unknown",frame,"{\"call_boundary\":true}");std::unique_lock<std::mutex> lock(q.mutex);{lxtrace::Wait wait("等待全部微批输出","two layer inference completion");q.changed.wait(lock,[&]{return q.completed==s.microbatches;});}lx_range_end(observe);}
    {lx_handle join=lx_range_begin("回收推理线程","unknown",frame,"{\"call_boundary\":true}");{lxtrace::Wait wait("join 两个流水级","own worker threads");first.join();second.join();}lx_range_end(join);}
    const double elapsed=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count();
    lx_frame_end(frame);
    lx_counter("inference.elapsed_wall_ms",elapsed);lx_counter("stage1.thread_cpu_ms",stage_cpu_ms[0]);lx_counter("stage2.thread_cpu_ms",stage_cpu_ms[1]);
    lx_shutdown();
    double max_abs=0,max_rel=0;bool finite=true;uint64_t hash=1469598103934665603ULL;
    for(size_t i=0;i<outputs.size();++i) {finite=finite&&std::isfinite(outputs[i]);double diff=std::fabs(static_cast<double>(outputs[i])-static_cast<double>(expected[i]));max_abs=std::max(max_abs,diff);max_rel=std::max(max_rel,diff/std::max(1e-6,std::fabs(static_cast<double>(expected[i]))));}
    const unsigned char *bytes=reinterpret_cast<const unsigned char*>(outputs.data());
    for(size_t i=0;i<outputs.size()*sizeof(float);++i) {hash^=bytes[i];hash*=1099511628211ULL;}
    const double tolerance=1e-5;const bool valid=finite&&std::isfinite(max_abs)&&max_abs<=tolerance;
    std::printf("RESULT {\"mode\":\"%s\",\"model\":\"Y=ReLU(XW1+b1)W2+b2\",\"dtype\":\"float32\",\"microbatches\":%d,\"microbatch_rows\":%d,\"input_features\":%d,\"hidden_features\":%d,\"output_features\":%d,\"macs\":%llu,\"activation_bytes_per_microbatch\":%llu,\"activation_bytes_total\":%llu,\"output_checksum\":\"%llu\",\"reference_max_abs_error\":%.12g,\"reference_max_rel_error\":%.12g,\"reference_abs_tolerance\":%.12g,\"reference_valid\":%s,\"elapsed_wall_ms\":%.6f,\"stage1_thread_cpu_ms\":%.6f,\"stage2_thread_cpu_ms\":%.6f,\"pacing_sleep\":false}\n",pipeline?"pipeline":"serial",s.microbatches,s.batch,s.input,s.hidden,s.output,static_cast<unsigned long long>(macs),static_cast<unsigned long long>(activation_bytes),static_cast<unsigned long long>(activation_bytes*s.microbatches),static_cast<unsigned long long>(hash),max_abs,max_rel,tolerance,valid?"true":"false",elapsed,stage_cpu_ms[0],stage_cpu_ms[1]);
    return valid?0:4;
}
