# 408 统一复盘事件 v1

本 schema 定义日常做题、晚间 D0 和晨间复盘共用的安全事件账本。它只记录学习证据、派生调度和待确认路由，不是正式错题库，也不修改正式掌握度、正式日期、节点或关系边。

## 一、真源与投影

唯一学习证据真源：

```text
wiki/study_vaults/408-full/state/review-loop/events.jsonl
```

以下文件都由账本或 session 状态重放生成，损坏或漂移时应重建，不得反向覆盖账本：

```text
wiki/study_vaults/408-full/state/review-loop/state.json
wiki/study_vaults/408-full/state/review-loop/runtime.json
wiki/study_vaults/408-full/state/review-loop/dashboard.json
wiki/study_vaults/408-full/StudyVault/00-Dashboard/当前晨间复盘.md
```

晨间行动队列是不可变的题目与来源定义。开始 session 后不得在队列正文或 frontmatter 中回写 `in_progress`、作答结果或关闭状态。当前状态以 session 状态为事实，再投影到 runtime、pointer 和 dashboard。

## 二、事件种类

账本 schema 固定为 `review_event_v1`，当前支持十一类事件：

1. `outcome`：一次首次作答的结构化结果，也是后续调度和结果路由的入口。
2. `repair`：反馈后的同日纠偏结果，只能形成 repair 证据，不能形成跨日保持证据。
3. `candidate_confirmation`：用户明确确认候选后记录 handoff；该事件本身仍不写正式库。
4. `candidate_disposition`：把未确认候选关闭为只读证据，并按显式请求关闭其未执行 action。
5. `item_disposition`：以答案安全的行政终态关闭重复、冗余或用户移除的 session item，不伪造作答。
6. `date_correction`：只对严格验证的旧跨日晨间事件追加日期校正，关闭或解绑仍 pending 的旧日期 action，并生成替代 action；不改写原 outcome。
7. `scheduling_reconciliation`：为已存在、不可改写且遗漏调度的晨间 outcome 追加调度恢复；只消费其精确绑定的旧 action 并生成一个确定性后继，不伪造第二次作答。
8. `review_schedule_ensure_v1`：正式编纂前的纯调度门禁；只复用或补建一个 canonical action，或记录明确的 `maintenance_noop`，不伪造 learner outcome、baseline、retention 或 mastery。
9. `formal_review_binding_v1`：正式结果提交后，把 capture、ensure receipt、正式结果 receipt、canonical action、正式节点和 receipt-bound RU 显式绑定；固定 `generated_actions=[]`。
10. `grader_outcome_correction_v1`：对已有 outcome 的判分证据追加严格绑定的纠正，只关闭由该错误 outcome 独占触发的 action，不改写原事件。
11. `closed_equivalent_item_retirement_v1`：以正式 binding 和等价证据退休一个未作答的重复 item，不伪造作答、保持或掌握度。

新五步流程的晨间 `study_episode_capture/original_review` 在学习时不先写
`outcome`。网页增强正式执行时，复用第 1 类 `outcome`，并增加
`deferred_morning_binding=cs408-deferred-morning-outcome-binding-v1`，绑定唯一
Capture、episode、原包、display、私有 observed outcome 和网页 decision。其幂等
键由 episode 与 Capture 共同确定；同一 morning session/item 已有 canonical
outcome 时复用旧事件，不追加第二条。

时间优先绑定用户 turn 的真实 `observed_at`。该值为空时，只能读取同一
run/item 的 `campaign_response_observed.event_time`，并同时保存
`time_quality=runtime_observation_recorded_at` 及 runtime event ID/SHA；不得使用
正式入库时间或文件时间推造学习时间。

`review_schedule_ensure_v1` 与 `formal_review_binding_v1` 都是非作答事件，固定
`source=formal_intake_backflow`、`timezone=Asia/Shanghai`、
`formal_write_authorized=false`、`organic_baseline=false`、
`stable_retention=false` 和 `trigger_action_id=null`。它们不能进入 retention、自然
基线、候选路由或正式掌握度统计。

