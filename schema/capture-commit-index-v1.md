# 408 快速捕获提交索引与回执 v1

状态：旁路辅助合同，尚未接入 `intake_fact_capture_408.py` 的生产写入事务。

## 1. 目的

`capture_commit_index_v1` 为快速捕获事实账本提供可重建、可精确查询的内容寻址提交证明。它解决以下问题：

1. 用 `capture_id`、`event_id` 或 `idempotency_key` 精确定位同一个 `fact_captured` 事件。
2. 证明该事件当时使用的规范化 payload 哈希、JSONL 字节位置、事件字节哈希、事件链承诺和重放状态哈希。
3. 在热路径中只验证一个事件范围和当前状态文件，不扫描整个 JSONL。
4. 在冷路径中完整重放 JSONL，检查全部 receipt、索引和 `state.json` 是否一致。
5. 明确保持 `formal_write_count=0`，不把快速捕获误报成正式错题入库。

本辅助层不是事实真源。事实真源仍是 `intake-curation/events.jsonl`，`state.json` 仍是可重放状态投影，commit index 和 receipt 都是可重建派生物。

## 2. 文件布局

调用者必须显式指定派生输出目录。模块没有生产默认输出位置。

```text
<explicit-output-dir>/
  index.json
  receipts/
    <receipt_sha256>.json
```

写入顺序固定为：

1. 先写内容寻址 receipt。
2. 最后原子替换 `index.json`。

旧的、当前索引未引用的内容寻址 receipt 可以保留；它们不属于当前索引快照，冷审计只报告数量，不将其当成当前事实。

## 3. 热路径稳定接口

未来 capture writer 在自身写锁内完成事件与状态持久化后，可以调用：

```python
build_append_receipt(
    event_bytes=exact_jsonl_line,
    byte_offset=committed_offset,
    event_ordinal=committed_ordinal,
    previous_chain_sha256=prior_chain,
    post_event_state=replayed_state,
)
```

该函数是纯函数，不读取或写入任何文件。调用者必须提供：

- 已落盘的、仅含一条记录且以 LF 结尾的精确 JSONL 字节。
- 该记录在账本中的零基字节偏移和一基事件序号。
- 前一条账本事件的事件链承诺；首事件使用固定 genesis commitment。
- 包含当前事件的完整重放后状态。

函数只接受 `event_type=fact_captured`，并校验：

- 事件 schema。
- `capture_id`、`event_id`、`idempotency_key` 非空。
- `payload_sha256` 与规范化 `capture` 对象一致。
- 事件级和 payload 内部的 `idempotency_key` 一致。
- `post_event_state.event_count` 等于事件序号。
- 状态中的 capture 投影与事件身份、payload 和 payload 哈希一致。

当前版本不修改 `intake_fact_capture_408.py`，也不自动调用此接口。正式接入必须由 capture writer 在同一写锁与同一故障边界内完成；在接入前不得声称每次生产 capture 已产生 commit receipt。

## 4. Receipt 合同

Receipt schema 为 `capture_commit_receipt_v1`，kind 为 `capture_fact_event_commit`。

核心身份字段：

- `capture_id`
- `event_id`
- `idempotency_key`
- `payload_sha256`

精确位置字段：

- `event_sha256`：包含末尾 LF 的整条 JSONL 原始字节哈希。
- `byte_offset`：零基字节偏移。
- `byte_length`：含末尾 LF 的字节长度。
- `event_ordinal`：一基事件序号。

账本承诺：

```text
chain_i = SHA256(canonical_json({
  schema,
  previous_sha256,
  event_sha256,
  byte_offset,
  byte_length,
  event_ordinal
}))
```

每条 receipt 保存前一链值、本事件链值和事件结束偏移。事件链覆盖账本中的所有事件，不只覆盖 `fact_captured`，因此两个 capture 之间的授权或日终事件也不能被静默跳过。

状态承诺：

- `post_event_state_sha256`：该事件后完整重放状态按 capture writer 固定 JSON 排版序列化后的 SHA-256。
- `post_event_event_count`：重放状态对应的事件数。
- `capture_state_sha256`：该 capture 在事件提交时的状态投影规范哈希。

内容寻址：

1. 暂时移除 `receipt_id` 和 `receipt_sha256`。
2. 对其余 receipt material 做规范 JSON 序列化。
3. `receipt_sha256` 为该序列化结果的 SHA-256。
4. `receipt_id` 为 `CCR-` 加 digest 前 24 位。
5. 文件名为完整 digest 加 `.json`。

Receipt 不保存完整题干、答案、选项、完整作答、完整解析或图片，只保存身份、哈希、字节位置和承诺。

## 5. Index 合同

Index schema 为 `capture_commit_index_v1`，kind 为 `capture_fact_commit_index`。

`ledger_head` 绑定当前快照：

- 账本字节数。
- 事件总数。
- 完整账本 SHA-256。
- 当前事件链 head。
- LF JSONL 格式声明。

`state_head` 绑定当前 `state.json` 的原始文件字节：

- 字节数。
- 事件总数。
- state schema。
- SHA-256。

