# C/C++ 推理引擎 SDK 接入指南

本指南面向在 LX 平台部署 GLM、DeepSeek 等模型的 C/C++ 推理引擎开发者，说明如何在引擎／执行器中接入 SDK，记录帧、算子、复制、异步请求与显式等待的软件起止，并通过正式采集命令生成统一报告。

当前提供 **C ABI 和 C++11 封装**，没有现成的 GLM、DeepSeek 模型适配或其他推理框架插件，也没有 Python SDK。接入需要开发者在引擎调度、执行器回调或运行时边界实现适配。Python 3.9+ 仅用于命令行采集、格式转换与导出，不是被测引擎的运行依赖。

## 接入位置与生命周期

优先在引擎已有的执行边界集中适配，让同一执行器处理的模型复用这层代码。不要依赖外部采样自动恢复任意函数的类别，也不要把帧结束当作异步设备完成。

| 边界 | 接入原则 | 现有接口 |
|---|---|---|
| 进程／线程 | 入口调用一次初始化；每个参与线程登记名称与软件角色；所有线程与范围结束后关闭 | `lx_init(app_name, build_id)`、`lx_thread_role(name, role)`、`lx_shutdown()` |
| 帧 | 选择可比较的工作单元，如一次请求、decode step 或微批的一层；在引擎中产生明确、可区分的 ID | `lxtrace::Frame(id, driver_calls_complete, metadata)`；`ExecutionAdapter::frame(id, metadata, callback)` |
| 操作 | 在现有同步回调入口和返回处包裹范围；由适配层提供名称与类别 | `lxtrace::Range(name, category, metadata)`；`ExecutionAdapter::operation(name, category, metadata, callback)` |
| 复制／交换 | 包裹实际同步 API 调用；记录其软件范围，异步提交返回时不要继续延长这个同步范围 | `lxtrace::Exchange(name, metadata)`；`ExecutionAdapter::exchange(name, metadata, callback)` |
| 等待 | 包裹明确的 future、锁、条件变量、通信等待等调用，填写已知对象 | `lxtrace::Wait(name, object)`；`ExecutionAdapter::wait(name, object, callback)` |
| 异步请求 | 提交时建立独立 handle；只有观测到对应请求完成才闭合；为复用的请求提供可区分关联 ID | `ExecutionAdapter::submit(name, correlation, peer, bytes, transport)`、`complete(handle)` |
| 跨线程帧归属 | 在生产者捕获 context，随任务传递；消费者执行期间附加并在退出时恢复 | `lx_context_capture()`、`lxtrace::ContextScope(context)` |

`ExecutionAdapter` 是轻量回调封装，不调度线程、不替换引擎执行器，也不自动发现算子。其 `frame` 方法使用 `driver_calls_complete=0`。比如在现有 GEMM 回调处调用 `operation(name, "mat", metadata, callback)`，在 bias 或激活回调处调用 `operation(name, "vec", metadata, callback)`。只有引擎确实暴露独立边界时，才能分别标记融合操作的内部阶段。

所有同步 frame／range 必须在同线程严格嵌套闭合，C++ RAII 会在离开 scope 时闭合；不要让同步范围跨线程结束。异步 handle 可由另一线程调用 `lx_async_complete_observed(handle)`，但它表示完成观测，不是硬件完成时间戳。请求释放、提交函数返回或父帧结束都不应代替完成观测；未完成的请求保持未闭合。

context 仅在同一进程有效，不是跨进程关联协议。C API `lx_context_attach(context, &previous)` 成功返回 0，失败返回 -1；恢复 `previous` 前，应先结束当前线程中的所有子范围。C++ `ContextScope::active()` 可查询附加是否成功；析构时恢复之前的 context。生产者帧可以先于消费者工作结束，关联关系不会扩展生产者的实际帧区间。

建议元数据明确记录 `microbatch`、`layer`、`operator_id`，需要父帧关联时使用 `parent_application_frame_id`。`args_json` 必须是 JSON 对象文本；单条记录参数区容量为 767 字节，框架补入的保留字段也占容量。不要向元数据写入 tensor、权重或消息 payload。

SDK 在写入前校验参数对象的语法、UTF-8、转义和数值可表示性。非法或超出容量的参数会替换为 `{"invalid_args":true}` 并计入 `protocol_errors`，仍保留对应范围及可观测的结束边界；例如 `{oops}` 不会再破坏整条 JSONL 记录。参数校验与软件范围采集均在用户态执行，不需要 root 或 PMU 权限。

