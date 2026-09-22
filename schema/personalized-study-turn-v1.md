# 408 普通学习首答与个性化续题协议 v1（只读历史协议）

状态：已退役。`personalized_study_turn_408.py` 的公开写函数和 CLI 不能再为新题写 outcome、capture、authorization、overlay 或续题选择；它们在读取项目状态前即返回退役错误。现有对象只允许历史审计和格式识别。

有效替代：晨间 managed 题使用 `CurrentQuestionContext v1` 与
`managed_408_current_turn.py`；非终态只写 draft，terminal 后 final package → one
Capture → durable reopen → navigation。普通 standalone 教学只读，明确快速入库才
生成一个 `awaiting_sol_formalization` Capture。热路径无 personalization、后台
Luna/consumer、跨题推荐或变式信息。

以下正文仅说明既有 v1 回执当时的字段与语义，其中所有“公开入口”“固定顺序”“允许展示”和写入要求均不再具有执行权限。

本协议覆盖晨间 session 以外的普通单题、逐题真题复盘和晚间 D0。它把一次真实首答写入统一复盘账本，条件编排一次独立的快速捕获事务，再调用一次有界个性化切片。正式编纂仍是独立事务。

## 一、固定顺序

```text
回答前只读个性化切片
-> 展示当前题或最小讲解
-> 用户首答
-> 写入 daily_practice 或 evening_d0 canonical outcome
-> bounded commit-receipt and exact ledger-range verification
-> 非正确首答通过 exact receipt 条件快速捕获；独立正确固定跳过
-> 调用一次 post-answer personalization slice
-> 重新校验题源与 selection receipt
-> 才允许展示一项关联原题或已验证变式题
```

canonical outcome 与必要的 capture joint receipt 都通过前不得揭晓答案性反馈。热路径禁止运行 review ledger 全量审计。post-answer 个性化失败是 fail-soft，不得撤销已经成功写入的学习证据。

## 二、普通学习事件

公开入口：

```text
python3 scripts/personalized_study_turn_408.py \
  --repo . --package /path/to/answer-safe-package.json
```

输入只允许保存：模式、session/item/source ID、考点、机制、首答总判定、选择/结论判定、推理链判定、信心、提示层级、答案安全的首个断点及 provenance、时间、正式节点或复习单元引用、题面类型和幂等键。

入口拒绝所有未登记字段；出现 question/stem/options/answer/solution/analysis、完整作答、反馈、图片或附件字段时，在任何写入前失败。`first_break` 仍由统一 review writer 的答案安全扫描复核。真题逐题复盘使用 `source=daily_practice` 和 `presentation_kind=original_question`；只有可靠正式题目身份存在时，才传 `formal_node_id` 并推进原题冷却日期。

`choice_result` 使用 `correct | incorrect | partial | uncertain | not_applicable`，`reasoning_result` 使用 `sound | partial | diverged | not_observed`。二者必须分开记录；没有观察到推理时写 `not_observed`，不能把选项正确反推成推理正确。二者与总判定冲突时在落账前拒绝。

## 三、快速捕获边界

普通学习事件是个性化学习证据；快速捕获是待日终正式编纂的事实证据。两者由同一公开入口顺序编排，但仍是两个真源、两个 receipt，不能互相伪装：

- `personalized_study_turn_408.py` 对 `partial | wrong | uncertain` 自动把同一次 outcome 的 `review_event_id`、`review_event_sha256` 和 `review_commit_receipt_sha256` 原样交给 `ordinary_capture_hot_adapter_408.py`；
- `independent_correct` 固定返回 `NOT_ELIGIBLE`，不调用 capture adapter、不增长 capture ledger；
- 普通记录型 outcome 在没有本轮显式快速入库授权时，只能生成
  `captured_unconfirmed`；该兼容路径不得自行取得正式编纂授权；
- 当前用户消息明确说“快速入库”时，受管入口必须使用
  `record_explicit_fast_intake`：canonical outcome 先提交，随后同一个 capture
  lock 内依次提交一条 `fact_captured` 和一条独立
  `capture_authorized`，终态直接为 `awaiting_daily_curation`；
- 显式快速入库仍是事实捕获和授权两条独立事件、两份独立 receipt，不能把
  outcome 或 fact receipt 伪装成授权证明；
- 正确和 fragile-correct 都不创建 capture，也不创建 authorization；
- 日终正式语料层可绑定 capture、正式节点和 receipt；
- 没有 canonical learning outcome 的 capture 不得凭正式节点反推一次失败。

如果 outcome 已提交但 capture 失败，不回滚或删除 canonical outcome，也不调用 post-answer slice。入口返回 `incomplete_recoverable`、精确 event/receipt 恢复句柄、`feedback_authorized=false`，并要求以同一幂等键重试。fact 已成功但 authorization 未取得 PASS 证明时，只返回内容寻址 recovery ticket，不能报告快速入库完成；同键重试只补缺失授权，不重复 outcome 或 fact。这里是受管回答完成门禁，不宣称跨两个 JSONL 的分布式原子事务。

默认紧凑结果只返回 outcome、capture、authorization、终态 receipt 和恢复票据，
不超过 2 KiB。完整嵌套诊断只能按需读取，且不得重新运行全局 audit。

## 四、续题展示门禁

只有同时满足以下条件时，普通对话才能自动展示下一项：

1. post-answer slice 来自 `verified` 模式；
2. `schedule_allowed=true`；
3. 原题或变式题 source manifest 再次通过 SHA-256 与答案隔离检查；
4. selection receipt 与当前 manifest、learner-state commitment、7 天决策和跨科 live gate 一致；
5. 输出只携带题源定位和安全身份，不复制答案或解析。

任一门禁失败时返回受控 reason code，并回退为当前题的同题引导检查或停止续题。该入口不追加正式节点、关系、掌握度或正式复做记录，固定 `formal_write_count=0`。

成功结果还返回 `personalization_trigger_event_id` 和
`personalization_update_mode`。健康唯一首答必须把前者绑定到刚提交的
`review_event_id`，后者为 `incremental_overlay`；这两个字段用于证明本轮学习证据已经进入即时个性化投影，而不是仅完成账本追加。
