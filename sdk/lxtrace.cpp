#include "lxtrace.h"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <mutex>
#include <vector>
#include <string>
#include <functional>
#include <thread>
#include <unordered_set>
#if defined(_WIN32)
#include <process.h>
#include <windows.h>
#else
#include <unistd.h>
#include <sys/syscall.h>
#include <time.h>
#if defined(__linux__)
#include <pthread.h>
#endif
#endif

namespace {
uint64_t stamp() {
#if defined(_WIN32)
    return (uint64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
#else
    timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
#endif
}
long process_id() {
#if defined(_WIN32)
    return _getpid();
#else
    return getpid();
#endif
}
// These atomics must be usable before any function-local initialization guard.
// A fork child must never enter the inherited State guard, mutex, or containers.
std::atomic<long> owner_pid(0);
std::atomic<bool> fork_observed(false);
#if defined(__linux__)
static_assert(ATOMIC_LONG_LOCK_FREE == 2 && ATOMIC_BOOL_LOCK_FREE == 2,
              "fork protection requires lock-free process markers");
std::atomic<bool> fork_hook_registered(false);
void after_fork_parent() noexcept {
    if (owner_pid.load(std::memory_order_relaxed) == process_id())
        fork_observed.store(true, std::memory_order_relaxed);
}
#endif
bool current_process_allowed() noexcept {
    const long pid = process_id();
    long owner = owner_pid.load(std::memory_order_acquire);
    if (owner && owner != pid) return false;
    if (!owner) {
#if defined(__linux__)
        // Register before claiming ownership, including the State construction
        // window. Concurrent first calls may register the idempotent callback
        // twice; a registration lock could itself be inherited while locked.
        if (!fork_hook_registered.load(std::memory_order_acquire) &&
            pthread_atfork(0, after_fork_parent, 0) == 0)
            fork_hook_registered.store(true, std::memory_order_release);
#endif
        if (!owner_pid.compare_exchange_strong(owner, pid,
                                               std::memory_order_acq_rel) && owner != pid)
            return false;
    }
    return true;
}
long thread_id() {
#if defined(_WIN32)
    return GetCurrentThreadId();
#else
    return syscall(SYS_gettid);
#endif
}
void copy(char *out, size_t n, const char *in) {
    if(!n) return;
    if(!in) in="";
    size_t size=std::strlen(in), take=size<n?size:n-1;
    // Do not cut a multibyte UTF-8 label midway through its final code point.
    if(take<size) while(take && (((unsigned char)in[take]&0xc0)==0x80)) --take;
    std::memcpy(out,in,take); out[take]=0;
}
std::string quote(const char *s) {
    std::string o="\"";
    for (const unsigned char *p=(const unsigned char*)s; *p; ++p) {
        if (*p=='"' || *p=='\\') { o+='\\'; o+=(char)*p; }
        else if (*p<32) { char t[7]; std::snprintf(t,7,"\\u%04x",*p); o+=t; }
        else o+=(char)*p;
    }
    return o+'"';
}
std::string output_path(const char *pattern) {
    // Only the owning process flushes. Other percent sequences are literal text.
    std::string out, pid=std::to_string(process_id());
    for(const char *p=pattern; *p; ++p) {
        if(p[0]=='%' && p[1]=='p') { out+=pid; ++p; }
        else out+=*p;
    }
    return out;
}
std::string optional_string(const char *value) {
    return value && *value ? quote(value) : "null";
}
void host_identity(char *host, size_t host_size, char *boot, size_t boot_size) {
    host[0]=boot[0]=0;
#if defined(__linux__)
    if(gethostname(host,host_size-1)) host[0]=0;
    host[host_size-1]=0;
    FILE *f=std::fopen("/proc/sys/kernel/random/boot_id","rb");
    if(f) {
        if(!std::fgets(boot,(int)boot_size,f)) boot[0]=0;
        std::fclose(f);
        size_t n=std::strlen(boot);
        while(n && (boot[n-1]=='\n' || boot[n-1]=='\r')) boot[--n]=0;
    }
#else
    // A Windows steady-clock value alone does not establish common host/boot
    // identity. Leave unavailable identity explicit rather than fabricating it.
    (void)host_size; (void)boot_size;
#endif
}
// Metadata is bounded to one Record. Parse before embedding it in JSONL: a
// malformed argument must never remove an otherwise valid begin/end pair.
class JsonObject {
    const unsigned char *p, *end;
    void ws() { while(p!=end && (*p==' ' || *p=='\t' || *p=='\r' || *p=='\n')) ++p; }
    bool take(unsigned char c) { if(p==end || *p!=c) return false; ++p; return true; }
    bool hex4(unsigned &value) {
        value=0;
        for(int i=0;i<4;++i) {
            if(p==end) return false;
            unsigned c=*p++, d=c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:c>='A'&&c<='F'?c-'A'+10:16;
            if(d==16) return false;
            value=value*16+d;
        }
        return true;
    }
    bool string() {
        if(!take('"')) return false;
        while(p!=end) {
            unsigned c=*p++;
            if(c=='"') return true;
            if(c<32) return false;
            if(c=='\\') {
                if(p==end) return false;
                c=*p++;
                if(c=='u') {
                    unsigned u;
                    if(!hex4(u)) return false;
                    if(u>=0xd800 && u<=0xdbff) {
                        if(!take('\\') || !take('u') || !hex4(u) || u<0xdc00 || u>0xdfff) return false;
                    } else if(u>=0xdc00 && u<=0xdfff) return false;
                } else if(c!='"' && c!='\\' && c!='/' && c!='b' && c!='f' && c!='n' && c!='r' && c!='t') return false;
            } else if(c>=128) {
                unsigned n=c>=0xc2&&c<=0xdf?1:c>=0xe0&&c<=0xef?2:c>=0xf0&&c<=0xf4?3:0;
                if(!n || (size_t)(end-p)<n) return false;
                unsigned cp=c&((1u<<(6-n))-1);
                for(unsigned i=0;i<n;++i) { unsigned d=*p++; if((d&0xc0)!=0x80) return false; cp=(cp<<6)|(d&0x3f); }
                if((n==1&&cp<0x80)||(n==2&&cp<0x800)||(n==3&&cp<0x10000)||cp>0x10ffff||(cp>=0xd800&&cp<=0xdfff)) return false;
            }
        }
        return false;
    }
    bool number() {
        take('-');
        char digits[768]; int significant=0, fractional=0, exponent=0;
        const auto digit=[&](unsigned c) {
            if(c || significant) digits[significant++]=(char)('0'+c);
        };
        if(p==end) return false;
        if(*p=='0') ++p;
        else { if(*p<'1'||*p>'9') return false; do { digit(*p++-'0'); } while(p!=end&&*p>='0'&&*p<='9'); }
        if(take('.')) {
            if(p==end||*p<'0'||*p>'9') return false;
            do { digit(*p++-'0'); ++fractional; } while(p!=end&&*p>='0'&&*p<='9');
        }
        if(p!=end&&(*p=='e'||*p=='E')) {
            ++p; bool negative=take('-'); if(!negative) take('+');
            if(p==end||*p<'0'||*p>'9') return false;
            do { exponent=exponent<100000?exponent*10+(*p-'0'):1000000; ++p; } while(p!=end&&*p>='0'&&*p<='9');
            if(negative) exponent=-exponent;
        }
        if(!significant) return true;
        // The binary64 round-to-nearest overflow threshold is the exact
        // integer 2^1024 - 2^970. At the tie the result is infinity. Compare
        // decimal digits directly: floating approximations of long mantissas
        // can incorrectly accept values immediately above that threshold.
        static const char limit[]=
            "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497792";
        const int whole_digits=significant+exponent-fractional;
        if(whole_digits!=309) return whole_digits<309;
        for(int i=0;i<significant || i<309;++i) {
            const char actual=i<significant?digits[i]:'0', bound=i<309?limit[i]:'0';
            if(actual!=bound) return actual<bound;
        }
        return false;
    }
    bool literal(const char *text) {
        while(*text) { if(p==end||*p++!=(unsigned char)*text++) return false; }
        return true;
    }
    bool object(unsigned depth) {
        if(!take('{')) return false;
        ws(); if(take('}')) return true;
        do {
            ws(); if(!string()) return false;
            ws(); if(!take(':')||!value(depth+1)) return false;
            ws(); if(take('}')) return true;
        } while(take(','));
        return false;
    }
    bool value(unsigned depth) {
        if(depth>64) return false;
        ws(); if(p==end) return false;
        if(*p=='{') return object(depth);
        if(*p=='[') {
            ++p; ws(); if(take(']')) return true;
            do { if(!value(depth+1)) return false; ws(); if(take(']')) return true; } while(take(','));
            return false;
        }
        if(*p=='"') return string();
        if(*p=='t') return literal("true");
        if(*p=='f') return literal("false");
        if(*p=='n') return literal("null");
        return number();
    }
public:
    JsonObject(const char *text,size_t size) : p((const unsigned char*)text),end(p+size) {}
    bool valid() { ws(); if(!object(0)) return false; ws(); return p==end; }
};
bool valid_args(const char *args) {
    if(!args) return true;
    size_t n=0; while(n<768 && args[n]) ++n;
    return n<768 && JsonObject(args,n).valid();
}
std::string frame_args(const char *id, int complete, const char *extra) {
    std::string out = "{";
    if (extra) {
        const std::string text(extra);
        const size_t first=text.find_first_not_of(" \r\n\t"), last=text.find_last_not_of(" \r\n\t");
        if(first!=std::string::npos && last>first) {
            const std::string body=text.substr(first+1,last-first-1);
            if (body.find_first_not_of(" \r\n\t") != std::string::npos) out += body + ",";
        }
    }
    // Required boundary fields come last so adapter metadata cannot redefine
    // the frame's identity or claim complete driver coverage accidentally.
    out += "\"application_frame_id\":" + quote(id ? id : "") +
           ",\"driver_calls_complete\":" + (complete ? "true" : "false") +
           ",\"frame_boundary\":\"application\",\"wait_annotation_coverage\":\"explicit_sample\"}";
    return out;
}
struct Record {
    int type; uint64_t ts, id, parent, frame, instance; long tid;
    char kind[24], category[16], name[128], role[32], args[768];
    Record() : type(0),ts(0),id(0),parent(0),frame(0),instance(0),tid(0) { kind[0]=category[0]=name[0]=role[0]=0; copy(args,sizeof args,"{}"); }
};
struct ThreadLocal {
    bool registered; size_t depth; uint64_t stack[128], previous_frame[128], frame, instance;
    ThreadLocal() : registered(false),depth(0),frame(0),instance(0) {}
};
thread_local ThreadLocal local;
struct State {
    bool enabled; std::atomic<bool> closed; size_t capacity;
    std::mutex mutex; std::vector<Record> records; std::unordered_set<uint64_t> frame_handles;
    std::atomic<uint64_t> sequence,loss,errors,late;
    uint64_t incomplete, quality_revision; bool capture_written;
    std::string resolved_path, last_quality;
    char path[2048], app[128], build[128];
    State() : enabled(false),closed(false),capacity(32768),sequence(1),loss(0),errors(0),late(0),incomplete(0),quality_revision(0),capture_written(false) {
        path[0]=app[0]=build[0]=0;
        const char *p=std::getenv("LXTRACE_OUTPUT"), *d=std::getenv("LXTRACE_DISABLED");
        if (!p || !*p || (d && std::strcmp(d,"0"))) return;
        if (std::strlen(p)>=sizeof path) return;
        copy(path,sizeof path,p);
        const char *c=std::getenv("LXTRACE_CAPACITY");
        if(c) { char *e=0; unsigned long long n=std::strtoull(c,&e,10); if(e && !*e && n>=4 && n<=1000000) capacity=(size_t)n; }
        try { records.reserve(capacity); enabled=true; } catch (...) { std::fprintf(stderr,"lxtrace: allocation unavailable; instrumentation disabled\n"); }
    }
    void quality_locked(FILE *stream=0) {
        const bool forked=fork_observed.load(std::memory_order_relaxed);
#if defined(__linux__)
        const bool fork_tracking=fork_hook_registered.load(std::memory_order_acquire);
        const char *fork_status=fork_tracking?"available":"unavailable";
#else
        const bool fork_tracking=true;
        const char *fork_status="not_applicable";
#endif
        const uint64_t dropped=loss.load(), violations=errors.load()+((forked||!fork_tracking)?1:0);
        const bool clean=!dropped && !violations && !incomplete && !forked && fork_tracking;
        char fields[1024];
        std::snprintf(fields,sizeof fields,"\"dropped_records\":%llu,\"protocol_errors\":%llu,\"clean_shutdown\":%s,\"capacity\":%llu,\"late_records\":%llu,\"incomplete_events\":%llu,\"unsupported_fork_count\":%d,\"unsupported_fork_count_is_lower_bound\":true,\"fork_capture_policy\":\"child_disabled_until_exec\",\"fork_tracking_status\":\"%s\",\"clean_shutdown_scope\":\"observed_at_quality_snapshot\"",
                      (unsigned long long)dropped,(unsigned long long)violations,clean?"true":"false",(unsigned long long)capacity,(unsigned long long)late.load(),(unsigned long long)incomplete,forked?1:0,fork_status);
        if(last_quality==fields) return;
        const bool own_stream=!stream;
        if(own_stream) { if(!capture_written) return; stream=std::fopen(resolved_path.c_str(),"ab"); }
        if(!stream) { std::fprintf(stderr,"lxtrace: cannot append capture quality; application continues\n"); return; }
        const int written=std::fprintf(stream,"{\"type\":\"quality\",\"quality_semantics\":\"cumulative_snapshot\",\"quality_revision\":%llu,%s}\n",(unsigned long long)++quality_revision,fields);
        const bool failed=written<0 || std::ferror(stream);
        const bool close_failed=own_stream && std::fclose(stream)!=0;
        if(!failed && !close_failed) last_quality=fields;
        else std::fprintf(stderr,"lxtrace: capture quality write incomplete; application continues\n");
    }
    bool reject_closed() noexcept {
        if(!closed.load(std::memory_order_acquire)) return false;
        bool counted=false;
        try { std::lock_guard<std::mutex> g(mutex); ++loss; ++late; counted=true; quality_locked(); }
        catch(...) { if(!counted) { ++loss; ++late; } std::fprintf(stderr,"lxtrace: late record quality unavailable; application continues\n"); }
        return true;
    }
    void error() noexcept {
        ++errors;
        if(closed.load(std::memory_order_acquire)) {
            try { std::lock_guard<std::mutex> g(mutex); quality_locked(); }
            catch(...) { std::fprintf(stderr,"lxtrace: error quality unavailable; application continues\n"); }
        }
    }
    void put(Record &r) noexcept {
        if(!enabled) return;
        try { std::lock_guard<std::mutex> g(mutex); if(closed) { ++loss; ++late; quality_locked(); return; } if(records.size()>=capacity) ++loss; else { records.push_back(r); if (r.type==1 && !std::strcmp(r.kind,"frame")) frame_handles.insert(r.id); } }
        catch(...) { ++loss; }
    }
    void finish() noexcept {
        if(!enabled) return;
        try {
            std::lock_guard<std::mutex> g(mutex); if(closed) { quality_locked(); return; } closed=true;
            std::unordered_set<uint64_t> open;
            for(size_t i=0;i<records.size();++i) {
                if(records[i].type==1) open.insert(records[i].id);
                else if(records[i].type==2) open.erase(records[i].id);
            }
            incomplete=open.size(); resolved_path=output_path(path);
            FILE *f=std::fopen(resolved_path.c_str(),"wb");
            if(!f) { std::fprintf(stderr,"lxtrace: cannot write capture; application continues\n"); return; }
#if defined(_WIN32)
            const char *clock="STEADY_CLOCK_WINDOWS";
#else
            const char *clock="CLOCK_MONOTONIC";
#endif
            char host[256], boot[128]; host_identity(host,sizeof host,boot,sizeof boot);
            std::fprintf(f,"{\"type\":\"meta\",\"schema_version\":\"lxtrace.sdk.v1\",\"clock_id\":%s,\"app_name\":%s,\"build_id\":%s,\"pid\":%ld,\"timestamp_unit\":\"ns\",\"run_id\":%s,\"host_id\":%s,\"boot_id\":%s}\n",quote(clock).c_str(),quote(app).c_str(),quote(build).c_str(),process_id(),optional_string(std::getenv("LXTRACE_RUN_ID")).c_str(),optional_string(host).c_str(),optional_string(boot).c_str());
            for(size_t i=0;i<records.size();++i) {
                const Record &r=records[i];
                std::string id=std::to_string(process_id())+":"+std::to_string(r.id);
                std::string th=std::to_string(process_id())+":"+std::to_string(r.tid)+":"+std::to_string(r.instance);
                if(r.type==0) {
                    std::fprintf(f,"{\"type\":\"thread\",\"thread_id\":%s,\"pid\":%ld,\"tid\":%ld,\"name\":%s,\"role\":%s}\n",quote(th.c_str()).c_str(),process_id(),r.tid,quote(r.name).c_str(),quote(r.role).c_str()); continue;
                }
                if(r.type==2) { std::fprintf(f,"{\"type\":\"end\",\"event_id\":%s,\"ts\":%llu,\"tid\":%ld}\n",quote(id.c_str()).c_str(),(unsigned long long)r.ts,r.tid); continue; }
                std::string par=r.parent ? quote((std::to_string(process_id())+":"+std::to_string(r.parent)).c_str()) : "null";
                std::string fr=r.frame ? quote((std::to_string(process_id())+":"+std::to_string(r.frame)).c_str()) : "null";
                const char *type=r.type==1?"begin":r.type==3?"instant":"counter";
                std::fprintf(f,"{\"type\":\"%s\",\"event_id\":%s,\"thread_id\":%s,\"pid\":%ld,\"tid\":%ld,\"kind\":%s,\"category\":%s,\"name\":%s,\"ts\":%llu,\"parent_id\":%s,\"frame_id\":%s,\"args\":%s}\n",type,quote(id.c_str()).c_str(),quote(th.c_str()).c_str(),process_id(),r.tid,quote(r.kind).c_str(),quote(r.category).c_str(),quote(r.name).c_str(),(unsigned long long)r.ts,par.c_str(),fr.c_str(),r.args);
            }
            if(std::ferror(f)) ++errors;
            capture_written=true;
            quality_locked(f);
            if(std::fclose(f)) { ++errors; std::fprintf(stderr,"lxtrace: capture write incomplete; application continues\n"); quality_locked(); }
        } catch(...) { std::fprintf(stderr,"lxtrace: capture flush failed; application continues\n"); }
    }
};
State *state() noexcept {
    if (!current_process_allowed()) return 0;
    static State *s=[]()->State* { try { State *p=new State(); std::atexit(lx_shutdown); return p; } catch(...) { return 0; } }();
    return s;
}
void ensure_thread(State *s) {
    if(local.registered) return;
    if(!local.instance) local.instance=s->sequence++;
    Record r; r.type=0; r.tid=thread_id(); r.instance=local.instance; copy(r.name,sizeof r.name,"worker"); copy(r.role,sizeof r.role,"worker"); s->put(r); local.registered=true;
}
void args_copy(State *s, Record &r, const char *args) {
    if(!args) return;
    if(!valid_args(args)) { s->error(); copy(r.args,sizeof r.args,"{\"invalid_args\":true}"); return; }
    copy(r.args,sizeof r.args,args);
    // Legal JSON whitespace may contain real newlines outside strings. The
    // container is JSONL, so keep the event on one physical line.
    for(char *p=r.args; *p; ++p) if(*p=='\n'||*p=='\r'||*p=='\t') *p=' ';
}
lx_handle begin(const char *kind,const char *name,const char *category,uint64_t parent,const char *args,bool async=false) noexcept {
    State *s=state(); if(!s || !s->enabled || s->reject_closed()) return 0;
    ensure_thread(s);
    if(!async && local.depth>=128) { s->error(); return 0; }
    Record r; r.type=1; r.ts=stamp(); r.id=s->sequence++; r.tid=thread_id(); r.instance=local.instance;
    r.parent=parent?parent:(local.depth?local.stack[local.depth-1]:0); r.frame=local.frame;
    copy(r.kind,sizeof r.kind,kind); copy(r.name,sizeof r.name,name); copy(r.category,sizeof r.category,category); args_copy(s,r,args);
    const uint64_t previous_frame=local.frame;
    if(!std::strcmp(kind,"frame")) { local.frame=r.id; r.frame=r.id; }
    if(!async) { local.previous_frame[local.depth]=previous_frame; local.stack[local.depth++]=r.id; }
    s->put(r); return r.id;
}
}
extern "C" {
void lx_init(const char *app,const char *build) { State *s=state(); if(!s || !s->enabled || s->reject_closed()) return; try { std::lock_guard<std::mutex> g(s->mutex); if(s->closed) { ++s->loss; ++s->late; s->quality_locked(); return; } copy(s->app,sizeof s->app,app); copy(s->build,sizeof s->build,build); } catch(...) { s->error(); } }
void lx_thread_role(const char *name,const char *role) { State *s=state(); if(!s || !s->enabled || s->reject_closed()) return; if(!local.instance) local.instance=s->sequence++; Record r; r.tid=thread_id(); r.instance=local.instance; copy(r.name,sizeof r.name,name); copy(r.role,sizeof r.role,role); s->put(r); local.registered=true; }
lx_handle lx_frame_begin(const char *id,int complete) { return lx_frame_begin_ex(id, complete, "{}"); }
lx_handle lx_frame_begin_ex(const char *id,int complete,const char *extra) {
    if (!current_process_allowed()) return 0;
    try {
        State *s=state(); if(!s || !s->enabled) return 0;
        const char *safe=extra;
        if(!valid_args(extra)) { s->error(); safe="{\"invalid_args\":true}"; }
        std::string a = frame_args(id, complete, safe);
        if (a.size() >= 768) { s->error(); a=frame_args(id,complete,"{\"invalid_args\":true}"); }
        if(a.size() >= 768) return 0;
        return begin("frame",id,"unknown",0,a.c_str());
    } catch(...) { return 0; }
}
lx_context lx_context_capture(void) { if(!current_process_allowed()) { lx_context context={0,0}; return context; } lx_context context={local.frame,static_cast<uint64_t>(process_id())}; return context; }
int lx_context_attach(lx_context context,lx_context *previous) {
    if (!current_process_allowed()) return -1;
    State *s=state();
    if (context.process_id != static_cast<uint64_t>(process_id())) { if(s) s->error(); return -1; }
    if (s && s->enabled && !s->closed && context.frame) {
        std::lock_guard<std::mutex> guard(s->mutex);
        if (s->frame_handles.find(context.frame)==s->frame_handles.end()) { ++s->errors; if(s->closed) s->quality_locked(); return -1; }
    }
    if (previous) *previous=lx_context_capture();
    local.frame=context.frame;
    return 0;
}
lx_handle lx_range_begin(const char *name,const char *category,lx_handle parent,const char *args) { return begin("range",name,category,parent,args); }
lx_handle lx_wait_begin(const char *name,const char *object) { if(!current_process_allowed()) return 0; try { std::string a="{\"wait_object\":"+quote(object?object:"")+"}"; return begin("wait",name,"wait",0,a.c_str()); } catch(...) { return 0; } }
lx_handle lx_exchange_begin(const char *name,const char *args) { return begin("exchange",name,"cx",0,args); }
void lx_range_end(lx_handle h) { if(!h) return; State *s=state(); if(!s || !s->enabled || s->reject_closed()) return; if(!local.depth || local.stack[local.depth-1]!=h) { s->error(); return; } --local.depth; local.frame=local.previous_frame[local.depth]; Record r; r.type=2; r.id=h; r.ts=stamp(); r.tid=thread_id(); r.instance=local.instance; s->put(r); }
void lx_frame_end(lx_handle h) { lx_range_end(h); }
lx_handle lx_async_begin_transport(const char *name,const char *correlation,const char *peer,uint64_t bytes,const char *transport) { if(!current_process_allowed()) return 0; try { std::string a="{\"correlation_id\":"+quote(correlation?correlation:"")+",\"peer\":"+quote(peer?peer:"")+",\"bytes\":"+std::to_string(bytes)+",\"submit_uncertainty_ns\":null,\"completion_uncertainty_ns\":null,\"transport\":"+quote(transport?transport:"unknown")+"}"; return begin("request",name,"cx",0,a.c_str(),true); } catch(...) { return 0; } }
lx_handle lx_async_begin(const char *name,const char *correlation,const char *peer,uint64_t bytes) { return lx_async_begin_transport(name,correlation,peer,bytes,"software_queue"); }
void lx_async_complete_observed(lx_handle h) { if(!h) return; State *s=state(); if(!s || !s->enabled || s->reject_closed()) return; ensure_thread(s); Record r; r.type=2; r.id=h; r.ts=stamp(); r.tid=thread_id(); r.instance=local.instance; s->put(r); }
void lx_instant(const char *name,const char *args) { State *s=state(); if(!s || !s->enabled || s->reject_closed()) return; ensure_thread(s); Record r; r.type=3; r.id=s->sequence++; r.tid=thread_id(); r.instance=local.instance; r.ts=stamp(); r.frame=local.frame; r.parent=local.depth?local.stack[local.depth-1]:0; copy(r.kind,sizeof r.kind,"instant"); copy(r.name,sizeof r.name,name); copy(r.category,sizeof r.category,"unknown"); args_copy(s,r,args); s->put(r); }
void lx_counter(const char *name,double value) { State *s=state(); if(!s || !s->enabled || s->reject_closed()) return; if(!std::isfinite(value)) { s->error(); return; } ensure_thread(s); Record r; r.type=4; r.id=s->sequence++; r.tid=thread_id(); r.instance=local.instance; r.ts=stamp(); r.frame=local.frame; r.parent=local.depth?local.stack[local.depth-1]:0; copy(r.kind,sizeof r.kind,"counter"); copy(r.name,sizeof r.name,name); copy(r.category,sizeof r.category,"unknown"); std::snprintf(r.args,sizeof r.args,"{\"value\":%.17g}",value); s->put(r); }
void lx_shutdown(void) { State *s=state(); if(s) s->finish(); }
}
