# 408 受管回复回执协议 v1

> 兼容状态：v1 仍是可读取、可 finalize、可审计的有效当前合同。新 begin 默认写入
> `personalization_turn_begin_receipt_v2`，其 detached slice 规则见
> `schema/personalization-turn-receipt-v2.md`。不得通过改写既有 v1 对象完成迁移。

本协议证明仓库内受管 408 回复在组织文本前确实读取一次有界个性化切片，并将
最终回复字节与实际采用的证据绑定。它不等于平台级中间件，不能拦截绕过受管
入口的任意聊天文本。

## 一、十个受管入口

固定入口为：

```text
morning_selection -> morning_review
morning_answer_feedback -> morning_review
current_question -> current_question
past_exam_review -> past_exam_review
chapter_preview -> chapter_preview
knowledge_explanation -> knowledge_explanation
evening_d0_review -> evening_review
wrong_intake -> wrong_intake
nightly_curation -> wrong_intake
general_408_planning -> general_408
```

## 二、begin-turn

每个请求必须带唯一且可安全作为单层指针文件名的 `request_id`；不得包含路径分隔符。
`as_of` 必须在任何 state 目录解析、锁创建、pre-answer 调用或对象/指针写入之前通过
真实日历校验：值必须是规范 `YYYY-MM-DD` 字符串且日期实际存在。缺失、`null`、非字符串、
非补零格式和不存在的日期一律返回 `managed_turn_requires_valid_as_of`，并保证不产生
begin 对象、request pointer 或热路径锁。
`begin_personalized_turn` 在锁内对该请求调用
`safe_preanswer_slice` 恰好一次，并写入 content-addressed begin receipt。回执绑定：

```text
entry_kind / conversation_kind / request_id / request_sha256 / local_date
targets
root_manifest_sha256 / projection_build_id
recent_incremental_sha256
historical_manifest_sha256 / historical_index_sha256
slice_id / slice_sha256
slice_snapshot
retrieval_status / degraded_reason
response_plan / response_plan_sha256
preanswer_call_count=1
answer_safe=true / formal_write_count=0
```

`slice_snapshot` 是本次有界、答案安全检索结果的完整快照，其规范 JSON 哈希必须等于
`slice_sha256`。ready 快照的 build ID 和三层索引哈希必须与 begin 的 root manifest
绑定一致。相同请求的幂等重放返回原对象、原 `slice_snapshot`，且本次调用数为 0；
同一 request ID 绑定不同请求必须失败，不能在重放时重新检索。

当前 begin 对象只接受实现生成的精确字段集合；缺少 `kind`、`local_date`、`targets`、
`slice_snapshot` 等任一字段，或增加合同外字段，均不是当前合同。`kind` 固定为
`personalization_turn_begin`。外层 `response_plan` 必须与
`slice_snapshot.context.response_plan` 逐字段、逐值相同，不能只重新计算外层 plan 哈希；
ready begin 的外层 `targets` 必须与 `slice_snapshot.context.current_targets` 精确相同。
`local_date` 必须是合法 `YYYY-MM-DD`，并与 `slice_snapshot.as_of` 相同。

ready slice 的顶层字段集合固定包含 schema、status、slice/request/conversation/date/build
绑定、layer hashes、mode、read-only/formal-write/scheduling 门禁、截断与字节计数以及 context；
其中 schema 固定为 `personalization_preanswer_slice_v1`、`status=ready`、
`read_only=true`、`formal_write_count=0`、`scheduling_authority=none`。degraded begin 只接受
真实 fail-soft 的 `status=unavailable` slice，并在 begin 时补入本次 `as_of` 形成完整日期绑定；
它必须保留受控 reason、read-only/formal-write/scheduling 门禁，且外层降级 plan 只能是
无证据、无跨科边、禁止个性化的 `degraded_generic_answer`。因此，复制合法 slice 后只在
外层 plan 中伪造 `live_routable=true`，即使重算 plan SHA-256、turn ID 和对象 SHA-256，
仍必须在 finalize 和 audit 中失败。

