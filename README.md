# 灵晟 LX2 性能分析器

面向 LX 平台大模型推理应用开发者的性能采集与分析工具。

| 项目 | 说明 |
|---|---|
| 软件版本 | `0.2.0-demo`（开发预览版） |
| 会话格式 | `lxtrace.resource-session.v2` |
| 采集平台 | Linux，数据源可用性取决于节点配置与权限 |
| 分析与查看 | Python 3.9+；现代浏览器支持离线查看 |

## 快速开始

当前以源码模块方式运行。以下命令使用 Linux / Bash；采集端需要 Python 3.9+，无需安装额外 Python 包。集群用户应先在约定的项目工作目录克隆仓库，再进入已分配的计算作业执行能力检查和采集。

### 获取软件

```bash
git clone https://github.com/Istlahndu/lxight-system.git
cd lxight-system
python3 -m lxtrace --version
```

后续命令均在仓库根目录执行。共享账号环境中的克隆、编译、临时文件和采集结果应保存在约定的工作目录内。

### 生成第一份真实采集报告

下面的源码示例用于确认采集与报告流程可用，不依赖模型或 SDK。每次运行重新执行 `run_dir` 赋值，生成新的输出目录：

```bash
run_dir="$PWD/artifacts/run-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$run_dir"

python3 -m lxtrace doctor --output "$run_dir/capability.json"
python3 -m lxtrace record --profile proc-sample \
  --output "$run_dir/session.lxtrace" -- \
  python3 examples/plain_workload.py
```

命令正常结束后得到以下文件。`session.lxtrace/` 是原生会话目录，应完整保留；`session.html` 是软件自动生成的自包含交互报告，可以复制到开发电脑后用浏览器直接打开，无需网页服务。

```text
本次运行目录/
├── capability.json      # 环境与数据源能力
├── session.lxtrace/     # 原生会话与原始证据
└── session.html         # 交互报告
```

报告支持拖动框选、点击“放大到选区”、使用底部水平滚动条浏览、搜索事件及导出。此例展示实际采集的线程状态、最后运行 CPU 等离散样本；矩阵、向量、通信和等待的算子语义需要 SDK 接入。

### 采集自己的推理程序

先确认原有推理命令在目标环境能够正常运行，再将 `--` 后面的程序与参数替换为该命令。下面的路径是占位符，应按实际部署替换：

```bash
python3 -m lxtrace record --profile proc-sample \
  --output "$run_dir/inference.lxtrace" -- \
  /path/to/python /path/to/inference.py 原有参数
```

该命令默认生成 `inference.html`。`proc-sample` 仅观测直接启动进程的线程；包装多进程启动器时可能只看到启动器自身。要查看各 rank 的矩阵、向量、交换与等待流水线，需按 [SDK 接入指南](docs/sdk-quickstart.md) 将当前 SDK 编译接入各工作进程，然后使用 `--profile basic`；启动器必须等待全部工作进程正常退出。

首次使用请显式选择采集配置。默认的 `resource` 要求连续调度等必需来源，当前来源不满足时会在业务启动前失败；`basic` 也不会自动识别未接入 SDK 的推理程序。GLM、DeepSeek 等引擎的业务语义需要适配，当前尚无自动算子插件。完整的配置、接入、报告与导出步骤见下文及 SDK 接入指南。

## 1. 产品用途与接入方式

本工具用于记录推理程序的线程观测、任务级性能计数和应用标记的执行范围，生成可离线查看、导出和复核的性能会话。开发者可将其用于 LX 上部署的 GLM、DeepSeek 等模型推理程序；实际可观测内容取决于采集配置及推理引擎的接入情况。

当前提供两种接入方式：

| 接入方式 | 适用场景 | 可获得的数据 |
|---|---|---|
| 外部进程采集 | 通过本工具启动已有推理命令，无需修改应用代码 | 线程状态与 CPU 离散样本，或用户态任务级 cycles / instructions 计数窗 |
| C/C++ SDK 接入 | 在推理引擎、执行器或原生扩展中标记业务边界 | 应用定义的请求、阶段、层和算子范围，显式复制、异步请求及等待范围 |

