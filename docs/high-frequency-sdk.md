# 高频采集与边界 PMU

高频路径用于多线程推理执行器，提供每线程独立缓冲、16 字节普通事件、后台分段持久化及异常退出后的有效前缀恢复。它是可选的 C ABI，原 `lxtrace.h`、JSONL、GFTRACE1/2 导入与生产归因接口继续可用。原 JSONL 接口仍适合动态元数据、跨线程异步请求和上下文传递；需要高频记录的同步入口应接入 `lxtrace_compact.h`。

## 编译与生成报告

编译器需要支持 C++11 和线程库，Python 端无新增依赖：

```bash
mkdir -p build
g++ -std=c++11 -O2 -pthread -Isdk sdk/lxtrace_compact.cpp \
  examples/compact_sdk.cpp -o build/compact_sdk

python3 -m lxtrace record --profile compact \
  --output artifacts/run/session.lxtrace -- ./build/compact_sdk
```

每次使用新的输出路径。命令自动生成 `session.html`，支持原有多轨时间线、悬停详情、框选放大与水平滚动。程序必须先调用 `lx_compact_init_env`，采集器将 `LXTRACE_COMPACT_OUTPUT` 与共同的 `LXTRACE_RUN_ID` 传递给各进程，`%p` 展开为 PID。没有输出变量或 `LXTRACE_DISABLED=1` 时禁用采集，业务继续执行。

直接使用 `lx_compact_init(path, ring_records, flush_ms)` 时，路径按字面解释，且排他创建；多进程必须各写独立文件。已有文件不会覆盖。接入的应用自己处理返回值；采集失败不应破坏原有推理结果。

独立采集文件可经统一入口生成报告，无需临时编写 HTML：

```bash
python3 -m lxtrace report capture.lxc --output report.html \
  --session-output session.lxtrace
python3 -m lxtrace import --type sdk --input capture.lxc --output session.json
```

导入器根据文件头识别格式，扩展名不决定文件内容。原始二进制以 Base64 与 SHA256 证据保存在原生会话中。

长任务可先做流式检查，再选择完整线程生成 HTML：

```bash
python3 -m lxtrace compact-info capture.lxc --output capture-info.json
python3 -m lxtrace compact-select capture.lxc --thread 1 --thread 5 \
  --output selected.lxc
python3 -m lxtrace report selected.lxc --output selected.html
```

`compact-info` 逐段检查 CRC、序号、线程、站点、配对、计数和结束质量，内存不随事件总量线性增长。`--thread` 使用摘要中的 compact 线程实例 ID，不是操作系统 TID 或 CPU 号。选轨保留所选线程的完整时间范围、原文件 SHA256、选择范围与原始质量，原文件保持不变。选择结果中的原始 footer 计数仍代表源文件，不应解释为所选线程的独立统计；选轨不表示完整进程或全机覆盖。

当前交互报告仍在内存中归一化记录，默认限制为 128 MiB 二进制及 100 万条估算记录预算，PMU 读数按其展开后的样本与计数窗计算预算。超限会明确报错并保留原始文件，不会静默抽样。选轨后仍超限时需要进一步减少线程；流式摘要可以检查全量数据，尚不支持直接交互浏览任意大小的全量时间线。

## 接入原则

初始化、站点注册、线程注册和元数据写入属于冷路径。为每个静态入口只注册一次站点，在每个生产者线程开始工作时注册一次线程。站点保存名称、类别和 JSON 参数；热路径仅保存时间戳、站点 ID、BEGIN/END/INSTANT 与 16 位 aux。

```c
uint32_t gemm = lx_compact_site("dense.gemm", "mat", "{\"operator\":\"gate_up\"}");
/* 在各工作线程中： */
if (lx_compact_thread("executor.worker") < 0) { /* 记录接入失败 */ }
lx_compact_event(gemm, LX_COMPACT_BEGIN, 0, 0);
/* 原计算调用 */
lx_compact_event(gemm, LX_COMPACT_END, 0, 0);
```

类别使用 `mat/vec/mem/cc/cx/fe/wait/unknown`。参数 `kind` 可声明 `frame/range/wait/exchange`，例如 `{"kind":"wait"}`。`aux` 是应用自定义的短整数标签，可用于相位号；它不是异步请求 ID，也不能存放未经说明的截断字节数。

同一线程的同步范围必须严格嵌套，结束使用相同站点。跨线程计算与等待可以在时间上重叠；每个线程都有独立缓冲。高频接口不支持用 BEGIN/END 表达跨线程异步生命周期，此类请求继续使用原 SDK 的异步接口。线程名或 rank 不构成绑核证据，软件范围不表示硬件单元持续繁忙。

普通记录时间为 CLOCK_MONOTONIC 整数纳秒，Windows 使用 steady_clock；时间参数为 0 时由 SDK 读取。传入非零值时，调用方必须保证属于同一时钟。不要将 CNTVCT tick 或 PMU 计数直接填入该字段。GFTRACE2 原生 CNTVCT 的整数换算路径继续独立工作。

## 缓冲、落盘与异常恢复

每线程环形缓冲使用单生产者、单消费者协议，普通事件热路径没有全局互斥锁、动态分配或文件 I/O。后台线程写入带版本号、连续序号、CRC32 和尾标记的数据段，并周期执行 fflush 与 fsync（Windows 为 `_commit`）。内存容量按线程数与 `ring_records × 16` 字节估算，另加线程状态；PMU 扩展占用多个槽位。缓冲容量限制瞬时积压，不限制整段任务总记录数。

