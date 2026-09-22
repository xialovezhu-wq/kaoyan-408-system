# 408 晨间行动队列与派生 Session v3

本规则定义“库存核算”和“一次晨间真正执行什么”的边界。逐项结果统一落到 `schema/review-event-v1.md` 定义的事件账本；本流程不修改正式掌握度、日期、复做记录、正式错题事实或 `复盘输出/`。

## 一、四类集合

| 集合 | 含义 | 是否属于本次晨间分母 |
|---|---|---|
| `inventory_accounting` | 全部复习单元、原题轨、D0/D1、blocked 的来源核算 | 否 |
| `morning_executable_queue` | 本次实际打开并逐项首答的行动队列 | 是 |
| `same_day_extension_queue` | 容量外真实到期项和当天稍后继续项 | 否；保持 open |
| `baseline_inventory` | 尚无自然练习证据的历史库存 | 否；等待自然学习或有效问答产生证据 |

无日期、无评分的重构单元继续保留，但没有证据支持“同一天全部到期”的语义。默认不为它们创建专门的基线任务。

## 二、默认选择

默认参数是可配置工程值，不是学习科学常数：

- 目标：40 分钟；允许按实际日程调整到 35–45 分钟或更长。
- 最多 16 项。
- `baseline_quota=0`；不再用晨间容量专门扫描未校准库存。
- 独立原题最多 2 道，并且必须已激活、到期且满足 7 天保护。

优先级：

1. 救援项：最近失败、复发或掌握度 0–2。
2. 前晚 D0/D1 安全派生项。
3. 有真实下次日期且已经到期 / 过期的正式机制单元。
4. 已激活且满足 7 天保护的独立原题轨。
5. 由自然学习、晚间 D0 或晨间有效问答生成的派生后续 action。

容量外真实到期项进入 `same_day_extension_queue`；未校准库存留在 inventory，等待自然证据。二者都不能被改日期或称为已完成。

## 三、队列字段

`morning_review_action_queue_v3` 至少包含：

- `review_date`
- `generation_id=MRG-<review_date>-<generation_input_sha256>`
- `generation_input_sha256`
- `eligible_due_action_set_sha256`
- `formal_due_source_sha256`
- `queue_render_input_sha256`，防止非 action 的可执行选择或库存渲染变化复用同一 generation ID
- `scheduling_policy_version`
- `review_ledger_cutoff_sha256`、`review_ledger_cutoff_bytes` 与事件行数
- `queue_renderer_sha256` 与 `queue_schema_sha256`
- `definition_status: immutable_ready_snapshot`
- `runtime_status_source` 与 `review_event_source`
- `target_minutes`
- `max_items`
- `inventory_count`
- `formal_due_count`
- `baseline_backlog_count`，仅作库存统计
- `original_question_due_count`
- `derived_d0_count`
- `scheduled_count`
- 各类 selected 数
- `same_day_extension_count`
- `baseline_remaining_count`，仅作库存统计
- `blocked_count`
- `scheduling_gap_count`，表示已有答案但仍 pending、因而被新队列显式排除并等待 append-only 恢复的 action 数
- 复合 `source_hash`

每个 Item 使用稳定字段：

- `item_id`
- `source_id`
- `item_kind`
- `selection_reason`
- `prompt`

由派生 action 形成的 Item 还必须保存精确 `action_id`、`stage` 和
`trigger_event_id`。prepared pack 与 display receipt 以
`morning-schedule-binding-v1` 继续绑定 session、item、source、action、stage、
trigger、queue SHA、prepared-pack SHA 和 surface SHA。该绑定答案安全，只用于本地
调度事务，不能进入模型可见的 `CurrentQuestionContext`。

行动队列是不可变的来源与问题定义，不保存运行期结果或派生反馈。主模型只基于
当前公开题面、当前作答和本地 grader 的受控判定冻结反馈；不得为反馈读取正式
安全单元、D0 历史或其他题面。

