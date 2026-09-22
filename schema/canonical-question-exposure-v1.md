# Canonical question exposure v1

本协议限制 canonical 真题在晨间复盘准备中的可见性。它不改变正式错题、review ledger、session 或 Capture 事务。

## 记录字段

每行 `canonical-exposure.jsonl` 必须包含：

- `schema_version=canonical_question_exposure_v1`
- `canonical_id=EXAM408-YYYY-QNN`
- `year`
- `official_question_no`
- `main_knowledge`
- `cognitive_action`
- `question_form`
- `exposure_mode=direct_pool|style_only`
- `direct_replay_allowed`
- `style_observation_allowed`
- `question_asset_refs`：只含相对 locator、SHA-256、字节数与 page locator
- `answer_capsule_commitment_ref`、`answer_capsule_commitment_sha256`：仅 direct pool 使用；正确选项留在私有 sibling
- `condition_density`、`data_representation`、`legal_parameter_ranges`
- `calculation_scale`、`polarity`、`option_parallelism_proxy`
- `reasoning_depth`、`distractor_mechanism_categories`
- `v2_axes.invariant_axes`、`v2_axes.mutable_surface_axes`
- `v3_allowed_single_structural_edges`
- `answer_content_in_release=false`
- `production_usable`

## 模式

| 范围 | 数量 | exposure mode | direct replay | style observation |
|---|---:|---|---|---|
| 2009–2025 Q01–Q40 | 680 | `direct_pool` | 可进入后继运行时门禁 | 允许 |
| 2009–2025 Q41–Q47 | 119 | `style_only` | 禁止 | 允许 |
| 2026 provisional | 0 | 不生成 exposure | 禁止 | 未验收前禁止进入正式 profile |

`direct_replay_allowed=true` 不是展示授权。后继系统仍须核对题目来源、近 7 天规则、当前 action、答案隔离、不可变 prepared pack 和 session freshness。

## V2 读取门禁

V2 consumer 必须先验证 release manifest、artifact SHA-256 和本行 canonical 身份。以下任一情况失败关闭：

- release 未通过确定性 audit。
- canonical ID 不在 2009–2025 Q01–Q47 完整集合。
- `production_usable` 不为真。
- direct replay 请求的 `exposure_mode` 不是 `direct_pool`。
- 行或 release 声称含答案内容。
- direct pool 的题面资产 commitment 漂移，或私有 answer capsule 缺失、哈希漂移、权限不是 0600。
- strict parser 的答案缺失或歧义；此时保留 exposure，但 `production_usable=false`。
- answer-table OCR 与 per-question solution OCR 冲突；此时私有 capsule 写 `conflict`，公开层只反映 blocked 状态。

## V3 生成门禁

V3 可以把 style profile 当作只读结构约束，但不能把 canonical 题干、答案或考纲提示当作未审生成题的事实来源。进入 learner-visible prepared pack 前必须另有独立 author/verifier、source commitment、答案隔离、结构校验和运行时 freshness receipt。2026 provisional 与 `priority_hint_only` 均不得绕过该门禁。

V3 只能沿 `allowed_single_structural_edges` 改动一个结构轴。主知识点、认知动作、极性、推理深度和干扰项机制默认保持 invariant；叙事实体、表层符号、合法参数实例和选项措辞属于 mutable surface。任何同时改变两个结构轴的候选均失败关闭。
