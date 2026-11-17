# 生产归因报告使用指南

生产归因报告用于分析分别采集的只计时运行与 PMU 计数运行。该入口提供矩阵优先、核发令侧分解、单元利用率、dense 子步骤归因和 MFU 乘法链，输出固定模板的离线 HTML 与分析 JSON。

普通单次采集、多 rank 统一流水线继续使用 `lxtrace report`。生产归因使用独立的 `lxtrace production-report`，不改变原有导入、SDK 或报告流程。

## 输入与运行

准备同配置的 GFTRACE2 文件。每个二进制文件的同名 `.txt` 元数据必须保留，包含相位、层、计数帧标志、PMU 事件码以及运行配置：

```text
capture/
    trace-tT.bin    trace-tT.txt
    trace-tA.bin    trace-tA.txt
    trace-tB.bin    trace-tB.txt
    trace-tV.bin    trace-tV.txt
```

`tT` 只读取时钟，提供业务时间。`tA/tB/tV` 分别采集不同事件组，提供原始计数与周期。计数数组为零的 ticks-only 运行不会被当作真实 PMU 数据。

在仓库根目录执行（PowerShell 或 Linux 均可用单行形式）：

```bash
python -B -m lxtrace production-report --timing capture/trace-tT.bin --count tA=capture/trace-tA.bin --count tB=capture/trace-tB.bin --count tV=capture/trace-tV.bin --output out/production.html --evidence-output out/production.lxtrace
```

生成：

| 输出 | 用途 |
| --- | --- |
| `out/production.html` | 可独立打开的生产归因报告，包含框选放大、水平滚动和悬停详情 |
| `out/production.analysis.json` | 完整分析结果，保留工作块、比率来源、原始计数、计数周期、缺失与回退信息 |
| `out/production.lxtrace` | 可选的模型推算聚合证据，供现有资源分析器读取；不包含伪造的硬件活动区间 |

使用 `--analysis-output PATH` 修改 JSON 路径；`--bin-ms 0.2` 控制时间图的箱宽。输出必须使用新路径，不会覆盖已有文件或原始输入。HTML 内嵌展示所需的数据与脚本，不访问网络；完整分析 JSON 作为独立证据文件保留。

此流程只读取现有文件，使用 Python 标准库，不需要新增 root 权限、PMU 权限或 NumPy。产生 RAW PMU 数据本身仍取决于平台权限与引擎插桩能力。

## 对齐与估计口径

软件先验证各运行的 token、batch 等已声明配置、相位与层字典、池线程集合、逐线程层／相位块序列和采样帧一致，再按“线程 × 工作块 × 模式”对齐。不同运行的绝对时间戳不会合并为同一次执行。

事件搬运采用：`计数运行的事件数 / 该运行的周期 × tT 的周期`。例如计数运行某块有 110 万周期和 55 万次事件，而 tT 同块为 100 万周期，搬运估计是 50 万次。原始 55 万次、110 万周期、所用运行、公式及对齐键均保留。

动态领活可能使个别工作块缺少某种模式。默认按参考算法依次尝试同线程同相位／模式、同线程同模式的比率，并记录 `rate_evidence` 的来源与覆盖统计；没有可用读数时保持未知。`--strict-alignment` 可禁止回退。配置或层／相位序列不符会直接报错。

运行周期比和扰动范围描述计数运行与时间运行的观测差异，不能单独证明差异全部由采集开销造成，也不是统计置信区间。

## 报告内容

| 视图 | 如何理解 |
| --- | --- |
| 多 die 时间图与相位带 | 相位带来自 tT 软件边界。0.2 ms 分类箱把工作块的估计总量按时间比例投影，帮助定位损失集中在哪些阶段；不表示精确测得的内部停顿起止 |
| 矩阵优先 | 矩阵、向量／标量、暴露访存、计算内跨核、前端、显式交换、等待、池调用外；缺少证据的部分显示未知 |
| 核发令侧分解 | MOPA 发令、等待各级数据、前端及其余发令侧周期；与矩阵单元忙周期属于不同口径 |
| 单元利用率 | 矩阵、SVE、装存、L2 供数、L2 以外供数、IPC 分别除以自身峰值，彼此可重叠，不要求合计 100% |
| dense 归因 | 反量化、矩阵块、打包、领活与屏障，分解为矩阵、指令工作、远端数据、前端和等待；展示 ZA 标定及计数扰动产生的上下界 |
| MFU | 池占比 × 在计算 × 矩阵忙 × 有效 MOPA，及各步损失的瀑布图 |

矩阵优先与核发令侧分解在各自声明的周期范围内守恒。缺少事件码或计数关系异常会使相应视图标为 partial／unavailable，不会把未测量项补成测得的零。

所有推算结果标为 `model_calibrated:<算法>`、`representation=aggregate`。上下界是给定标定与计数扰动假设下的约束，不能当作硬件活动边界，也不能用于证明同一核上的真实硬件重叠。

## 覆盖与 MFU

报告显示实际保留的池线程、模型配置的预期线程及采样窗口。只有驱动轨的 die 不会补造池线程归因。池调用外是引擎软件状态，不是操作系统 idle 或 CPU 驻留。

截取少数池线程时，全机 MFU 保持不可用；不能用代表性线程外推全机。完整窗口的汇总计数可独立生成 MFU 报告：

```bash
python -B -m lxtrace production-report --mfu-counts complete-counts.json --output out/mfu.html
```

汇总 JSON 应包含来源与同一窗口的模型所需 FLOP，采用如下结构：

```json
{
  "label": "模型、配置与采集窗口的说明",
  "source": "汇总数据的出处",
  "frame": {
    "threads": 592,
    "seconds": 2.0,
    "compute_cycles": 1400000000000,
    "mopa": 300000000000,
    "inst": 1800000000000,
    "flop": 280000000000000
  },
  "phases": []
}
```

这些值由应用或模型适配层提供。`flop` 必须是相同窗口、相同模型配置、参考算法与稀疏激活口径的模型所需运算量，不能使用参数量近似或包含补零、多余计算的硬件 FLOP。汇总入口不验证业务侧分子的正确性，报告会保留其声明来源。

该入口与 `--timing` 互斥，避免把另一配置的完整 MFU 混入当前 trace。可用 `--reference reference.json` 加入外部对照；参考值始终单独标为未复算，不参与计算。

## 硬件与模型配置

默认硬件配置为 `lx2-profiled-v1`，模型／引擎配置为 `glm5f`。使用 `--hardware FILE.json`、`--model FILE.json` 提供其他版本。配置格式及示例在 `lxtrace/attribution/profiles/`。

硬件配置统一定义核数、频率、矩阵指令周期与 FLOP、单元吞吐、重叠模型和 dense 标定，报告记录配置版本与来源。默认归因标定保留交接 golden 的历史口径；该口径与较新微基准的部分延迟值不同，更换标定版本可能改变分类结果。

模型／引擎配置定义计算、交换、等待、池调用外模式及交换型相位、预期池线程和 dense 矩阵块参数。模型名称不会让工具自动识别未插桩的推理引擎。

算法移植来源：`glm5f-dsub@148a1202`（对齐与 dense）、`glm5f-tl3@850d5a0f`（三种分解）、`mfu-kit-tl3@09d3036a`（硬件事实与 MFU）。产品运行使用仓库内实现，不依赖交接包脚本或 golden 文件。
