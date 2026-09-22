# 408 canonical review SQLite 热状态 v1

## 范围

`scripts/review_hot_state_408.py` 是 `events.jsonl` 的可重建只读热投影，不是真源。
本版本只新增独立 API，不修改或接入 `review_feedback_loop.py`、晨间 session、正式表或
`state.json`。

冷 rebuild 完整读取 JSONL，校验每行换行、大小、JSON、schema、event ID、幂等 key、
答案安全和唯一性，再与 `review_feedback_loop._read_events` 结果逐项比较，并通过
`review_feedback_loop._reduce` 完成 reducer replay。SQLite 中保存：

- canonical event 的 ID、幂等 key、session/item、identity/date、字节范围和事件 JSON；
- reducer 当前 action 状态；
- organic baseline；
- retention count；
- action、baseline、retention 与 meta 的逻辑行哈希；
- 四个派生集合按稳定键排序形成的集合 commitment；
- ledger stat、indexed offset、完整冷 hash、尾部 hash和递增 event-chain commitment。

冷路径允许一次 ledger 全扫描；热路径禁止 ledger 全扫描、`state.json` 和正式表读取。
热路径会在固定的 50,000 行、32 MiB 上限内重算派生集合 commitment，并与 SQLite 中
带行哈希的 commitment 以及内容寻址 manifest 的当前代际承诺同时比较。任一逻辑列、
JSON 载荷、行哈希、meta、插入、删除或集合顺序发生静默漂移，都必须 fail-closed 并要求
冷 rebuild；不得把结构上仍可查询的 SQLite 当成可信状态。

## 热 API

`begin_view(idempotency_key, session_id, item_id, identity, observed_date,
trigger_action_id=None)` 使用 SQLite
索引精确返回：

- `existing_event_ref`
- `same_morning_item_ref`
- `eligible_trigger`
- `prior_cross_day`
- `has_baseline`
- `retention_count`
- `precondition`

事件按 `event_id` 与 `idempotency_key` 都有唯一索引；morning item、identity/date 与
eligible action 也有专用索引。热调用只 stat ledger 并读取最多 65,536 bytes 的尾部，
不得遍历 JSONL。

`begin_view` 不返回 `event_json` 或完整 event。`existing_event_ref` 只包含 event ID、幂等
key、session/item/identity/date 摘要、event/line hash 和 exact byte range。若同一个晨间
event 同时命中幂等 key 与 session/item，`same_morning_item_ref` 只返回一个带
`alias_of=existing_event_ref` 的小型 alias，不重复第二份 ref，更不重复 event 载荷。完整
event 只能在调用方确认确有幂等重放需要后，通过 `lookup_event(event_id=...)` 精确读取一次。
因此即使 ledger 中单个 event 达到允许上限 65,536 bytes，默认 32,768-byte begin view 仍须
保持在预算内。

`lookup_commit_receipt(event_id=...)` 与
`lookup_commit_receipt(idempotency_key=...)` 只允许提供一种精确身份。若事件由当前 hot
projection 的 `append_commit` 提交，返回原内容寻址 `receipt_ref`、`receipt_sha256` 和
receipt，并核对 SQLite receipt 唯一行、canonical receipt hash、文件内容地址、build ID、
event ID、event hash 与幂等 key hash。若事件只来自 cold bootstrap，固定返回
`status=no_hot_commit_receipt` 和空 receipt 字段；不得根据事件内容补造 receipt。事件本身
不存在时返回 `status=not_found`。该查询不遍历 ledger 或 receipt 目录。

`trigger_action_id` 不传时保持旧行为，选择当前 identity 下到期的最早 pending action。
显式传入后只能返回该 action，并依次校验存在性、identity、pending 状态、action 类型和
`due_date <= observed_date`。任一条件失败都直接拒绝，禁止回退到最早 action。显式 trigger
也进入 precondition；append 的 event 必须绑定同一个 `trigger_action_id`。

## 追加与崩溃边界

未来调用方必须先持有 `review_feedback_loop.runtime_lock`，再调用：

```text
append_commit(event, expected_precondition, runtime_lock_held=True)
```

precondition 绑定 build ID、SQLite generation、ledger stat、offset、尾部 hash、event-chain
以及 begin 请求身份。追加顺序固定为：

1. 重新核对当前 ledger 和 SQLite 与 precondition 完全相等；
2. append canonical event line；
3. flush 与 fsync ledger；
4. SQLite `BEGIN IMMEDIATE` 更新 event/action/baseline/retention、行哈希、集合 commitment、
   ledger commitment 与 generation；
5. 发布新一代内容寻址 manifest 并原子切换 active pointer；
6. 发布内容寻址 commit receipt。

若在第 3 步后、第 4 步前崩溃，ledger 大于 indexed offset。下一次 begin、lookup、append
或 verify 必须返回 `ledger_commitment_drift_rebuild_required`；不得热恢复或吞掉尾部，
必须执行冷 rebuild。

若 SQLite 已提交而新 manifest pointer 尚未切换，旧 manifest 的 generation、ledger
commitment 或派生集合 commitment 与 SQLite 不一致；下一次热调用同样 fail-closed，必须
冷 rebuild，不得猜测哪一侧是事实。

同一 idempotency key 与完全相同 event 可返回原 receipt；内容不同必须拒绝。晨间同一
session/item 已有 outcome 时必须拒绝第二条 outcome。

## commit verification

`verify_commit(receipt_sha256)` 固定校验：

- receipt 内容地址和 schema；
- SQLite `commit_receipts` 与 event 唯一行；
- ledger 中 receipt 记录的 exact byte offset/length；
- line hash、event hash、event ID；
- 当前 ledger stat 与 bounded tail commitment。

该验证不扫描历史 ledger，不读取 `state.json` 或正式表。