GLM、DeepSeek 是被分析应用的模型类型。本版本提供通用采集接口，尚未提供针对这些模型或 PyTorch、vLLM 等框架的自动适配插件。要查看 prefill、decode、Attention、MoE 或通信算子等业务名称，需由引擎在相应执行入口接入 SDK。现版本的框架兼容性与成熟推理引擎端到端性能仍需结合实际部署验证。

## 2. 功能范围

### 2.1 采集、分析与文件管理

| 功能 | 当前实现 |
|---|---|
| 环境检查 | `doctor` 记录资源拓扑、采集源访问状态及失败原因 |
| 线程采样 | `proc-sample` 读取直接启动进程的线程状态、最后执行 CPU 与累计 CPU tick；不递归采集子进程 |
| 任务计数 | `task-pmu` 通过 `perf_event_open` 采集自行启动进程树的用户态 cycles / instructions，保留 raw、enabled、running 与 scaled |
| 软件语义采集 | C ABI、C++11 RAII 及执行器回调适配接口，支持范围、帧、请求关联与跨线程上下文 |
| 统一报告 | `record` 默认生成自包含 HTML；`report` 从单会话、同目录会话或多个输入生成统一报告，支持同节点同轮软件多进程时间线 |
| 调度数据导入 | 按约定格式导入 switch JSONL，重建连续驻留并保留迁核、丢失、未知与未闭合状态 |
| 资源分析 | 在具备相应调度与分配证据时，计算核时、墙钟并集、并发、容量及 target / other / idle / unknown 覆盖 |
| 交互分析 | 资源与线程选择、时间选区、缩放平移、事件搜索、软件执行轨及证据详情 |
| 数据导出 | 自包含 HTML、原生会话 JSON、分析 JSON / CSV、Trace Event |
| 会话管理 | `.lxtrace` 归档、原始文件 SHA-256 校验、派生数据与索引重建、历史软件记录导入 |

### 2.2 业务类别与解释

以下软件范围需由应用或引擎显式标记。范围的起止表示调用经过时间，其间可能包含调度换出与硬件停顿。

| 类别 | 当前可观测内容 | 解释边界 |
|---|---|---|
| 算·矩阵 | 标记的 GEMM、矩阵乘等函数范围 | 软件调用范围，不代表矩阵执行单元持续繁忙 |
| 算·向量/标量 | 标记的归一化、激活、元素运算等函数范围 | 类别与名称由引擎提供；融合范围内的子算子需另行标记 |
| 访存（暴露） | 尚无内部访存停顿起止来源 | 无法从函数耗时直接分离访存停顿 |
| 通信·计算内跨核 | 尚无隐式缓存与一致性通信边界来源 | 无法从线程并行或任务计数推导该类区间 |
| 通信·显式交换 | 标记的复制或通信调用、提交至完成观测的请求生命周期 | 异步调用返回与设备完成需分别观测；请求全程不等于实际传输 |
| 其他·前端/分支 | 尚无内部前端与分支停顿起止来源 | 用户态 cycles / instructions 不提供该类边界 |
| 等待 | 标记的队列、同步、future 或 join 等等待范围 | 软件等待范围不能直接解释为 CPU idle |
| 驱动线程调用外 | 完整覆盖声明下，驱动帧内已标记调用之外的软件时间补集 | 仅在应用确认调用集合完整时成立 |

## 3. 部署要求

将软件目录部署到具有读写权限的工作目录。命令行以源码模块方式运行，以下命令均从软件根目录执行。

- Linux：Python 3.9+，可访问目标进程的 `/proc`；PMU 采集另需节点允许自身任务使用 `perf_event_open`。
- SDK：支持 C++11 的编译器与线程库；C 程序可使用 C ABI，SDK 实现以 C++ 编译并链接。
- 查看端：现代浏览器。Windows 可进行会话分析和 HTML 查看，实机外部采集使用 Linux。
- 软件运行无需安装额外 Python 包、Node.js 或联网加载前端资源。推理引擎、模型权重及其运行依赖由应用环境提供。

