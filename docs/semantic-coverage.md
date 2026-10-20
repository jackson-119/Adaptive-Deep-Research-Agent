# 按验收要点评估语义覆盖

本阶段增加模型辅助的任务覆盖判断，原来的 sources 模式保留为默认基线。
`--coverage-mode semantic` 仅用于 static/adaptive，判定覆盖时不再使用已阅读 URL 数量。

## 计划中的验收要点

每个子任务增加 criteria，包含最多 8 个具有唯一 ID 的要点。开启语义模式时，每个任务必须至少有一个。
示例：

```json
{
  "id": "state_management",
  "description": "解释状态管理与持久化",
  "queries": ["LangGraph state schema reducer", "LangGraph checkpointer thread_id"],
  "criteria": [
    {"id": "state_schema", "description": "说明状态 schema 如何定义字段"},
    {"id": "state_update", "description": "说明节点如何提交部分状态更新"},
    {"id": "reducer", "description": "说明默认覆盖行为与自定义合并"},
    {"id": "persistence", "description": "说明 checkpointer 与 thread_id 各自的作用"}
  ]
}
```

自动规划配合 semantic 模式时，会要求模型生成要点；遗漏会触发原有的有限修正流程。
要点描述也加入全文片段选择的关注词项。模式为 sources 时，即使计划有 criteria，仍按数量判定。

语义模式会给每个要点单独分配片段选择机会，避免把整个任务的关键词混在一起后只选出概述。
要点可选 `retrieval_terms`（最多 20 个字符串），用于补充原文语言中的术语。例如中文的
“说明默认覆盖”可配 `["default", "reducer", "ignores", "left", "replaces", "right"]`。
这些词只用于词法检索，不是支持该要点的证据；最终仍需评估和原句校验。
目前在读取原文、共享来源关联新任务时选段，尚未实现每次评估后自动按缺口重新选段。

adaptive + semantic 的调度先在预算可行、依赖满足且未完成的任务中，选择尝试次数最少的任务，
再按原来的优先级、预计收益与成本评分。SEARCH/READ 每次尝试均计数（含失败），稳定平局按计划顺序。
这让独立任务轮流获得机会，防止某个任务的未读来源耗尽预算；不代表已经估计了真实边际收益。
预算不足时仍可能无法启动所有任务。static 和 sources 模式继续使用原有调度规则。

语义模式在任务内部按未支持要点的描述及 retrieval_terms，对未读来源的标题/摘录与剩余查询做词法评分。
先执行原计划首条查询；有已读证据后，若备用查询得分高于最佳未读来源，且剩余预算至少允许搜索加阅读，
可以提前搜索，不必读完候选队列。否则优先读最相关的来源。轨迹候选项的 selection_reason 会记录
gap_source 或 gap_query。评分只是词项重合度，不保证相关性，更不是模型预测的信息增益。

固定计划可以配置顶层 allowed_url_prefixes，例如：

```json
["https://docs.langchain.com/oss/python/langgraph", "https://reference.langchain.com/python"]
```

列表内部取并集，与命令行 allowed-domain 取交集；前缀按完整路径段匹配，不会把 /pythonish 当成 /python。
执行器过滤搜索结果、读取目标和最终引用；Tavily 提取返回的来源 URL 也必须满足范围，
缺失来源 URL 或披露跳转到其他语言路径时拒绝。搜索服务仍只接收域名过滤，路径过滤在本地执行。
不支持识别服务未披露的跳转或页面正文语言，路径符合范围也不等于内容一定适用。

## 判断与程序校验

每次进入决策节点，评估各任务关联的已阅读证据；搜索摘要不参与语义覆盖判断。
输入没有变化时复用缓存，避免在 describe、decide、write 中重复调用评估模型。
无已阅读证据时直接标为 missing，不调用模型。

模型返回每项的 supported / partial / missing、理由，以及 evidence_id 和逐字引用片段。
程序要求：

- 要点 ID 必须恰好覆盖当前任务，不得遗漏、重复或添加未知要点。
- supported/partial 必须引用；missing 不得携带支持引用。
- 证据必须已阅读且属于当前任务。
- 引用必须完整存在于一个已选片段内，不能跨不连续片段拼接。
- 所引用片段必须与保存原文的字符区间严格相等。

通过后保存引用在原文中的 start/end 和 raw_sha256。校验失败、缺失回放或模型异常时，
本次任务的所有要点保守标为 missing，并保存安全的异常类型。相同证据输入不会无限重试；
证据变化后可重新评估。失败不会自动退回数量覆盖。

