# 408 跨科变式题 Demand Bridge v1

`scripts/cross_variant_demand_planner_408.py` 是只在用户手动启动的明确日期日终流程中运行的
冷路径。它不生成题目、不读取答案、不调度题目，也不修改正式节点、正式关系、复做记录或
掌握度。它只把一个真实失败事件与当前可验证的原题、7 天冷却状态和 live 跨科关系封装成
内容寻址 demand。

## 一、请求边界

请求 schema 为 `cross_variant_demand_request_v1`，并且只允许以下字段：

```json
{
  "schema": "cross_variant_demand_request_v1",
  "execution_scope": "dated_nightly_cold_path",
  "canonical_failure_event_id": "RE-...",
  "canonical_failure_event_sha256": "<sha256>",
  "anchor_formal_node_id": "CO_2099_912",
  "original_source_ref": "personalization-source:question_sources/CO_2099_912.json",
  "original_source_sha256": "<sha256>",
  "original_answer_isolation_attestation_sha256": "<sha256-or-null>",
  "expected_effective_last_attempt_date": "2026-07-24",
  "verified_cross_edge_id": "CSR408-0123456789ABCDEF",
  "targets": [
    {"subject": "CO", "point_id": "CO06-14", "mechanism_key": "RU_CO_INTERRUPT"},
    {"subject": "OS", "point_id": "OS05-13", "mechanism_key": "RU_OS_INTERRUPT"}
  ],
  "expected_cross_activation": "verified_live",
  "personalization_manifest_sha256": "<sha256>",
  "personalization_build_id": "<sha256>",
  "cross_source_fingerprint": "<sha256>",
  "as_of": "2026-07-24",
  "cooldown_days": 7,
  "idempotency_key": "nightly:2026-07-24:RE-...:cross-variant",
  "formal_write_count": 0
}
```

请求不得包含题干、选项、答案、解析、完整作答、反馈、first break、图片或附件。请求文件
必须是仓库内的普通文件，不能通过绝对外部路径、`..` 或符号链接逃逸。

## 二、确定性验证

bridge 在生成 demand 前完成以下检查：

1. `as_of` 必须等于当前 `Asia/Shanghai` 日期，且根 manifest 的 SHA-256、`build_id`、
   `as_of`、答案安全标志和零正式写入均与请求一致。
2. `event_mappings.json`、`question_index.json` 和 canonical review ledger 必须与根
   manifest 的哈希一致。
3. canonical event 只能通过 review hot state 的精确 event ID 读取；事件 SHA-256、
   `partial/wrong/uncertain`、原题展示类型、唯一正式节点身份和映射必须完全一致。
4. anchor 端的考点与机制必须同时命中 event、event mapping 和 current question index；
   两个 target 必须属于不同学科。除此之外，两端的 `mechanism_key` 都必须在当前 manifest
   绑定的 `question_index.json -> mechanisms` 中存在，并由同一条机制记录同时证明精确
   `point_id` 与对应学科的正式节点身份。机制名即使通过格式正则，只要绑定缺失就进入
   `source_gap`，绑定到别的考点或学科就进入 `blocked`；不得用命名相似性兜底。
5. 原题 source ref、source SHA-256、最近作答日期和独立 answer-isolation attestation
   必须与请求承诺一致。完整题源通过 `validate_source_for_practice(..., practice_mode="original")`
   重放证明链；文件名或 `answer_isolated=true` 不能替代证明。
6. `cross-subject-shadow.json` 必须由当前根 manifest 绑定，source fingerprint 必须一致；
   edge 端点必须精确等于两个 target。只有 `verified_existing + activation_eligible +
   enabled_for_live_routing + verified_live` 能通过 live 重验证。
7. 最近原题日期到当前日期不足 7 天时才允许生成跨科变式 demand；恰好 7 天或更久时
   返回原题路线，不能用变式替代应做的原题。

## 三、四种终态

- `ready_for_cross_variant`：原题在 7 天内、题源及独立隔离证明有效、跨科 edge 当前 live，
  可以进入后续 prepare。
- `original_due`：可靠原题已满 7 天，返回原题路线，不进入跨科变式生成。
- `source_gap`：原题题面、独立隔离证明、冷却日期或当前投影证据缺失；不得临时编题。
- `blocked`：端点、考点、机制或 live policy 不一致；不得用弱边、shadow 边或关键词共现
  兜底。

输出 schema 为 `cross_variant_demand_v1`。它只含受控 ID、哈希、日期、状态和 prepare 的
固定字段，不含私有答案定位。`prepare_contract` 只有在 `ready_for_cross_variant` 时存在。

## 四、幂等与准备

受管状态位于：

```text
wiki/study_vaults/408-full/state/linked-practice/cross-variant-demand/
  requests/<request-sha256>.json
  demands/CVD-*.json
  idempotency/<idempotency-key-sha256>.json
  prepared/CVD-*/<generation-request-sha256>.json
```

同一幂等键和同一请求返回原 demand；同一键绑定不同请求时 fail closed。所有文件均为
可重建派生状态，`formal_write_count=0`。在锁定、读取既有 demand 或写入任一受管文件前，
实现必须从仓库根开始逐级 `lstat` 检查 `linked-practice/cross-variant-demand` 及目标路径的
所有已存在组件；任何符号链接都拒绝。词法检查后，解析路径仍必须位于受允许的
`linked-practice` state root 内，不能通过输出根或 `demands/requests/prepared` 等父目录的
符号链接写到仓库外。

`prepare-demand` 会重新验证当前 event、manifest、题源、冷却日期和 live edge，然后要求
`cross_variant_authoring_evidence_v1` 的原题、两端 target、edge 与 cross projection 精确
匹配 demand，最后才调用 `cross_variant_pipeline_408.py prepare`。私有权威答案、作者身份、
校验者身份以及后续盲解仍由原 cross-variant pipeline 独立验证；demand 不能替代任何一步。

从读取受管 demand、重验完整输入到下游 prepare 固化完成，必须持续持有 personalization
builder 共用的 `personalization/.hot-path.lock`。本 bridge 不得先持有 demand lock 再等待
hot lock；顺序固定为 hot lock 在外、下游 prepare 自身锁在内，prepare 返回后才短暂取得
demand lock 写 receipt。这样 manifest、event、冷却状态、原题 source、live edge 和 mechanism
投影不能由遵守 builder 协议的并发发布在验证与固化之间切换。manifest、受管 demand、请求、
evidence 和投影等关键内容寻址输入使用同一文件描述符完成 `fstat/read/hash`，并在读取前后
核对文件身份与元数据；读取中替换或漂移必须 fail closed。

固定的 personalization、review-loop state root 以及 `.hot-path.lock` 本身也必须在读取或
加锁前逐级 `lstat`，并确认解析后仍位于 repo 内。即使仓库外目录含一套哈希自洽的投影，
也不得通过父级符号链接让 plan 或 prepare 使用该目录。demand 已持有 hot lock 后调用的是
pipeline 的内部 hot-held prepare 入口；外部 direct prepare 仍由 pipeline 自行取得 hot lock。

## 五、命令

```bash
python3 scripts/cross_variant_demand_planner_408.py --repo . plan \
  --request REQUEST.json

python3 scripts/cross_variant_demand_planner_408.py --repo . prepare-demand \
  --demand-id CVD-... --evidence AUTHORING_EVIDENCE.json \
  --answer-key-private-root /受管权限目录
```

该脚本不得接入普通单题热路径。普通回答仍只读取根 manifest 绑定的有界个性化投影；
跨科 demand 只在手动日终冷路径中创建。
