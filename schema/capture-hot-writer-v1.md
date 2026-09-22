# 408 快速捕获热写联合提交 v1

状态：底层联合提交协议只供 2026-08-30 新版 terminal final-package Capture 与
standalone 显式快速入库精确复用。旧首答自动 Capture、条件 Capture、独立
authorization event、ordinary/morning adapter 和 broker 路由只保留只读兼容，
不再是生产入口；生产事实真源与正式库不因本协议迁移。

## 1. 目标

`capture_hot_writer_408.py` 负责在上游已经封存并验证最终完整会话包后，联合提交
一个 `fact_captured`：

1. 接收上游已经完成来源绑定与规范化的 canonical event，或者接收已经规范化的 capture payload。
2. 不扫描 review ledger、morning session ledger、正式错题表、关系表或整个 capture ledger。
3. 在调用方持有 capture ledger 锁时，对幂等键执行精确索引查询。
4. 有界读取并哈希绑定当前 `state.json`。
5. 只增量新增一个 `awaiting_sol_formalization` Capture state，不重放完整
   ledger；一个 managed session item 或 standalone explicit request 最多一个
   Capture。
6. 依次提交 JSONL append、state 原子替换、bounded JSONL exact index、capture receipt 和联合 manifest。
7. 只有联合验证通过并清除 durable pending marker 后才返回成功。

Batch start、batch result、batch close 和其他 event type 不进入该热写 API。旧
`authorize`、`authorize-hot`、broker 和 rich-intake 命令已经退役，不能再凭
`capture_id` 和幂等键生成新授权。历史无 proof 的授权事件仅可读、可重放；只允许
兼容恢复代码在精确绑定既有 authorization event 行 SHA-256 与 fact receipt 后重建
state 和热索引，事件追加数必须为 0。新版 managed Capture 只由
`managed_408_current_turn.py` 在 `terminal_resolved=true`、最终包已 seal/reopen 后
发起；standalone 只由带显式快速入库 authorization 的
`standalone_question_intake_408.py` 发起。恢复复用原 finalization identity 与
Capture payload。

### 1.1 当前题统一接入

新版晨间与普通题均由 `managed_408_current_turn.py` 直接调用底层联合提交器。
`morning_capture_hot_adapter_408.py` 只保留既有 receipt 的精确恢复和历史兼容，
不得再作为新 `answer-turn` 或新 capture 的公开入口。旧适配器识别的证据包括：

- canonical `review_event_v1` outcome。
- review hot-state 的 event-specific commit receipt SHA-256。
- morning session `first_recorded` 的 event-specific commit receipt。
- morning session `first_bound_to_loop` 的 event-specific commit receipt。
- 通过 session hot-state 按 sequence 精确读取的 `session_started`。

适配层会重新执行 receipt verify，不信任调用方的“已验证”标签。它还必须确认 review outcome、首答事件、binding 事件、session ID、item ID、首答结果、信心度、提示层级、first break 及时间一致。

旧适配器的 `partial`、`wrong`、`uncertain` 与 `independent_correct` 路由只用于
历史 receipt 解释，不能覆盖当前 capture 矩阵。`state_bound_without_event_receipt`
不足以支持任何恢复，必须 fail closed。

历史关联练习 outcome 的验证规则仅供既有账本重放；新版当前题热路径不读取关联练习或 personalization selection receipt。

晨间热适配层不得调用 review audit、session replay、capture audit、capture replay
或正式 apply。新 managed 题在非 terminal 阶段一律不捕获；terminal 时无论是首答
完全独立正确还是多轮纠正后解决，都在最终包封存后创建恰好一个
`awaiting_sol_formalization` Capture。choice 正确但推理存在矛盾不 terminal、不
捕获。普通 standalone 教学不进入本 writer；显式快速入库才进入。

## 2. 事实真源和派生层

事实真源保持不变：

- `events.jsonl` 是 capture 事件真源。
- `state.json` 是可由事件账本重放的状态投影。

热写辅助目录只保存可重建派生证据：

```text
<explicit-hot-root>/
  .capture-hot-writer.key
  joint-manifest.json
  pending-joint-commit.json
  bounded-jsonl-index/
  capture-commit-bootstrap/
  capture-receipts/
  capture-receipt-links/
```

