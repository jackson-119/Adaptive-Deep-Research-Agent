# Adaptive Deep Research Agent

面向复杂研究任务的 Python Agent 项目，探索**如何在有限工具调用预算下选择下一步行动**：搜索什么、阅读哪些来源，以及何时停止。

当前提供研究 Agent 全链路：固定/自动规划、预算内搜索与阅读、可选语义覆盖、引文笔记、报告与批量评测入口。动态重规划、Reflection 和 Planner 策略训练仍在后续路线中。

> 当前处于研究原型阶段。离线示例使用合成资料；根据云服务器实测反馈，DeepSeek + Tavily 已完成在线端到端功能验证；算法有效性尚未评测。来源覆盖指标用于检查调度行为，不代表事实正确性或报告质量。

[快速开始](#快速开始) · [在线研究](#在线研究) · [策略与架构](#策略与架构) · [复现实验](#复现实验) · [开发路线](docs/roadmap.md)

## 当前能力

| 能力 | 实现状态 |
| --- | --- |
| 研究执行 | LangGraph 循环，支持 `SEARCH`、`READ`、`STOP` |
| 工具接入 | Tavily 搜索与正文提取；离线精确查询快照 |
| 预算控制 | 工具调用数、决策步数、在线模型请求次数上限；请求超时；重复动作拦截 |
| 语义覆盖 | 可选模型逐要点评估、精确引文校验、缺口记录；尚非独立事实验证 |
| 任务规划 | 人工/模型生成任务 DAG、依赖校验、备选查询、静态与自适应调度 |
| 证据管理 | URL 去重、跨任务来源共享、全文选段、原文偏移、域名与 URL 路径约束 |
| 报告与轨迹 | 在线语义模式基于已校验引文笔记生成报告；保存完整状态、轨迹与失败记录 |
| 实验 | 5 种合成场景；真实任务批量对照、固定计划副本、成本汇总、人工复核表 |
| 待实现 | 报告声明级事实验证、独立质量评测、动态图更新、Reflection、SFT/GRPO |

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

`outputs/` 默认不纳入 Git。规划模式输出 `plan.json`，语义模式还输出 `coverage.json` 与
`research_notes.json`。固定或自动规划运行异常会保存 `failure.json`（命令行参数解析错误除外）。

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
| `--allowed-domain` | 无限制 | 可重复指定允许的完整主机名；用于搜索、结果接收、正文提取和引用校验 |
| `--strategy` | `baseline` | 可选 `baseline`、`static`、`adaptive` |
| `--plan` | 无 | 任务计划 JSON；static/adaptive 使用此项或 `--auto-plan`，baseline 不接受 |
| `--auto-plan` | 关闭 | 在线生成并校验任务计划；与 `--plan` 互斥，仅支持 static/adaptive |
| `--coverage-mode` | `sources` | 可选 `semantic`：按验收要点判断任务完成，需 static/adaptive |
| `--coverage-replay` | 无 | 离线语义模式必填；按输入哈希匹配人工标注回放 |
| `--max-tools` | `6` | 工具调用硬上限，范围 0–100 |
| `--max-steps` | `10` | 非 STOP 决策步骤硬上限，范围 1–200 |
| `--max-model-calls` | `32` | 在线规划、评估、报告等模型请求总上限，失败请求也计数 |
| `--output` | `outputs` | 运行结果的父目录，程序在其中创建唯一子目录 |

查看帮助：`uv run --locked adra --help`。

## 在线研究

在线模式通过 LangChain 模型接口规划、评估和写作，通过 Tavily 搜索与提取正文。当前 `live` 可选依赖包含 Anthropic 与 DeepSeek 集成。

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

### 限定官方来源

```sh
uv run --locked --extra live adra \
  '根据官方文档，说明 LangGraph 的状态管理与条件路由机制' \
  --strategy adaptive --auto-plan \
  --allowed-domain docs.langchain.com \
  --allowed-domain reference.langchain.com \
  --max-tools 6 --max-steps 8
```

域名只填主机名，不填 `https://`、路径、端口或通配符。多个参数表示允许其中任意一个。
匹配忽略大小写和一个末尾点，并进行 IDNA 规范化；**不自动包含子域名**。
`docs.langchain.com.example.org` 不会匹配 `docs.langchain.com`。

限制传入 Tavily 的 `include_domains`（restrict 模式），执行器再次过滤搜索结果，
READ 请求及提取服务返回的 URL 也须通过检查，最后复核报告引用的证据来源。
受限模式下，Extract 未返回来源 URL 会拒绝该正文。离线快照执行相同结果过滤。
过滤后没有可用来源时保留空证据，不回退至任意网站；调用仍计入原预算。

`config.allowed_domains` 保存有效配置，`rejected_sources` 统计被拒绝的结果/阅读次数。
新的 `provenance` 字段记录代码提交、工作区是否有修改和依赖版本，便于保留对照。
域名约束不证明文本真实或语义支持；云端提取器未披露的重定向也无法在本地独立核验。
当前约束作用于结构化证据引用，不能作为任意模型输出文本中所有 URL 的净化器。
后续计划见 [证据质量改进](docs/evidence-quality.md)。

### 长文档片段选择

```sh
uv run --locked adra \
  --snapshot data/passages/snapshot.json \
  --plan data/passages/plan.json --strategy adaptive \
  --allowed-domain example.org --max-tools 2
```

这是合成演示：关键 `checkpointer` / `thread_id` 信息放在正文第 6000 字符之后。
系统扫描全文，按子任务描述和查询选择片段，记录 `original_length`、`selected_length`、
`truncated`、`raw_sha256` 和 `passages`。每个片段包含原文字符区间、段落编号及匹配分数。

`run.json.raw_documents[证据ID]` 分别保留 `search` 摘要和 `read` 正文。
原始文本不发送给模型，也不复制到每一步轨迹。共享来源关联新任务时，会用本地正文重新选段，
不额外消耗工具调用。该方法是词项匹配，尚无语义相关性或引用支持判断。
位置定义与限制详见 [长文档处理](docs/passages.md)。

### 按验收要点评估覆盖

在线命令增加 `--coverage-mode semantic`，自动生成计划时会要求每个子任务包含 `criteria`。
模型按要点判断 supported / partial / missing，并提供原文引用及理由。程序核验引用属于
已阅读的关联来源、存在于单个选中片段且与保存原文一致。只有全部要点 supported 才完成任务。

```sh
uv run --locked adra \
  --snapshot data/coverage/snapshot.json --plan data/coverage/plan.json \
  --strategy adaptive --coverage-mode semantic \
  --coverage-replay data/coverage/assessments.json --max-tools 2
```

该人工标注合成回放中，阅读数量已达标，但缺少 persistence 要点，因此任务不会完成。
改为 `--max-tools 4` 后会尝试已有备选查询并补齐示例证据。判断保存在 `coverage.json`；
在线评估的 Token 和延迟纳入总成本。不启用该参数时，保留原有来源数量基线。
详见 [语义覆盖与复核说明](docs/semantic-coverage.md)。模型判断仍可能出错，不代表独立事实验证。

## 策略与架构

以下为在线语义模式的完整链路；sources 模式保留来源数量基线。

```mermaid
flowchart LR
    Input[问题与研究范围] --> Plan[固定或自动任务计划]
    Plan --> Decide[预算与任务调度]
    Decide --> Research[搜索与相关阅读]
    Research --> Evidence[范围过滤 / 全文选段 / 原文记录]
    Evidence --> Assess[覆盖评估与精确引文校验]
    Assess -->|仍有缺口且预算允许| Decide
    Assess -->|完成或停止| Notes[抽取式引文笔记]
    Notes --> Write[报告生成与引用来源检查]
    Write --> Output[产物 / 指标 / 人工复核]
```

策略与工具相互独立：`Policy` 决定下一步动作，`ResearchTools` 提供工具反馈。在线服务与离线快照复用同一执行流程，便于后续替换 Planner 并比较结果。

1. **明确范围**：问题、任务说明、验收标准及允许来源共同定义研究范围；固定计划便于复测，自动规划便于处理新问题。
2. **执行研究**：adaptive + semantic 成功搜索后先阅读一个本次返回的合规未读来源，再交还调度；空结果或失败释放控制。两次请求分别计预算。
3. **检查证据**：模型判断每项 supported/partial/missing；程序核验引文确实来自选中的已读原文。仍有缺口时按相关性选择来源或备用查询。
4. **整理笔记并写作**：在线语义 Writer 只收到已校验引文、范围与覆盖摘要。没有有效引文或模型预算耗尽时输出局限性，保留证据，不强行生成正文。
5. **评测**：保存运行与失败产物；批量入口固定计划进行 sources/semantic 对照，人工按相同标准核查报告，模型自评分不等于独立质量评分。

选择这种分层结构是为了分别测试检索、调度、证据判断和写作；保留来源与偏移方便追查错误；
抽取式笔记减少重复上下文；顺序执行使小预算下的成本和轨迹更容易核对。它参考了 Open Deep Research 的阶段划分、
Anthropic 的任务边界与上下文管理、STORM 的研究/写作分离，并作了适配；没有宣称复现这些系统的完整架构或性能。
逐模块参考链接和实现边界见 [架构说明](docs/architecture.md)。

| 策略 | 下一步动作如何产生 | 报告如何生成 |
| --- | --- | --- |
| baseline + 快照 | 确定性演示：搜索原问题，逐个阅读来源 | 提取式合成资料演示 |
| baseline + 在线 | 模型输出结构化 SEARCH/READ/STOP | 模型生成带证据编号的声明 |
| static | 选择计划中第一个满足依赖与预算约束的任务 | 使用当前模式对应的报告生成器 |
| adaptive | sources 按代理收益/成本评分；semantic 在研究轮次之间平衡任务机会并按缺口选源 | 在线 semantic 使用引文笔记，sources 使用选中证据 |

sources 模式的自适应策略使用以下启发式：

```text
C = min(已阅读的关联 URL 数 / target_sources, 1)
expected_proxy_gain = min(1 / target_sources, 1 - C)
score = priority × expected_proxy_gain / estimated_cost
```

READ 的预计成本为 1，SEARCH 为 2，后者要求预算能够容纳一次后续阅读。
adaptive + semantic 对成功搜索安排一次相关阅读；其他模式的成本检查不保证下一步一定续读。
两种规划策略都检查剩余工具预算和步骤预算；分数相同时保持计划顺序。候选动作、评分和最终选择保存在轨迹中。

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
| `truncated_sources` | 当前选中片段未覆盖完整工具文本的来源数量 |
| `rejected_sources` | 被域名/URL 校验拒绝的结果或阅读次数，非唯一域名数量 |
| `tool_errors` | 执行阶段错误记录数，包含重复动作拦截 |
| `total_tokens` | 模型服务返回的总用量；离线演示为 `null` |
| `token_usage_complete` | 是否获得完整模型用量元数据 |
| `citation_reference_validity` | 非空报告中的引用编号通过已有证据检查；空报告为 `null` |
| `source_coverage_proxy` | 规划模式下，各子任务来源阅读达标比例的平均值 |
| `covered_tasks` | sources 模式下达到阅读目标；semantic 模式下所有验收要点均支持的任务数 |
| `semantic_coverage` | semantic 模式下所有验收要点中 supported 的比例 |
| `coverage_assessment_failures` | 最终状态中覆盖评估失败的任务数 |
| `citation_support` / `task_quality` | 尚未评测，保留 `null` |

目前有以下边界：

- 工具次数、步骤和在线模型请求次数有上限；总 Token、金额和总运行时间尚无硬预算。默认模型单次输出上限为 2048 Token。
- 工具实际请求失败会消耗调用预算；调用前拒绝的非法或重复动作只消耗决策步骤。工具请求没有隐式重试。
- READ 只允许读取已收集来源。原始工具文本保存在 `run.json.raw_documents`；提供给模型的每来源文本不超过 6000 字符，从全文选取而非直接取开头。
- 引用检查只能确认编号存在，不能确认来源支持声明。没有证据时允许空结论。
- 轨迹中的 `reward`、逐步 `token_cost` 为 `null`，尚未实现奖励或逐步 Token 归因。
- 已实现可选任务级语义覆盖和引文存在性校验；暂未实现报告声明级事实验证、RAG、MCP、Reflection、SFT 或 GRPO。
- 缺口相关性仍为词法启发式，可能漏选、错选；计划查询有限，不会自动生成缺口查询或重写任务图。
- 共享来源会使多个任务重新评估，增加模型成本；抽取式笔记也可能丢失必要上下文。
- 路径过滤无法保证页面正文只含指定语言/版本；引文存在不证明模型理解正确。
- 顺序执行增加耗时，尚无并发研究者与中途断点恢复；本地测试通过不代表真实报告质量已经达标。

## 项目结构

```text
.
├── src/adra/
│   ├── cli.py          # 命令行、配置与结果保存
│   ├── state.py        # 动作、证据、报告与预算结构
│   ├── graph.py        # LangGraph 执行循环与运行指标
│   ├── policy.py       # 演示策略、模型策略与报告生成
│   ├── planning.py     # 任务 DAG、静态/自适应调度
│   ├── coverage.py     # 覆盖判断、引文校验与研究笔记
│   ├── passages.py     # 全文选段及原文位置
│   ├── domains.py      # 域名与路径范围
│   ├── configuration.py # 分阶段模型与请求预算
│   ├── live_evaluation.py # 真实任务批次与人工复核入口
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

模块划分、官方参考实现、运行配置与实现边界见 [研究 Agent 架构](docs/architecture.md)。
新增批量验收入口默认不调用 API：

```sh
uv run --locked --extra live python -m adra.live_evaluation
# 检查任务与预算后，追加 --execute 才会实际运行
```

在线 CLI 支持 `--max-model-calls 32` 限制模型请求总数。语义模式生成 `research_notes.json`，
报告只使用已校验的阅读引文；固定计划运行失败也会保存 `failure.json`。

官方来源、长文档片段选择和语义覆盖的真实调用验收，见 [固定计划真实任务复测](docs/live-evaluation.md)：包含原 LangGraph 问题的配对命令、5 个固定计划和 3–5 个任务的批量运行方法。

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
