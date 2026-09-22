# 408 真题风格 Release v1

本协议定义 2009–2025 canonical 真题的答案安全风格画像、来源对账和内容寻址 release。完整题干、选项、答案、解析、图片和 OCR 继续只存在于外部详情库；本协议和主仓库不得复制这些内容。

## 来源边界

- canonical 真源是外部详情库既有 `indexes/question-index.csv` 与 `sources/source-manifest.csv`，共 799 题、34 个 year-role 来源槽位。
- T9 version2 附件范围必须得到 33 个 SHA-256 完全匹配。
- 唯一允许的缺口是 2021 题卷；它必须登记既有 version1 fallback 的精确路径与 SHA-256，并验证与 source manifest 相同。缺失、漂移或其他 fallback 一律失败关闭。
- release 不重跑 OCR，不改写既有 799 corpus，不把真题内容写入主仓库。

## Canonical exposure

每道 canonical 题生成一个 `canonical_question_exposure_v1` 记录：

- Q01–Q40：`exposure_mode=direct_pool`，17 年共 680 题。
- Q41–Q47：`exposure_mode=style_only`，17 年共 119 题。
- `direct_replay_allowed` 只在 `direct_pool` 为真；实际展示仍需运行时近 7 天、来源、答案隔离和 session freshness 门禁。
- 所有记录必须声明 `answer_content_in_release=false`。

## 风格画像

风格画像按以下三元组确定性分组：

1. `main_knowledge`：canonical index 中已复核的唯一主知识点代码。
2. `cognitive_action`：受控词法规则得到的认知动作。
3. `question_form`：单选直接、判断、情境、定量或综合题结构。

机器表面是 `style-profiles.jsonl`；人类表面是 `style-guide.md`。两者只含结构分类、canonical ID、年份和计数，不含题目或答案正文。

### 深层答案安全规格

每个 canonical exposure 与聚合 profile 还必须包含：

- `condition_density`
- `data_representation`
- `legal_parameter_ranges`
- `calculation_scale`
- `polarity`
- `option_parallelism_proxy`
- `reasoning_depth`
- `distractor_mechanism_categories`

Canonical 推断必须记录 `confidence` 和 `source`。Profile 只保存类别分布、计数桶、confidence/source 汇总。`legal_parameter_ranges` 只保存参数类型、合法域类别、观察计数桶和范围形态，不得保存题目中的原始数值、最小值、最大值或示例值。

V2 记录 `invariant_axes` 与 `mutable_surface_axes`。V3 只允许 `allowed_single_structural_edges` 中列出的单轴变化，且每条边必须声明 `max_changed_structural_axes=1`；release 不因存在这些边而获得生成授权。

## Direct surface 与私有答案 commitment

每个 canonical exposure 必须公开题面资产的相对 locator、SHA-256、字节数和 source page locator，但不复制资产内容。

Q01–Q40 每题在 `style-releases-private/<release_id>/answer-capsules/` 生成一个 mode 0600 的私有 capsule。公开 exposure 只保存 `private-sibling://` commitment ref 与 capsule SHA-256，不保存正确选项。严格 parser 只能接受唯一明确答案；缺失或歧义分别写 `missing|ambiguous`，该题 `production_usable=false`，不得猜测。Q41–Q47 不生成答案 capsule commitment。

2009–2024 解析 PDF 的答案表页必须以 350 dpi 渲染，并使用 Tesseract `chi_sim+eng` 生成 TSV。Year-specific 空间表格 parser 只接受完整的 Q01–Q40 网格；私有 evidence 保存来源页、表格 bbox、render/TSV SHA-256、逐格 OCR candidate、置信度和 parser 方法。代表版式至少抽样检查 grouped、numbered、scan 和新版横向网格。

答案晋升规则：完整且结构清晰的单一答案表可以作为权威表；若表不完整，只能在表格单元 candidate 与独立 per-question solution OCR 的明确 `故选X/答案X/选项X` 一致时晋升。完整表与 solution OCR 冲突时写 `conflict` 并失败关闭。2025 分科答案页没有形成单一完整 Q01–Q40 表时保持 blocked，不得用局部答案猜全卷。

## 2026 provisional

三份 2026 PDF 只生成 source manifest、冲突状态和结构画像：

- source count 必须为 3，全部绑定 SHA-256 与页数。
- 未完成逐题身份、题面和答案的三源交叉复核前，`production_usable_question_count=0`。
- 不为 2026 分配 canonical 题 ID，不纳入 680 direct pool，不提供生成或展示授权。
- 固定表面写入外部详情库 `provisional/2026/`；同一状态也由内容寻址 release 哈希绑定。

## 重点考纲

`408重点考纲.pdf` 只能登记为 `authority=priority_hint_only`。它可以影响人工优先级讨论，但不能充当题目事实、答案依据、生成授权、canonical 身份或验收依据。

## 内容寻址与审计

release 位于外部详情库 `style-releases/ESR-<sha-prefix>/`。`content_sha256` 由除 release ID 和自身哈希外的完整 manifest 规范 JSON 计算；目录名必须与其一致。manifest 逐文件记录 SHA-256 与字节数。

确定性审计至少验证：

- 33 个 T9 match 与 1 个 2021 fallback。
- 799 exposure、680 direct pool、119 style-only。
- style profile 汇总计数完全相同。
- 2026 source count 为 3 且 production usable 为 0。
- priority hint 没有 generation authority。
- 所有 artifact hash、byte count、release ID 和 content hash 一致。
- 799 题面资产 locator/SHA/page commitment 可重开验证。
- 680 个私有答案 capsule 的 ref/hash/mode 有效，公开 exposure 不含答案字母。
- 深层 canonical/profile 字段完整，推断含 confidence/source，参数范围不含原始值。

CLI：

```text
python3 scripts/build_exam_style_release_408.py build
python3 scripts/build_exam_style_release_408.py audit --release <release-dir>
```

## V2/V3 gate

`canonical_question_exposure_gate_v2` 只允许读取已审计 release 中 `production_usable=true` 的 canonical 项；直接原题还必须是 `direct_pool`。

`exam_style_generation_gate_v3` 只描述后继生成链需要满足的结构条件：独立 author、独立 verifier、source commitment、答案隔离和运行时 freshness。release 本身不授权生成；2026 provisional 始终失败关闭，直到独立 successor 协议逐题验收并发布新 release。