本层不修改：

- `节点总表.md`
- `关系边表.md`
- `知识点命中索引.md`
- review-unit 和复习调度
- mastery
- 正式节点或正式日期

所有返回值、manifest、pending、link 和 receipt 固定包含 `formal_write_count=0`。

## 3. 调用方锁合同

热写函数不会偷偷另开一个与 capture writer 无关的锁。调用方必须先持有 capture root 的 `.lock`，再调用 `capture_fact`。

当前独立适配层提供：

```python
with capture_ledger_lock(ledger_path) as lock_token:
    result = capture_fact(
        ledger_path,
        state_path,
        hot_root,
        lock_token=lock_token,
        event=canonical_event,
    )
```

以下独立 `capture_authorized` 调用只解释历史对象，不再用于 2026-08-30 新题：

```python
authorization = authorize_capture(
    ledger_path,
    state_path,
    hot_root,
    lock_token=lock_token,
    capture_id=result["capture_id"],
    idempotency_key=authorization_key,
    authorization_proof=verified_managed_current_message_proof,
)
```

历史两次增量提交各自有 pending 和 joint 边界。新 terminal final-package 与
standalone explicit quick-intake Capture 不追加该 authorization event；其
`awaiting_sol_formalization` admission 已绑定在同一最终包 Capture payload 和 durable
receipt 中。

Token 同时绑定：

- ledger 绝对路径
- 当前进程
- 当前线程
- context 有效期

伪造 token、跨线程使用、锁释放后复用或绑定其他 ledger 均 fail closed。

历史 ordinary adapter 的联合锁与 rich-intent 规则只用于解释既有回执；该 adapter 的公开写函数已经退役。新版当前题事务直接复用底层联合提交器，不读取 study-turn authorization receipt 或 rich intent。`intake_fact_capture_408.py authorize-hot` 也不是写入口。

锁顺序固定为：

1. 调用方 capture ledger lock。
2. bounded JSONL index lock。

所有接入者必须保持这一顺序，避免死锁。

## 4. 输入 API

### 4.1 Canonical event

首选 API 是直接传入上游已经绑定证据的 event：

```python
capture_fact(..., event=canonical_event)
```

Event 必须恰含：

```text
schema
event_type
event_id
idempotency_key
created_at
capture_id
payload_sha256
capture
```

并满足：

- schema 为 `intake_fact_capture_event_v1`。
- event type 为 `fact_captured`。
- capture payload 已经通过权威 `_validate_capture_payload`，且输入值本身就是规范化结果。
- event 和 payload 的 idempotency key 相同。
- payload SHA-256 正确。
- event ID 与 capture ID 符合当前 capture writer 的确定性身份公式。
- `created_at` 是带时区 ISO 时间。

本模块不会根据 event 反查 review、session、题目附件或来源账本；来源绑定必须由上游在调用前完成。

### 4.2 Validated payload

也可以传入：

```python
capture_fact(
    ...,
    validated_payload=normalized_payload,
    created_at="2026-07-23T01:00:00+00:00",
)
```

`canonical_fact_event_from_payload` 使用当前 capture writer 的确定性公式生成：

- `event_id`
- `capture_id`
- `payload_sha256`

`event` 和 `validated_payload` 必须二选一。

### 4.3 独立授权事件（仅历史兼容，新写禁用）

`capture_authorized` 必须恰含：

```text
schema
event_type
event_id
idempotency_key
created_at
capture_id
authorization_proof
```

`authorization_proof` 必须是 `managed_408_capture_authorization_proof_v1`。同轮系统题使用 `proof_kind=managed_current_message_intent`：只保存 study authorization receipt 与 rich intent 的 ref/SHA、request ID、display receipt SHA、当前消息 SHA/字节数、authorization binding SHA，以及 `authorization_body_stored=false`。

既有 `captured_unconfirmed` 在后续当前消息中获得明确授权时，必须使用 `proof_kind=managed_existing_capture_current_message`。该 proof 绑定 content-addressed existing-capture intent ref/SHA、capture ID、原 fact event ID 与精确行 SHA-256、fact receipt SHA-256、request ID、当前消息 SHA/字节数和重算可验证的 authorization-binding SHA；同样不保存消息正文。受管入口必须先 bounded verify 原 fact，再在 capture lock 内复核一次，才调用 writer。