只有已确认的驱动线程角色、完整帧及完整调用全集才能启用 `driver_calls_complete=1`，用于推导“驱动线程调用外”补集；不满足时保持 0。`frame_driver`、`worker` 等只是软件角色，不代表固定物理核或硬件单元。

## 异步计算与通信重叠

SDK 不要求同一线程的计算范围与异步请求串行。适配层应在提交前创建 `lx_async_begin_transport(...)` 句柄，在已有执行器中提交通信，记录可独立推进的计算范围，并在实际观测到该请求完成后调用 `lx_async_complete_observed(handle)`。完成可由提交线程或进度线程记录；一个请求只完成一次。多个并发请求分别持有句柄，即使业务名称相同也不能共用句柄。

异步请求不进入同步范围栈，其生命周期可以与计算交叉，也可以晚于所属帧结束。统一 HTML 将帧、操作和请求生命周期分轨显示，重叠区间保留各自边界；请求显示在提交线程的生命周期轨上，进度线程的真实调用可另用 `Exchange` 标记，并通过 context 关联帧。只标记非阻塞提交函数的入口和返回，会缺少返回后的请求生命周期。SDK 不会自动拦截通信库或恢复未标记的完成回调。

这些区间能够说明计算软件范围与通信调用／请求在途时间相交，不能证明整段实际占用计算单元或传输引擎。同一逻辑 CPU 上的多个软件线程可交替推进；设备也可能在主机调用返回后继续执行。只有独立、可校时且语义明确的硬件活动边界才能支持硬件重叠结论。仅有 SDK 数据时，报告的硬件活动并存显示 `unavailable`，不是零重叠；当前 v2 界面也不提供独立的软件重叠比例指标。

## 长任务与多进程接入限制

当前 SDK 先在进程内缓冲记录，默认容量为 **32768 条记录**。可在首次 SDK 调用前设置 `LXTRACE_CAPACITY`，取值范围为 4～1000000；无效值沿用默认容量。线程登记、范围开始和结束等分别占用记录，容量不是完整算子的数量。缓冲满后新增记录会丢失，并在落盘质量信息的 `dropped_records` 中报告；结束记录也可能丢失，导致范围未闭合。长时间推理应选择有限的采集窗口、控制埋点粒度，并检查丢失记录与未闭合范围，不能将采集结果视为完整执行历史。

事件记录在 `lx_shutdown()` 或正常进程退出时写入文件，没有周期落盘或连续事件流式刷新。关闭后本进程不再接受事件，也不能通过再次 `lx_init()` 开始新会话；后续事件尝试计入 `late_records` 与 `dropped_records`，并向原文件追加质量快照。仍未闭合的同步范围或异步请求保持未闭合，`clean_shutdown=false`，不会补造结束时间。重复调用 shutdown 且质量未变化时，不重复追加快照。

新 SDK 的质量行带有 `quality_semantics="cumulative_snapshot"`、递增的 `quality_revision`，其中计数是截至快照时的累计值，不能逐行相加。产品导入器保留质量历史，合并累计快照，并让异常状态持续影响原生档案和 HTML 报告；旧版无标志质量行仍按原增量语义读取。`clean_shutdown` 只描述该采集的已观察质量，不证明业务结果正确，也不保证最后一次快照之后仍活跃线程的完整覆盖。

窗口需要由引擎控制埋点或采用独立的采集进程生命周期。强制终止、崩溃或跳过正常退出流程时，可能没有任何记录文件；本版本没有增加崩溃恢复。应在工作线程与范围结束后主动调用 `lx_shutdown()`，并检查输出质量信息。

同节点多进程引擎可使用 `record --profile basic` 启动。采集器为本轮注入 `run_id`，并通过含 `%p` 的 `LXTRACE_OUTPUT` 为各工作进程指定原档路径；最新 SDK 将 `%p` 替换为当前 PID。启动器必须向工作进程传递这些环境变量，并等待所有工作进程正常退出，采集器才汇集原档、生成原生会话与统一 HTML。不要将多个工作进程的输出改成同一个固定文件。