## 三、outcome 字段

每条 `outcome` 至少表达：

- 身份：`event_id`、`idempotency_key`、`request_hash`。
- 时间：`event_time`、`observed_date`；晨间事件还必须有 `session_review_date`。
- 来源：`source`，只能是 `daily_practice`、`evening_d0`、`morning_review`。
- 会话与项目：`session_id`、`item_id`、`source_id`。
- 学习对象：`mechanism_key`、可选 `knowledge_point`、`formal_node_id`、`review_unit_id` 和 `mapped_formal_node_ids`。
- 首答总判定：`first_result`，只能是 `independent_correct`、`partial`、`wrong`、`uncertain`。
- 分离判定：`choice_result` 只记录选择/结论是否正确，`reasoning_result` 单独记录推理链质量；旧事件缺失时分别按 `not_applicable`、`not_observed` 读取，不得反推为正确或合理。
- 主观与过程证据：`confidence`、`prompt_level`、`hint_used`、`first_break`、`first_break_provenance`、可选 `response_seconds`、`latency_state`。
- 纠偏：`correction_result`。
- 跨日与有效性：`cross_day`、`question_valid`。
- 派生判定：`organic_baseline`、`evidence_kind`、`stable_retention`、`fragile_mastery`。
- 调度链：可选 `trigger_action_id` 和 `generated_actions`。
- 路由：`route`。
- 写入边界：`formal_write_authorized` 必须为 `false`。

### Campaign 快速选择 outcome

`morning_coverage_campaign_v1` 的作答仍写为 `event_kind=outcome`，但必须通过
`review_outcome_hot_408.record_campaign_outcome_hot` 提交。该入口只复用 canonical
review hot-state 的 begin、append、verify 与全局 runtime lock；不得调用 legacy
session、旧 D1-D90 outcome writer、Capture adapter 或 Capture writer。

Campaign outcome 除通用字段外必须保存：

```text
response_mode: choice_only | choice_plus_reason
screened_correct: true | false
reason_audited: true | false
recognized_original: true | false
true_item_id
variant_id: string | null
campaign_run_id
campaign_segment_id
campaign_item_commitment_sha256
capture_required: true | false
```

作答时长继续使用通用 `response_seconds`；没有可靠测量时必须为 `null`。FSRS 的
`Again | Hard | Good` rating 是上述原始事实的确定性派生，只能出现在 writer 返回
receipt 与 scheduler 投影中，不得写入原始 `review_event_v1`。

Campaign 映射固定为：

- 未审理由的正确快答：`first_result=partial`、`choice_result=correct`、
  `reasoning_result=not_observed`、`response_mode=choice_only`、
  `screened_correct=true`。它只能派生为 Hard，不能解释为独立掌握。
- 理由已审、无提示、推理 sound 且中高置信：
  `first_result=independent_correct`、`response_mode=choice_plus_reason`、
  `reason_audited=true`，可派生为 Good。
- 理由已审且 sound 但低置信：仍可保存 `first_result=independent_correct`，但只能
  派生为 Hard。
- 错误、不确定、正确选项配错误推理、提示后解决或理由审查失败：按事实分别写
  `wrong | uncertain | partial`，并固定 `capture_required=true`，派生为 Again。

所有 Campaign outcome 固定：

```text
generated_actions=[]
trigger_action_id=null
organic_baseline=false
stable_retention=false
formal_write_authorized=false
scheduling_mode=campaign_fsrs_successor
route.kind=campaign_evidence_only
```

因此该事件立即成为下一次 campaign scheduler 的学习证据，但不会生成 legacy
D1/D1_rescue、候选或自动 Capture。失败所需的完整会话包与唯一 Capture 仍由 campaign
外部完成门拥有；Capture 成功不得追加第二条 canonical outcome。

以下动态关联练习字段已退役，只允许读取既有 outcome，不允许新事件写入：

```text
practice_mode: original | variant
actual_question_formal_node_id
anchor_formal_node_id
variant_of_formal_node_id
followup_event_id
parent_trigger_event_id
context_slice_id
candidate_id
source_ref
source_sha256
selection_receipt_ref
selection_receipt_sha256
```