授权 event ID 由授权幂等键确定性生成。授权键必须由受管显式入库路径绑定 canonical fact/review 证据与 capture ID；非正确结果、置信度、提示层级或 writer 调用本身都不能生成授权意图。只有 `captured_unconfirmed` 可以首次追加该事件。

reader 与 cold replay 继续按下述形状验证既有事件，但新 managed terminal 和
standalone explicit quick-intake 调用不得生成 `capture_authorized`。reader 兼容不能
转换成新写权限。

## 5. 冷启动

受管晨间适配层的默认派生目录为：

```text
wiki/study_vaults/408-full/state/intake-curation/.capture-hot-writer-v1/
```

该目录以及 capture `events.jsonl`/`state.json` 必须在进入受管首答之前完成 cold bootstrap。`answer-turn` 本身不得尝试创建、重建或修复这些热状态；缺失、漂移或存在 pending marker 时必须停在反馈门禁前。

首次启用或发生任何冷路径事件后，调用：

```python
with capture_ledger_lock(ledger_path) as lock_token:
    cold_bootstrap(
        ledger_path,
        state_path,
        hot_root,
        lock_token=lock_token,
    )
```

Cold bootstrap 执行完整冷检查：

1. 读取完整 capture ledger。
2. 使用权威 capture replay 校验当前 state。
3. 生成 `capture_commit_index_v1` bootstrap bundle 和历史 capture receipts。
4. 生成 `bounded_jsonl_index_v1` exact index、chunk commitments 和 HMAC manifest。热捕获专用索引固定使用 4 KiB chunk；这避免当前小型 capture ledger 因通用 1 MiB chunk 而在每次尾恢复时整表重读。
5. 生成联合 HMAC manifest，绑定 ledger 路径、state 路径、两个派生索引 head 和 capture event-chain head。

Cold bootstrap 是明确冷路径，允许一次完整 ledger read。返回值固定标记：

```text
hot_path=false
ledger_full_read_count=1
```

如果存在 crash pending marker，必须显式使用：

```python
cold_bootstrap(..., reconcile=True)
```

但 `reconcile=True` 不会擅自修复权威 state。如果 append 已经落盘而 state 尚未更新，必须先运行权威 capture reconcile，使 state 与 ledger replay 一致；随后 cold bootstrap 才会重建派生层并清除 pending。

## 6. 热路径前置检查

每次热写在任何 ledger 变更前执行：

1. 验证不存在 `pending-joint-commit.json`。
2. 验证 joint manifest HMAC、schema 和路径绑定。
3. 验证 bounded JSONL manifest、ledger inode、大小、mtime 和 committed head。
4. 验证 joint manifest 指向当前 bounded manifest。
5. 有界读取 `state.json`，默认最大 16 MiB。
6. 验证 state 的大小、SHA-256、schema 和 event count 与 joint manifest 一致。
7. 验证 state event count 与 bounded JSONL event count 一致。
8. 通过 idempotency shard 精确查重。
9. 通过 event shard 精确检查 event ID 冲突。
10. 确认 capture ID 不在 state 中形成未索引漂移。

幂等键已存在且 canonical event 完全相同时，不追加任何字节，先执行 bounded joint verify。Fact 返回 `ALREADY_COMMITTED`，authorization 返回 `ALREADY_AUTHORIZED`。

幂等键或 event ID 绑定了不同内容时，写入计数为 0 并 fail closed。

## 7. 增量 state 更新

热路径不调用 `replay(events)`，也不读取 JSONL 历史。

对于唯一一个 `fact_captured`，增量更新只执行与权威 replay 相同的确定性变化：

- `event_count += 1`
- 在 `captures[capture_id]` 新增初始 capture state
- 新 final-package payload 必须已绑定 terminal 或 explicit-fast-intake authorization，
  设置 `quality_status=awaiting_sol_formalization`，并把 Capture ID 排序加入对应
  `pending_by_date[study_date]`
- 历史 payload 仍按其不可变 `formalization_authorized` 重放为
  `awaiting_daily_curation` 或 `captured_unconfirmed`，不得升级或改写
- `batches` 不变

对于历史既有的唯一一个 `capture_authorized`：