每个工作进程应在自身入口初始化 SDK。首次 SDK 调用之前创建进程，或在 `exec` 加载新程序后初始化，均可建立独立记录。已经使用 SDK 的进程再 `fork` 时，子进程在 `exec` 前禁用 SDK 记录和落盘，不复用继承的缓冲、句柄、context 或锁；再次调用 `lx_init()` 也不能解除这一限制。在 Linux 标准 `fork()` 路径上，父进程输出 `unsupported_fork_count`（至少一次的下界）、协议错误及明确警告，采集结果标为 partial；即使子进程随后 `exec` 并正常记录，父进程仍保守降级。不要依赖直接系统调用、`vfork` 或其他绕过 `pthread_atfork` 的机制报告完整子进程覆盖；本策略不提供通用的多线程 fork 子进程运行支持。

context 只在同一进程有效；统一报告汇集进程时间线，不自动建立跨进程请求或帧关联。框架特定关联仍由适配层提供。`basic` 不支持 `--duration`，指定时会在启动业务前拒绝执行；需让有限任务正常结束并落盘。

## 编译到已有引擎

将当前版本的 `sdk/lxtrace.cpp` 加入构建，把 `sdk` 加入头文件搜索路径。升级后应重新编译并重新链接，确认实际进程加载的是最新 SDK；多进程 `%p` 输出和运行身份元数据依赖当前实现，不能只替换头文件而继续加载旧共享库。接口分布如下：

- `sdk/lxtrace.h`：C ABI，以及 `Frame`、`Range`、`Wait`、`ContextScope` 的 C++11 RAII 封装。
- `sdk/lxtrace_adapter.h`：`Exchange` 和回调式 `ExecutionAdapter`。

C 工程可使用 `lx_frame_begin_ex`、`lx_range_begin`、`lx_exchange_begin`、`lx_wait_begin`，并成对调用 `lx_frame_end`／`lx_range_end`；异步使用 `lx_async_begin_transport` 和 `lx_async_complete_observed`。SDK 实现以 C++ 编译，最终使用 C++ 链接器链接。已有引擎采用其他构建系统时，沿用其编译选项和 ABI，仅加入对应源文件与头文件路径。

## 独立的 SDK API 用法示例

下面是可独立编译的接口示例，执行 2×2 矩阵乘法、bias、ReLU 和线程间复制，用于说明范围、异步请求与 context 的配对方式。它不是模型验证或性能基准；数据量很小，记录区间可能接近时钟有效分辨率。接入引擎时，用现有真实操作替换相应代码块，保留正确的生命周期边界。

以下命令在有写权限的 SDK 项目根目录执行，该目录应含 `sdk/` 和 `lxtrace/`。每次创建新的个人输出目录，编译临时文件也留在项目内。

Linux：

```bash
run_dir="artifacts/sdk-user/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$run_dir/tmp"
```

Windows PowerShell：

```powershell
$sdkRun = Join-Path 'artifacts/sdk-user' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path "$sdkRun/tmp" | Out-Null
```

将以下代码保存为新目录中的 `my_profile.cpp`：

```cpp
#include "lxtrace_adapter.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <future>
#include <thread>

int main() {
    lx_init("my-program", "sdk-example-1");
    lx_thread_role("inference", "frame_driver");

    float x[4] = {1, 2, 3, 4};
    float weights[4] = {1, -1, 2, 1};
    float bias[2] = {-1, 1};
    float output[4] = {}, received[4] = {};

    {
        // 0 表示未声明调用全集完整，不派生“驱动调用外”补集。
        lxtrace::Frame frame("mb0.layer0", 0,
            R"({"microbatch":0,"layer":0,"operator_id":"my.layer"})");
        {
            lxtrace::Range scope("my.matmul", "mat");
            // 自有程序可在这里调用已有 matmul()/GEMM。
            for (int i = 0; i < 2; ++i)
                for (int j = 0; j < 2; ++j)
                    for (int k = 0; k < 2; ++k)
                        output[i * 2 + j] += x[i * 2 + k] * weights[k * 2 + j];
        }
        {
            lxtrace::Range scope("my.bias", "vec");
            for (int i = 0; i < 4; ++i) output[i] += bias[i % 2];
        }
        {
            lxtrace::Range scope("my.relu", "vec");
            for (float &value : output) value = std::max(0.0f, value);
        }

        // 请求与复制范围分开。这里用线程模拟应用自身的异步执行器。
        lx_handle request = lx_async_begin_transport(
            "activation request", "activation.0", "copy-worker",
            sizeof(output), "application_thread_memcpy");
        lx_context frame_context = lx_context_capture();
        std::promise<void> completed;
        std::future<void> ready = completed.get_future();
        std::thread worker([&] {
            lx_thread_role("copy-worker", "worker");
            lxtrace::ContextScope context(frame_context);
            {
                lxtrace::Exchange scope("copy.activation",
                    R"({"mechanism":"cpu_memcpy"})");
                std::memcpy(received, output, sizeof(output));
            }
            lx_async_complete_observed(request);
            completed.set_value();
        });
        {
            lxtrace::Wait scope("wait.activation", "completion future");
            ready.wait();
        }
        worker.join();
    } // 所有同步 scope、frame 和工作线程结束后，再关闭 SDK。
    lx_shutdown();
    std::printf("result: %.1f %.1f %.1f %.1f\n",
                received[0], received[1], received[2], received[3]);
    return 0;
}
```