`original` 必须有唯一 `actual_question_formal_node_id`；`variant` 必须令该字段和 `formal_node_id` 为空，只用 `variant_of_formal_node_id` 保存出题锚点。变式 outcome 可更新考点与机制证据，但不得推进锚点原题的最近真实作答日期。

受控枚举：

```text
confidence: low | medium | high
choice_result: correct | incorrect | partial | uncertain | not_applicable
reasoning_result: sound | partial | diverged | not_observed
prompt_level: none | minimal | progressive | full
latency_state: normal | abnormal | unmeasured
first_break_provenance: user_report | visible_evidence | not_observed | derived_classification
correction_result: independent_correct | partial | wrong | uncertain | relearn_required | not_attempted
```

`independent_correct` 必须同时满足 `prompt_level=none`。反应时未测量时写 `null` 并使用 `latency_state=unmeasured`，不得用 0 伪装测量结果。
当 `choice_result` 为 `incorrect`、`partial` 或 `uncertain`，或 `reasoning_result` 为 `partial`、`diverged` 时，`first_result` 不得写成 `independent_correct`。选项正确但推理偏离时必须保留 `choice_result=correct`，同时把总判定写成 `partial`。
`fragile_correct` 只允许作为 session 内教学标签，不是 canonical review outcome。
提示后答案与推理达到 terminal 时，canonical outcome 固定写
`first_result=partial`、`choice_result=correct`、真实非 `none` 的
`prompt_level`、`hint_used=true`，并用独立 `teaching_resolved` 事件关闭当前题。
它生成正常 D1/repair 证据，不得写成 `independent_correct` 或稳定保持证据。
所有新 outcome 的 `event_time` 转换到 `Asia/Shanghai` 后必须与 `observed_date` 同日；`observed_date` 表示真实作答日期，驱动日终学习汇总、快速捕获日期、冷却和后续调度。日常单题不得通过单独填写日期回填或提前记录。

晨间 outcome 的 `session_review_date` 单独保存不可变行动队列所属日期。跨日续答时，`session_review_date` 可以早于 `observed_date`，不得再用题单日期覆盖真实作答日期。旧晨间事件没有 `session_review_date` 时，不改写原账本；只有 session start 的 `review_date`、唯一首答事件和 `event_time` 全部一致，且已有 canonical binding 不冲突，冷投影才可由 `event_time` 推导真实作答日期，否则标记为不完整并停止日期推断。

## 四、答案安全

事件只保存结构化判定和安全摘要，不保存：

- 完整题干或题目原文。
- 完整答案、标准答案、正确答案或解析。
- 选项文本、选项字母、正确选项或用户此前错选。
- 图片、手写痕迹或临时附件路径。
- 可从字段直接反推出答案的内容。

`source_id`、`mechanism_key`、`knowledge_point` 和 `first_break` 也必须经过受保护内容检查。安全检查失败时拒绝落账。

## 五、幂等与重放

同一自然事件必须使用稳定 `idempotency_key`。同一个 key 加同一个请求重复提交时返回原事件，不新增事件；同一个 key 携带不同证据时拒绝。`event_id`、后续 action ID 和 candidate ID 都是内容寻址 ID。

`state.json` 必须能完全由 `events.jsonl` 重放得到。重放结果至少包含：

- `actions` 和 `pending_actions`。
- `candidates`。
- `organic_baselines`。
- `retention`。
- `review_schedule_ensures` 与 `formal_review_bindings`。
- `action_formal_bindings`；它是从 binding 事件派生的 action 到 RU、一个或多个正式节点、capture 和正式 receipt 的只读投影。
- `formal_write_count=0`。

## 六、自然基线

默认 `baseline_quota=0`，不再建立专门的每日基线 campaign，也不删除或伪造已有未校准记录。

同一学习对象的第一条满足以下条件的自然练习证据，自动成为 `organic_practice_baseline`：