- 目标必须存在且状态为 `captured_unconfirmed`
- `event_count += 1`
- 顶层 `formalization_authorized=true`
- `quality_status=awaiting_daily_curation`
- capture 原始 payload 保持不可变，原 payload 中的授权字段仍为 false
- 把 capture ID 排序加入对应日期的 `pending_by_date`
- `batches` 不变

新 state 在写入前再次执行最大字节门禁，并使用当前 capture writer 相同的 JSON 排版：UTF-8、sort keys、indent 2、末尾 LF。

## 8. 联合提交顺序

Hot writer 在两个锁均持有时执行：

1. 原子发布 HMAC-bound pending intent。
2. 向 ledger 追加一条完整 LF JSON event。
3. Flush 并 fsync ledger。
4. 原子替换 state 并 fsync state 目录。
5. 只读取未索引 tail，更新 bounded JSONL 受影响 shards、chunk commitments 和 generic commit receipt。
6. 发布 capture-specific content-addressed receipt。
7. 发布 event-ID keyed HMAC receipt link。
8. 原子发布新的 joint manifest。
9. 在 pending 尚存在时执行内部 bounded joint verify。
10. 验证通过后删除 pending 并 fsync 辅助目录。
11. Fact 返回 `CAPTURED`；authorization 返回 `AUTHORIZED`。

`capture_fact` 与 `authorize_capture` 必须把第 9 步已经完成的 PASS 证明以 `joint_verification` 嵌套返回。调用方不得为获得“独立证明”紧接着再调用一次 `verify_joint_commit`；每个增量事件的新写和幂等重放的 `state_read_count` 都不得超过 2。

Capture receipt 绑定：

- capture ID
- event ID
- 原始 idempotency key
- payload SHA-256
- JSONL raw line SHA-256
- byte offset、byte length 和 event ordinal
- 覆盖所有 capture ledger events 的连续 event-chain commitment
- capture 提交时状态投影 SHA-256
- 完整 post-event state SHA-256

Authorization receipt 使用独立 schema 和 receipt ID 前缀，并绑定同样的 event 身份、raw line、位置、连续 event-chain、授权后 capture projection 与完整 post-event state。它明确记录 `daily_curation_authorized=true`，同时固定 `formal_write_authorized=false` 和 `formal_write_count=0`。

## 9. Crash 和 fail-closed

Pending intent 是联合提交的 durable fence。

只要 pending 已发布，后续任何一步抛错：

- 不删除 pending。
- 不返回 `CAPTURED`。
- 不把 generic index 成功等同于 capture joint commit 成功。
- 不自动截断 ledger。
- 不自动猜测 state 是否应前进。

下一次 `capture_fact` 或 `authorize_capture` 首先看到 pending，并立即返回 reconcile required。可能情况包括：

- Ledger 已 append，state 未发布。
- Ledger 和 state 已发布，JSONL index 未发布。
- JSONL index 已发布，capture receipt 未发布。
- Receipt 已发布，joint manifest 未发布。
- Joint manifest 已发布，但 pending 尚未清除。

恢复顺序固定为：

1. 如果权威 state 与 ledger replay 不一致，先运行 capture authoritative reconcile。
2. 在 capture lock 内运行 `cold_bootstrap(reconcile=True)`。
3. 运行 `verify_joint_commit`。
4. 再用原 fact 或 authorization 幂等键恢复白天 hot transaction。

## 10. Bounded joint verify

```python
verify_joint_commit(
    ledger_path,
    state_path,
    hot_root,
    event_id="CE-...",
)
```

或使用一个 idempotency key。两者必须二选一。

验证读取范围：

- 一个 bounded JSONL manifest。
- 一个 event shard。
- 必要时一个 idempotency shard。
- 目标 event 所在的 bounded ledger chunk 和精确 byte range。
- 一个有界 state 文件。
- 一个 capture receipt 和一个小型 receipt link，或 bootstrap receipt index。

验证内容：

- 当前 joint manifest HMAC。
- 不存在 pending intent。
- 当前 bounded JSONL head 与 joint binding 一致。
- 当前 state bytes 与 joint binding 一致。
- Exact event/idempotency binding。
- Receipt 内容地址、身份、payload、raw line、offset、length 和 event-chain 单步承诺。
- 从单个 event 可重建的 capture commit-state projection。
- 如果目标 receipt 是当前 ledger head，完整 post-event state hash 必须等于当前 state hash。

