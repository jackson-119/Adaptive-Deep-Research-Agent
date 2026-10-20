# 固定计划真实任务复测

本轮验证官方来源约束、长文档片段选择和语义覆盖能否在真实调用中工作。固定计划排除自动规划的波动；真实网页仍会变化，因此这是一组探索性回归实验，不能单独证明算法收益。

## 1. 准备

在云服务器同步当前代码和 `data/live-eval/`，在项目根目录执行下面的命令。沿用已经成功运行的 DeepSeek、Tavily 环境变量和 provider 依赖；`.env` 不会自动加载。先执行：

```sh
UV_HTTP_TIMEOUT=300 uv run --locked --extra dev --extra live pytest -q
git rev-parse HEAD
git status --short
```

记录代码版本、模型名称、执行时间和预算。若代码未提交，另存不含密钥的代码差异，否则仅记录 commit 无法复现当前实现。不要把 `.env` 或 API Key 纳入实验记录。

本轮全部使用 `--plan`，不用 `--auto-plan`。保持同一模型、计划、允许域名、策略和工具预算，仅切换 `--coverage-mode`。

## 2. 原 LangGraph 问题：先跑两次

```sh
for mode in sources semantic; do
  UV_HTTP_TIMEOUT=300 uv run --locked --extra live adra \
    '根据官方文档，说明 LangGraph 的状态管理与条件路由机制' \
    --plan data/live-eval/langgraph-state-routing.json \
    --strategy adaptive \
    --coverage-mode "$mode" \
    --allowed-domain docs.langchain.com \
    --allowed-domain reference.langchain.com \
    --max-tools 4 \
    --max-steps 6 \
    --output "outputs/langgraph-fixed-4/$mode"
done
```

这保留了原实验的 4 次工具预算和 6 步限制。每次会在指定输出父目录中创建独立运行目录，不会覆盖历史报告。

计划保留原来的两个子任务及首条查询，增加了六项验收标准和备用查询。因此，旧的自动规划报告仅作问题复现参考；真正的配对对照是上面两次使用同一新计划的运行。

检查每个运行目录中的：

- `report.md`：是否清楚回答状态、reducer、持久化、条件路由、映射和结束机制；缺少证据的地方是否如实说明。
- `run.json`：来源 URL、实际阅读状态、工具错误、停止原因、token 和耗时，以及片段选择和原文记录。
- `trajectory.jsonl`：是否因预算耗尽停止，是否有机会执行备用查询。
- `coverage.json`：语义模式下逐项查看 `supported / partial / missing`、引用原句和校验状态。

六项标准为：状态 schema 与部分更新、默认覆盖与 reducer、checkpointer 与 thread_id、状态驱动路由、path_map、导入的 END 常量。人工核对报告是否把 END 常量误写成无映射的普通字符串 `"END"`。

4 次工具调用不足以覆盖六项标准是合理结果。若仍有缺口，把两个模式**都**改为 `--max-tools 8 --max-steps 12`，输出改为 `outputs/langgraph-fixed-8/$mode` 后重新执行。保留小预算结果，不只保留最好的运行。

## 3. 扩展为 3–5 个真实任务

已有任务清单：

| 计划 ID | 问题 | 官方域名 |
| --- | --- | --- |
| langgraph-state-routing | 状态管理与条件路由 | docs.langchain.com、reference.langchain.com |
| python-asyncio | Python 3.12 TaskGroup 与 gather 的异常和取消 | docs.python.org |
| pydantic-validation | Pydantic v2 strict 与 extra | docs.pydantic.dev |
| langgraph-persistence | checkpointer 与 Store、内存持久化边界 | docs.langchain.com、reference.langchain.com |
| httpx-timeouts | 超时、连接池限制与总时限 | www.python-httpx.org |

先跑前三个，每个任务两个模式，共 6 次。下面命令会实际调用模型和 Tavily，使用当前环境配置；默认统一预算为 8 次工具调用、12 步：

```sh
UV_HTTP_TIMEOUT=300 uv run --locked --extra live python - <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

selected = {"langgraph-state-routing", "python-asyncio", "pydantic-validation"}
cases = json.loads(Path("data/live-eval/manifest.json").read_text())
batch = Path("outputs") / (
    "live-eval-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    + "-" + uuid4().hex[:8]
)
batch.mkdir(parents=True)
results = []
for case in cases:
    if case["id"] not in selected:
        continue
    plan = json.loads(Path(case["plan"]).read_text())
    for mode in ("sources", "semantic"):
        out = batch / case["id"] / mode
        command = [
            sys.executable, "-m", "adra.cli", plan["query"],
            "--plan", case["plan"], "--strategy", "adaptive",
            "--coverage-mode", mode, "--max-tools", "8", "--max-steps", "12",
            "--output", str(out),
        ]
        for domain in case["allowed_domains"]:
            command += ["--allowed-domain", domain]
        print(f"\nRunning {case['id']} / {mode}", flush=True)
        completed = subprocess.run(command)
        results.append({
            "case": case["id"], "mode": mode,
            "returncode": completed.returncode, "output": str(out),
        })
        (batch / "runs.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
print(f"\nBatch: {batch}")
sys.exit(1 if any(item["returncode"] for item in results) else 0)
PY
```

批次清单保留失败运行的退出码；固定计划失败时可能没有完整运行产物，请保留终端报错。该脚本仅顺序执行和登记结果，不做自动质量评分。

第一批检查通过后，将 `selected` 加入另外两个 ID，即可扩展为 5 个任务、10 次调用。再考虑重复三轮，总计 30 次运行，统计均值和范围。语义评估会增加模型调用，即使工具预算相同，token 和费用也可能增加；先用前三个任务的实际用量估算后续成本。

## 4. 验收与比较

为每次运行维护一行记录：

| Task | Mode | Budget | Stop | Tools | Tokens | Seconds | Source proxy | Semantic | 人工报告覆盖 | 评估失败 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| … | sources/semantic | 8/12 | … | … | … | … | … | … | … | … |

- 官方来源：搜索保留来源、阅读来源和引用来源的主机名都应在白名单中。白名单按精确主机名匹配；Python 任务还需人工确认文档版本为 3.12。
- 长文档：`truncated_sources > 0` 本身不是失败。检查选中片段是否包含问题相关内容、原文位置是否匹配，尤其是文档后半段；仅看截断数量不能证明问题已解决。
- 语义评估：检查 `coverage_assessment_failures` 和各任务校验状态。对每个 `supported` 人工判断原句是否真的支持对应标准；引用存在不等于语义判断正确。
- 报告质量：对两个模式都按同一份计划标准人工判定“回答正确且有证据 / 部分 / 缺失”。证据覆盖不保证最终报告写出了正确答案。
- 成本与停止：一起比较工具调用、模型调用、token、耗时和停止原因。保留失败和未覆盖项。

`source_coverage_proxy` 与 `semantic_coverage` 定义不同，不能相减当作质量提升；sources 模式没有语义分数时填空，不填零。`citation_reference_validity=1` 只代表引用编号有效，`citation_support`、`task_quality` 仍未自动评测。语义评估使用模型自评，也不是独立质量裁判。

当来源约束有效、评估无未解决异常、引用和截断片段抽查通过、报告缺口被诚实标记时，可判定本轮工程回归通过。随后根据失败案例改进检索与片段选择；若要研究调度策略收益，再单独固定覆盖模式，对比 static/adaptive，避免一次改变多个因素。