- `question_valid=true`。
- 首次作答证据可用。
- `prompt_level=none`。
- 该对象此前没有 organic baseline。

自然基线只证明“已有一条可审计的自然表现”，不自动生成正式掌握度或正式复习日期。同日重学后的答对不能充当跨日稳定证据。

## 七、D0 到 D1 与晨间后续调度

日常练习与晚间 D0 的 `wrong`、`partial`、`uncertain`，以及正确但中低置信、存在提示、推理断点、耗时异常或其他脆弱结果，生成次日唯一 D1 action。高置信、无提示、无推理断点的 `independent_correct` 不生成晨间 action。提示后答对只能另记 `repair`，不能把原首答改成稳定掌握。action 必须保留 `trigger_event_id`，重复落账不得生成重复任务。

晨间只有同时满足以下条件才生成保持证据并向 D3、D7 等后续锚点前进：

- `source=morning_review`。
- `cross_day=true`。
- `first_result=independent_correct`。
- `prompt_level=none`。
- 置信度为 `medium` 或 `high`。
- 反应时不异常，且没有脆弱覆盖标记。

提示后正确只算 repair；仍错误、部分正确或不确定则进入次日 `D1_rescue`；低置信、猜测或耗时异常进入短期无提示复测。由 v3 session 同步得到的 outcome 是这些判定的真实来源，不再依赖退役的 v2 队列结果。

`D1` 与 `D1_rescue` 在去重时属于共同的 `D1_family`。相同学习身份与到期日只保留一个执行权威；两者并存时 `D1_rescue` 优先，其他仍 pending 的等价 action 只能由后续 `review_schedule_ensure_v1` 或 `formal_review_binding_v1` 追加 `closed_equivalent_d1_family` disposition 后关闭。不得改写或删除旧 action，也不得把已完成 action 伪装成未执行。

正式回流的 ensure 必须绑定 `batch_id`、`capture_id`、原学习日期、来源证据 receipt、调度策略版本和可用的 source review event 因果链。复用既有 action 不能只凭相同正式 ID 共现；缺少 source event 时只能使用本 capture 的幂等 ensure 证据，不能猜测跨 capture 关系。晨间来源必须复用 outcome 或 scheduling reconciliation 已产生的唯一后继；缺失时先恢复原调度，不得由正式回流猜下一锚点。

`formal_review_binding_v1` 的 `review_unit_id` 必须来自已验证的正式结果 receipt，`review_unit_identity_basis=formal_result_receipt`；禁止按 `RU_` 加正式节点 ID 推导。action 原事件保持不可变，reducer 只在派生投影中补齐 bound identity、RU 和正式节点集合。同一 RU 可以绑定多个正式节点；此时保留 `formal_node_ids`，单值 `formal_node_id` 置空，避免伪造唯一节点。

`current_question_minimal` 只限制读取范围，不得清空已经由当前 session item 和 display receipt 精确绑定的 `action_id`。存在该绑定时，outcome、action 消费和唯一后继 action 必须在同一个热事务中提交；`cross_day` 只能由 action 的 `origin_date` 与 outcome 的 `observed_date` 确定性计算。不存在精确绑定时，热路径不得扫描全账本、正式库或其他题来寻找候选 action，也不得从 `review_unit_id` 猜测绑定。

同一 action 最多被消费一次，同一 outcome 最多生成一个后继 action。action、session item、display receipt、queue 或 prepared-pack commitment 任一不一致时必须 fail closed 并返回明确错误，不得静默改绑。非 terminal 回合保持 `capture_write_count=0`、`luna_call_count=0` 和 `formal_write_count=0`；达到 terminal 的 item 只有在最终完整会话包封存后才允许 `capture_write_count=1`。

## 八、当前 capture 路由与历史派生兼容

新当前题事务不在答题回合读取历史、关系、正式映射或生成跨题推荐。当前题精确
绑定的间隔复习 action 消费与单个后继 action 属于同一学习调度事务，不属于跨题
推荐或正式写入。历史
`original_question` action、candidate 和 route 只作旧账本重放兼容，不得用于
当前题反馈、下一题选择或正式写入。

