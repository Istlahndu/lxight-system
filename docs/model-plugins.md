# 硬件配置与模型插件接入指南

模型插件声明推理引擎的阶段、子步骤、类别、FLOP 口径和显示信息。对于已经使用 GFTRACE1 / GFTRACE2 的引擎，更换插件即可沿用同一导入、归因和 HTML 呈现流程，无需在绘图代码中增加模型判断。

插件不会自动插桩推理引擎，也不会把一种二进制格式转换成另一种格式。引擎仍须提供正确的阶段、层、帧和子步骤边界；新的 trace 格式需要对应的读取适配器。

## 三层职责

| 层 | 配置与入口 | 负责内容 |
| --- | --- | --- |
| 硬件层 | `lxtrace/attribution/hardware.py`、`lxtrace/attribution/profiles/lx2-profiled-v1.json`；CLI `--hardware` | 核数、频率、峰值吞吐、PMU 语义角色与 RAW 编码、标定和计数分解算法 |
| 模型层 | `lxtrace/plugins/`、`lxtrace/attribution/profiles/glm5f.json`；CLI `--model` | GFTRACE 阶段／site 字典、计算／交换／等待模式、阶段族、可选子步骤配置、模型所需 FLOP 定义 |
| 通用呈现层 | `web/attribution/` | 根据分析 JSON 的 `presentation` 描述绘制时间图、分类、阶段汇总、子步骤与 MFU；只消费通用数值与显示信息 |

同一模型在不同硬件上的事件编码和峰值由硬件配置提供；同一硬件上更换模型时，阶段名称、子步骤类别和 FLOP 定义由模型插件提供。颜色或标题的变化不改变数值计算。模型插件缺少某项细分能力时，相应视图保持不可用或隐藏，不套用另一个模型的规则。

## 查看并选择插件

在仓库根目录运行：

```bash
python3 -m lxtrace plugins
```

当前内置 `glm5f` 和 `generic-transformer`，默认硬件配置为 `lx2-profiled-v1`。

| 模型插件 | 用途与边界 |
| --- | --- |
| `glm5f` | GLM-5.3-Flash 的现有 GFTRACE 阶段、dense 子步骤及参考 prefill FLOP 口径；默认保留历史数据解释 |
| `generic-transformer` | 可复制修改的通用接入结构示例，展示阶段映射、矩阵／向量／交换分类和组件式 FLOP；不代表某个生产模型的完整计算图 |

导入原始 GFTRACE 时指定模型插件：

```bash
python3 -m lxtrace import --type gftrace \
  --model generic-transformer \
  --input capture/trace-tT.bin --output out/imported.lxtrace

python3 -m lxtrace report capture/trace-tT.bin \
  --model generic-transformer --output out/timeline.html
```

导入时选中的插件用于解释该文件的阶段和 site。导入后的原生会话已保存解释结果；更换语义时应从原始 GFTRACE 重新导入，不能仅在已有会话上换一个模型名称。

生产归因使用相同的选择方式：

```bash
python3 -m lxtrace production-report \
  --model generic-transformer --hardware lx2-profiled-v1 \
  --timing capture/trace-tT.bin \
  --count tA=capture/trace-tA.bin \
  --count tB=capture/trace-tB.bin \
  --count tV=capture/trace-tV.bin \
  --output out/production.html
```

这里的输入文件必须由该插件所描述的引擎生成。文件名相同不意味着阶段、site 或工作量相同；工具仍执行计时／计数运行的一致性检查。

## 创建模型插件 JSON

可复制完整的 [generic-transformer.json](../lxtrace/plugins/generic-transformer.json)，保存为自己的 JSON 文件，并通过 `--model /path/to/plugin.json` 选择。以下是该示例的核心声明，保留了相同的阶段和 site：

