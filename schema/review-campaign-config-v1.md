# 408 节点覆盖 campaign 配置 v1

`review_campaign_config_v1` 定义节点级晨间复盘 campaign 的不可变策略输入。它是冷路径调度配置，不授权修改正式错题、复习事件、会话或 Capture。

## 固定边界

```json
{
  "schema": "review_campaign_config_v1",
  "campaign_id": "RC-408-2026-110D",
  "campaign_start": "2026-08-31",
  "exam_date": "2026-12-19",
  "consolidation_start": "2026-12-12",
  "latest_safe_day": "2026-12-11",
  "desired_retention": 0.90,
  "cohort_expected_count": 454,
  "segment_size": 16,
  "semantic_cap": null,
  "timezone": "Asia/Shanghai"
}
```

- `campaign_start` 到 `exam_date` 相差 110 天；考试日不生成晨间 run。
- `latest_safe_day` 是未知日期节点首次平滑进入主周期的最后一天，固定为收束期前一天。
- `2026-12-12` 至 `2026-12-18` 是收束期，只处理按 0.90 保持率已经到期的预测遗忘风险，不机械清零。
- `semantic_cap=null`：语义排序不能截断到期集合；全部到期节点都进入当日 run。
- `segment_size=16` 只切分执行段，不改变到期分母。

## cohort 与 admission

基线 cohort 是 campaign 启动时的 454 个正式节点。此后新增或刷新的正式节点只通过追加账本进入调度真源：

```text
wiki/study_vaults/408-full/state/review-campaign/admissions.jsonl
```

事件 schema 为 `review_campaign_admission_event_v1`，仅接受：

- `campaign_node_admitted`：campaign 启动后新增的正式节点；它增加总 cohort，不改变基线 454。
- `campaign_node_refreshed`：已有节点的正式行刷新。若该节点属于原始基线且这是它的第一条 admission 事件，必须写 `baseline_member=true` 且 `previous_admission_event_id=null`；后续刷新必须指向同节点上一事件。

成功事件必须含 `status=succeeded`、稳定 `event_id`、`idempotency_key`、`formal_node_id`、`formal_node_row_sha256`、来源 receipt 引用和 SHA-256。`event_sha256` 是移除自身后按 canonical JSON 计算的 SHA-256。失败或未完成事件不得进入该真源。

每个正式节点还必须在 `原题复做轨总表.md` 恰好出现一行。轨状态为 `blocked_needs_user` 的节点进入 blocked，并保留 `待补事项` 和轨行 SHA-256；轨缺失或重复同样 blocked。轨中出现正式表不存在的额外节点时整体 fail closed。campaign 启动后 admitted 的新节点也必须已经有唯一原题轨，否则不能进入 included。

每次 forecast/run 都冻结 admission ledger 的 byte size、事件数、文件 SHA-256、末事件 ID/hash 和逐节点末事件绑定。已冻结 run 永远保留原 cutoff；新 admission 只进入下一次未冻结 forecast/run。

forecast 同时生成 `review_campaign_forecast_refresh_receipt_v1`，绑定 admission cutoff、逐节点 admission binding-set hash、可选 handoff 的触发事件 ID/hash、cohort/memory/config hash 与 forecast content hash。

backflow worker 可提交 `review_campaign_refresh_handoff_v1`：

```json
{
  "schema": "review_campaign_refresh_handoff_v1",
  "request_id": "稳定请求 ID",
  "admission_ledger_sha256": "64 位十六进制",
  "trigger_admission_event_id": "事件 ID",
  "trigger_admission_event_sha256": "64 位十六进制"
}
```

CLI 通过 `--admission-ledger` 和 `--refresh-handoff` 接收。handoff 的事件 ID/hash 必须精确存在于当前 admission cutoff，否则 fail closed。

## 三分恒等式

正式表的每一数据行恰好进入：

- `included`：可调度节点。
- `excluded`：有明确考纲外证据的节点；保留原因和证据引用，不占主周期。
- `blocked`：身份、重复、列数、日期或 admission 行哈希无法验证。
- `blocked` 还包括原题轨 `blocked_needs_user`、缺失或重复；该分类保留轨状态、原因和轨行哈希。

发布时必须满足：

```text
cohort_count == included_count + excluded_count + blocked_count
```

source bindings 必须同时绑定 `节点总表.md` 与 `原题复做轨总表.md` 的整文件 SHA-256。

未知日期不是 blocked。它使用确定性的 `balanced_hash_spread`，先按带盐 SHA-256 排序，再轮转分配到 `campaign_start..latest_safe_day`；各日首次进入量最大差不超过 1。

## FSRS 后端

配置必须显式写 `backend=fsrs==6.3.2`、21 个 FSRS-6 参数、`desired_retention=0.90` 和 `enable_fuzzing=false`。运行时必须验证已安装分发版本恰好为 6.3.2；缺失或漂移时失败关闭，不允许静默 fallback。依赖固定在 `requirements-morning-coverage.txt`。
