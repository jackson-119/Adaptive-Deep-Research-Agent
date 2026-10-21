# Adaptive Deep Research Agent

有预算约束的研究 Agent：生成研究简报、动态搜索、共享原文证据、生成带引用报告，再独立评估报告。

**当前正式入口已切换为 `global_research_v2`。** 原来的静态/自适应任务轮转、按 URL 数量停止、每次阅读后全量评分不再用于在线研究。旧代码隔离在 `adra.legacy`，仅供历史合成实验复现。

本次重构参考 [Open Deep Research](https://github.com/langchain-ai/open_deep_research) 的研究循环与写作分离、[STORM](https://github.com/stanford-oval/storm) 的共享证据检索。不是这两个系统的完整复刻，也尚未证明真实任务质量提高。

## 整体流程

```mermaid
flowchart LR
    A[问题或固定简报] --> B[研究控制器]
    B --> C[搜索 / 抓取 / 本地检索]
    C --> D[共享原文库与全局检索]
    D --> B
    B -->|完成或预算停止| E[检索所有验收主题的证据]
    E --> F[生成报告]
    F --> G[最终报告评估]
```

- **研究简报**规定范围与验收标准，初始查询仅是建议，不是必须执行的队列。
- **研究控制器**根据发现生成新查询、选择未读来源、换关键词检索已有原文，或结束。没有按任务轮转的固定规则。
- **全局证据库**按 URL 去重、保留正文和 SHA256；任何主题都能检索全部已读取原文，发现来源的任务不再决定可见性。
- **搜索**请求 Tavily 同时返回正文，有正文即入库，无正文才需要 READ。一次工具调用对应一次外部请求。
- **检索**使用轻量 BM25 排序、重叠文本块和字符偏移。`RETRIEVE` 不消耗外部工具预算，但消耗研究步骤；不是向量检索。
- **写作**只引用从已读取原文中选出的片段，不依赖评估器先行批准。
- **最终评估**在报告完成后按主题执行一次，检查报告是否回答验收项及原文支持；失败不丢失报告。可单独重评，无须重新搜索。

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
```

`.env` 已被 Git 忽略。程序不自动加载它，在可信的自建 `.env` 上执行：

```sh
set -a
source .env
set +a
```

## 跑一个真实任务

固定简报复测 LangGraph（限制 Python 官方文档路径）：

```sh
UV_HTTP_TIMEOUT=300 uv run --locked --extra live adra \
  '根据官方文档，说明 LangGraph 的状态管理与条件路由机制' \
  --plan data/live-eval/langgraph-state-routing.json \
  --allowed-domain docs.langchain.com \
  --allowed-domain reference.langchain.com \
  --max-tools 8 --max-steps 12 --max-model-calls 32 \
  --output outputs/langgraph-v2
```

不传 `--plan` 时自动生成简报，也可显式加 `--auto-plan`。旧 `--strategy adaptive/static/baseline` 不再被正式入口接受；默认仅有 `research`。旧 `--coverage-mode sources` 已移除。`--coverage-mode none` 仅关闭最终评分，不改变研究策略。

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--plan` | 自动生成简报 | JSON 简报，query 必须与输入一致 |
| `--allowed-domain` | 无 | 可重复指定精确主机名，与简报的路径限制取交集 |
| `--max-tools` | 6 | 外部搜索/抓取请求上限，失败请求也计数 |
| `--max-steps` | 10 | 研究行动上限，包括本地检索与被拒绝的动作 |
| `--max-model-calls` | 32 | 规划、研究、写作、评分共享的硬请求上限 |
| `--coverage-mode` | semantic | 最终评分；none 表示关闭 |
| `--output` | outputs | 运行目录的父路径 |

研究阶段为写作保留最后一次模型调用；最终评估使用剩余预算。预算不足时 `evaluation_status=incomplete`、`semantic_coverage=null`，不是质量得分零。工具预算用尽会进入写作，写作前仍执行本地证据检索。

## 测试与评测

本地回归，不调用付费 API：

```sh
uv run --locked --extra dev --extra live pytest -q
```

真实任务批次：先检查任务清单，再实际执行。默认包含 LangGraph、Python asyncio、Pydantic 三个问题，每个问题研究一次并评估其报告。

```sh
uv run --locked --extra live python -m adra.live_evaluation
uv run --locked --extra live python -m adra.live_evaluation \
  --cases langgraph-state-routing python-asyncio pydantic-validation \
  --max-tools 8 --max-steps 12 --max-model-calls 32 --execute
```

可用 `--repeat 3` 测量波动；会按次数增加真实 API 消耗。批次保存简报副本、汇总和人工复核表。

只重评已有 v2 报告（替换为真实 run.json 路径），不需要 Tavily 请求：

```sh
uv run --locked --extra live python -m adra.report_evaluation \
  --run outputs/langgraph-v2/实际运行目录/run.json \
  --output outputs/review-v2.json --max-model-calls 8
```

## 产物和指标

每次在输出目录中新建唯一子目录，包含：

- `report.md`：带来源 ID 的报告。
- `run.json`：原文、来源目录、配置、模型调用计量、最终状态。
- `trajectory.jsonl`：行动理由、参数、观测、工具成本，不重复存储整份证据。
- `plan.json`：研究简报；历史 priority、dependencies、target_sources 字段仅兼容输入，不控制执行。
- `research_notes.json`：写作实际使用的片段与原文偏移。
- `evaluation.json` / `coverage.json`：报告验收结果、引文位置和失败状态。
- `failure.json`：异常类型及安全诊断，参数解析错误除外。

`semantic_coverage` 现在是**报告满足且获得原文支持的验收项比例**，不是旧版本的研究中证据覆盖率，不能直接比较。`citation_reference_validity` 仅检查引用 ID 是否存在；不代表引用内容正确。`phase_tokens` 分阶段统计消耗；未知消耗记 null。

## 架构范围与限制

这是以成熟模式重构的单研究者实现。没有复制 Open Deep Research 的并行 supervisor/researcher 系统，也没有复制 STORM 的多视角访谈和向量检索。

BM25 对中英术语、同义词与跨段落事实可能召回不足；片段上下文有限。控制器可能选择错误查询或提前停止，模型评分也可能误判。官方路径限制无法保证页面内不混用其他语言示例。引文位置校验只证明原文存在，不能证明语义蕴含。还没有声明级独立事实核验、进程中断恢复或训练链路。

新架构已用可控工具和模型替身验证运行约束；真实 DeepSeek + Tavily 的质量、成本改善需要重新实测，不能由通过单测推断。

细节见 [架构与参考](docs/architecture.md)、[真实评测](docs/live-evaluation.md)、[最终评估](docs/semantic-coverage.md)。

历史合成基线独立运行：

```sh
uv run --locked python -m adra.legacy.cli --snapshot data/demo.json
```

`adra.legacy` 不被新生产图导入；保留历史实验代码仅用于验证旧结果和建立对照，旧在线命令不再是推荐路径。
