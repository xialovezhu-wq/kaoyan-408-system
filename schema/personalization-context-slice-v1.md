# 408 单题个性化上下文切片 v1（只读历史协议）

状态：已退役。当前题、晨间复盘、普通讲题、`continue-current`、`next-item`、
日终 closeout 和下一晨题包准备均不得调用本协议、读取其投影或追加其选择回执。
既有 manifest、slice、selection receipt、linked followup 和 variant 对象只用于
历史审计与旧 session 重放，缺失或漂移不得影响新流程。

有效替代：主对话只使用 `CurrentQuestionContext v1` 和当前私有 grader capsule；
managed 非终态只写 draft，terminal 后 final package → one Capture → durable
reopen。普通 standalone 只读，明确快速入库才 Capture。热路径没有后台 Luna；
指定范围的明确正式请求才允许 Sol 使用只读原生 Luna 叶子并串行编纂。

以下正文仅记录历史 v1 格式，不再授予任何实时调用、写入或调题权限。

本协议定义 `answer-turn` 在首答已经进入统一复盘账本之后，如何取得一个固定大小、答案安全的个人化上下文。它不改变首答判定、快速捕获、反馈门禁、正式题库或正式关系网。

## 一、调用位置

唯一实时入口：

```python
safe_context_slice(
    repo,
    current_event=canonical_event,
    state_dir=...,
    max_bytes=12288,
    mode="off|shadow|verified",
)
```

晨间 `answer-turn` 的顺序固定为：

```text
record-first 与 canonical 绑定
-> 非正确首答快速 capture
-> review / capture 审计
-> reveal-feedback
-> 调用一次 safe_context_slice
-> 可选注册一个已通过门禁的 linked followup
```

个性化失败是 fail-soft：返回 `status=unavailable`，但不得回滚已经成功的首答、capture 或反馈。原有 review、capture、queue hash 和 `formal_write_count=0` 门禁继续 fail-closed；这些门禁失败时不得提前调用切片器。

## 二、实时读取边界

切片器直接使用调用方已经取得的 canonical event，不得按 event ID 重新扫描账本。

允许读取的有界投影：

```text
manifest.json
catalog.json
learner_state.json
failure_signatures.json
question_index.json
cross-subject-shadow.json              仅当 manifest 绑定其 SHA-256
corpus/manifest.json                    仅当根 manifest 绑定
corpus/indexes/<sha256>.json            最多一个内容寻址索引
corpus/objects/<sha256>.json            仅当前受控键命中的少量对象
```

单题阶段禁止读取：

```text
review-loop/events.jsonl
review-loop/state.json
intake-curation/events.jsonl
intake-curation/state.json
event_mappings.json
完整知识点命中索引
正式节点表和正式关系边表
完整 Obsidian vault
参考书或真题全集
```

这只保证个性化路径不扫描历史。若上层 answer-turn 仍执行完整 review 或 capture
账本审计，必须单独计入其延迟；在改成局部回执校验以前，不得声称整个
answer-turn 已经是严格常量时间。

## 三、增量更新

冷路径每天或资料变化后重建全部投影。热路径只把本次 canonical event 叠加到它可靠映射出的：

- 最多几个考点状态；
- 最多几个机制状态；
- 至多一个确定题目身份。

每个实体只展示最近 5 个 event ID，切片最多展示 3 个。另有完整但只含 ID 的 `applied_performance_event_ids` 用于精确幂等，选择承诺只保存 event、mode、预算和选择摘要哈希；二者都不进入回答切片。累计结果、失败、脆弱和保持次数保留为计数；不把完整历史重新塞入上下文。

热路径更新 `learner_state.json` 和 `question_index.json` 后，以最后替换 `manifest.json` 作为投影提交标记。若中途失败，下一次读取会因哈希不一致而返回 unavailable，不得用半写状态继续调题。

同一 event、同一 manifest、同一预算和同一 mode 重放时，必须得到相同 `slice_id` 和相同选择；不得追加第二条复盘、capture 或 session 事件。

## 四、固定预算

默认总预算为 12,288 UTF-8 bytes，可配置范围 1,024 到 65,536 bytes。切片最多包含：

- 当前目标：3 个考点、3 个机制、2 个确定题目身份；
- 个人状态：3 个考点或机制；
- 最近与正式历史：少量答案安全 failure signature 和匹配证据；
- 共性：只由至少两个不同 failure outcome 形成的有据模式；
- 跨科上下文：2 条有界候选边；
- 题目候选：3 个安全定位摘要；
- 九字段 response plan；
- 最终选择：1 道题或 1 个 grounded variant 请求。

超过预算时依次裁剪普通候选、跨科上下文、corpus 元数据、背景兜底和次要状态，
优先保留当前目标、至少两条直接近期证据、至少一条直接正式历史、至少一个有据
共性和证据缺口。若这些仍无法装入极小的调用方预算，才进入显式 compacted 模式，
不得静默把“历史已读取”变成“历史未进入回复计划”。不得为了保留更多信息突破
预算，也不得在切片中放入题干、选项、答案、解析或完整用户回答。

