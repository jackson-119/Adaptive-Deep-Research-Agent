# Adaptive Deep Research Agent

面向复杂研究任务的 Python Agent 项目，探索**如何在有限工具调用预算下选择下一步行动**：搜索什么、阅读哪些来源，以及何时停止。

当前提供可运行的研究基线、人工或模型生成任务图上的静态与自适应调度，以及可复现的离线对照实验。后续将逐步引入语义证据评估、动态重规划、Reflection 和 Planner 策略训练。

> 当前处于研究原型阶段。离线示例使用合成资料；在线适配尚未完成带凭据的端到端验证。来源覆盖指标用于检查调度行为，不代表事实正确性或报告质量。

[快速开始](#快速开始) · [在线研究](#在线研究) · [策略与架构](#策略与架构) · [复现实验](#复现实验) · [开发路线](docs/roadmap.md)

## 当前能力

| 能力 | 实现状态 |
| --- | --- |
| 研究执行 | LangGraph 循环，支持 `SEARCH`、`READ`、`STOP` |
| 工具接入 | Tavily 搜索与正文提取；离线精确查询快照 |
| 预算控制 | 工具调用数、决策步数硬上限；请求超时；重复动作拦截 |
| 任务规划 | 人工/模型生成任务 DAG、依赖校验、备选查询、静态与自适应调度 |
| 证据管理 | URL 去重、跨任务来源共享、来源编号与正文记录 |
| 报告与轨迹 | Markdown 报告、JSON 状态、JSONL 逐步轨迹 |
| 实验 | 5 种合成场景，分别运行两种规划策略；保存输入和汇总 |
| 待实现 | 语义覆盖与事实验证、动态图更新、Reflection、SFT/GRPO |

## 快速开始

需要 **Python 3.11+** 和 **uv**。以下命令均在仓库根目录执行。离线流程不需要 GPU 或 API Key；首次安装依赖需要网络。

```sh
# 按 uv.lock 安装运行依赖
uv sync --locked

# 运行基础离线演示
uv run --locked adra --snapshot data/demo.json
```

程序会打印报告路径和指标。基础演示执行 1 次搜索、2 次阅读，输出两条来源及引用。

每次运行创建独立目录，已有结果不会被覆盖：

```text
outputs/<UTC时间戳>-<随机ID>/
├── report.md          # 研究内容、局限性与来源
├── run.json           # 配置、指标、证据、最终状态和完整轨迹
└── trajectory.jsonl   # 每行一个状态—动作—观测记录
```

`outputs/` 默认不纳入 Git。规划模式还会输出可复用的 `plan.json`；自动规划失败时保存 `failure.json`。

### 运行规划策略

```sh
uv run --locked adra \
  --snapshot data/planning/snapshot.json \
  --plan data/planning/plan.json \
  --strategy adaptive \
  --max-tools 4
```

将 `adaptive` 换成 `static`，即可比较相同任务计划下的选择顺序。这个合成示例中，自适应策略会先处理预计成本较低的任务。

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | 无 | 研究问题；离线模式可省略，取快照中的 `query` |
| `--snapshot` | 无 | 离线快照路径；不指定时使用在线模式 |
| `--strategy` | `baseline` | 可选 `baseline`、`static`、`adaptive` |
| `--plan` | 无 | 任务计划 JSON；static/adaptive 使用此项或 `--auto-plan`，baseline 不接受 |
| `--auto-plan` | 关闭 | 在线生成并校验任务计划；与 `--plan` 互斥，仅支持 static/adaptive |
| `--max-tools` | `6` | 工具调用硬上限，范围 0–100 |
| `--max-steps` | `10` | 非 STOP 决策步骤硬上限，范围 1–200 |
| `--output` | `outputs` | 运行结果的父目录，程序在其中创建唯一子目录 |

查看帮助：`uv run --locked adra --help`。

## 在线研究

在线模式通过 LangChain 模型接口决策和写作，通过 Tavily 搜索与提取正文。当前 `live` 可选依赖包含 Anthropic 集成。

以下为 macOS/Linux shell 示例；模型名称需与所用服务账户支持的模型一致：

```sh
uv sync --locked --extra live

export ADRA_MODEL='anthropic:claude-sonnet-4-6'
export ANTHROPIC_API_KEY='你的模型服务密钥'
export TAVILY_API_KEY='你的搜索服务密钥'

uv run --locked --extra live adra \
  '比较静态规划与动态规划在研究 Agent 中的适用场景' \
  --max-tools 6 --max-steps 10
```

[.env.example](.env.example) 仅提供配置示例，程序**不会自动加载 `.env`**。请在本地设置密钥，不要将真实密钥提交到仓库。

在线模式会将问题和检索内容发送给模型服务，将查询与 URL 发送给 Tavily，并产生相应服务费用。更换模型服务时，需要安装对应 LangChain provider 集成，并确认模型支持结构化输出。

static/adaptive 也可以使用在线工具：去掉 `--snapshot`，提供研究问题、`--plan` 和 `--strategy`。问题必须与计划中的 `query` 一致。在这两种策略下，任务选择由确定性调度器完成，模型负责最终报告；使用 `--auto-plan` 时还负责初始任务分解。

### 自动生成任务计划

```sh
uv run --locked --extra live adra \
  '比较静态规划与动态规划在研究 Agent 中的适用场景' \
  --strategy adaptive --auto-plan --max-tools 6 --max-steps 10
```

模型先分解任务，程序校验依赖、查询唯一性、问题一致性和预计预算，再进入研究循环。
计划最多包含 6 个任务；任务数加目标来源数总和不超过工具/步骤预算中较小者。
这只是初始可行性约束，搜索失败或备选查询仍可能耗尽实际预算。

不合法计划最多修正一次；两次均失败则停止，不启动搜索。`--auto-plan` 不支持离线快照，
且至少需要 2 次工具和步骤预算。生成后的任务图在运行中仍保持不变。

`run.json` 包含 `plan_source`、`plan_generation` 和分阶段 `model_call_records`。
规划调用及失败修正的 Token 计入总用量；延迟包括自动规划阶段。
`plan.json` 可在后续运行中通过 `--plan` 重用，以避免每次生成不同计划影响对照。
失败记录只保存异常类型、用量和校验状态，不保存供应商原始错误或模型原始响应。
详见 [自动规划说明](docs/auto-planning.md)。

## 策略与架构

```mermaid
flowchart LR
    Input[问题与可选任务计划] --> Decide[预算检查与动作选择]
    Decide --> Execute[执行 SEARCH / READ / STOP]
    Execute -->|搜索或阅读结果| State[更新证据与轨迹]
    State --> Decide
    Execute -->|STOP| Write[生成报告与检查引用编号]
    Write --> Output[报告 / 状态 / 轨迹]
```

策略与工具相互独立：`Policy` 决定下一步动作，`ResearchTools` 提供工具反馈。在线服务与离线快照复用同一执行流程，便于后续替换 Planner 并比较结果。

| 策略 | 下一步动作如何产生 | 报告如何生成 |
| --- | --- | --- |
| baseline + 快照 | 确定性演示：搜索原问题，逐个阅读来源 | 提取式合成资料演示 |
| baseline + 在线 | 模型输出结构化 SEARCH/READ/STOP | 模型生成带证据编号的声明 |
| static | 选择计划中第一个满足依赖与预算约束的任务 | 使用当前模式对应的报告生成器 |
| adaptive | 每轮重新比较候选动作的覆盖增量与成本 | 与 static 使用相同报告生成器 |

当前自适应策略使用以下启发式：

```text
C = min(已阅读的关联 URL 数 / target_sources, 1)
expected_proxy_gain = min(1 / target_sources, 1 - C)
score = priority × expected_proxy_gain / estimated_cost
```

READ 的预计成本为 1，SEARCH 为 2，后者预留一次正文阅读。两种规划策略都检查剩余工具预算和步骤预算；分数相同时保持计划顺序。候选动作、评分和最终选择保存在轨迹中。

这个分数尚未考虑来源相关性、事实支持或搜索成功概率。当前任务图在运行中保持不变。完整规则见 [规划说明](docs/planning.md)。

### 自定义任务计划与快照

可以从 [示例计划](data/planning/plan.json) 修改任务，每项包含：

| 字段 | 用途 |
| --- | --- |
| `id`、`description` | 子任务标识与目标 |
| `queries` | 按顺序尝试的备选查询 |
| `dependencies` | 必须先达到来源阅读目标的任务 ID |
| `target_sources` | 目标阅读来源数，默认 1 |
| `priority` | 自适应评分使用的优先级，默认 1 |

计划加载时校验重复 ID、未知依赖、循环依赖和空查询。目前查询必须在整个计划内唯一；不同查询返回同一 URL 时，可以共享来源。

离线快照格式参考 [基础快照](data/demo.json) 或 [规划快照](data/planning/snapshot.json)：

- `query`：研究问题。
- `searches`：查询字符串到搜索结果列表的映射；每条结果含 `url`、`title`、`content`。
- `pages`：URL 到正文的映射。

查询使用精确匹配。明确的空结果应写为 `[]`；缺少查询或正文会记录工具错误。离线模式使用确定性演示策略和报告生成器，不会调用模型。

## 复现实验

```sh
uv run --locked python -m adra.evaluation \
  --suite data/planning/suite.json \
  --output outputs/planning-comparison-v2
```

与单次研究命令不同，比较器的 `--output` 指向**本次实验目录本身**，该目录必须尚不存在。重复运行时更换目录名称。

内置实验包含 5 个手工构造场景，每个场景分别执行 static 和 adaptive，共 10 次运行：

| 场景 | 验证内容 |
| --- | --- |
| `budget_tradeoff` | 预算不足时，不同任务目标大小影响选择顺序 |
| `dependency` | 依赖关系优先于文件中的任务排列顺序 |
| `empty_fallback` | 首次搜索为空时尝试备选查询 |
| `failed_read` | 正文读取失败后不会无限重试 |
| `shared_source` | 跨任务复用来源且不修改历史状态 |

比较器保存 `summary.md`、`summary.json`、原始 `suite.json`，以及各场景、各策略的完整运行产物。汇总包含输入 SHA-256 和部分依赖版本；单次结果包含预算和任务计划。

固定输入下，动作顺序、报告与覆盖代理可复现；运行延迟会波动。两个策略使用相同预算上限，实际调用次数可以不同。

**这些实验仅验证程序和调度规则，不能作为真实研究质量提升的证据。** 它们不等同于真实任务开发集、DeepResearch Bench 评测或多随机种子统计实验。

## 指标与实现边界

| 指标 | 当前含义 |
| --- | --- |
| `tool_calls` / `steps` | 已执行的工具调用数与非 STOP 决策步骤数 |
| `source_count` / `read_source_count` | 去重来源总数与成功阅读来源数 |
| `tool_errors` | 执行阶段错误记录数，包含重复动作拦截 |
| `total_tokens` | 模型服务返回的总用量；离线演示为 `null` |
| `token_usage_complete` | 是否获得完整模型用量元数据 |
| `citation_reference_validity` | 非空报告中的引用编号通过已有证据检查；空报告为 `null` |
| `source_coverage_proxy` | 规划模式下，各子任务来源阅读达标比例的平均值 |
| `covered_tasks` | 达到来源阅读目标的子任务数量 |
| `citation_support` / `task_quality` | 尚未评测，保留 `null` |

目前有以下边界：

- 工具次数与步骤有硬上限；总 Token 和总运行时间尚无硬预算。模型单次输出上限为 2048 Token。
- 工具实际请求失败会消耗调用预算；调用前拒绝的非法或重复动作只消耗决策步骤。工具请求没有隐式重试。
- READ 只允许读取已收集来源，证据正文最多保留 6000 字符。
- 引用检查只能确认编号存在，不能确认来源支持声明。没有证据时允许空结论。
- 轨迹中的 `reward`、逐步 `token_cost` 为 `null`，尚未实现奖励或逐步 Token 归因。
- 暂未实现语义事实验证、RAG、MCP、Reflection、SFT 或 GRPO。

## 项目结构

```text
.
├── src/adra/
│   ├── cli.py          # 命令行、配置与结果保存
│   ├── state.py        # 动作、证据、报告与预算结构
│   ├── graph.py        # LangGraph 执行循环与运行指标
│   ├── policy.py       # 演示策略、模型策略与报告生成
│   ├── planning.py     # 任务 DAG、静态/自适应调度
│   ├── tools.py        # Tavily 与离线快照工具
│   └── evaluation.py   # 固定快照双策略对照
├── data/
│   ├── demo.json
│   └── planning/       # 示例计划、快照与场景集合
├── tests/              # 基线、适配器与规划测试
├── docs/
│   ├── planning.md     # 算法规则与实验限制
│   └── roadmap.md      # 分阶段交付与验收标准
├── .env.example
├── pyproject.toml
└── uv.lock
```

## 开发与验证

```sh
# 运行离线测试，不需要 API Key
uv run --locked --extra dev pytest -q

# 可选静态检查
uv run --locked --extra lint ruff check src tests
```

测试覆盖执行流程、预算、错误处理、引用编号、工具请求格式、依赖图、策略选择、来源共享、历史状态不变性和命令行参数约束。适配器与自动规划测试使用模拟响应，不能替代真实在线验证。仅安装 dev 时，与在线 CLI 集成相关的测试会跳过；完整运行使用 `uv run --locked --extra dev --extra live pytest -q`。

## 常见问题

| 问题 | 检查方式 |
| --- | --- |
| 提示缺少模型或搜索配置 | 设置 `ADRA_MODEL`、`TAVILY_API_KEY` 及模型服务对应密钥；`.env` 不会自动加载 |
| 在线运行缺少 LangChain/provider | 使用 `--extra live`；其他 provider 需要额外安装对应集成 |
| 计划与研究问题不匹配 | 对齐命令行问题、计划 `query` 和快照 `query` |
| 对照实验提示目录已存在 | 更换 `--output`，比较器不会覆盖已有实验 |
| 没有执行工具就停止 | 检查预算、依赖和候选动作；规划策略至少需要 2 次剩余调用才能启动搜索 |
| 报告不完整或工具报错 | 查看 `run.json` 的 `stop_reason`、`planning` 与轨迹 `observation`；工具错误仅记录异常类型 |

## 后续路线

1. 验证自动任务分解的真实效果，补充语义证据评估与真实任务基线。
2. 动态任务图更新、停滞/冲突检测与触发式 Reflection。
3. 决策数据收集、按原始任务划分数据集和 SFT。
4. 固定执行器与报告生成器，研究 Planner 的 GRPO 优化。
5. 独立质量评测、消融实验、多次运行统计与失败分析。

阶段状态与验收标准见 [开发路线](docs/roadmap.md)。

## 接口参考

- [LangGraph Quickstart](https://docs.langchain.com/oss/python/langgraph/quickstart)
- [LangChain Models](https://docs.langchain.com/oss/python/langchain/models)
- [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)
