# 408 错误签名与共性检索协议 v1

本协议把统一复盘账本中的近期失败和已经完成日终正式编纂的历史证据，压缩为
答案安全、可审计、可重建的语义索引。它用于个性化解释和练习建议，不是掌握度、
正式错因、正式关系边或完整错题库。

## 一、两层证据

`recent_incremental` 只来自规范 `outcome`。结论、选择或推理任一部分为
`partial`、`wrong`、`uncertain` 或 `diverged` 时可形成失败签名；repair、纯正确
结果和行政事件不能形成失败签名。默认只保留最近 30 天、总计最多 512 条，单个
倒排键最多 8 条。

`formal_history` 只来自根 personalization manifest 精确绑定的日终 corpus。
corpus 必须已经通过正式 closeout、答案安全和对象哈希校验。未正式编纂的旧
canonical event 不得伪装成历史层。

日终 corpus 在冷路径为每个 object 派生受控的 mechanism、error pattern 与 method
倒排键。回答前最多按当前目标命中少量对象，再对对象重新生成签名并复核真实交集。
所以“长期历史”可以跨考点编号按机制召回，但共享词、同科或同模块仍不能升级为
直接证据。

## 二、失败签名

每条 `failure_signature_v1` 至少绑定：

```text
signature_id
layer
subject
point_ids
mechanism_ids
mechanism_primitives
objects
preconditions
state_transitions
causal_directions
boundaries_and_units
error_patterns
method_templates
first_action.value + provenance
first_break.value + provenance
question_type
presentation_kind
conclusion_result
reasoning_result
first_result
confidence
evidence.event_id / question_id / observed_date / source_id / provenance / content_sha256
evidence_gaps
answer_safe=true
```

只允许受控短标签进入语义面。不得保存完整题干、选项、答案、解析、完整作答或
自由扩写的错因。`first_break` 只有在内容扫描通过且 provenance 明确时保留；未
观察到时记录缺口，不猜测。

## 三、九级匹配基础

匹配基础固定为：

```text
exact_question
exact_point
same_mechanism
same_error_pattern
same_method
verified_cross_subject
module_fallback
subject_fallback
weak_semantic_candidate
```

前六类只有在稳定 ID、受控机制原语或已验证跨科边支持时，才可进入直接证据。
后三类只能进入 `background_context`，不能驱动个人历史陈述、第一检查边界或调题。
共享表面词不等于共享机制；没有受控规则或验证边时，最多是弱候选。

## 四、重复共性门禁

只有至少两个不同 canonical failure outcome event 才能输出 `repeated`。同一事件
的 repair、同题纠正和独立正确不增加失败事件数。每个共性对象必须返回事件 ID、
题目 ID、日期、共同机制或错误模式、关系理由、证据强度、反证和练习资格。
只有一条事件时状态为 `single_candidate`；证据冲突或仅弱语义时为
`insufficient_evidence`，不得使用“反复”或“共同薄弱点”等措辞。

## 五、九字段回复计划

`personalization_response_plan_v1` 固定包含：

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

预算裁剪时优先保留当前目标、直接证据和证据缺口；背景兜底不能改变 neutral
解释策略。回复计划只给教学策略，不具有调度或正式写入权限。