确认版本与命令入口：

```sh
python3 -m lxtrace --version
python3 -m lxtrace --help
```

集群环境中，应在已分配的计算作业内执行能力检查及采集命令。采集范围为本工具自行启动的程序；现版本不支持附加到已运行的推理服务。共享账号部署时，构建、临时文件及结果保存在约定的项目工作目录内。

## 4. 采集推理程序

### 4.1 准备启动命令与输出目录

先确认推理程序能够在目标节点独立运行。建议使用有限请求数、能够正常退出的推理任务，并保留模型版本、引擎配置、并发数与输入规模，便于比较不同采集运行的结果。

下文中的 `/path/to/inference-python`、`/path/to/inference.py`、`/path/to/model` 为占位路径；请替换为已验证的 GLM、DeepSeek 或其他推理程序的实际命令。`--model` 是示意的应用参数，应按所用引擎调整。推理所需的环境变量与工作目录应在启动前配置；需要切换工作目录时，可使用最终以 `exec` 启动推理进程的启动脚本。

建立本轮唯一目录并检查节点能力：

```sh
run_dir="$PWD/artifacts/profile/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$run_dir"
python3 -m lxtrace doctor --output "$run_dir/capability.json"
```

每次采集使用新的输出路径。原生会话与采集失败证据已存在时，工具会拒绝覆盖。

### 4.2 线程与 CPU 点采样

```sh
python3 -m lxtrace record --profile proc-sample \
  --output "$run_dir/session.lxtrace" -- \
  /path/to/inference-python /path/to/inference.py --model /path/to/model
```

采集完成后默认生成同名 `session.html`；需要指定报告位置时，在 `--` 前增加 `--html /path/to/report.html`。采集器参数位于 `--` 前，原始推理命令及参数位于 `--` 后。该配置提供线程/CPU 离散观测点与累计 CPU tick，适合检查被观测线程及其 CPU 分布；它不恢复连续驻留、函数调用栈或算子名称。默认轮询间隔为 10 ms，短时工作可能漏采；默认所有线程合计最多保存 100,000 个样本，达到配额后停止采集，业务继续运行。可通过 `--config` 中的 `sample_interval_ms` 与 `max_samples` 调整。

### 4.3 用户态任务 PMU 计数

在允许 PMU 访问的节点上，使用另一次推理运行采集任务计数：

```sh
python3 -m lxtrace record --profile task-pmu \
  --output "$run_dir/pmu.lxtrace" -- \
  /path/to/inference-python /path/to/inference.py --model /path/to/model
```

该命令默认生成 `pmu.html`，也可通过 `--html PATH` 指定位置。该配置在采集区间内为每个事件记录一个聚合计数窗，通过计数器继承覆盖自行启动的进程及后代的用户态 cycles / instructions，采集寿命仍跟随直接启动进程。它不覆盖设备侧活动，也不等价于逐核利用率或硬件单元 busy。计数器访问失败时，查看能力报告中的具体原因。

### 4.4 采集生命周期与配置选择

一次 `record` 选择一个配置。不同配置启动的是不同运行，结果不会自动关联为同一次推理执行。`basic` 支持同节点 SDK 多进程采集与统一报告，启动器必须等待所有工作进程正常退出；使用方法见下一节。`proc-sample` 不递归采集子进程；若启动的是 torchrun、vLLM 等多进程启动器，可能只观测到启动器自身线程，需针对实际工作进程安排启动方式。`task-pmu` 可继承后代计数，但启动器退出后采集也会结束。当前不支持多节点统一采集与未经校时的多节点报告；已存在服务、远端 rank、独立守护进程与设备活动不在上述保证范围内。

`proc-sample`、`task-pmu` 等外部配置的 `record --duration 30s` 在指定时长后停止采集，仍运行的业务会继续运行。`business_exit=None` 表示尚未观察到业务退出；业务的最终退出由其作业或服务管理机制负责。`basic` 不支持 `--duration`，指定时会在业务启动前拒绝执行；SDK 记录依赖各工作进程正常落盘。