当前最多注册 1024 个线程实例，同步嵌套深度最多 128。线程退出后缓冲保留到进程结束，以支持检查迟到调用，因此内存应按整个进程生命周期中注册过的线程数估算，不能只按同时活跃线程数估算。线程池应复用工作线程。

生产速率高于消费或磁盘持续吞吐时仍可能丢失。SDK 返回失败，并保存累计丢失数、首次/末次尝试序号和时间边界。这是保守的丢失范围，不表示每一条缺失事件的精确时刻。导入器将穿过该范围的调用标为不完整，防止重复站点的 BEGIN/END 被错误配对。必须检查报告中的质量信息，不能仅凭生成 HTML 就判定采集完整。

异常退出后仅恢复连续通过校验的数据段；遇到截断、损坏、序号断裂即停止，不跳过缺口拼接后续记录。未落盘的尾部无法恢复。缺少最终质量快照时，即使保存的事件都有配对，也标记 partial，不能声称任务完整。文件系统或存储设备是否在断电后真正保存数据由其持久化语义决定。

正常退出应先结束范围、join 所有生产者，再 shutdown。初始化后 fork 的子进程直到 exec 前禁用采集，父进程保守标记 partial；应在首次 SDK 调用前 fork，或在 exec 后初始化。采集器不会改变系统策略或自动终止业务。

shutdown 后的调用会计入迟到记录，后续 shutdown 或 atexit 会追加累计质量快照；最后一次落盘之后才发生的静态析构调用不能保证被持久记录。不要用这一机制替代先 join 再 shutdown 的生命周期约定。

## RAW 与边界计数

Linux 可选 `lxtrace_pmu.h/.cpp` 为调用线程打开一个 perf 组：cycles 作为 leader，最多 8 个不同的 16 位 RAW 事件。一次组 read 返回所有计数及共同的 `time_enabled/time_running`，保持 uint64 原值；记录读取前后 CLOCK_MONOTONIC 时刻。权限拒绝、未运行、短读和线程归属错误分别返回明确状态，不用零代替不可用读数。句柄由创建线程使用并关闭。

```c
lx_pmu_event events[] = {
    {LX_PMU_TYPE_HARDWARE, 0, 0, 0},
    {LX_PMU_TYPE_RAW, 0x7201, 0, 0},
    {LX_PMU_TYPE_RAW, 0x7005, 0, 0}
};
lx_pmu_config config = {events, 3, 0};
lx_pmu *reader = 0;
lx_pmu_error error;
int status = lx_pmu_open(&config, &reader, &error);
/* 若 status != LX_PMU_OK，保存 error，按应用策略停止此次计数采集。 */
```

链接 `sdk/lxtrace_pmu.cpp` 后，可通过 `lxtrace_pmu_compact.h` 的 `lx_compact_boundary` 在边界同时保存软件事件与完整读数。先在冷路径写入字典，数组顺序必须与实际 perf 组一致：

```c
lx_compact_metadata("{\"pmu_events\":[{\"name\":\"cycles\",\"type\":0,\"config\":0},{\"name\":\"MOPA\",\"type\":4,\"config\":29185},{\"name\":\"memstall\",\"type\":4,\"config\":28677}]}");
int result = lx_compact_boundary(reader, gemm, LX_COMPACT_BEGIN, 0, &error);
/* 检查 result；业务计算；随后同样记录 END。 */
```

导入结果保留累计读数样本，并生成同一线程相邻读数之差的计数窗。计数窗不代表硬件活动边界；部分选区不按时长摊分。复用时的缩放是估计，继续排除在精确合计之外。实际事件含义必须与目标硬件字典及已知指令微基准核验，成功打开 RAW 不足以证明其语义。

每个样本保留读取前后时间、站点、边界种类、aux 及可证明的软件范围。只有同一次 BEGIN/END 的相邻读数才标为同一软件范围；跨采集窗口或没有共同 frame 的差值标记为未归属累计窗口。若 perf 组在未采集期间仍启用，这些差值会包含期间的工作，不能归入前一帧或后一帧。已知事件丢失范围内不派生计数窗，原始累计读数继续保留。

外部进程树聚合计数也可指定 RAW，无需接 SDK：

```json
{"raw_events":["0x7201","0x7005"]}
```

将此对象保存为配置文件后，传入 `record --profile task-pmu --config config.json`。这仍是进程树聚合，不能等价解释为逐线程或逐核数据。未配置 RAW 时继续使用原 cycles/instructions 配置。

## 开销与验收边界

16 字节只表示普通事件负载；段头、线程/站点元数据、质量与 PMU 扩展另有开销。是否零丢失取决于事件速率、线程数、缓冲、落盘间隔和磁盘吞吐。≤1% 是需要按具体推理配置测量的目标，不能由无锁设计直接保证；纯打点紧循环的相对开销也不等于推理开销。

只读时钟采集和计数采集应分成独立运行。PMU read 自身会扰动执行，生产归因继续使用 tT 时间与 tA/tB/tV 比率搬运，见 [生产归因报告](production-attribution.md)。采集、分析与硬件语义验证不需要软件自动申请 root；若节点禁止自身任务 perf，必须由管理员提供相应访问能力后才能完成硬件计数验收。