Linux 编译：

```bash
TMPDIR="$(pwd)/$run_dir/tmp" g++ -std=c++11 -O2 -pthread -Isdk sdk/lxtrace.cpp \
  "$run_dir/my_profile.cpp" -o "$run_dir/my_profile"
```

PowerShell 编译，将编译器临时目录限制在本次输出目录，并在结束后恢复环境：

```powershell
$sdkSavedTemp = $env:TEMP
$sdkSavedTmp = $env:TMP
try {
    $env:TEMP = (Resolve-Path "$sdkRun/tmp").Path
    $env:TMP = $env:TEMP
    g++ -std=c++11 -O2 -pthread -Isdk sdk/lxtrace.cpp "$sdkRun/my_profile.cpp" -o "$sdkRun/my_profile.exe"
} finally {
    $env:TEMP = $sdkSavedTemp
    $env:TMP = $sdkSavedTmp
}
```

## 采集与统一报告

### 1. 运行已接入 SDK 的程序

Linux：

```bash
python3 -m lxtrace record --profile basic \
  --output "$run_dir/session.lxtrace" -- "$run_dir/my_profile"
```

PowerShell：

```powershell
python -m lxtrace record --profile basic --output "$sdkRun/session.lxtrace" -- "$sdkRun/my_profile.exe"
```

接入引擎后，将 `--` 后的可执行文件与参数替换为自己的引擎启动命令。同节点多进程使用相同入口；启动器需等待所有已接入 SDK 的 worker 正常退出：

```bash
python3 -m lxtrace record --profile basic \
  --output "$run_dir/session.lxtrace" -- /path/to/sdk-enabled-launcher args
```

采集器保留业务退出状态，默认生成 `session.html`。需要改变 HTML 路径时，在 `--` 前使用 `--html /path/to/report.html`；原生会话仍由 `--output` 决定。每轮使用新的输出目录，保留统一会话及各进程原档：

```text
run/
├── session.html                         # 自包含统一报告
├── session.lxtrace/                     # 原生 v2 会话目录
└── session-<uuid>.capture/
    └── raw/
        ├── sdk-<pid1>.jsonl
        └── sdk-<pid2>.jsonl
```

未设置输出路径，或设置 `LXTRACE_DISABLED=1` 时，SDK 调用为 no-op，业务回调仍正常执行，但不会生成这组语义记录。

### 2. 查看自动生成的 HTML

直接打开 `session.html`，无需网页服务或公网。默认“流水线总览”以每个 rank 的彩色算子流水线呈现执行顺序，矩阵、向量、交换 API 和等待使用统一配色，帧与请求保持独立轨道。在“轨道”菜单选择“按类别分轨”，可分别查看帧、矩阵、向量、交换 API、请求和等待。两种视图均可缩放、平移、搜索业务名称，并点击对象查看起止、所属帧、质量和原始来源。

在时间线上拖动框选，再点击“放大到选区”，所选时间段即铺满横轴；时间线获得焦点时也可按 `Z`。放大后，拖动底部水平滚动条左右浏览；这只改变显示视图，不改变统计选区。点击“全览”或按 `F` 返回完整时间轴。

图形由软件自带的固定 Web 模板自动生成：`web/index.html` 定义页面结构与控件，`web/timeline.js` 负责轨道布局与配色，`web/viewer.js` 负责会话加载、时间线绘制及交互。`record`、`report` 与 `export --format html` 共用这套模板，并将模板资源和数据内嵌到自包含 HTML；接入引擎后不需要为每次运行另编绘图脚本。单进程与同节点多 rank 使用一致的报告流程。

同节点多进程记录以相同 host、相同 boot 的 `CLOCK_MONOTONIC` 绝对时间统一原点，保留进程间原有时间差，不按帧、迭代号或屏障人为对齐。帧 ID 和请求关联仍采用应用提供的语义；时间相交本身不建立业务归属。