| 配置 | 行为 |
|---|---|
| `proc-sample` | 采集线程/CPU 点样本 |
| `task-pmu` | 采集用户态任务计数窗 |
| `resource`（默认）、`schedule` | 要求连续调度与核级来源；现版本缺少必需来源时在业务启动前失败，并保存能力报告 |
| `basic` | SDK 软件语义配置，采集同节点工作进程并生成统一报告，见下一节 |

工具不会在必需来源不可用时静默降级。`--attach` 当前拒绝执行；`--job` 或自填 PID 不建立附加采集授权。

## 5. 接入推理引擎业务语义

要将时间线对应到请求、prefill / decode、层、算子、显式通信及等待，在 C/C++ 引擎或执行器中接入 `sdk/lxtrace.h` 与 `sdk/lxtrace_adapter.h`。可在统一执行回调处完成适配，业务名称和边界由引擎提供。Python 推理程序可使用外部采集；业务打点需在其 C/C++ 原生执行路径接入，当前没有 Python 打点 SDK。

重新编译并链接当前版本的 `sdk/lxtrace.cpp`，确保实际运行程序使用包含多进程输出与运行身份元数据的最新 SDK。已接入 SDK 的单进程程序或同节点启动器均使用正式 `record` 入口：

```sh
# 启动器需等待全部已接入 SDK 的工作进程正常退出。
python3 -m lxtrace record --profile basic \
  --output "$run_dir/session.lxtrace" -- /path/to/sdk-enabled-launcher args
```

命令默认生成 `session.html`，可使用 `--html PATH` 更改位置。采集器注入本轮 `run_id` 及 `LXTRACE_OUTPUT`；最新 SDK 将输出路径中的 `%p` 替换为各进程 PID，使多个工作进程分别保存原始记录。启动器应将这些环境变量传递给工作进程，不要让多个进程改写同一个原档。每个工作进程独立初始化 SDK，并在全部线程及范围结束后关闭；不要在 SDK 已初始化后依赖 `fork` 自动建立新会话。

一次采集的结果目录形如：

```text
run/
├── session.html                         # 默认统一报告
├── session.lxtrace/                     # 原生 v2 会话
└── session-<uuid>.capture/
    └── raw/
        ├── sdk-<pid1>.jsonl             # 各进程原始 SDK 记录
        └── sdk-<pid2>.jsonl
```

统一软件时间线按同一主机、同一次系统启动的 `CLOCK_MONOTONIC` 绝对时间统一原点，保留各进程真实时间差，不按帧或屏障人为对齐。报告默认以每个 rank 的彩色算子流水线展示软件执行顺序；矩阵、向量、交换 API 和等待使用统一配色，帧与请求保留独立轨道。可在“轨道”菜单中从“流水线总览”切换为“按类别分轨”，分别查看帧、矩阵、向量、交换 API、请求和等待。没有相应硬件来源时，硬件轨与指标仍显示 `unavailable` 或 `N/A`。

SDK 采用有界内存缓冲，并在正常退出或 `lx_shutdown` 时落盘；长时间服务应评估缓冲容量与丢失。异步工作应记录提交、关联标识与完成观测；同步 scope 只能覆盖其实际调用范围。具体 API、编译与跨线程接入方法见 [C/C++ 推理引擎 SDK 接入指南](docs/sdk-quickstart.md)。历史 v1 数据仍可使用 `import --type v1` 转换，软件证据不会因此增加物理 CPU 驻留或设备执行边界。

## 6. 查看、分析与导出

### 6.1 查看结果

直接用浏览器打开 `record` 默认生成的 `session.html`、`pmu.html`，或 `--html` 指定的文件。这些文件是自包含报告，可以复制到开发工作站离线查看，无需启动网页服务。

报告图形由软件固定模板自动生成，`record`、`report` 和 `export --format html` 共用同一套实现，无需另写绘图脚本。`web/index.html` 定义报告页面与控件，`web/timeline.js` 负责流水线／类别轨道布局及配色，`web/viewer.js` 负责加载会话、绘制时间线和缩放、筛选、选区、搜索等交互。导出时将这些资源与会话数据内嵌到 HTML，单进程和同节点多 rank 使用一致的视图与解释规则。