```json
{
  "schema_version": "lxtrace.model-plugin.v1",
  "profile_id": "my-transformer-v1",
  "model": "My transformer",
  "compute_modes": ["busy", "affine", "activation"],
  "exchange_modes": ["shuffle"],
  "wait_modes": ["bar", "xw", "spin"],
  "idle_modes": ["idle"],
  "exchange_stages": ["redistribute"],
  "trace": {
    "format": ["GFTRACE1", "GFTRACE2"],
    "stages": {"0": "encoder", "1": "projection", "2": "redistribute"},
    "sites": {
      "41": {"mode": "affine", "category": "mat"},
      "42": {"mode": "activation", "category": "vec"},
      "43": {"mode": "shuffle", "category": "cx"}
    },
    "mode_categories": {"busy": "unknown"}
  },
  "mode_labels": {
    "affine": "投影矩阵", "activation": "激活向量", "shuffle": "显式交换"
  },
  "dense": {"backend": "none"},
  "stage_families": {
    "encoder": {"label": "编码器", "color": "#336699", "stages": ["encoder", "projection"]},
    "transport": {"label": "交换", "color": "#228877", "stages": ["redistribute"]}
  },
  "presentation": {
    "views": {"dense_enabled": false},
    "stage_labels": {"projection": "投影阶段"}
  },
  "flop": {
    "adapter": "matmul_components.v1",
    "definition": "每层一次 H×H 投影及每元素 4 FLOP 激活；仅覆盖声明的组件",
    "components": [
      {"kind": "matmul", "stage": "projection", "m": ["B", "T"], "n": "config.hidden_size", "k": "config.hidden_size", "repeat": "layers"},
      {"kind": "elementwise", "stage": "encoder", "elements": ["B", "T", "config.hidden_size"], "flop_per_element": 4, "repeat": "layers"}
    ]
  }
}
```

例如，引擎把 site 41 放在矩阵函数边界、42 放在激活函数边界、43 放在显式交换边界，导入器就会分别显示矩阵、向量和交换。这些 ID 必须与引擎实际写出的 ID 一致，不能按函数名称猜测。

| 字段 | 接入要求 |
| --- | --- |
| `profile_id`、`model`、`revision` | 标识配置与版本；修改语义或计算口径后应更新版本 |
| 四组 `*_modes` | 计算、交换、等待、调用外模式分别声明，组间不能重复；保留实际读取适配器产生的协议模式 |
| `trace.stages`、`trace.sites` | 数字 ID 到阶段名、模式与类别的映射；阶段／site ID 必须符合 GFTRACE 编码范围 |
| `trace.mode_categories` | 未由 site 指定类别的模式；类别使用 `mat`、`vec`、`mem`、`cc`、`cx`、`fe`、`wait`、`unknown` 或 `drv` |
| `exchange_stages` | 整体按显式交换处理的阶段；软件调用耗时仍不等于设备真实传输时间 |
| `expected_pool_threads` | 可选的预期池线程数，用于覆盖判断；必须来自当前引擎配置 |
| `dense` | 可选的子步骤归因配置；只有采用相应算法且具备有效标定时才配置 `dense_za_v1`，否则使用 `none` |
| `stage_families`、`presentation` | 阶段族、显示名、颜色、视图标题和可选面板；不参与硬件计数计算 |
| `flop` | 选择已支持的 FLOP 适配器并声明完整计算口径；插件 JSON 不执行 Python、Shell 或表达式 |

`presentation` 可提供 `stage_labels`、`stage_colors`、`labels`、`metric_labels`、`palettes` 与 `views`。色值采用 `#RRGGBB`。新增阶段可通过阶段族得到统一配色，通用绘图代码无需识别阶段名。

未知模式、冲突类别、重复 ID 或不支持的适配器会报错；未配置的未知语义保持未知。更换模型插件并不证明模型已完成生产验证，DeepSeek 等模型仍需接入其真实引擎字典、计算口径和数据后验证。

## 模型 FLOP 分子

模型所需 FLOP 独立于硬件核数、频率和实测 MOPA 数。当前支持以下适配器：

| 适配器 | 内容 |
| --- | --- |
| `glm5f_prefill.v1` | GLM-5.3-Flash 参考 prefill 计算口径，读取模型配置和工作负载；不等于任意 GLM 版本或任意稀疏执行口径 |
| `matmul_components.v1` | 由插件列举矩阵与逐元素组件；矩阵采用 `2×m×n×k×repeat`，逐元素采用 `elements×flop_per_element×repeat` |
| `none`、`external_summary` | 不从插件自动计算；需要应用提供同窗口的完整分子 |

