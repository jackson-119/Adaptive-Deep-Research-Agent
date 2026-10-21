# v2 真实任务评测

旧 sources/semantic 两路对应的是两种研究停止规则；新架构已取消它们。现在每个问题只运行一个动态研究过程，写作后评价这份报告。

## 首轮验收

先用 README 中的固定 LangGraph 简报命令跑一次，检查：

- `run.json.architecture` 为 global_research_v2。
- 模型调用中 writing 在 coverage 之前，无研究过程中穿插的 coverage。
- SEARCH 可能直接获得已读正文，不要求每次 SEARCH 后都 READ。
- 同一原文可被不同主题的最终评估引用，task_ids 不限制可见性。
- 阅读不足时控制器可生成新查询，或 RETRIEVE 重检已下载内容。
- `evaluation_status`、报告内容与人工验收结论一致；引用 ID 有效不等于内容正确。

不要把“必须按某个顺序搜索或访问某网址”当作通过标准。检查的是答案和约束，而不是固定轨迹。

## 扩展任务

```sh
uv run --locked --extra live python -m adra.live_evaluation \
  --cases langgraph-state-routing python-asyncio pydantic-validation \
  --max-tools 8 --max-steps 12 --max-model-calls 32
```

上面仅展示任务，不调用 API。加 `--execute` 执行，`--repeat 3` 检查波动。每次运行会保存简报副本；批次生成 summary.json 和 review.md。

人工逐项复核：范围与语言、问题完整性、引用是否支持声明、有无把不同 API 混同、局限性是否准确。记录运行成功率、人工覆盖、总 token、研究/写作/评估分项 token、外部请求数与耗时。评分 incomplete 的运行应单独报告，不能当零分或排除后声称成功率更高。

新旧架构对比必须用相同问题、预算、来源范围和人工标准。Tavily raw_content 改变了单次请求取得的信息量，须说明这一差异。旧 semantic_coverage 是证据覆盖，新指标是报告验收，不能用两个数的升降直接证明架构收益。

本次代码测试只验证控制流、共享检索、预算、引文校验与错误处理，尚未完成新的在线质量验收。

## 反思消融

```sh
uv run --locked --extra live python -m adra.live_evaluation \
  --cases langgraph-state-routing python-asyncio pydantic-validation \
  --variants baseline event always --repeat 3 \
  --max-tools 8 --max-steps 12 --max-model-calls 32
```

这是 27 次运行的预览；加 `--execute` 才执行。baseline 是当前相同检索与核验链路关闭反思，event 为事件触发，always 为每步触发（仍最多 3 次）。不是旧提交的逐字节复现。三组共享同一冻结简报，按 repeat 轮换执行顺序。

除了 summary.json，还生成 aggregate.json：按问题与组别统计均值、样本标准差、bootstrap 描述性区间，显式列出缺失指标、运行失败和评估未完成数量。少量重复不能证明统计显著性；评分缺失不能悄悄补零或删掉失败样本。

可追加 `langgraph-persistence httpx-timeouts` 扩展到当前全部 5 个开发问题。这些尚不是具有独立参考答案的 50 题研究测试集。新标准 BM25 与旧二值实现也不能混为一组，应记录 research_revision、retrieval_method 和 Git provenance。
