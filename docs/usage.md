# 安装、运行与配置

[返回项目首页](../README.md) · [实验协议](live-evaluation.md) · [评分规则](semantic-coverage.md)

## 安装和配置


Python **3.11+**，无需本地 GPU/NPU；在线研究调用模型 API 与 Tavily。

```sh
uv sync --locked --extra dev --extra live
cp .env.example .env
chmod 600 .env
```

在本地编辑 `.env`，填入自己的密钥：

```dotenv
ADRA_MODEL=deepseek:deepseek-chat
DEEPSEEK_API_KEY=你的密钥
TAVILY_API_KEY=你的密钥
# 可选：不同阶段使用不同模型，未设置时继承 ADRA_MODEL
# ADRA_PLANNING_MODEL=deepseek:deepseek-chat
# ADRA_WRITING_MODEL=deepseek:deepseek-chat
# ADRA_COVERAGE_MODEL=deepseek:deepseek-chat
# ADRA_REFLECTION_MODEL=deepseek:deepseek-chat
# ADRA_VERIFICATION_MODEL=deepseek:deepseek-chat
```

`.env` 已被 Git 忽略。程序不自动加载它，在可信的自建 `.env` 上执行：

```sh
set -a
source .env
set +a
```

## 跑一个真实任务

**真实运行，会调用付费 API。** 固定简报复测 LangGraph（限制 Python 官方文档路径）：

```sh
UV_HTTP_TIMEOUT=300 uv run --locked --extra live adra \
  '根据官方文档，说明 LangGraph 的状态管理与条件路由机制' \
  --plan data/live-eval/langgraph-state-routing.json \
  --allowed-domain docs.langchain.com \
  --allowed-domain reference.langchain.com \
  --max-tools 8 --max-steps 12 --max-model-calls 32 \
  --output outputs/langgraph-v2
```

不传 `--plan` 时自动生成简报，也可显式加 `--auto-plan`。旧 `--strategy adaptive/static/baseline` 不再被正式入口接受；默认仅有 `research`。旧 `--coverage-mode sources` 已移除。`--coverage-mode none` 仅关闭验收项评分，不改变研究策略；声明核验由 `--no-verify-claims` 单独关闭。

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--plan` | 自动生成简报 | JSON 简报，query 必须与输入一致 |
| `--allowed-domain` | 无 | 可重复指定精确主机名，与简报的路径限制取交集 |
| `--max-tools` | 6 | 外部搜索/抓取请求上限，失败请求也计数 |
| `--max-steps` | 10 | 研究行动上限，包括本地检索与被拒绝的动作 |
| `--max-model-calls` | 32 | 规划、研究、写作、评分共享的硬请求上限 |
| `--coverage-mode` | semantic | 最终评分；none 表示关闭 |
| `--reflection-mode` | event | off 基线 / event 事件触发 / always 每步触发，均受上限约束 |
| `--max-reflections` | 3 | 反思尝试次数上限，失败也计数 |
| `--reflection-context` | compact | compact 使用目标、缺口、最近三步与有限证据；full 使用研究控制器上下文 |
| `--history-window` | 6 | 模型仅接收最近 K 个动作与旧历史计数摘要 |
| `--verify-claims` | 开启 | 写作后逐声明核验；`--no-verify-claims` 关闭 |
| `--output` | outputs | 运行目录的父路径 |

研究阶段为写作保留最后一次模型调用；反思需同时留出下一次决策和写作额度。最终验收和声明核验使用剩余预算，声明较多时可提高总上限，比较实验必须使用同一上限。预算不足时 `evaluation_status=incomplete`、`semantic_coverage=null`，不是质量得分零。工具预算用尽会进入写作，写作前仍执行本地证据检索。

## 测试与评测

本地回归，不调用付费 API：

```sh
uv run --locked --extra dev --extra live pytest -q
```

真实任务批次：**第一条仅预览，不调用 API；第二条含 `--execute`，真实执行并产生 API 消耗。** 默认包含 LangGraph、Python asyncio、Pydantic 三个问题，每个问题研究一次并评估其报告。

```sh
uv run --locked --extra live python -m adra.live_evaluation
uv run --locked --extra live python -m adra.live_evaluation \
  --cases langgraph-state-routing python-asyncio pydantic-validation \
  --max-tools 8 --max-steps 12 --max-model-calls 32 --execute
```

可用 `--repeat 3` 测量波动；会按次数增加真实 API 消耗。批次保存简报副本、汇总和人工复核表。反思消融实验：

```sh
uv run --locked --extra live python -m adra.live_evaluation \
  --cases langgraph-state-routing python-asyncio pydantic-validation \
  --variants baseline event always --repeat 3