评估提示要求严格按原要点判断，不额外增加内部实现要求。校准示例包括：

| 原要点与证据 | 预期 |
| --- | --- |
| 根据状态选择节点；代码读取 state 并返回目标节点 | 可以直接支持，不要求额外解释 Pregel |
| schema 与部分更新；schema 描述加 State -> Partial 签名 | 可以结合判断，不必有完全相同的一句定义 |
| Python Annotated；只有 JavaScript Annotation 示例 | 不能据此判定 Python 行为已支持 |
| END 常量与标签区别；仅见返回 END 的示例 | 仍不足以支持完整区别 |

这是提示词校准而非自动放宽分数；需要下一轮真实调用和人工逐项复核才能验证效果。

只有全部要点为 supported 才完成任务并解除依赖。partial 不按半分计入完成率。
尚有缺口时，可继续阅读现有来源或使用计划中已有的备选查询；目前不生成新查询、不修改任务图。
预算耗尽仍会评估最后一次读取的证据，并在报告局限性中列出未完成任务、要点和理由。

## 在线使用

配置好当前模型与 Tavily 后：

```sh
uv run --locked --extra live adra \
  '根据官方文档解释 LangGraph 的状态更新、reducer 与持久化' \
  --strategy adaptive --auto-plan --coverage-mode semantic \
  --allowed-domain docs.langchain.com \
  --allowed-domain reference.langchain.com \
  --max-tools 6 --max-steps 8
```

覆盖评估使用当前模型，记录为 model_call_records 中的 coverage 阶段。
用量与延迟计入总体指标，但不消耗工具调用数。模型请求有超时，尚无总 Token/时间硬上限。
每次任务的已阅读证据输入变化会产生一次评估；多任务共享来源可能增加模型调用成本。

覆盖评估只发送任务验收要点及选中片段；报告生成只发送问题、精简证据及覆盖摘要，
不发送执行轨迹或重复的 content/passages 正文。原始正文、完整轨迹和引文位置仍保存在运行产物中。
这减少重复输入，但尚未设置总 token 硬上限，也没有取消增量阅读后的全任务重新评估。

## 离线验收与人工复核

```sh
uv run --locked adra \
  --snapshot data/coverage/snapshot.json --plan data/coverage/plan.json \
  --strategy adaptive --coverage-mode semantic \
  --coverage-replay data/coverage/assessments.json --max-tools 2
```

合成例包含 schema 与 persistence 两个要点。2 次工具调用后，来源覆盖率为 1，
语义覆盖为 0.5，任务未完成。改为 `--max-tools 4` 后，读取第二条资料，覆盖为 1。
这是人工编写的合成标注回放，不是离线运行语义模型，更不是算法有效性评测。

assessments.json 以 `assessment_key(task, evidence)` 为键，值为 Assessment 结构。
回放只匹配完全相同的任务和证据输入；更换正文、片段或计划后不会沿用旧结论。
增加 retrieval_terms 默认字段及按要点选段后，旧回放哈希也会变化；仓库合成样例已按原标注更新键。
生成键与模型对象序列化一致，不依赖预算和运行时间。不要将测试任务标注作为训练奖励复用。

每次运行保存 coverage.json，逐任务记录 input_sha256、judgments 和 validation。
run.json 和轨迹同样保留对应判断。人工复核可按引用位置回看 raw_documents 中的原文，
重点检查 partial/missing 及模型误判为 supported 的样本。当前还没有独立抽样复核管线。

新增指标：

- semantic_coverage：所有任务要点中 supported 的比例；按要点数计算，而非任务平均。
- covered_tasks：semantic 模式下表示验收要点全部支持的任务数。
- coverage_assessment_failures：最终状态中评估失败的任务数，不是累计错误调用数。

## 边界

原文存在性校验只证明引用没有被编造，不证明片段真的支持要点。评估模型仍可能忽略矛盾、
误解要点或错误断言支持。它与 Writer 共用模型，**不是独立质量评测器**。
报告中的每条声明仍未逐条经过该覆盖评估；citation_support 和 task_quality 继续保持 null。
未选入上下文的内容不会用于判断，missing 表示当前选中证据不足，不能推断网页或事实不存在。

下一阶段才是缺口驱动的新查询生成、计划版本和动态重规划，并使用独立评测与人工复核验证收益。