## 五、七天与题目选择

7 天是原题复做冷却门禁，不是掌握度评分。

题目身份可靠且历史上已有非正确首答时：

- 最近真实作答距当前至少 7 天：可以选择同一原题；
- 最近真实作答不足 7 天：只允许选择该原题锚定、独立生成并独立校验通过的变式；另一道到期旧题可以留在候选证据中，但不得绕过当前锚点的变式门禁；
- 已验证变式本身也执行实际展示冷却：最近一次真实展示在 0 至 6 天内时不得再次选择，满 7 天或日期未知才可进入候选；选择、入队和预取不推进该日期；
- 如果本轮 canonical event 本身就是该原题的真实作答，则无论重建前日期多早，本轮都按第 0 天处理，禁止立即把同一原题再次排回；
- 尚无已验证变式时，只有当前原题存在可靠 source ref 才能返回 `status=variant_request`。它只提供原题锚点和变式生成要求，`schedule_allowed=false`；在独立变式题面、唯一答案检查和耐久 source SHA-256 都准备好以前，不得注册 followup。variant 只把原题作为出题锚点，不更新该原题的最近真实作答日期；
- 没有可靠题源：返回 `source_gap`，不得从安全摘要猜题、答案或干扰项。

有效最近真实作答日期由正式最近复做日期与统一 review ledger 的确定题目 outcome 取最大值。只展示、入队、候选映射和同日 repair 都不能伪装成原题真实首答。

## 六、跨科模式

`off`

只做增量状态同步，不向回答提供个性化内容，也不注册关联题。

`shadow`

返回系统本来会选择的跨科候选和原因，`schedule_allowed=false`，不改变 session。
该模式用于评估新候选，不是日常默认。

`verified`

跨科边必须同时满足 `verified_existing`、非低置信、非弱关系、非 R07 类比、`activation_eligible=true` 和 `enabled_for_live_routing=true`，才允许进入 linked followup。`verified` 模式本身不是启用授权。新批量生成的 `shadow_candidate` 不得实时启用。

日常默认使用 `verified`；若没有合格 live edge，则保守退化为同科解释或机制检查，
不能把 shadow 候选当作已确认关联。

跨科候选生成器默认保持 `enabled_for_live_routing=false`；只有显式 allowlist 可以把已通过资格门禁的 `verified_existing` 设为 true，随后仍由切片器和 session 注册器共同复核。知识点候选不能自动扩张为正式题目关系边。

## 七、linked followup

影子模式不追加 session 事件。验证模式若存在 `schedule_allowed=true` 的唯一选择，可以追加一个 `followup_scheduled`：

- 不改写原不可变 queue；
- 同一 trigger event 最多一个；
- 只保存题源引用、稳定哈希、选择回执引用与 SHA-256、机制、考点、模式、父事件和 slice ID；
- 不保存题干、选项、答案或解析；
- `original` 绑定自己的正式题目身份；
- `variant` 只接受独立、耐久且已验证的变式题面 source；不得复用原题 question-source manifest。它只保存 anchor，canonical outcome 不得更新 anchor 原题日期。

动态题的首答仍要进入统一 review ledger；非正确首答仍按 standing policy 快速 capture。capture 必须验证唯一 `followup_scheduled`，并把该事件哈希纳入 session 证据链。

`selection_receipt` 必须同时绑定 trigger event、slice、candidate、题源、7 天决策、跨科 live gate、投影 build 和投影内 selection commitment。注册、首次作答前、canonical 同步前和快速 capture 时都要重新验证回执与题源；任一文件或 SHA-256 漂移都必须在写入前失败。CLI 的 activation 参数不构成用户授权，实时注册只接受默认受管 personalization 投影中的已验证回执。

## 八、可见 receipt

成功最少返回：

```json
{
  "status": "ready",
  "slice_id": "PS-...",
  "trigger_event_id": "RE-...",
  "projection_id": "...",
  "projection_build_id": "...",
  "byte_count": 4096,
  "truncated": false,
  "mode": "verified",
  "formal_write_count": 0,
  "selection_receipt_ref": "personalization-selection:selection_receipts/PS-....json",
  "selection_receipt_sha256": "...",
  "context": {
    "current_targets": {},
    "learner_states": [],
    "recent_evidence": [],
    "cross_subject_context": [],
    "question_candidates": [],
    "selected_practice": {},
    "response_plan": {
      "current_target": {},
      "direct_recent_errors": [],
      "direct_historical_errors": [],
      "common_patterns": [],
      "verified_cross_connections": [],
      "explanation_strategy": {},
      "first_boundary_to_check": {},
      "optional_practice_action": {},
      "evidence_gaps": []
    }
  }
}
```

失败只返回受控 `reason_code`，例如 `missing_projection`、`stale_projection`、`projection_hash_mismatch` 或 `event_mapping_unavailable`。不得回传堆栈、绝对隐私路径或原始异常文本。