没有可信资源分配、调度或设备活动证据时，硬件轨保持缺失，资源指标为 `N/A`，对应类别为 `unavailable`。汇集软件记录不增加物理核驻留、设备执行或真实传输边界。

独立 API 示例的预期标准输出为 `result: 4.0 2.0 10.0 2.0`；其中 `my.matmul`、`my.bias`、`my.relu`、`copy.activation`、`wait.activation` 与 `activation request` 分别有记录。实际引擎的名称、类别与可见粒度由适配层决定。

### 3. 从已有会话重新生成或汇集报告

正式 `report` 命令可读取单会话、包含会话的目录或多个显式输入；通过 `--session-output` 同时保存汇集后的原生会话。下面示例从项目根目录执行，所有输出使用新路径：

Linux：

```bash
# 单会话重新生成报告。
python3 -m lxtrace report "$run_dir/session.lxtrace" \
  --output "$run_dir/review.html"

# 同目录发现会话；目录中仅放置本次准备汇集的采集产物。
python3 -m lxtrace report /path/to/same-run-sessions \
  --output "$run_dir/report.html" --session-output "$run_dir/merged.lxtrace"

# 或明确指定同轮各进程的会话。
python3 -m lxtrace report /path/to/rank0.lxtrace /path/to/rank1.lxtrace \
  --output "$run_dir/ranks.html" --session-output "$run_dir/ranks.lxtrace"
```

PowerShell：

```powershell
python -m lxtrace report "$sdkRun/session.lxtrace" --output "$sdkRun/review.html"
```

输入必须有可比较的同 host、同 boot 单调时钟身份和明确的运行归属。不同 `run_id` 的输入不会被静默视为同一次运行；旧会话缺少运行身份时也必须通过 `--run-id explicit-group` 声明分组。显式分组只声明输入归属，不能校正时钟；工具仍拒绝未经校时的多节点汇集。

```bash
# 仅在确认旧会话确属同一可比较组后声明。
python3 -m lxtrace report /path/to/old-rank0.lxtrace /path/to/old-rank1.lxtrace \
  --output "$run_dir/group.html" --session-output "$run_dir/group.lxtrace" \
  --run-id explicit-group
```

### 4. 转换历史 v1 会话

历史软件会话可继续导入，保留原有软件证据和原始引用：

Linux：

```bash
python3 -m lxtrace import --type v1 --input /path/to/software-v1.lxtrace \
  --output "$run_dir/imported.lxtrace"
python3 -m lxtrace report "$run_dir/imported.lxtrace" \
  --output "$run_dir/imported.html" --run-id historical-run
```

PowerShell：

```powershell
python -m lxtrace import --type v1 --input /path/to/software-v1.lxtrace --output "$sdkRun/imported.lxtrace"
python -m lxtrace report "$sdkRun/imported.lxtrace" --output "$sdkRun/imported.html" --run-id historical-run
```

原生 `.lxtrace` 是含文件校验、原始来源和规范化记录的目录归档，应完整保留。旧记录缺少可比较时钟信息时，`--run-id` 也不能使其满足跨进程时间汇集条件。

## 证据解释与支持边界

| 记录 | 可以解释为 | 不能解释为 |
|---|---|---|
| `mat` / `vec` | 适配层选择的矩阵／其他计算函数范围 | ISA 指令分类、矩阵／向量执行单元持续 busy |
| 复制／交换范围 | 同步调用经过时间 | DMA／NIC 活动或连续物理传输 |
| request | 应用提交至完成观测的生命周期 | 设备实际开始／完成及全程持续传输 |
| wait | 显式等待调用范围，立即返回时也可能很短 | CPU idle、硬件 stall、全部 off-CPU 时间 |
| frame / context | 应用定义的工作单元与显式归属 | 自动发现的模型边界或由时间相交推定的归属 |

函数范围可能包含调度换出、缓存未命中、分支与硬件停顿，SDK 不拆分这些内部成分。`mat`、`vec` 是适配层提供的人工类别；没有接入 SDK 的程序不会自动识别这些类别，外部 `/proc` 样本与 PMU 计数也不会自动恢复任意函数的这组起止。

融合算子只暴露一个调用边界时，只记录该整体范围；要分别查看 bias、ReLU 等阶段，需要引擎本身提供对应边界并适配。跨进程请求、框架特定回调、异常与取消策略也需要在引擎适配中明确处理，现有通用 SDK 不等于已完成特定推理框架集成。