begin 在与投影构建器相同的 `.hot-path.lock` 内依次取得切片、root manifest 五字段、
content-addressed begin 对象和 request pointer，并沿用 finalize 的“投影锁在外、回执锁在内”
顺序。这样构建切换只能发生在整个 begin 快照事务之前或之后，不能形成旧切片与新 manifest
绑定。`safe_preanswer_slice.status=unavailable` 会规范化为
`retrieval_status=degraded`，原始 unavailable 状态和受控原因仍保留在 `slice_snapshot`；
它可以通过 `degraded_generic_answer` 完成 fail-soft finalize，不得改为扫描完整账本或 vault。

## 三、finalize-reply

显示前必须以最终回复的精确 UTF-8 字节调用 `finalize_personalized_reply`。绑定对象
只保存 reply SHA-256，不保存正文。adoption 固定声明：

```text
personalization_claimed
used_evidence_ids
used_match_ids
actions
degraded_reason
```

采用的 evidence 和 match ID 必须属于 begin receipt 的回复计划。action 不仅校验
枚举，还必须通过以下语义门禁：

- `boundary_check` 必须有回复计划中的明确边界，并采用对应近期、历史或共性证据；
- `historical_connection` 必须声明个性化且采用直接历史证据；
- `cross_subject_connection` 必须采用回复计划中 `live_routable=true` 的 edge ID；
- `mechanism_check` 必须由计划明确给出 mechanism-check 动作并采用对应证据；
- `active_recall`、快速捕获状态和日终编纂状态必须匹配受管入口类型；
- `original_question`、`related_question`、`verified_variant` 不能由只读 pre-answer
  的 optional action 授权。v1 adoption 没有独立 selection/source receipt 字段，
  因此这三个展示动作固定 fail-closed；后续协议只有显式绑定并校验独立回执后才可放行。

检索不可用时不得声称使用个人历史，action 必须包含
`degraded_generic_answer`；ready 回执不得反向冒充 degraded。finalize 在取得与投影
构建器相同的 `.hot-path.lock` 后，重新计算当前 root manifest 五字段绑定，并要求与
begin 完全一致，同时核对 request pointer。任一漂移、指针缺失、回复哈希冲突或
重复异值绑定均 fail-closed，且不得返回 `display_authorized=true`。

## 四、审计边界

`audit_receipts` 双向验证 request pointer 与 begin、finalize pointer 与 finalize，
并检查 content-addressed 对象哈希、begin/finalize 身份、回复哈希、response-plan
哈希、adoption 子集和上述动作门禁。悬空指针、孤儿对象、被删除或篡改的指针都使
`integrity_status=fail`。

未 finalize 的合法 begin 不属于对象损坏，但必须以
`completion_status=incomplete`、`unfinalized_begin_count` 和精确 begin SHA-256
报告；此时顶层 `status=incomplete`，不能用完整性 `pass` 冒充所有受管回复已经完成
展示绑定。只有完整性通过且所有 begin 均有有效 finalize 时，顶层 `status=pass`。

无效、哈希漂移或合同不完整的 begin 同时属于 integrity failure 和未完成 begin；它不能因
未进入 `valid_begins` 集合而被漏出完成态计算。只要存在这类 begin 且没有有效 finalize，
`completion_status` 必须为 `incomplete`、`all_begun_turns_finalized=false`，并通过
`invalid_begin_count` 和未完成对象标识暴露；顶层状态仍为 `fail`。

唯一例外是第七节的严格 invalid-begin aborted seal。它不把 begin 变成有效合同、不生成
finalize，也不重新授权展示；只把有精确未展示确认的历史失败尝试从“未完成回复”转为
“已保留的中止尝试”。审计通过 `aborted_attempt_count` 和精确 begin SHA-256 单独报告，
不计入 `finalized_reply_count`、`unfinalized_begin_count` 或 `invalid_begin_count`。

审计通过只证明仓库内受管入口合同，不证明 Codex 平台所有输出都经过此包装器，
也不提供任意平台聊天拦截能力。

## 四-A、managed root 路径门禁

`turn-receipts` 的锁、对象写入、指针写入和审计遍历只能发生在调用所确定的
personalization state root 内。默认 state root 和显式 `--state-dir` 都必须在操作前逐级
`lstat` 已存在的受管路径组件；任一 state 子路径、`turn-receipts`、objects、pointers 或
锁文件父组件为符号链接时 fail-closed。随后还必须验证目标 `resolve` 结果仍位于该
personalization state root 内。普通临时目录可以作为显式测试 state root，但不能借父目录
symlink 把 managed root、锁、写入或遍历重定向到 root 外。

