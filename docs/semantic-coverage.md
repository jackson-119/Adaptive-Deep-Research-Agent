# 最终报告评估（v2）

评分在写作后发生，不进入研究控制器，不作为停止规则，也不提前过滤写作事实。

1. 对每个简报主题，从全局已读取原文中按所有验收项重新检索，单主题最多 8000 字符。
2. 将主题、最终报告和片段交给模型，一次评价该主题的全部验收项。
3. 验证返回项集合、引用来源、引文在片段及原文中的位置。
4. 保存 supported / partial / missing 和理由；引文无效、解析失败或预算不足标记 assessment_failed。

只有报告已经回答且检索到的原文支持，才能 supported。不能用“库中存在答案”替代“报告回答了”。代码示例可支持行为，不要求未列入标准的内部机制或常量字面值。

所有主题评估完成后，`semantic_coverage = supported数 / 验收项数`。存在评估失败时整体为 null，`evaluation_status=incomplete`。无已读证据时验收项为 missing；这与 API 失败导致无法评分不同。

`quotes_validated` 仅表示引文确实存在，语义仍由模型判断。`citation_support` 和 `task_quality` 保持 null，因为尚无声明级独立验证。人工复核不能省略。生产评估与旧的在线证据覆盖定义不同，不能直接比较数字。

用 `python -m adra.report_evaluation --run ... --output ...` 可重评同一份 v2 报告，不重新检索网络，不改写原文件，也不复用旧评分缓存。可通过 `ADRA_COVERAGE_MODEL` 更换评估模型；仍需配置 `ADRA_MODEL`。输出必须为新路径。