### 不可变 generation 与换代

新队列写入
`wiki/study_vaults/408-full/state/morning-review-queue-generations/generations/<date>/<generation_id>/<generation_id>.md`，
日期 current pointer 只移动到完整的新 generation，不覆盖旧 generation。旧的
`<date>-408晨间行动队列.md` 仅作为 legacy generation 只读兼容。

`session_started` 尚未持久化时，若 eligible due-action 集、正式到期源、调度策略、
renderer 或 schema 发生变化，旧 job 与已存在 pack 通过 append-only receipt 标记
`stale_pre_session`，随后创建完整 replacement generation。单纯追加与可执行集合无关的
review-ledger suffix 不使 queue/pack 失效；旧 ledger cutoff 仍作为生成时证据承诺保留。

最终 freshness 检查必须在 `session_started` 所使用的 review runtime lock 内执行，
检查与事件提交处于同一临界区。session 已经 `in_progress` 或 `paused` 后不得换代、
覆盖队列或动态插题。新到 action 保留原 due date 与 pending 状态，并生成
`morning_late_action_deferred_v1` 回执；下一未冻结 generation 将其作为 overdue action
正常纳入选择。晚到集合必须由当前 due-action 集减去 generation manifest 冻结的完整
`eligible_due_actions` 得出，不能用 session 已选 item 集代替。legacy generation 无完整
冻结集合时只返回 `legacy_generation_late_action_unverifiable` 和零回执，不伪造晚到证明。

绑定状态逐层报告：正式回流只能证明 `formal_backflow_satisfied`，generation manifest
只能证明 `queue_bound`，完整 prepared manifest 只能证明 `pack_bound`，durable
`session_started` 才能证明 `session_bound`。任何前一层不得替代后一层。

### 冻结题包执行模式

需要低延迟逐题反馈时，行动队列之后增加一个独立的冷准备阶段。执行器按
`schema/morning-review-prepared-pack-v1.md` 为全部 selected item 一次性生成并
独立校验 main、guided、repair 三套公开 A/B/C/D 题面及各自私有判题胶囊。
只有整包全部 ready，才允许用 `morning_review_session.py start
--prepared-manifest ...` 启动 `interaction_mode=frozen_prepared_pack` 的 session。

题包是答案隔离的 delivery sidecar，不是行动队列、session truth、review
ledger 或正式库。session 只保存 manifest 以及三种角色的 surface 和 grading commitment，
不得保存题干、选项、答案键或反馈正文。健康逐题路径不再生成题目、检索题源、
扫描历史或动态追加 followup；唯一正常写入口是
`managed_408_current_turn.py answer-current-and-next`。旧 `current-turn`、
`publish-evaluation-capsule`、`continue-current`、`next-item`、direct router、
`morning_review_session.py answer-turn` 和 `schedule-followup` 已退役。题包不完整
或哈希漂移时在判题前 fail closed，不能在用户等待期间退化成现场翻库。

## 四、Session 状态机

运行状态由 session truth 持有，并投影到 `runtime.json`、pointer 和 dashboard；不得通过改写队列 frontmatter 驱动状态：

```text
ready -> in_progress -> paused -> in_progress -> closed
                    \-> cancelled
```

`cancelled` 只用于用户明确说明整场晨间复盘未执行，且 session 中尚无任何首答、反馈、修复或 canonical review event 的情形。执行器必须通过 `morning_review_session.py cancel` 追加 `session_cancelled`，保留队列不可变快照和仍为 pending 的派生 action；不得删除 session、手改 JSONL，或用逐项 `dismiss-item` 冒充整场未执行撤销。

逐项事件：

```text
record-first -> reveal-feedback -> record-repair
```