## 五、pre-slice-snapshot 旧回执兼容封存

早期 `personalization_turn_begin_receipt_v1` 可能已经形成完整的 request pointer、
finalize binding 和 finalize pointer，但当时尚未保存 `slice_snapshot`，并把 fail-soft
结果记为 `retrieval_status=unavailable`。这类对象不满足当前 begin 合同，禁止通过放宽
`_validate_begin_contract` 将其升级为当前有效回执。

只允许使用显式命令逐个封存：

```text
python3 scripts/personalization_turn_receipt_408.py \
  --repo . \
  --state-dir wiki/study_vaults/408-full/state/personalization \
  seal-legacy \
  --turn-receipt-sha256 <exact-old-begin-sha256>
```

可封存对象必须同时满足以下全部条件：

- begin 对象文件名与实际 SHA-256 一致，固定缺少 `slice_snapshot`，
  `retrieval_status=unavailable`、`slice_id=null`、`preanswer_call_count=1`、
  `answer_safe=true`、`formal_write_count=0`；
- begin 只能含旧合同的精确字段集合，turn ID、response plan 哈希、manifest 绑定字段、
  entry/conversation 映射和安全 targets 均通过校验；
- response plan 必须是无近期证据、无历史证据、无共性、无跨科边的
  `degraded_generic_answer`，不得授权个性化；
- request pointer 必须精确指回该 begin；finalize pointer 必须精确指向唯一 finalize；
- finalize 必须与 begin 的 turn、request、slice 和 response-plan 哈希相互绑定，
  `reply_body_stored=false`，不得存在 `reply_body` 字段，且 `formal_write_count=0`；
- adoption 必须是 `personalization_claimed=false`、空 evidence、空 match、唯一动作
  `degraded_generic_answer`，并保留非空降级原因。

封存写入新的 content-addressed 对象：

```text
turn-receipts/objects/legacy-seal/<seal-sha256>.json
turn-receipts/pointers/legacy-seal/<old-begin-sha256>.json
```

seal 同时绑定 begin SHA、request pointer SHA、finalize SHA、finalize pointer SHA、reply
SHA 和旧合同身份，并固定声明：`slice_snapshot_recorded=false`、
`current_contract_valid=false`、`seal_scope=audit_compatibility_only`、
`reauthorizes_display=false`、`formal_write_count=0`。命令不改写、不移动、不删除原 begin、
finalize 或指针；对同一精确对象重复执行返回同一 seal。任何旧对象、指针或 seal 的漂移
都必须 fail-closed。

审计分别输出 `current_contract_*` 与 `legacy_sealed_*` 字段。旧 begin 即使封存成功，
仍计入 `current_contract_invalid_begin_count`，不能冒充当前合同有效；它只计入
`legacy_sealed_begin_count` 和 `legacy_sealed_finalize_count`。只有所有当前合同 begin 都有
有效 finalize，且所有严格识别出的旧合同 begin 都有完整 seal 时，顶层状态才可以为
`pass`。未封存旧合同、被篡改对象、孤儿对象、缺 request/finalize 指针或缺 finalize 均为
`fail`。

## 六、pre-exact-target-binding ready 旧回执兼容封存

少量早期 ready 回执已经完整保存 `slice_snapshot`，外层 `response_plan` 也与
`slice_snapshot.context.response_plan` 完全相同，但外层 `targets` 仍使用旧 envelope 风格
八字段，而 snapshot 的 `current_targets` 使用含 `subjects`、`point_views` 和
`resolution_sources` 的新形态。这类对象仍不满足当前精确 targets 绑定，禁止放宽
`_validate_begin_contract`；只能由同一显式 `seal-legacy` 命令逐个兼容封存。

第二个 legacy contract ID 固定为
`personalization_turn_begin_receipt_v1_pre_exact_target_binding`，并只接受：

- begin 具有当前字段全集、ready slice 的精确字段集合、答案安全只读门禁、零正式写入、
  精确 schema/status/date/request/conversation/slice/build/layer hash/turn identity 绑定；
- 除 targets 形态外满足当前 ready begin 的全部约束；外层 plan 的规范 JSON 必须与
  snapshot plan 完全相同，不能加入伪造 live edge 后重算哈希；