历史结果路由枚举保留为：

- `evidence_only`：独立正确；只写证据和调度。
- `update_existing`：明确映射到唯一正式节点且同一旧错误再次出现；生成更新既有节点候选。
- `new_wrong_candidate`：有明确新错误机制但没有正式节点映射；生成新错题待确认候选。
- `needs_user`：第一个断点不足、映射冲突或证据不足；不推断用户错因。

新结果统一保持 `formal_write_authorized=false`。首答错误、部分、空答、不确定、
提示、解释、复述和纠正阶段只写同一 item 的私有缓冲，不创建 Capture 或 pending
index。根 Sol 的冻结语义判定与私有 choice grader 共同证明选项/结论正确、
`reasoning_result=sound`、无已知矛盾且提示依赖准确后，才允许 terminal
finalization：封存并重开最终完整包，创建该 item 的唯一
`awaiting_sol_formalization` Capture，再重开验证 durable receipt。高置信无提示
首答若答案与推理均独立正确，也走同一 final-package → one-Capture 门禁。
该 Capture 不授权正式写入；旧 `current_question_failure_standing_policy_v1` 只用于
历史事件解释。

既有个性化 `context_slice`、`selection_receipt`、`followup_scheduled` 和
`variant_request` 不属于新 review event 流程。它们只读保留，不得由新
`answer-turn`、`continue-current` 或 `next-item` 追加、校验或消费。

## 九、运行状态一致性

同一时刻最多一个 `in_progress` 或 `paused` 的晨间 session。发布新 ready 队列时，如果已有活跃 session，必须拒绝替换。

`runtime.json` 从 session truth 和当前不可变队列生成；`dashboard.json` 与 `当前晨间复盘.md` 再从 runtime 投影。状态修复统一使用：

```bash
python3 scripts/review_feedback_loop.py reconcile
python3 scripts/review_feedback_loop.py audit
```

审计必须检查账本重放、重复 action、受保护内容、活跃 session 数、runtime/dashboard/pointer 漂移和 `formal_write_count`。

## 十、迁移与命令

既有 v1/v3 晨间 session 使用以下命令幂等同步，不重写旧 session：

```bash
python3 scripts/review_feedback_loop.py sync-session --session MR-...
python3 scripts/review_feedback_loop.py migrate-sessions
python3 scripts/review_feedback_loop.py status
python3 scripts/review_feedback_loop.py daily-summary --date YYYY-MM-DD
python3 scripts/review_feedback_loop.py date-drift-audit
python3 scripts/review_feedback_loop.py correct-date-drift --event-id RE-...
python3 scripts/review_feedback_loop.py correct-date-drift --event-id RE-... --apply
```

`daily-summary` 是只读冷路径：按真实 `observed_date` 汇总正确与非正确首答，并关联既有错题 capture 和人工中性保存。它不启动日终编纂、不写正式库；返回 `complete=false` 时必须同时保留具体的旧事件日期投影错误，不得静默漏题。

`date-drift-audit` 只读列出旧跨日晨间事件及其仍为 pending 的旧日期 action。`correction_status=needs_append_only_correction` 表示该历史事件仍会影响未来调度；审计本身不改写事件或 action，也不得被表述为已完成日期校正。

`correct-date-drift` 默认只返回预览。实际追加必须同时提供 `--apply`，并显式给出一个或多个 `--event-id`，或使用 `--all-verified-pending`。每个 `date_correction` 保存 `source_event_id`、旧/实际日期、验证依据、`closed_action_ids`、共享 action 的 `detached_action_ids`、`replacement_actions` 和旧新 action 映射。同一来源事件使用稳定幂等键，只能形成一次校正。已有 capture 为 `curated` 或 `already_current` 时只保留审计结果，不应用调度变更。