```

此命令仅预览 **27 次运行**，加 `--execute` 才调用 API。三组使用相同的修正后 BM25、简报、模型配置、工具/模型预算和最终核验；只改变反思触发方式。不是对旧提交的原样复现。每个 repeat 轮换组别顺序。`aggregate.json` 按问题和组别给出均值、样本标准差、描述性 bootstrap 区间及缺失数量；小样本区间不支持效果显著性结论。

### 评估失败重试与反思上下文对照

生产覆盖评估按子任务、声明核验按声明独立尝试，默认最多两次。只重试无效输出、引用校验失败及部分暂时性请求错误；已完成的低分不会重试，认证失败和模型预算耗尽不重试。所有尝试保留 `attempts`（错误类型、阶段），失败仍计为未完成，不能当作零分。

已有 `run.json` 可以独立重评，**会调用模型 API**，不重新搜索或改写报告：

```bash
UV_HTTP_TIMEOUT=300 uv run --locked --extra live python -m adra.report_evaluation \
  --run outputs/你的运行目录/run.json \
  --output outputs/reevaluation-v2.json \
  --max-model-calls 48 --max-attempts 2
```

新文件保存原始输入快照、原运行文件 SHA256、评分版本、模型配置和尝试记录；输出已存在会拒绝覆盖。重评会对全部标准与声明重新评分，不挑选历史最高分。版本或模型变化后的分数应分别报告，并给 Baseline/Event 使用相同设置。48 次只是调用上限，仍可能不足，必须检查覆盖评估和声明核验两项状态。

默认反思使用 `--reflection-context compact`：原验收目标、当前缺口、最近三步、剩余预算和最多 6000 字符的证据正文；`full` 保留完整研究上下文作为对照。压缩可能丢失上下文，收益尚需真实实验验证。

先预览三个任务、每组重复三次的实验（默认不调用 API）：

```bash
uv run --locked --extra live python -m adra.live_evaluation \
  --cases langgraph-state-routing python-asyncio pydantic-validation \
  --variants baseline event event-full --repeat 3 --max-model-calls 48
```

确认预算后追加 `--execute`，共 27 次真实研究运行；只比较 Baseline/Event 则移除 `event-full`，共 18 次。`event` 是压缩反思，`event-full` 是完整上下文反思。来源数量、单次高分及未完成评分均不能证明算法收益。

本轮同时修正 RETRIEVE 查询参数优先级，以区间并集计算新增证据字符，并按原始标准及行动查询在历史完整片段间分配最终交接预算。交接仍有 12000 字符上限，并不保证保留全部证据。研究阶段不使用最终评分调度动作。

<a id="artifacts"></a>

## 产物和指标

每次在输出目录中新建唯一子目录，包含：

- `report.md`：带来源 ID 的报告。
- `run.json`：原文、来源目录、配置、模型调用计量、最终状态。
- `trajectory.jsonl`：行动理由、参数、观测、工具成本，不重复存储整份证据。
- `plan.json`：不变的研究简报；priority 作为目标重要性提示，dependencies/target_sources 不控制调度。
- `goals.json` / `reflections.json`：目标状态、反思触发原因、输入及引文。
- `decisions.jsonl`：模型实际决策上下文与行动，包含 FINISH；系统指令记录在模型调用记录中。
- `claim_verification.json`：逐声明结果，完整核验才发布支持/矛盾/证据不足比例。
- `research_notes.json`：写作实际使用的片段与原文偏移。
- `evaluation.json` / `coverage.json`：报告验收结果、引文位置和失败状态。
- `failure.json`：异常类型及安全诊断，参数解析错误除外。

`semantic_coverage` 现在是**报告满足且获得原文支持的验收项比例**，不是旧版本的研究中证据覆盖率，不能直接比较。`citation_reference_validity` 仅检查引用 ID 是否存在；不代表引用内容正确。`phase_tokens` 分阶段统计消耗；未知消耗记 null。

`tool_attempts` 是 SEARCH/READ 动作数，`tool_requests_sent` 是实际调用外部适配器的次数（包括请求失败，不等同于服务商计费账单），`tool_successes` 是成功处理次数，`tool_rejections` 是请求前的本地拒绝数。`tool_calls` 仍等于外部适配器调用数。本地 RETRIEVE 仅计步骤。`new_evidence` 表示来源内容变化或出现未见过的原文字符区间；`new_characters` 按同一来源版本的区间并集计算，二者都不是语义信息增益。

