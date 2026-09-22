# 408 受管回复回执协议 v2

## 一、目标

v2 保留 v1 的一次 pre-answer、manifest 绑定、精确回复哈希、adoption 子集、幂等 pointer、
fail-soft 和零正式写入语义，同时减少热路径重复 JSON、标准输出和 finalize 重读量。

新 begin 默认使用：

```text
personalization_turn_begin_receipt_v2
```

既有 v1 对象不迁移、不重写，继续按 v1 合同读取、finalize 和审计。

## 二、detached slice

`safe_preanswer_slice` 在 begin 锁内仍恰好调用一次。完整结果只写一个内容寻址对象：

```text
turn-receipts/objects/slice/<slice-object-sha256>.json
```

v2 begin 不含 `slice_snapshot`，也不复制完整 `response_plan`。它新增：

```text
slice_object_ref
slice_object_sha256
```

并继续绑定：

```text
slice_id
slice_sha256
response_plan_sha256
root_manifest_sha256 / projection_build_id
recent_incremental_sha256
historical_manifest_sha256 / historical_index_sha256
```

`slice_object_sha256` 是磁盘对象精确 UTF-8 字节的 SHA-256；`slice_sha256` 是 pre-answer
规范紧凑 JSON 的语义哈希。reader 必须同时验证文件名、对象字节哈希、规范 slice 哈希、
request/date/conversation/build/layer binding、答案安全门禁和 response-plan 哈希。

ready plan 来自 detached slice 的 `context.response_plan`。degraded slice 没有 context 时，
reader 只能根据已绑定 targets 和 reason code 重建固定的无证据
`degraded_generic_answer` plan。不得从当前投影、账本或其他对象补齐。

## 三、begin 输出

默认公共结果 schema 为 `personalization_turn_begin_result_v2`，UTF-8 紧凑 JSON 上限为
2,048 bytes。它只含：

- receipt ID、SHA、ref 和版本；
- pre-answer 调用数、ready/degraded 状态和受控原因；
- 紧凑 slice 状态；
- 九字段 runtime plan；
- `formal_write_count=0`。

runtime plan 必须保留九个字段名：

```text
current_target
direct_recent_errors
direct_historical_errors
common_patterns
verified_cross_connections
explanation_strategy
first_boundary_to_check
optional_practice_action
evidence_gaps
```

弱 `background_context` 不进入默认结果。直接证据只返回有限 ID、匹配依据和必要边界；
预算不足时按历史、跨科、共性、近期的次序继续裁剪，但九个字段名不得消失。

CLI 默认输出单行 compact JSON。`--pretty` 只改变展示缩进，不改变对象语义。
`begin-turn --full` 可以在本次结果附带完整 begin 和 slice；默认不得返回它们。

## 四、inspect-receipt

```text
python3 scripts/personalization_turn_receipt_408.py \
  --repo . inspect-receipt \
  --turn-receipt-sha256 <sha256>
```

该入口只读取并验证指定 content-addressed begin 和其精确 slice：

- 不调用 `safe_preanswer_slice`；
- 不运行 `audit_receipts`；
- 不读取当前学习账本或正式表；
- 不写任何对象或指针；
- 返回 `preanswer_call_count=0`、`audit_call_count=0`。

它也能展开具有嵌入 snapshot 的当前 v1 begin。严格 legacy begin 只能返回已有元数据，
不能伪造缺失 snapshot。

## 五、finalize 与 adoption

finalize 在原锁顺序内验证 begin、request pointer 和当前 root manifest。v2 必须加载 begin
精确引用的 detached slice，以完整 plan 校验 adoption；不得仅依据裁剪后的公共 runtime plan。

finalize binding schema 仍为 `personalization_reply_binding_v1`，继续绑定 begin SHA、turn ID、
request SHA、slice SHA、response-plan SHA 和精确 reply SHA。正文不落盘。

任何 slice 缺失、symlink、文件名不等于对象 SHA、字节漂移、plan hash 不符或 manifest
变化都必须 fail-closed，不能返回 `display_authorized=true`。

## 六、幂等和混合版本

- request pointer 是版本无关的唯一绑定。相同请求重放读取原 v1 或 v2 begin，调用数为 0。
- 同一 request ID 绑定不同 request hash 继续失败。
- v1 和 v2 可以共存；audit 分别报告 `current_v1_valid_begin_count` 和
  `current_v2_valid_begin_count`。
- audit 必须验证所有 v2 slice 引用，并把缺失、漂移、symlink 和无引用 slice 对象报告为
  integrity failure。
- v2 写入不能放宽 v1 legacy seal 或 invalid-begin seal 的历史合同。

## 七、CLI 返回与退出码

成功 begin/replay/finalize/inspect 仍返回 0；合同错误返回 2 和受控 `reason_code`。
`--full`、`--pretty` 和 `inspect-receipt` 都不得触发第二次 pre-answer 或 receipt full audit。
