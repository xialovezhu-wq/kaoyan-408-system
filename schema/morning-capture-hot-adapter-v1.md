# 408 受管晨间热捕获适配器 v1（兼容层）

状态：旧适配器仅保留历史 receipt 识别、精确恢复和旧 session 兼容。新晨间
首答不得把它作为公开 `answer-turn` 入口；统一由
`managed_408_current_turn.py` 创建当前题私有证据 bundle，并按当前 standing
policy 条件捕获。下文涉及 linked followup、personalization selection 或 variant
的规则只说明旧对象如何审计，不允许新写入。

## 1. 使用边界

`scripts/morning_capture_hot_adapter_408.py` 只负责把一个已完成 canonical review 提交和 morning session 绑定的晨间非正确首答，转为一个答案安全、幂等的 `fact_captured`。

它不是正式错题入库器，不分配正式 ID，不改节点、关系边、掌握度、review-unit 或复习调度。所有入口固定 `formal_write_count=0`。

## 2. 热路径输入

`capture_managed_morning_outcome` 必须同时收到：

1. 当前 canonical `review_event_v1` outcome。
2. 该 outcome 的 event-specific review hot commit receipt SHA-256。
3. `first_recorded` 的 event-specific morning session commit receipt。
4. `first_bound_to_loop` 的 event-specific morning session commit receipt。

适配器必须自行通过有界 hot state 重新验证三个 receipt，再精确读取 sequence 1 的 `session_started`。调用方传入的“已验证”状态不能替代此步。

`state_bound_without_event_receipt`、缺失 receipt、receipt 哈希漂移、session/item 不一致、canonical binding 不一致或首答字段不一致，均必须在 capture append 前 fail closed。

## 3. 结果路由

以下仅描述旧 receipt 的历史枚举，不是新 capture 判定入口：

- 旧 `partial`、`wrong`、`uncertain` receipt 可以按原规则验证。
- 旧 `independent_correct` receipt 可以解释为 `NOT_ELIGIBLE`。
- 不得通过本适配器为新首答追加 capture 或扩大旧 policy。

当前判定只由 `managed_408_current_turn.py` 执行：错误、部分、空答、不确定、
可验证推理断点，以及正确但中低置信均 capture；只有高置信、无提示、无断点的
独立正确不 capture。

## 4. 历史关联题证据

只读审计旧 outcome 携带的 `practice_mode` 时，兼容检查器还必须：

1. 按 `followup_event_id` 有界精确读取 `followup_scheduled`。
2. 按 `parent_trigger_event_id` 有界精确读取 parent review outcome。
3. 重新验证 question-only source 文件及 SHA-256。
4. 重新验证 personalization selection receipt、当前 projection 身份、原题或变式题身份和 live gate。

不允许从安全摘要即席编造题面、选项或答案。

## 5. 热写与回执

默认派生热目录是：

```text
wiki/study_vaults/408-full/state/intake-curation/.capture-hot-writer-v1/
```

热写必须在 capture root 的共享 `.lock` 内调用 `capture_hot_writer_408.capture_fact`，执行单条 append、state 原子替换、exact index 增量提交和 joint receipt 发布。

`capture_fact` 必须把其内部已完成的 joint verify 作为 `joint_verification` 返回。适配器直接验收该证明中的 event ID、capture ID、receipt SHA-256 和 `formal_write_count=0`，不得立即再调用一次 `verify_joint_commit` 导致第三次完整 state 读取。新写与幂等重放的 `state_read_count` 上限均为 2。幂等重放只返回原 joint receipt，不追加第二条 event。

morning queue 依然必须与 `session_started.queue_sha256` 一致，但热路径只允许对非链接普通文件做一次最多 1 MiB 的哈希，然后复用该哈希写入 capture evidence ref；不得为构包再次完整读取 queue。

## 6. 禁止的热路径行为

`answer-turn` 中禁止：

- 全读 review ledger。
- 全读 morning session ledger。
- 全读或 replay capture ledger。
- 运行 review audit、capture audit 或全库审计。
- 运行 cold bootstrap、reconcile 或修复。
- 调用正式 apply。

缺少或漂移 hot state 时，必须在答案性反馈前失败，不回退到冷扫描。

## 7. 冷路径运维入口

日终 closeout、日初准备或显式故障恢复可在 `answer-turn` 外运行：

```text
python3 scripts/morning_capture_hot_adapter_408.py bootstrap --repo <repo>
python3 scripts/morning_capture_hot_adapter_408.py bootstrap --repo <repo> --reconcile
python3 scripts/morning_capture_hot_adapter_408.py verify --repo <repo> --event-id <CE-id>
python3 scripts/morning_capture_hot_adapter_408.py verify --repo <repo> --idempotency-key <key>
```

`bootstrap` 允许完整读取 capture ledger，但只重建可派生热状态。它要求权威 `events.jsonl` 与 `state.json` 已存在且一致，不得发明两个真值文件。`--reconcile` 只用于权威 capture 流程已经先修复 state/ledger 一致性之后的显式重绑。

`verify` 只做一次 exact joint verification，不改任何真值或派生层。

## 8. 兼容性

未受管的 legacy morning recovery 仍保留 `capture_review_event` 冷路径。`daily_practice` 与 `evening_d0` 继续使用 `capture_ordinary_review_event`，不自动获得 formalization 授权。
