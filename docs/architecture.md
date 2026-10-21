# 研究架构 v2

正式入口 `adra.cli → adra.graph` 不再使用旧任务调度器。

## 参考实现与取舍

| 参考 | 原实现做法 | 本项目采用 | 未复制的部分 |
| --- | --- | --- | --- |
| [Open Deep Research](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py) | 简报、模型驱动研究循环、压缩结果、最终写作 | 简报驱动动态查询，研究与写作分开 | supervisor 并行分派、多研究者、模型摘要压缩 |
| [ODR 搜索工具](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/utils.py) | 搜索获取 raw_content，再整理给研究者 | 搜索请求正文，有正文直接入库 | 不额外为每页调用摘要模型 |
| [STORM 信息表](https://github.com/stanford-oval/storm/blob/main/knowledge_storm/storm_wiki/modules/storm_dataclass.py) | 合并不同访谈来源，向量检索共享片段 | 全局证据可见、按主题检索、URL 去重 | 当前是 BM25，不是其 SentenceTransformer 编码器 |

这是明确的架构借鉴，不是源代码复制或效果复现。外部项目也不能保证召回、判断和生成完全正确。

## 模块边界

- `planning.py`：简报 schema、输入范围校验。兼容旧任务文件，字段不再驱动轮转或成本评分。
- `research_models.py`：SEARCH / READ / RETRIEVE / FINISH 决策合同，含理由与关注主题。
- `policy.py`：模型适配、请求计量、生成简报、研究决策、写作、最终报告判断。
- `graph.py`：LangGraph 阶段编排与预算执行，写作后才调用最终评估。
- `evidence_store.py`：全文按 URL 存储、摘要目录、全局 BM25 片段检索、原文摘要校验。
- `tools.py` / `domains.py`：外部请求、源地址范围校验。
- `coverage.py`：最终评估结构和原文引文校验；保留历史函数兼容测试，但生产调用显式使用全局检索结果。
- `report_evaluation.py`：只对保存的报告重评，无搜索和改写。
- `live_evaluation.py`：固定简报批次、成本记录、人工复核。
- `legacy/`：历史调度器、图、CLI，不被新主流程调用。

## 执行约束

研究者看到简报、来源目录、至多 12000 字符的检索原文、行动观测和剩余预算。它可以生成初始查询列表之外的新查询。每个非 FINISH 动作消耗一步；搜索/抓取消耗一次外部调用，本地检索不消耗外部调用。重复动作与已读 URL 再读被拒绝并留下观测。

搜索正文与 READ 正文保存在原文库，目录只展示短预览。检索从全文重新选段，而非在第一次截断结果上继续检索。每次按所有主题和当前 focus 分配片段，写作时再按全部验收主题检索。片段保留偏移与摘要；任务发现记录不充当权限过滤器。

控制器保留一次模型请求给写作，写作完成后最终评估使用剩余请求预算；评估不足或无效引文显示 incomplete，不将其伪装成零分。单次输出 token 上限仍由 RuntimeConfig 控制；模型调用数上限不等于总费用上限。

## 故障与局限

外部工具失败记录安全异常类型并计入请求成本；模型执行失败会由 CLI 保存 failure.json。当前没有中断检查点恢复，失败记录不保证保留失败前所有中间原文。评估器失败在阶段内处理，不销毁已经生成的报告。

BM25 使用中英词项集合与重叠窗口，属于轻量检索实现。不会自动理解同义词，也没有 reranker。全文会驻留内存，因此大任务需要进一步限制文档规模或迁移持久化索引。当前单研究者没有并行隔离上下文，长任务的目录与历史仍会增长。

## 审查后补充：目标、反思与声明核验

`research_control.py` 管理与简报分离的目标状态和反思触发规则。`reflection_mode=off/event/always` 可消融；CLI 默认 event，程序调用 `run()` 不传 options 则保留 off/不核验的基础行为，测试和嵌入式调用应显式传入 ResearchOptions。

生产图增加 reflect 节点，反思结果不能改简报，只能更新附带原文引文的暂定目标状态和下一步建议。研究者报告疑似冲突、停滞、过半预算或提前完成时可进入此节点；每步至多一次，总次数和模型请求均受限。始终为后续决策与写作保留请求额度。

最终主题评估之后增加 verify 节点：对报告中的每条 claim，只检索它实际引用的来源并进行语义核验。缺少词法命中时使用写作时的原文片段作为回退。有效引文加模型 verdict 仍不是独立真值。结果单独保存，报告正文不自动改写。

模型输入只保留最近 K 步及旧历史计数摘要，来源目录最多 30 项，全局原文库不受目录截断影响。decisions.jsonl 保存模型实际输入和 FINISH，模型调用记录保存 decision/reflection 系统指令；可检查决策上下文，但没有生成概率，不构成直接可用的 GRPO 数据。

检索方法已改为 `bm25_tf_v2`：保留词频，重叠片段合并。前文提到的词项集合评分是旧版本，实现与精确定义见 [修改对照](review-followup.md)。