1. 先确认会话来源与证据类型。`LIVE` 表示本工具采集，`IMPORTED` 表示导入，`SYNTHETIC` 表示合成数据。
2. 选择资源或软件线程；使用时间输入框或拖动框选确定分析区间。
3. 拖动框选后点击“放大到选区”，将所选时间段铺满横轴；时间线获得焦点时也可按 `Z`。
4. 放大后拖动时间线底部的水平滚动条左右浏览。水平滚动只改变显示视图，保留原有统计选区；点击“全览”或按 `F` 返回完整时间轴。
5. 搜索引擎提供的事件名称；选中记录查看起止、作用域、时钟、质量与原始引用。
6. 按需导出选区 JSON、CSV、Trace Event 或保留当前视图的离线 HTML。

浏览器“导入会话”接收原生 v2 会话 JSON；原生 `.lxtrace` 目录先通过 CLI 导出 HTML 或 JSON。选区 JSON 与分析 JSON 不能替代完整会话。

需要通过服务器查看时，可仅服务本次结果目录：

```sh
python3 -m lxtrace serve --directory "$run_dir" --port 8876
```

服务监听服务器回环地址。工作站另开终端建立 SSH 隧道（替换 `USER@HOST`）：

```sh
ssh -N -L 8876:127.0.0.1:8876 USER@HOST
```

随后访问 `http://127.0.0.1:8876/session.html`。SDK 或 PMU 结果使用相应文件名。保留整个 `.lxtrace` 目录用于后续分析；HTML 用于查看与评审。

### 6.2 重新生成或汇集报告

`report` 支持单个原生会话、包含会话的目录，或多个显式输入。通过 `--session-output` 可同时保存用于复核和再次分析的合并会话：

```sh
# 从单会话重新生成报告。
python3 -m lxtrace report "$run_dir/session.lxtrace" \
  --output "$run_dir/review.html"

# 在同一目录发现会话；该目录应只包含准备汇集的采集产物。
python3 -m lxtrace report /path/to/same-run-sessions \
  --output "$run_dir/report.html" --session-output "$run_dir/merged.lxtrace"

# 也可明确列出各输入。
python3 -m lxtrace report /path/to/rank0.lxtrace /path/to/rank1.lxtrace \
  --output "$run_dir/ranks.html" --session-output "$run_dir/ranks.lxtrace"
```

软件多进程汇集要求输入具有可比较的同主机、同 boot `CLOCK_MONOTONIC` 时钟身份与明确的运行归属。存在不同 `run_id` 时，工具会要求使用 `--run-id explicit-group` 显式声明分组；旧会话缺少运行身份时也必须提供该参数。分组声明不会校正时钟，未经校时的多节点输入仍会拒绝处理。报告不通过帧号或屏障对齐，也不会把不同配置、不同运行的数据默认视为同次执行。

```sh
# 仅在已核对输入确属同一可比较采集组后显式声明；旧会话必须声明。
python3 -m lxtrace report /path/to/old-rank0.lxtrace /path/to/old-rank1.lxtrace \
  --output "$run_dir/group.html" --session-output "$run_dir/group.lxtrace" \
  --run-id explicit-group
```

### 6.3 命令行分析与文件交换

```sh
# 指定选区；时间单位支持 ns、us、ms、s，不写单位按纳秒处理。
python3 -m lxtrace analyze "$run_dir/session.lxtrace" \
  --from 1s --to 2s --output "$run_dir/selection.json"

python3 -m lxtrace export "$run_dir/session.lxtrace" \
  --format json --output "$run_dir/session.json"

python3 -m lxtrace export "$run_dir/session.lxtrace" \
  --format csv --output "$run_dir/summary.csv"

python3 -m lxtrace export "$run_dir/session.lxtrace" \
  --format chrome-trace --output "$run_dir/trace.json"

python3 -m lxtrace rebuild "$run_dir/session.lxtrace" \
  --output "$run_dir/rebuilt.lxtrace"
```