返回值固定包含：

```text
ledger_full_read_count=0
review_or_session_ledger_read_count=0
formal_write_count=0
```

它不声称取代 cold audit 对历史全部 chunks 和全部历史状态的完整验证。

## 11. Cold-path 漂移

以下操作继续由既有冷路径执行：

- `curation_batch_started`
- `curation_item_result`
- `curation_batch_closed`
- 既有 writer 对整个 ledger 的原子重写

兼容命令 `intake_fact_capture_408.py authorize` 仍走旧冷路径，因此也会使 hot joint 漂移；受管普通显式入库不得调用它。`authorize-hot` 仅作同协议下的有界恢复或检查入口。

这些操作会改变 ledger 大小、mtime、inode、state hash 或 event count。Hot joint manifest 因而自动失效，后续 verify 或 write fail closed。不得尝试仅恢复 JSONL tail 来绕过 capture state 语义；必须重新 cold bootstrap。

## 12. 历史 100 天性能合同

本节不再是现行验收目标。新版速度门禁由 `benchmark_current_question_hotpath_v1.py` 覆盖 prepared option、普通 quick capture 和 rich current turn。

正常 hot capture 只允许：

- 0 次完整 ledger read。
- 0 次完整 ledger replay。
- 0 次 review/session ledger read。
- 2 次有界 state read：提交前绑定当前 state，发布后验证落盘 state。
- 常数个 exact shard、manifest、receipt 和目标 chunk 读取。
- 1 条 JSONL append。
- 1 次 state 原子替换。

历史普通显式快速入库曾使用两个增量事件；该形状不再是现行验收目标。新 managed
terminal 与 standalone explicit quick intake 各新增恰好一个 Capture event，并在
返回前重开验证同一 durable receipt 与最终包 locator/SHA-256。它仍固定为 0 次完整
capture ledger 读取和 0 次 replay。

测试基线以连续 100 个学习日、每天一个 capture 为最小规模，要求：

- 每次返回 `ledger_full_read_count=0`。
- 每次返回 `review_or_session_ledger_read_count=0`。
- P95 低于 500 ms。
- 最终 ledger 和 state event count 均为 100。

该阈值是回归门禁，不是对所有硬件的绝对延迟承诺。真实部署应持续记录 P50、P95、state bytes 和 exact shard 大小，但不得为了追求延迟放宽 durability 或 fail-closed 门禁。

## 13. 历史接入边界

以下 ordinary/explicit 接入顺序只解释已有对象，不得用于新题。新版调用方只有
managed terminal finalization 和 standalone explicit quick intake；两者都必须先有
sealed final package，并且都不使用下述独立 authorization 事件。

历史 ordinary adapter 的显式接入顺序是：

1. 上游按当前流程完成 canonical payload 和来源绑定。
2. 在现有 capture lock 内构造或传入 canonical event。
3. 在同一 capture lock 内调用 `capture_fact`。
4. 验证 fact joint receipt 后，以绑定 review event 与 capture ID 的确定性授权键调用 `authorize_capture`。
5. 只有授权返回 `AUTHORIZED` 或经过 joint verify 的 `ALREADY_AUTHORIZED`，且终态为 `awaiting_daily_curation`，才报告快速入库成功。
6. Fact 成功而 authorization 尚未开始时返回 content-addressed recovery ticket；同键重试只补授权。Authorization pending 已发布后的故障必须先按 pending fence 冷恢复。
7. `personalized_study_turn_408.py` 的显式入口只提交轻量 personalization overlay，不执行下一题 ranking、selection receipt 或 display-candidate revalidation。

Adapter 不得：

- 让 hot writer 自己扫描 review/session ledger。
- 在调用前先用旧 writer 写一遍同一事件。
- 把 receipt 当成正式错题入库 receipt。
- 遇到 pending 时自动重试 append。
- 在 state/index/receipt 任一阶段失败后先向用户宣告成功。
- 把 canonical 非正确结果或 writer 调用解释为授权意图。
- 在显式热授权成功后再调用旧冷 `authorize`。