- 旧 targets 必须恰好含 `subject` 加七个 ID 列表。七个列表分别与 snapshot 同名列表的
  规范 JSON 精确相同，不能使用子集、超集、重排、缺项或替代 ID；`subject` 必须实际出现
  在 snapshot 的 `subjects` 中；
- finalize 与 begin 的 turn/request/slice/plan 哈希精确绑定，adoption 固定为
  `personalization_claimed=false`、空 evidence、空 match、唯一 `direct_answer`，且无降级
  reason。个性化、历史连接、边界检查、跨科连接和任意调题动作均不能兼容封存。

该 seal 固定记录
`slice_snapshot_recorded=true`、`legacy_retrieval_status=ready`、
`current_contract_valid=false`、`seal_scope=audit_compatibility_only`、
`reauthorizes_display=false` 和 `formal_write_count=0`。它不改写旧 begin/finalize，不重新
授权展示，也不把对象提升为 current contract。审计只把完整有效的 seal 计入
`legacy_sealed_*`，仍不计入 `current_contract_valid_*`。

## 七、invalid-begin 中止封存

在日期先验门禁加入前，可能遗留已持久化但从未展示、从未 finalize 的无效 begin。不得删除
原对象，不得伪造 reply hash 或 finalize。只能由确认当时没有展示回复的操作员逐个执行：

```text
python3 scripts/personalization_turn_receipt_408.py \
  --repo . \
  --state-dir wiki/study_vaults/408-full/state/personalization \
  seal-invalid-begin \
  --turn-receipt-sha256 <exact-invalid-begin-sha256> \
  --confirm-reply-not-displayed
```

命令仅在以下条件同时满足时创建 seal：

- begin 文件名、文件内容 SHA-256 与命令参数精确一致；
- schema、kind、`answer_safe=true`、`preanswer_call_count=1` 和
  `formal_write_count=0` 构成最小安全恢复候选；
- current begin、pre-slice-snapshot legacy begin、pre-exact-target-binding legacy begin
  三个受支持合同各自独立校验且全部失败；任一合同通过就拒绝封存；
- request pointer 的 key、namespace、object SHA 和指针文件 SHA 都精确绑定该 begin；
- 既不存在该 begin 的 finalize pointer，也不存在任何绑定该 begin SHA 的 finalize object；
- 操作员显式提供 `--confirm-reply-not-displayed`。系统不能从缺少 finalize 自动推断未展示，
  因此没有这项明确确认时 fail-closed。

派生对象固定写入：

```text
turn-receipts/objects/aborted-begin-seal/<seal-sha256>.json
turn-receipts/pointers/aborted-begin-seal/<invalid-begin-sha256>.json
```

seal 内容寻址绑定 begin SHA、request ID、request pointer SHA、三个合同的精确失败代码，
并固定声明 `no_finalize_pointer=true`、`no_finalize_object=true`、
`operator_confirmed_not_displayed=true`、`reply_displayed=false`、`finalized=false`、
`reason=invalid_begin_contract`、
`reauthorizes_display=false` 和 `formal_write_count=0`。重复执行只能返回同一 seal；命令不得
改写原 begin、原 request pointer，也不得创建 finalize。

审计每次重新验证上述条件。seal 或原对象篡改、seal 无指针、指针无对象、request pointer
漂移、后来出现 finalize、绑定正常 current begin、绑定任一可兼容 legacy begin，均使
`integrity_status=fail`。通过校验的对象只计入 `aborted_attempt_count`，其
`current_contract_valid=false` 和 `finalized=false` 永久保持。

## 八、与 v2 的兼容边界

- 既有 `personalization_turn_begin_receipt_v1` 的字段、对象 SHA、request pointer、turn ID
  和 finalize binding 不变；reader、finalize 与 audit 继续使用本文件的原验证规则。
- 新 writer 不把 v1 的 `slice_snapshot` 搬走、不重写 begin，也不重新计算旧 turn ID。
- 同一 request ID 已绑定 v1 时，v2 默认入口返回原 v1 对象的紧凑视图，
  `preanswer_call_count=0`，不得为升级格式再次调用 pre-answer。
- `inspect-receipt` 可以读取 v1 的完整嵌入式 snapshot；它不调用 pre-answer、
  不运行全量 receipt audit，也不改变回执。
- v1 与 v2 begin 可以共存于同一个 `turn-receipts` root。audit 必须分别计数，
  但 completion、finalize pointer 和 reply SHA 门禁保持统一。
