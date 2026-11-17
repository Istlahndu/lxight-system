#include "lxtrace_compact.h"
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <memory>
#include <string>
#include <thread>
#include <vector>
#include <fcntl.h>
#include <sys/stat.h>
#if defined(_WIN32)
#include <io.h>
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
const uint32_t MAGIC=0x3143584c, TRAILER=0x4543584c, MARKER=0xffffffffu;
static_assert(ATOMIC_LLONG_LOCK_FREE==2,"compact rings require lock-free uint64 atomics");
static_assert(ATOMIC_LONG_LOCK_FREE==2&&ATOMIC_BOOL_LOCK_FREE==2&&ATOMIC_INT_LOCK_FREE==2,
              "compact rings and fork guards require lock-free native atomics");
static_assert(ATOMIC_POINTER_LOCK_FREE==2,"compact hot path requires lock-free state pointer");
long pid() {
#if defined(_WIN32)
    return _getpid();
#else
    return getpid();
#endif
}
long tid() {
#if defined(_WIN32)
    return GetCurrentThreadId();
#else
    return syscall(SYS_gettid);
#endif
}
uint64_t clock_ns() {
#if defined(_WIN32)
    return (uint64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
#else
    timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return (uint64_t)t.tv_sec*1000000000ull+t.tv_nsec;
#endif
}
void u16(unsigned char *p,uint16_t n) { p[0]=(unsigned char)n;p[1]=(unsigned char)(n>>8); }
void u32(unsigned char *p,uint32_t n) { for(int i=0;i<4;++i)p[i]=(unsigned char)(n>>(8*i)); }
void u64(unsigned char *p,uint64_t n) { for(int i=0;i<8;++i)p[i]=(unsigned char)(n>>(8*i)); }
uint16_t r16(const unsigned char *p) { return (uint16_t)p[0]|((uint16_t)p[1]<<8); }
uint32_t r32(const unsigned char *p) { uint32_t n=0;for(int i=0;i<4;++i)n|=(uint32_t)p[i]<<(8*i);return n; }
uint32_t crc32(const unsigned char *p,size_t n,uint32_t crc=0xffffffffu) {
    // Only the cold sink invokes this; the producer never initializes a table.
    struct Table {
        uint32_t value[256];
        Table(){for(unsigned i=0;i<256;++i){uint32_t c=i;for(unsigned b=0;b<8;++b)c=(c>>1)^(0xedb88320u&-(int)(c&1));value[i]=c;}}
    };
    static const Table table;
    for(size_t i=0;i<n;++i)crc=table.value[(crc^p[i])&255]^(crc>>8);
    return crc;
}
bool valid_utf8(const char *text,size_t limit) {
    if(!text)return true;
    size_t i=0;
    while(i<limit&&text[i]){
        const unsigned char c=(unsigned char)text[i];
        if(c<128){++i;continue;}
        const unsigned n=c>=0xc2&&c<=0xdf?2:c>=0xe0&&c<=0xef?3:c>=0xf0&&c<=0xf4?4:0;
        if(!n||i+n>limit)return false;
        for(unsigned j=1;j<n;++j)if(!text[i+j]||((unsigned char)text[i+j]&0xc0)!=0x80)return false;
        const unsigned char next=(unsigned char)text[i+1];
        if((c==0xe0&&next<0xa0)||(c==0xed&&next>=0xa0)||(c==0xf0&&next<0x90)||(c==0xf4&&next>=0x90))return false;
        i+=n;
    }
    return true;
}
std::string quote(const char *text,size_t limit=4096) {
    std::string out="\"";if(!text)text="";
    for(size_t i=0;text[i]&&i<limit;++i){const unsigned char c=(unsigned char)text[i];
        if(c=='"'||c=='\\'){out+='\\';out+=(char)c;}
        else if(c<32){char b[7];std::snprintf(b,sizeof b,"\\u%04x",c);out+=b;}
        else if(c<128)out+=(char)c;
        else {
            const unsigned n=c>=0xc2&&c<=0xdf?2:c>=0xe0&&c<=0xef?3:c>=0xf0&&c<=0xf4?4:0;
            bool valid=n&&i+n<=limit;
            if(valid)for(unsigned j=1;j<n;++j)if(!text[i+j]||((unsigned char)text[i+j]&0xc0)!=0x80){valid=false;break;}
            if(valid){const unsigned char next=(unsigned char)text[i+1];valid=!((c==0xe0&&next<0xa0)||(c==0xed&&next>=0xa0)||(c==0xf0&&next<0x90)||(c==0xf4&&next>=0x90));}
            if(valid){out.append(text+i,n);i+=n-1;}else out+="\\ufffd";
        }}
    return out+'"';
}
size_t bounded(const char *s,size_t limit) { if(!s)return 0;size_t n=0;while(n<limit&&s[n])++n;return n; }
std::string hostname_text() {
    char name[256]={0};
#if defined(_WIN32)
    DWORD length=sizeof name-1;if(!GetComputerNameA(name,&length))return "";
#else
    if(gethostname(name,sizeof name-1))return "";
#endif
    return name;
}
std::string boot_id_text() {
#if defined(__linux__)
    char value[128]={0};FILE *f=std::fopen("/proc/sys/kernel/random/boot_id","rb");if(!f)return "";
    const size_t n=std::fread(value,1,sizeof value-1,f);std::fclose(f);
    size_t length=n;while(length&&(value[length-1]=='\n'||value[length-1]=='\r'))--length;
    return std::string(value,length);
#else
    return "";
#endif
}
FILE *exclusive_file(const char *path) {
#if defined(_WIN32)
    const int fd=_open(path,_O_WRONLY|_O_CREAT|_O_EXCL|_O_BINARY,_S_IREAD|_S_IWRITE);
    if(fd<0)return 0;
    FILE *f=_fdopen(fd,"wb");if(!f)_close(fd);return f;
#else
    const int fd=open(path,O_WRONLY|O_CREAT|O_EXCL,0600);
    if(fd<0)return 0;
    FILE *f=fdopen(fd,"wb");if(!f)close(fd);return f;
#endif
}
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
        std::string digits; int significant=0, fractional=0, exponent=0;
        const auto digit=[&](unsigned c) {
            if(c || significant) { digits+=(char)('0'+c); ++significant; }
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
struct Slot { unsigned char b[16]; };
static_assert(sizeof(Slot)==16,"compact event slot must be 16 bytes");
struct Ring {
    const uint32_t id,capacity; const long os_tid;
    Slot *slots;
    std::atomic<uint64_t> head; char head_padding[64];
    std::atomic<uint64_t> tail; char tail_padding[64];
    std::atomic<bool> active; char active_padding[64];
    std::atomic<uint64_t> attempted,dropped,late,first_seq,last_seq,first_ts,last_ts,loss_revision;
    uint64_t producer_attempted;
    uint32_t stack[128],depth;
    std::atomic<uint64_t> incomplete;
    uint64_t saved_loss,saved_late;
    Ring(uint32_t i,uint32_t cap):id(i),capacity(cap),os_tid(tid()),slots(new Slot[cap]),head(0),tail(0),active(false),
        attempted(0),dropped(0),late(0),first_seq(0),last_seq(0),first_ts(0),last_ts(0),loss_revision(0),producer_attempted(0),depth(0),incomplete(0),saved_loss(0),saved_late(0) {}
    ~Ring() { delete[] slots; }
    void loss(uint64_t seq,uint64_t ts,bool is_late=false) {
        loss_revision.fetch_add(1,std::memory_order_acq_rel);
        if(dropped.fetch_add(1,std::memory_order_relaxed)==0){first_seq.store(seq,std::memory_order_relaxed);first_ts.store(ts,std::memory_order_relaxed);}
        last_seq.store(seq,std::memory_order_relaxed);last_ts.store(ts,std::memory_order_relaxed);
        if(is_late)late.fetch_add(1,std::memory_order_relaxed);
        loss_revision.fetch_add(1,std::memory_order_release);
    }
};
struct State;
std::atomic<State*> global_state((State*)0);
std::atomic<long> owner(0);
std::atomic<bool> forked(false);
std::mutex init_mutex;
thread_local Ring *local_ring=0;
#if defined(__linux__)
std::atomic<bool> fork_hook_registered(false);
std::atomic<bool> child_disabled(false);
void fork_parent() { if(owner.load(std::memory_order_relaxed)==pid())forked.store(true,std::memory_order_relaxed); }
void fork_child() { if(owner.load(std::memory_order_relaxed))child_disabled.store(true,std::memory_order_release); }
#endif
bool allowed() {
#if defined(__linux__)
    if(child_disabled.load(std::memory_order_acquire))return false;
    if(fork_hook_registered.load(std::memory_order_acquire))return true;
    const long p=owner.load(std::memory_order_acquire);return !p||p==pid();
#else
    return true;
#endif
}
struct State {
    FILE *file; const uint32_t capacity,period;
    std::atomic<bool> closed,stop,io_error;
    std::atomic<uint32_t> sites;
    std::atomic<uint64_t> errors;
    std::mutex registry,sink,drain,wait_mutex,shutdown;
    std::condition_variable wake;
    std::vector<Ring*> rings;
    std::thread worker;
    uint64_t sequence,flush_revision,last_footer_dropped,last_footer_errors,last_footer_late,last_footer_incomplete; bool last_footer_fork;
    bool footer_written,fork_tracking;
    State(FILE *f,uint32_t c,uint32_t ms):file(f),capacity(c),period(ms),closed(false),stop(false),io_error(false),sites(0),errors(0),
        sequence(0),flush_revision(0),last_footer_dropped(0),last_footer_errors(0),last_footer_late(0),last_footer_incomplete(0),last_footer_fork(false),footer_written(false),fork_tracking(true) {}
    bool chunk(uint16_t type,uint32_t thread_id,uint32_t count,const unsigned char *payload,size_t size) {
        std::lock_guard<std::mutex> guard(sink);
        if(io_error.load(std::memory_order_relaxed))return false;
        unsigned char h[32]={0},end[8];u32(h,MAGIC);u16(h+4,1);u16(h+6,type);u64(h+8,++sequence);u32(h+16,thread_id);u32(h+20,count);u32(h+24,(uint32_t)size);
        uint32_t crc=crc32(payload,size,crc32(h,28))^0xffffffffu;u32(h+28,crc);u32(end,TRAILER);u32(end+4,crc);
        const bool ok=std::fwrite(h,1,sizeof h,file)==sizeof h&&std::fwrite(payload,1,size,file)==size&&std::fwrite(end,1,sizeof end,file)==sizeof end;
        if(!ok)io_error.store(true,std::memory_order_relaxed);
        return ok&&!io_error.load(std::memory_order_relaxed);
    }
    bool json_chunk(uint16_t type,uint32_t thread_id,const std::string &s) { return chunk(type,thread_id,1,(const unsigned char*)s.data(),s.size()); }
    bool sync() {
        std::lock_guard<std::mutex> guard(sink);bool ok=std::fflush(file)==0;
#if defined(_WIN32)
        if(ok)ok=_commit(_fileno(file))==0;
#else
        if(ok)ok=fsync(fileno(file))==0;
#endif
        if(!ok)io_error.store(true,std::memory_order_relaxed);
        return ok&&!io_error.load(std::memory_order_relaxed);
    }
    std::vector<Ring*> snapshot() { std::lock_guard<std::mutex> guard(registry);return rings; }
    void loss_chunk(Ring *ring,bool force=false) {
        unsigned char b[64];uint64_t loss=0,late=0;bool stable=false;
        for(int attempt=0;attempt<8;++attempt){const uint64_t revision=ring->loss_revision.load(std::memory_order_acquire);if(revision&1)continue;
            loss=ring->dropped.load();late=ring->late.load();
            u64(b,loss);u64(b+8,ring->first_seq.load());u64(b+16,ring->last_seq.load());u64(b+24,ring->first_ts.load());u64(b+32,ring->last_ts.load());u64(b+40,ring->attempted.load());u64(b+48,late);u64(b+56,ring->head.load());
            if(revision==ring->loss_revision.load(std::memory_order_acquire)){stable=true;break;}}
        if(!stable||(!force&&loss==ring->saved_loss&&late==ring->saved_late))return;
        if(chunk(5,ring->id,1,b,sizeof b)){ring->saved_loss=loss;ring->saved_late=late;}
    }
    void consume() {
        std::lock_guard<std::mutex> serial(drain);
        const std::vector<Ring*> current=snapshot();std::vector<unsigned char> events;events.reserve(65536);
        for(size_t i=0;i<current.size();++i){Ring *ring=current[i];uint64_t tail=ring->tail.load(std::memory_order_relaxed),head=ring->head.load(std::memory_order_acquire);
            while(tail<head){const Slot &slot=ring->slots[tail%ring->capacity];
                if(r32(slot.b+8)==MARKER){const uint16_t slots=r16(slot.b+14);const size_t bytes=r32(ring->slots[(tail+1)%ring->capacity].b+0); // length prefix of extension
                    if(!slots||tail+slots>head||bytes>416||bytes<32){++errors;tail=head;break;}
                    unsigned char payload[416];for(size_t n=0;n<bytes;++n){const size_t pos=n+4;payload[n]=ring->slots[(tail+1+pos/16)%ring->capacity].b[pos%16];}
                    chunk(7,ring->id,1,payload,bytes);tail+=slots;
                }else{events.clear();while(tail<head&&events.size()<65536){const Slot &record=ring->slots[tail%ring->capacity];if(r32(record.b+8)==MARKER)break;events.insert(events.end(),record.b,record.b+16);++tail;}
                    if(!events.empty())chunk(4,ring->id,(uint32_t)(events.size()/16),events.data(),events.size());}
                ring->tail.store(tail,std::memory_order_release);
            }ring->tail.store(tail,std::memory_order_release);loss_chunk(ring);
        }
    }
    void background() {
        while(!stop.load(std::memory_order_acquire)){try{consume();sync();}catch(...){++errors;}
            std::unique_lock<std::mutex> lock(wait_mutex);wake.wait_for(lock,std::chrono::milliseconds(period),[this]{return stop.load();});}
        try{consume();sync();}catch(...){++errors;}
    }
    void footer() {
        std::lock_guard<std::mutex> serial(drain);
        uint64_t dropped=0,late=0,attempts=0,records=0,incomplete=0;const std::vector<Ring*> current=snapshot();
        for(size_t i=0;i<current.size();++i){Ring *ring=current[i];dropped+=ring->dropped.load();late+=ring->late.load();attempts+=ring->attempted.load();records+=ring->head.load();incomplete+=ring->incomplete.load();}
        const uint64_t violations=errors.load();const bool f=forked.load(),clean=!dropped&&!violations&&!incomplete&&!io_error.load()&&!f&&fork_tracking;
        if(footer_written&&dropped==last_footer_dropped&&late==last_footer_late&&violations==last_footer_errors&&incomplete==last_footer_incomplete&&f==last_footer_fork)return;
        for(size_t i=0;i<current.size();++i)loss_chunk(current[i],true);
        std::string s="{\"quality_semantics\":\"cumulative_snapshot\",\"quality_revision\":"+std::to_string(++flush_revision)+",\"dropped_records\":"+std::to_string(dropped)+",\"late_records\":"+std::to_string(late)+",\"protocol_errors\":"+std::to_string(violations)+",\"attempted_records\":"+std::to_string(attempts)+",\"published_slots\":"+std::to_string(records)+",\"clean_shutdown\":"+(clean?"true":"false")+",\"fork_capture_policy\":\"child_disabled_until_exec\",\"unsupported_fork_count\":"+(f?"1":"0")+",\"fork_tracking_status\":\""+(fork_tracking?"available":"unavailable")+"\",\"io_error\":"+(io_error.load()?"true":"false")+"}";
        s.insert(s.size()-1,",\"incomplete_events\":"+std::to_string(incomplete)+",\"max_threads\":1024,\"max_scope_depth\":128");
        if(json_chunk(6,0,s)){footer_written=true;last_footer_dropped=dropped;last_footer_late=late;last_footer_errors=violations;last_footer_incomplete=incomplete;last_footer_fork=f;}
    }
};
State *state() { if(!allowed())return 0;return global_state.load(std::memory_order_acquire); }
int publish(State *s,Ring *r,const unsigned char *payload,size_t size,bool extension,uint64_t ts) {
    if(!r)return -1;
    r->active.store(true,std::memory_order_seq_cst);
    const uint64_t seq=++r->producer_attempted;r->attempted.store(seq,std::memory_order_relaxed);
    if(s->closed.load(std::memory_order_seq_cst)){r->loss(seq,ts,true);r->active.store(false,std::memory_order_release);return -1;}
    const uint32_t site=r32(payload+8);const uint16_t kind=r16(payload+12);
    if(kind==LX_COMPACT_BEGIN){
        if(r->depth==128){++s->errors;r->active.store(false,std::memory_order_release);return -1;}
        r->stack[r->depth++]=site;r->incomplete.store(r->depth,std::memory_order_relaxed);
    }else if(kind==LX_COMPACT_END){
        if(!r->depth||r->stack[r->depth-1]!=site){++s->errors;r->active.store(false,std::memory_order_release);return -1;}
        --r->depth;r->incomplete.store(r->depth,std::memory_order_relaxed);
    }
    const size_t slots=extension?1+(size+4+15)/16:1;
    const uint64_t head=r->head.load(std::memory_order_relaxed),tail=r->tail.load(std::memory_order_acquire);
    if(head-tail+slots>r->capacity){r->loss(seq,ts);r->active.store(false,std::memory_order_release);return -1;}
    if(extension){Slot marker={};u64(marker.b,ts);u32(marker.b+8,MARKER);u16(marker.b+12,7);u16(marker.b+14,(uint16_t)slots);r->slots[head%r->capacity]=marker;
        for(size_t i=0;i<slots-1;++i)std::memset(r->slots[(head+1+i)%r->capacity].b,0,16);
        unsigned char length[4];u32(length,(uint32_t)size);for(size_t n=0;n<size+4;++n){const unsigned char c=n<4?length[n]:payload[n-4];r->slots[(head+1+n/16)%r->capacity].b[n%16]=c;}
    }else std::memcpy(r->slots[head%r->capacity].b,payload,16);
    r->head.store(head+slots,std::memory_order_release);r->active.store(false,std::memory_order_release);return 0;
}
}

extern "C" {
int lx_compact_init_env(uint32_t capacity,uint32_t period) {
    if(!allowed())return -1;
    const char *disabled=std::getenv("LXTRACE_DISABLED"),*pattern=std::getenv("LXTRACE_COMPACT_OUTPUT");
    if((disabled&&!std::strcmp(disabled,"1"))||!pattern||!pattern[0])return 1;
    try{
        if(bounded(pattern,32768)>=32768)return -1;
        std::string path;const std::string process=std::to_string(pid());
        for(size_t i=0;pattern[i];++i){
            if(pattern[i]=='%'&&pattern[i+1]=='p'){path+=process;++i;}
            else path+=pattern[i];
        }
        return lx_compact_init(path.c_str(),capacity,period);
    }catch(...){return -1;}
}
int lx_compact_init(const char *path,uint32_t capacity,uint32_t period) {
    if(!allowed()||!path||!path[0]||capacity<64||capacity>1048576||period<1||period>60000)return -1;
#if defined(__linux__)
    // Register before claiming ownership and before any inheritable mutex.
    // Concurrent first init attempts may register this idempotent callback twice.
    if(!fork_hook_registered.load(std::memory_order_acquire)&&pthread_atfork(0,fork_parent,fork_child)==0)
        fork_hook_registered.store(true,std::memory_order_release);
#endif
    long unowned=0;
    if(!owner.compare_exchange_strong(unowned,pid(),std::memory_order_acq_rel)&&unowned!=pid())return -1;
    try{std::lock_guard<std::mutex> guard(init_mutex);if(global_state.load())return -1;FILE *f=exclusive_file(path);if(!f)return -1;
        State *s=0;try{s=new State(f,capacity,period);}catch(...){std::fclose(f);return -1;}
#if defined(__linux__)
        s->fork_tracking=fork_hook_registered.load(std::memory_order_acquire);
#endif
        const std::string clock=
#if defined(_WIN32)
            "STEADY_CLOCK_WINDOWS";
#else
            "CLOCK_MONOTONIC";
#endif
        const std::string host=hostname_text(),boot=boot_id_text();
        const std::string meta="{\"schema_version\":\"lxtrace.compact.v1\",\"pid\":"+std::to_string(pid())+",\"clock_id\":"+quote(clock.c_str())+",\"host_id\":"+quote(host.c_str())+",\"boot_id\":"+quote(boot.c_str())+",\"record_bytes\":16,\"ring_records\":"+std::to_string(capacity)+",\"flush_ms\":"+std::to_string(period)+",\"loss_semantics\":\"per_thread_attempt_sequence_bounds\",\"run_id\":"+quote(std::getenv("LXTRACE_RUN_ID"))+"}";
        if(!s->json_chunk(1,0,meta)||!s->sync()){std::fclose(f);delete s;return -1;}
        try{s->worker=std::thread([s]{s->background();});}catch(...){std::fclose(f);delete s;return -1;}
        global_state.store(s,std::memory_order_release);if(std::atexit(lx_compact_shutdown))++s->errors;return 0;
    }catch(...){return -1;}
}
uint32_t lx_compact_site(const char *name,const char *category,const char *args) {
    State *s=state();if(!s)return 0;
    try{std::lock_guard<std::mutex> guard(s->registry);if(s->closed.load()||s->sites.load()>=MARKER-1)return 0;
        const bool invalid=bounded(name,256)>=256||bounded(category,32)>=32||bounded(args,768)>=768;
        const bool invalid_args=!valid_args(args);
        const bool invalid_text=!valid_utf8(name,255)||!valid_utf8(category,31)||!valid_utf8(args,767);
        if(invalid||invalid_text||invalid_args)++s->errors;
        const uint32_t id=s->sites.load()+1;
        const std::string text="{\"site_id\":"+std::to_string(id)+",\"name\":"+quote(name,255)+",\"category\":"+quote(category,31)+",\"args_json\":"+quote(invalid||invalid_args?"{\"invalid_args\":true}":args?args:"{}",767)+",\"invalid_text\":"+(invalid_text?"true":"false")+"}";
        if(!s->json_chunk(2,0,text))return 0;
        s->sites.store(id,std::memory_order_release);return id;
    }catch(...){++s->errors;return 0;}
}
int lx_compact_metadata(const char *json) {
    State *s=state();if(!s)return -1;
    try{std::lock_guard<std::mutex> guard(s->registry);const size_t size=bounded(json,65536);
        if(s->closed.load()||size>=65536||(json&&!JsonObject(json,size).valid())){++s->errors;return -1;}
        const bool invalid_text=!valid_utf8(json,65535);if(invalid_text)++s->errors;
        return s->json_chunk(8,0,"{\"metadata_json\":"+quote(json?json:"{}",65535)+",\"invalid_text\":"+(invalid_text?"true":"false")+"}")?0:-1;
    }catch(...){++s->errors;return -1;}
}
int lx_compact_thread(const char *name) {
    State *s=state();if(!s)return -1;if(local_ring)return s->closed.load()?-1:(int)local_ring->id;
    try{std::lock_guard<std::mutex> guard(s->registry);if(s->closed.load())return -1;
        if(s->rings.size()>=1024){++s->errors;return -1;}
        std::unique_ptr<Ring> r(new Ring((uint32_t)s->rings.size()+1,s->capacity));
        const bool invalid=bounded(name,256)>=256||!valid_utf8(name,255);if(invalid)++s->errors;
        const std::string meta="{\"thread_id\":"+std::to_string(r->id)+",\"tid\":"+std::to_string(r->os_tid)+",\"name\":"+quote(name,255)+",\"invalid_text\":"+(invalid?"true":"false")+"}";
        s->rings.push_back(r.get());
        if(!s->json_chunk(3,r->id,meta)){s->rings.pop_back();return -1;}
        local_ring=r.release();return (int)local_ring->id;
    }catch(...){++s->errors;return -1;}
}
int lx_compact_event(uint32_t site,uint16_t kind,uint16_t aux,uint64_t ts) {
    State *s=state();if(!s)return -1;if(!local_ring||!site||site>s->sites.load(std::memory_order_acquire)||kind<1||kind>3){++s->errors;return -1;}
    if(!ts)ts=clock_ns();
    unsigned char payload[16];u64(payload,ts);u32(payload+8,site);u16(payload+12,kind);u16(payload+14,aux);return publish(s,local_ring,payload,sizeof payload,false,ts);
}
int lx_compact_pmu(uint32_t site,uint16_t kind,uint16_t aux,uint64_t ts,uint32_t count,const uint64_t *values,const uint64_t *enabled,const uint64_t *running) {
    if(!ts)ts=clock_ns();
    return lx_compact_pmu_window(site,kind,aux,ts,ts,count,values,enabled,running);
}
int lx_compact_pmu_window(uint32_t site,uint16_t kind,uint16_t aux,uint64_t ts,uint64_t read_end,uint32_t count,const uint64_t *values,const uint64_t *enabled,const uint64_t *running) {
    State *s=state();if(!s)return -1;if(!local_ring||!site||site>s->sites.load(std::memory_order_acquire)||kind<1||kind>3||count>16||!count||!values||!enabled||!running){++s->errors;return -1;}
    if(!ts||read_end<ts){++s->errors;return -1;}
    unsigned char payload[416];u64(payload,ts);u32(payload+8,site);u16(payload+12,kind);u16(payload+14,aux);u32(payload+16,count);u32(payload+20,0);u64(payload+24,read_end);
    for(uint32_t i=0;i<count;++i){u64(payload+32+i*24,values[i]);u64(payload+40+i*24,enabled[i]);u64(payload+48+i*24,running[i]);}
    return publish(s,local_ring,payload,32+count*24,true,ts);
}
int lx_compact_flush(void) { State *s=state();if(!s)return -1;try{s->consume();return s->sync()?0:-1;}catch(...){++s->errors;return -1;} }
void lx_compact_shutdown(void) {
    State *s=state();if(!s)return;
    try{std::lock_guard<std::mutex> serial(s->shutdown);const bool first=!s->closed.exchange(true,std::memory_order_seq_cst);if(first){const std::vector<Ring*> rings=s->snapshot();for(size_t i=0;i<rings.size();++i)while(rings[i]->active.load(std::memory_order_acquire))std::this_thread::yield();
            s->stop.store(true,std::memory_order_release);s->wake.notify_all();if(s->worker.joinable())s->worker.join();}
        s->consume();s->footer();s->sync();
    }catch(...){++s->errors;}
}
}