`followup_scheduled`、动态关联练习和变式续题从当前流程退役。历史 session
中的同名事件只作只读重放兼容，不能用于新写入、当前题反馈或下一题选择。
统一答题操作在未解决时保持同一 display receipt；只有当前题回执链完成且该题
仍是 session 导航前沿时，才可在同一次返回中发布既有不可变 queue 的一个后继。
不得独立调用导航原语，不得用旧放行回执跨过后来已作答但未解决的题，也不得临时
生成、检索或推荐题目。

首答记录：

- `result`: `independent_correct / fragile_correct / partial / wrong / blank`
- `choice_result`: `correct / incorrect / partial / uncertain / not_applicable`
- `reasoning_result`: `sound / partial / diverged / not_observed`
- `confidence`: 答后置信度
- `response_seconds`
- `prompt_level`
- `first_break`

`fragile_correct` 是 session 内教学状态，不直接进入 canonical review ledger。
提示后 terminal 必须映射为 canonical `first_result=partial`、
`choice_result=correct`、真实提示级别和 `hint_used=true`，随后由独立
`teaching_resolved` 关闭当前 item；不得产生 independent mastery。

冻结题包的记录型首答还必须接收瞬时 `learner_choice` 与
`display_surface_sha256`。写入前先核对展示题面 commitment，再由私有 capsule
确定性派生 `choice_result`；调用者给出的 `choice_result` 只作一致性断言。两项
瞬时输入都不进入 session event、canonical review ledger 或 capture。无作答使用
受控 `BLANK`，不得用错误 A/B/C/D 选项伪造独立正确。

选项或结论正确与推理链成立是两条独立证据；没有观察到理由时必须写 `reasoning_result=not_observed`，不得由选项正确反推为 `sound`。

当前题 terminal 还必须由根 Sol 冻结的语义判定与私有 choice grader 共同证明：
选项或结论正确、`reasoning_result=sound`、没有已知矛盾、提示依赖与真实 trace
一致。choice-only grader 不得覆盖语义判定。正确选项配 `partial|diverged|not_observed`
推理时仍留在同一 display，继续写私有缓冲，Capture、pending index 和导航均为 0。
无提示首答只有答案与推理都独立正确时才可直接 terminal；提示后解决必须保留
提示依赖，不能改写为独立掌握。

反馈类型使用受控枚举：

- `brief_verification`
- `minimal_hint`
- `progressive_hint`
- `misconception_contrast`
- `corrective_feedback`
- `relearn_required`

未有 `record-first` 事件时，执行器必须拒绝 `reveal-feedback`。需要修复的项目默认隔至少 3 个其他项目再提取；同日最多两次失败修复，之后标 `relearn_required`，不在同日无限刷。

## 五、关闭条件

Session 关闭必须同时满足：

1. `scheduled_count` 中每项都有首答。
2. `independent_correct` 项未使用提示。
3. 每项在首答落账后都已完成必要的安全反馈；包括独立正确项的简短核验。
4. 需修复项已经达到上述 terminal；`relearn_required` 只是暂停/后续重学状态，
   不再单独构成正常 session-complete 证据。
5. 没有静默跳过项。
6. 每个已解决 item 都已封存最终完整会话包、创建恰好一个 Capture，并重开验证
   durable Capture receipt 与最终包 canonical SHA-256 的直接绑定。

`relearn_required` 表示同日安全停止并安排后续重学，不等于通过。关闭 session 也不自动生成正式掌握度。

## 六、统一事件账本

晨间 session 保留自身前端状态和事件历史，但所有可驱动后续调度的结果必须幂等同步到统一账本：

```text
wiki/study_vaults/408-full/state/review-loop/events.jsonl
```

统一账本是学习证据真源。`state.json`、`runtime.json`、`dashboard.json` 和 `当前晨间复盘.md` 均为可重建投影。账本只保存 Item、来源 ID、结果、置信度、反应时、提示等级、首个断点、纠偏结果和调度链；不复制完整题干、答案、选项、答案字母、图片或手写痕迹。