`entries` 每个 `fact_captured` 一项，按账本序号排列。`lookups` 同时维护三个一对一索引：

- `capture_id -> receipt_sha256`
- `event_id -> receipt_sha256`
- `idempotency_key -> receipt_sha256`

任何空身份、重复身份、重复 receipt、lookup 与 entry 不一致或自哈希不一致都 fail closed。

Index 的 `index_sha256` 是移除自身字段后对其余 index material 的规范 JSON SHA-256。

## 6. 精确查询

`lookup` 只接受三个 key kind 之一，并要求恰好命中一个 entry 和一个内容寻址 receipt。查询过程校验：

- index 自哈希。
- entry 与 lookup 映射一致。
- receipt 文件名与内容 digest 一致。
- receipt 身份与请求身份一致。

模糊搜索、主题搜索和语义搜索不属于本层职责。

## 7. 有界验证

`bounded_verify` 不完整读取账本。它只读取：

- 有界大小的 `index.json`。
- 一个有界大小的 receipt。
- Receipt 指定的一个 JSONL 字节范围。
- 有界大小的当前 `state.json`。

它验证：

- 当前账本大小等于 index 快照大小，追加或截断立即失败。
- 精确事件范围的 SHA-256、JSON、身份和 payload 哈希。
- Receipt 的事件链单步计算。
- 当前 `state.json` 的大小、哈希、schema 和 event count。
- 从该 capture 事件可独立重建的提交时 capture 状态投影哈希。
- 如果目标 receipt 恰好是账本 head，receipt 的完整 post-event state hash 必须与当前 state hash 一致。

边界说明：有界验证不声称已经重新读取并证明账本其他字节仍然不变。相同大小但发生在目标范围外的任意账本漂移，需要 `cold_audit` 才能发现。返回值固定包含：

```text
whole_ledger_rescanned=false
cold_audit_required_for_whole_ledger=true
```

这不是成功声明的省略项，而是显式证据边界。

## 8. 冷重建与冷审计

`cold_rebuild`：

1. 有界读取整个 capture ledger 和当前 state。
2. 严格解析每一条 LF JSONL 记录。
3. 拒绝重复 `event_id`、重复 `idempotency_key` 和重复 `capture_id`。
4. 计算每条事件的原始字节哈希、字节范围和连续事件链。
5. 使用 capture 模块的权威 `replay(events)` 重放完整账本。
6. 要求当前 `state.json` 的 JSON 值与完整重放结果相同。
7. 对每个 `fact_captured` 前缀单独重放，恢复其真实 post-event state hash。
8. 生成确定性的 index 与 receipts bundle。

`cold_audit` 重新执行上述过程，并逐字义比较当前 index 和所有被引用 receipt。它检查全账本、当前 state 和历史 post-event state。

通用 JSONL 索引基础设施当前尚未出现。本模块暂时自包含精确 offset 与 event-chain 逻辑；未来如果引入通用实现，必须保持本 schema、哈希算法、lookup 唯一性和 fail-closed 行为不变，迁移前后需要固定向量测试。

## 9. 漂移与失败规则

以下情况均 fail closed，不返回部分成功：

- 账本缺失、超限、非 UTF-8、非 JSON、空行、CRLF、末尾缺 LF。
- 事件 schema、身份、payload 哈希或幂等绑定非法。
- 当前 state 缺失、超限、schema 非法或与完整 replay 不一致。
- Index、entry、lookup、receipt 文件名或内容地址不一致。
- 事件字节、offset、length、ordinal 或事件链不一致。
- 有界验证时账本大小或当前 state 发生漂移。
- 任何对象声明 `formal_write_count` 非 0。

CLI 失败时使用 `status=FAIL_CLOSED` 和非零退出码。

## 10. 写入边界

本层允许写入的对象只有调用者显式指定目录中的：

- `index.json`
- `receipts/<sha256>.json`

本层禁止：

- 修改 capture ledger。
- 修改 capture `state.json`。
- 分配正式节点 ID。
- 修改 `节点总表.md`、关系边、知识点索引、复习调度或 mastery。
- 触发日终正式编纂。

冷重建本身只返回内存 bundle；只有调用者显式传入输出目录时，`write_bundle` 才发布派生文件。

## 11. CLI

所有路径均必须显式给出，没有 repo 或生产 state 默认值。

```bash
python3 scripts/capture_commit_index_408.py rebuild \
  --ledger /path/to/events.jsonl \
  --state /path/to/state.json

python3 scripts/capture_commit_index_408.py rebuild \
  --ledger /path/to/events.jsonl \
  --state /path/to/state.json \
  --output-dir /explicit/derived/output

python3 scripts/capture_commit_index_408.py lookup \
  --index-dir /explicit/derived/output \
  --key-kind capture_id \
  --value CAP-...

python3 scripts/capture_commit_index_408.py verify \
  --ledger /path/to/events.jsonl \
  --state /path/to/state.json \
  --index-dir /explicit/derived/output \
  --receipt-sha256 <sha256>

python3 scripts/capture_commit_index_408.py audit \
  --ledger /path/to/events.jsonl \
  --state /path/to/state.json \
  --index-dir /explicit/derived/output
```
