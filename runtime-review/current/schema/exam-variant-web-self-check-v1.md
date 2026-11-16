# 408 网页自检变式导入 v1

网页 GPT-6 Pro 可以在 D 输出 ZIP 根目录提供 `web_variants.jsonl`。每行是一道由同一网页 actor 出题、求解并做反例自检的变式。它只会进入现有私有 variant bank，状态固定为 `web_self_checked_ready`；它不是 `verified_ready`，不生成或冒充 blind/adversarial verifier，也不创建正式节点或学习结果。

## `web_variants.jsonl`

```json
{
  "schema": "exam_variant_web_self_check_submission_v1",
  "web_variant_id": "WEB-D-20260913-CN01-08-01",
  "web_actor_id": "gpt-6-pro-web",
  "question_id": "CN_UNK_017",
  "target_concept_id": "CN01-08",
  "due_version": "64位小写十六进制",
  "concept_priority_version": "64位小写十六进制或null",
  "formal_node_sha256": null,
  "exam_style_release": null,
  "style_profile_id": "ESP-28698fc83a7b7e7c",
  "style_fallback": null,
  "canonical_anchor_commitments": [
    {"canonical_id": "EXAM408-2025-Q33"}
  ],
  "variant_level": "V2",
  "deep_style_spec": {
    "condition_density": "medium",
    "data_representation": "diagram_description",
    "legal_parameter_ranges": [
      {"parameter": "n", "constraint": "n为正整数", "unit": "个"}
    ],
    "calculation_scale": "multi_step",
    "polarity": "neutral_or_implicit",
    "option_parallelism": "strict",
    "reasoning_depth": 3,
    "distractor_mechanisms": ["direction_or_order_reversal", "formula_component_omission", "off_by_one_or_counting", "unit_or_scale_confusion"],
    "v2_invariant_axes": ["main_knowledge", "cognitive_action", "polarity", "reasoning_depth", "distractor_mechanism_categories"],
    "v2_mutable_axes": ["narrative_entities", "parameter_instantiation_within_legal_parameter_ranges"],
    "v3_invariant_axes": ["main_knowledge", "cognitive_action"],
    "v3_mutable_axes": ["condition_density"]
  },
  "public_surface": {
    "stem": "公开题干",
    "options": [
      {"option_id": "OPT-A1", "text": "公开选项一"},
      {"option_id": "OPT-B1", "text": "公开选项二"},
      {"option_id": "OPT-C1", "text": "公开选项三"},
      {"option_id": "OPT-D1", "text": "公开选项四"}
    ]
  },
  "private_material": {
    "grading_path": "private/web-variants/WEB-D-20260913-CN01-08-01.grading.json",
    "grading_sha256": "64位小写十六进制",
    "solution_path": "private/web-variants/WEB-D-20260913-CN01-08-01.solution.json",
    "solution_sha256": "64位小写十六进制",
    "self_check_path": "private/web-variants/WEB-D-20260913-CN01-08-01.self-check.json",
    "self_check_sha256": "64位小写十六进制"
  }
}
```

示例中的 profile、anchor 与 axes 取自当前发布合同，`question_id/target_concept_id` 仍须替换为本次 selected/supplement 的真实绑定。`formal_node_sha256`、`exam_style_release`、anchor 的 `canonical_exposure_sha256` 与 `release_content_sha256` 可省略或写 `null`。这些值不要求网页猜测：native 依据同一个 `due_version`、当前正式节点快照和固定 ESR 本地派生并写入导入证据；网页若提供，必须逐字段一致。`style_profile_id`、anchor identity、`deep_style_spec`、V2/V3 变化轴仍必须来自发布的 style/profile 合同。V1 永久拒绝。

`private_material` 三个路径必须是 ZIP 解压根内的相对 JSON 路径，不能包含 `..` 或符号链接。三个 SHA-256 都是必填，native 会逐字节读回。网页 D 的 selected/supplement item 用 `web_variant_id` 引用新增变式；锁定既有 bank 变式时用 `preferred_true_item_id=<EVB-...>`。可同时给 `true_item_id` 与 `surface_sha256` 作为额外一致性断言。

## 发布画像字段兼容

网页的deep_style_spec可使用原示例的原生词表，或固定ESR发布画像的具体类别：data_representation可用address_or_bit_layout、code_or_pseudocode、formula_or_symbolic、graph_tree_or_topology、multi_asset_visual、natural_language_scenario、sequence_or_trace、table_or_matrix、timing_or_state_diagram；calculation_scale可用local_single_step、short_multi_step、extended_multi_stage；option_parallelism可保留low/medium/high代理值。原值会保留，未知值仍拒绝。

允许附cognitive_action、reasoning_depth_bucket和representation_note；前两项必须与绑定profile相符，附加原值保留在导入证据。distractor_mechanisms列本题真实机制类别，三项干扰可以同类，不要求虚构三种机制；grading仍须恰好覆盖三个错误选项。V2可令v3_mutable_axes为空，V3仍必须有允许的结构轴及唯一结构变化。