同一时刻最多一个活跃或暂停 session。发布新 ready 队列时若已有活跃 session，必须拒绝替换。历史 v1/v3 session 通过 `review_feedback_loop.py sync-session` 或 `migrate-sessions` 幂等迁移，不以旧 v2 队列作为结果真源。同步时，无提示的 `fragile_correct` 归一为带脆弱标记的 `independent_correct`，使用提示的 `fragile_correct` 归一为 `partial`，`blank` 归一为 `uncertain`；统一 ledger 的受控枚举以 `schema/review-event-v1.md` 为准。

新队列生成必须排除“已有可验证 outcome 但 action 仍 pending”的调度缺口，且在
frontmatter、审计和 dashboard 中显示 `scheduling_gap_count`，不得把它们再次当作
正常到期项。缺口只允许按 `schema/review-event-v1.md` 的
`scheduling_reconciliation` append-only 恢复；不得改写旧队列、旧 session 或原
outcome。恢复后旧 action 不再进入后续队列，唯一后继只在其真实 due date 到达后
重新具备选择资格。

若不可变 prepared pack 或 active session 已冻结同一个后来被更早 outcome 恢复消费的
action，原 pack 保留审计；未作答 item 通过 append-only
`item_scheduling_superseded` disposition 进入 administrative terminal。导航跳过该
item，session state 与 Dashboard 显示 `superseded`，但不得补写首答、反馈、capture
或 Luna 任务。

## 七、正式回流

派生 session 结果不能直接修改：

- `复习单元总表.md`
- `原题复做轨总表.md`
- `错题复做记录.md`
- 节点、关系边、专题链
- `复盘输出/`

派生调度不等待用户给 0–5 分：跨日无提示独立正确形成保持证据并安排下一锚点；提示后正确只形成 repair；仍错误或脆弱结果进入短期复测。上述动作只存在于派生层，不能更新正式掌握度和正式日期。

2026-08-30 起，晨间当前题统一由
`managed_408_current_turn.py answer-current-and-next` 执行私有缓冲与 terminal
finalization。错误、部分、空答、不确定、提示、解释、复述和纠正轮只追加到同一
item 缓冲；不得创建 Capture 或 pending index。`first_break` 没有直接 provenance
时不得写成用户事实；`record-first`、`capture-review-event`、旧 observation、旧
evidence bundle、trace supplement 和旧 adapter 只保留只读历史兼容。

terminal 时固定执行 final package seal/reopen → one Capture → durable Capture
receipt reopen/verify → successor、最后一题 session complete 或显式
`hold_current`/close。Capture 必须直接绑定最终包 locator 与 canonical SHA-256。
held receipt 永久保持 `advance_allowed=false`；用户以后明确继续时，只能通过
`resume-held-session` 创建绑定该 receipt 的新 publish-successor authorization，
不得修改原 answer operation policy。
任一步失败都留在当前题并返回精确恢复 locator；恢复复用同一 item、display、
finalization idempotency 与 Capture identity，不能产生第二个 Capture。

晨间 Capture 只进入 `awaiting_sol_formalization`，始终
`formal_write_authorized=false`、`formal_write_count=0`。热路径不运行
preflight/apply、正式关系检索、全局审计、Luna、8767、后台 worker、consumer、
report 或个性化投影。之后只有独立明确的正式入库授权才允许 Sol 重开最终包与
Capture 并串行编纂。

## 八、退役控制面的只读兼容

既有 `followup_scheduled`、personalization、cross-subject activation、linked
practice、variant demand、selection receipt 与旧 v1/v2 投影均只允许历史审计和
旧 session 重放。新 `answer-current-and-next`、日终 closeout
和下一晨题包准备均不得读取、更新、重建或发布这些对象。它们缺失、过期或损坏
也不能影响当前题反馈、capture 成功状态、Sol 批次或下一晨既有 queue 的发布。
