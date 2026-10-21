# 最终报告评估（v2）

评分在写作后发生，不进入研究控制器，不作为停止规则，也不提前过滤写作事实。

1. 对每个简报主题，从全局已读取原文中按所有验收项重新检索，单主题最多 8000 字符。
2. 将主题、最终报告和片段交给模型，一次评价该主题的全部验收项。
3. 验证返回项集合、引用来源、引文在片段及原文中的位置。
4. 保存 supported / partial / missing 和理由；引文无效、解析失败或预算不足标记 assessment_failed。

只有报告已经回答且检索到的原文支持，才能 supported。不能用“库中存在答案”替代“报告回答了”。代码示例可支持行为，不要求未列入标准的内部机制或常量字面值。

所有主题评估完成后，`semantic_coverage = supported数 / 验收项数`。存在评估失败时整体为 null，`evaluation_status=incomplete`。无已读证据时验收项为 missing；这与 API 失败导致无法评分不同。

`quotes_validated` 仅表示引文确实存在，语义仍由模型判断。`semantic_coverage` 本身不验证每条引用；声明支持指标见下方新增核验阶段，`task_quality` 仍为 null。人工复核不能省略。生产评估与旧的在线证据覆盖定义不同，不能直接比较数字。

用 `python -m adra.report_evaluation --run ... --output ...` 可重评同一份 v2 报告，不重新检索网络，不改写原文件，也不复用旧评分缓存。可通过 `ADRA_COVERAGE_MODEL` 更换评估模型；仍需配置 `ADRA_MODEL`。输出必须为新路径。

## 声明级支持检查

CLI 默认还会在主题评估之后执行声明核验，用 `--no-verify-claims` 关闭。`ADRA_VERIFICATION_MODEL` 可单独配置，否则继承 coverage 模型，再继承主模型。

每条声明的检索范围严格限制在它引用的来源内。支持与矛盾 verdict 必须提供实际原文引文；证据不足可没有引文。假引文、无关来源、解析错误和预算不足保留为未完成，不归入 insufficient。只有全部声明完成时才公布 supported/contradicted/insufficient claim rate。空报告的比例为 null。

核验不会改写报告，report.md 追加核验状态，claim_verification.json 保留逐声明结论。citation_support 现取模型核验的 supported_claim_rate，应与 citation_reference_validity 区分；task_quality 仍为 null。模型支持率需人工校准，不能当作事实正确率。

单报告重评默认同时核验声明；如只需主题评分，可添加 `--no-verify-claims`。总模型预算仍是硬上限，声明较多时需要预留更多调用。