选区应位于会话实际时段内。资源筛选可使用 `analyze --resource` 或 `--cpu`，资源 ID 与 CPU ID 以会话为准。CSV 为资源分析结果，同时生成 metadata JSON，当前不提供通用算子耗时明细表。Trace Event 存在语义损失，应同时保留原生会话。原始证据哈希不匹配时拒绝读取；`rebuild` 仅向新目录重建派生数据和索引。

具备外部调度生产者时，可导入受支持的 switch 流；生产者需提供拓扑、时钟、稳定线程实例及作用域说明：

```sh
python3 -m lxtrace import --type schedule \
  --input /path/to/events.jsonl --metadata /path/to/schedule-meta.json \
  --output "$run_dir/schedule.lxtrace"
```

数据格式见 [调度流导入说明](docs/scheduler-interchange.md)。

## 7. 结果解释与当前限制

| 情况 | 含义与处理 |
|---|---|
| 资源指标显示 `N/A` | 缺少连续调度或可信资源分配证据；函数范围、点样本与任务计数不足以重建这些指标 |
| 类别显示 `unavailable` | 该会话缺少相应来源；不能解释为该活动未发生 |
| 选区计数显示 `N/A` | 选区切过计数窗；工具不会按时间比例摊分整窗计数 |
| 采集在业务启动前失败 | 必需来源或权限不可用，检查同路径的 `*.lxtrace.capability.json` |
| 未显示请求、层或算子名称 | 外部采集不自动恢复模型语义，需在实际引擎执行入口接入 SDK |
| 跨线程请求较长 | 生命周期可能包含排队与等待；应结合调用范围及完成观测解释 |

现版本尚未完成连续调度实时服务、核级硬件来源语义、真实设备传输边界与跨源校准集成；无法据此给出矩阵/向量执行单元繁忙区间、内部访存/前端停顿，或硬件计算与传输重叠结论。全计算域、608 CPU / 10⁷ 事件规模、采集开销预算及成熟大模型引擎接入仍待验收。本版本应按上述已实现范围进行部署与评估。

## 8. 目录与文档

| 目录或文件 | 用途 |
|---|---|
| `lxtrace/` | 采集、归档、资源分析、导入导出与 CLI |
| `sdk/` | C ABI、C++ RAII 与执行器适配接口 |
| `web/` | 新版资源与软件证据查看器 |
| `legacy_web/` | 历史软件会话兼容查看器 |
| `configs/` | 采集配置 |
| `tools/` | 正式文档构建等维护工具 |
| `examples/` | SDK 接口与采集用法的源码示例 |
| `tests/`、`fixtures/` | 随软件维护的自动回归测试与合成测试输入，纳入 Git 管理 |
| `docs/` | 当前版本的正式接入文档与数据格式合同 |
| `artifacts/` | 本地采集产物、测试记录、复现说明与临时文件，不纳入 Git 管理 |
| `artifacts/local-docs/` | 已归档的测试说明与历史实验文档，不纳入 Git 管理 |
| [SDK 接入指南](docs/sdk-quickstart.md) | 引擎开发者的编译、打点与采集流程 |
| [调度流导入说明](docs/scheduler-interchange.md) | 外部调度数据接入格式与解释规则 |
| [版本支持合同](CONTRACT.txt) | 当前版本的证据边界、支持范围与待验收能力 |

Git 仓库保存软件源码、正式说明、配置、源码示例及回归测试。具体测试运行的日志、会话、离线报告、服务器环境记录和复现说明统一保存在 `artifacts/` 下，由 `.gitignore` 排除。现有实验的说明与完整复现步骤随对应 `artifacts/v2/` 实验目录保留；仓库中的通用使用步骤以本 README 和 SDK 接入指南为准。

修改正式 Markdown 文档后，执行 `python -m pip install -r requirements-docs.txt`、`python tools/build_docs.py` 重建三份 HTML 并校验本地链接。运行软件无需安装此可选文档依赖。需要同步生成 `artifacts/v2/` 服务页面时，使用 `python tools/build_docs.py --served`。

完整命令参数以 `python3 -m lxtrace COMMAND --help` 为准；将 `COMMAND` 替换为相应子命令。