canonical_anchor_commitments只能使用production_usable=true的正式已验收锚点。style_observation_allowed=true不等于可以据此正式生成/导入变式。输入style_profiles.canonical_anchor_eligibility直接给出资格；不可把已有素材或可读风格当作锚点准入。受阻锚点的历史草稿要保留，本地实际读取合法同点来源后才能校准，不伪造网页已读该新来源。

既有精确原题也可使用preferred_true_item_id=<EXAM408-...或FOS-...>和实际surface_sha256锁定；它们按原题资格/曝光检查，不能被当成EVB或自动换题。原题公开图仍必须属于同一question_id的已发布题图；额外风格锚点素材只能用role=reference并放private，绑定本题web_variant的真实profile/anchor。

## grading JSON

```json
{
  "schema": "exam_variant_web_grading_v1",
  "web_variant_id": "WEB-D-20260913-CN01-08-01",
  "web_actor_id": "gpt-6-pro-web",
  "correct_option_id": "OPT-B1",
  "distractors": [
    {"option_id": "OPT-A1", "mechanism": "formula_component_omission", "misconception": "具体误区", "refutation": "反证或排除理由"},
    {"option_id": "OPT-C1", "mechanism": "off_by_one_or_counting", "misconception": "具体误区", "refutation": "反证或排除理由"},
    {"option_id": "OPT-D1", "mechanism": "unit_or_scale_confusion", "misconception": "具体误区", "refutation": "反证或排除理由"}
  ]
}
```

## solution JSON

```json
{
  "schema": "exam_variant_web_solution_v1",
  "web_variant_id": "WEB-D-20260913-CN01-08-01",
  "web_actor_id": "gpt-6-pro-web",
  "explanation": "网页 actor 的完整求解；不得称官方答案",
  "source_commitments": [
    {"fact_id": "FACT-1", "source_kind": "canonical_anchor", "source_id": "EXAM408-2025-Q33", "source_sha256": null}
  ],
  "transformation_axes": [
    {"axis": "parameter_instantiation_within_legal_parameter_ranges", "kind": "numeric", "before": "原参数", "after": "新参数"},
    {"axis": "narrative_entities", "kind": "non_numeric", "before": "原语境", "after": "新语境"}
  ],
  "changed_structural_edges": []
}
```

V2 至少两个变化轴且至少一个为 `non_numeric`，`changed_structural_edges=[]`。V3 恰好一个 structural edge；其 `axis + relation` 必须命中 profile 发布的允许边。`source_commitments` 继续使用现有 author 确定性合同；canonical anchor 的 `source_sha256` 可写 `null`，native 会补成固定 ESR content SHA-256。非 canonical 的 authoritative reference 仍必须给出真实 SHA-256。

## self-check JSON

```json
{
  "schema": "exam_variant_web_self_check_v1",
  "web_variant_id": "WEB-D-20260913-CN01-08-01",
  "web_actor_id": "gpt-6-pro-web",
  "self_check_kind": "same_actor_solution_and_counterexample",
  "verdict": "accept",
  "derived_option_id": "OPT-B1",
  "reasoning": "不读取标签、重新求解后得到的理由",
  "option_checks": [
    {"option_id": "OPT-A1", "judgment": "incorrect", "counterexample_or_proof": "具体反例或排除证明"},
    {"option_id": "OPT-B1", "judgment": "correct", "counterexample_or_proof": "成立证明"},
    {"option_id": "OPT-C1", "judgment": "incorrect", "counterexample_or_proof": "具体反例或排除证明"},
    {"option_id": "OPT-D1", "judgment": "incorrect", "counterexample_or_proof": "具体反例或排除证明"}
  ],
  "second_best_search": {"found": false, "analysis": "为何不存在第二个同样正确的选项"},
  "checks": {
    "answer_isolated": true,
    "counterexample_search_complete": true,
    "distractors_plausible": true,
    "facts_traceable": true,
    "four_distinct_options": true,
    "one_best_answer_derived": true,
    "refutations_consistent": true,
    "style_profile_match": true,
    "target_knowledge_match": true,
    "unique_key": true,
    "variant_level_gate": true
  },
  "independent_reviewed": false,
  "independent_actor_ids": []
}
```

native 只做确定性结构、哈希、身份、选项唯一性、轴约束、来源承诺和自检一致性检查，不重新判断语义正确性。导入先校验整批对象，再写 content-addressed bank 对象；进程或磁盘错误可能留下不可变的中间对象，但不会产生 index entry、consumption、展示、学习或正式写入。相同输入重试会复用相同哈希对象并完成 manifest/index，web identity 改内容则失败关闭。

selected item 仍需原有 `question_id`、`review_target_concept_ids`、完整 suppression、`source_label/source_refs`、`problem_path/solution_path`。可选 `teaching_path` 指向题前线索、答后完整解法、易错边界与提示层级；import receipt 保存其已验证相对路径、SHA-256 和字节数，不内嵌答案。