`reconcile-scheduling` 默认只读预览。它只接受能够同时验证原 outcome、session item、action、queue、prepared pack、session state SHA 和处理契约的遗漏调度。一个 action 有多条合格旧 outcome 时，只允许最早的可验证 outcome 成为 authoritative，后续事件仅列为 superseded evidence，不得再次推进。实际恢复必须显式使用 `--apply`，并指定 `--action-id` 或 `--all-eligible`。恢复追加唯一 `scheduling_reconciliation`，保存恢复前 state SHA、全部绑定 SHA、authoritative outcome、被消费 action、生成 action 和稳定幂等键；不改写原 outcome，不追加 session 作答，也不创建 capture。

恢复后的 reducer 必须把旧 action 标为 completed 或 superseded，并且只能保留一个 authoritative 后继。若原证据不能证明稳定保持，返回 `scheduling_evidence_incomplete`；若 action 已被其他事件消费，执行精确幂等重放或 fail closed。应用后在同一运行锁内重建 state、review hot-state、runtime、dashboard 和 pointer。恢复始终保持 `formal_write_count=0`、`capture_write_count=0` 和 `luna_call_count=0`。

若同一旧 action 还冻结在另一个 active/paused session 的未作答 item 中，恢复链必须再追加一个答案安全的 `item_scheduling_superseded` session event，绑定 action SHA、authoritative outcome、reconciliation event 和处理契约。该事件不含用户作答、不生成 outcome、不显示反馈，导航与 closeout 将该 item 视为 administrative terminal 并跳过。事件已存在时只做精确幂等验证；绑定冲突时 fail closed。Dashboard 必须显示这种 superseded item，不能把它当作已答对或正常 pending。

校正属于冷路径。`--apply` 追加事件后，必须在同一把运行锁内重建并验证 review hot-state，再发布运行投影；只有 hot-state 返回 `ready` 才能报告 `DATE_CORRECTED`。若追加后重建中断，原命令的幂等重试负责补做重建，下一次热答题不需要另行运行 `reconcile`。

活跃晨间 session 不得直接调用 `record-outcome`，也不得调用已退役的公开
`record-first` 与 `reveal-feedback` 分步入口。必须通过
`managed_408_current_turn.py answer-current-and-next` 在一个受管当前题事务中写入
session、幂等同步统一 ledger、绑定冻结反馈和私有缓冲；只有 terminal finalization
才创建一个直接绑定最终包的 Capture。
内部 session writer 只接受受管调用标志；旧分步命令仅供历史事件解释，不再注册
CLI。只有晚间 D0、日常练习或不存在活跃 session 的兼容导入使用 direct
`record-outcome`。命令行入口中的 `--question-valid` 是显式开关；有效自然证据
必须传入该参数。例如晚间首次答错：

```bash
python3 scripts/review_feedback_loop.py record-outcome \
  --source evening_d0 \
  --session-id D0-2026-07-16 \
  --item-id D0-Q1 \
  --source-id RU_example \
  --mechanism mechanism-safe-key \
  --first-result wrong \
  --confidence medium \
  --prompt-level none \
  --first-break mechanism-boundary-missed \
  --event-time 2026-07-16T21:00:00+08:00 \
  --observed-date 2026-07-16 \
  --idempotency-key evening:D0-2026-07-16:D0-Q1:first \
  --question-valid \
  --review-unit-id RU_example
```

反馈后修复使用 `record-repair`，并显式保留事件来源、独立幂等键与尝试次数。晚间 D0 的修复示例：

```bash
python3 scripts/review_feedback_loop.py record-repair \
  --source evening_d0 \
  --session-id D0-2026-07-16 \
  --item-id D0-Q1 \
  --source-id RU_example \
  --result independent_correct \
  --confidence medium \
  --prompt-level minimal \
  --event-time 2026-07-16T21:05:00+08:00 \
  --observed-date 2026-07-16 \
  --idempotency-key evening:D0-2026-07-16:D0-Q1:repair:1 \
  --attempt 1
```

用户明确确认待处理候选时使用：

```bash
python3 scripts/review_feedback_loop.py confirm-candidate \
  --candidate-id RC-... \
  --idempotency-key confirm:RC-... \
  --user-confirmed
```

任何命令都不得把普通复盘事件升级为正式错题写入权限。