组件维度可以使用非负整数、`B`、`T`、`T_c`、`layers`、`config.<字段>` 和这些因子的乘积列表。`layers` 表示所选层数；这里不支持任意公式求值。模型配置可使用顶层字段或 `text_config` 内字段。

`glm5f_prefill.v1` 包含其参考口径中的 KDA／DSA、路由与共享专家工作；所选层包含模型最后一层时，每个请求计一次输出头。该口径排除补零、重算、MTP 与整数查表等额外工作。更换模型结构或执行语义时，应核对定义或提供新的适配器，不能仅沿用模型系列名称。

例如，为通用结构示例提供 `model-config.json`：

```json
{"hidden_size": 4096, "num_hidden_layers": 32}
```

以及 `workload.json`：

```json
{"T": 4096, "B": 1, "T_c": 0, "layers": [0, 31]}
```

其中 `T` 为本次处理的 token 数，`B` 为独立请求数，`T_c` 为已有上下文长度，`layers` 为首尾均包含的层范围。各参数必须与实际计算窗口对应；`T_c` 是否影响结果取决于适配器是否包含使用它的算子。

```bash
python3 -m lxtrace model-flops --model generic-transformer \
  --config model-config.json --workload workload.json --output out/model-flops.json
```

输出记录总量、分阶段量、适配器、工作量声明和模型配置哈希。示例仅计算声明的一次投影与激活，未计 Attention、MoE、输出头等；不能把这个分子用于完整生产模型的 MFU。

可以在生产归因时同时提供配置与工作负载：

```bash
python3 -m lxtrace production-report --model /path/to/plugin.json \
  --model-config model-config.json --flop-workload workload.json \
  --timing capture/trace-tT.bin --count tA=capture/trace-tA.bin \
  --count tB=capture/trace-tB.bin --count tV=capture/trace-tV.bin \
  --output out/production.html
```

`--model-config` 与 `--flop-workload` 必须一起提供。这会计算并保存模型分子，不能自动证明 trace 覆盖了相同的完整业务窗口。缺少完整线程覆盖、完整帧时间、必要计数或同窗口证明时，MFU 保持 `unavailable`。不能仅凭 token 数相同或分子可计算，就把局部 trace 当作完整模型 MFU。完整汇总入口及证据要求见 [生产归因报告](production-attribution.md)。

## 更换硬件配置

可复制完整的 [lx2-profiled-v1.json](../lxtrace/attribution/profiles/lx2-profiled-v1.json)，按目标平台已核实的事实、事件语义和标定更新，并通过 `--hardware /path/to/hardware.json` 选择。

`events` 按 `matrix_instruction`、`load_stall`、`instruction` 等语义角色声明实际 RAW 编码；导入保留原始编码和读数，分解公式通过硬件字典取数。缺少事件的视图保持 partial／unavailable，不把缺失读数当成零。

当前分解后端为 `lx2-sme-v1`。更换同语义事件编码或经确认的吞吐／标定可以使用 JSON 配置；不同的计数器语义或分解公式需要实现新的硬件后端，不能只改芯片名称。每次变更应保留依据与版本，分析结果会记录所选配置。

## 兼容性与权限

未指定模型时仍使用 `glm5f`，未指定硬件时仍使用 `lx2-profiled-v1`。历史模型 v1 配置与旧 GFTRACE 的默认解释继续保留；历史硬件配置未声明事件字典时沿用原 LX2 映射。自定义插件须显式声明自己的映射，不继承 GLM 专属阶段规则。

读取配置、导入已有文件、计算模型分子和生成报告均为离线操作，无需新增 root 权限，也无需安装运行时依赖。采集端是否能读取 RAW PMU 仍由节点权限决定；插件不会授予或绕过硬件权限。详细采集步骤见 [SDK 接入指南](sdk-quickstart.md) 与 [高频采集说明](high-frequency-sdk.md)。
