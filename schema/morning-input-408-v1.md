# 408 晨间备题输入 v1

2026-09-21现行限额：本地已授权的morning-selection-policy使同一导出器输出单个cs408-morning-selection-input-v1选题包。内部完整原生快照仍保留对账用途，网页从合格候选池全局选30题，只给所选题做完整备题。排序为精确7天硬排除、上次真实接触日期最早优先、近期真实断点次优先；未知日期在后。带selection-contract.json的回包根据本地登记输入自动生成可信subset decision，未选点保持未复习。下文全量逐点覆盖与自动分片仅适用于未启用限额的旧输入或用户明确要求的全量准备。

限额回包的question_id必须属于candidate-pool相应知识点/真题行的formal_ids，表示本地候选锚点。实际真题通过true_item_id与surface_sha256绑定；另一个真实来源正式ID保留为canonical_source_formal_id，不替换登记锚点。source_refs同时保留登记锚点的同版安全卡与实际原题来源，asset_refs.question_id和questions/<question_id>/assets/的公开路径跟随登记锚点；源图路径及SHA不随锚点改变。

`export_morning_input_408.py --date YYYY-MM-DD` 从当前就绪的固定发布快照导出完整备题资料，不选定代表、不生成题、不启动学习、不改正式记录。正式加强入库的apply收尾会在正式对象/个人摘要/同版发布完成后调用同一导出器；本次或补导出也可独立调用。

成功交付 `408_morning_input_<目标日期>.zip` 和 `START_WEB_D_<目标日期>.md`，状态为READY_FOR_WEB_PREPARATION。这是资料准备就绪，不是网页语义备题完成或学习ready。

包内含：
- scope/的完整due_items、due_concepts、concept_requests、review_concepts、review_concept_ids、blocked和来源绑定；每个集合有连续分页到EOF的读回记录。
- concept-index.json和每点concepts/<code>.json，包含所有原生候选、当前正式个人摘要、相关完整摘要源、对应风格与既有变式公开题面。
- candidates/<formal_id>.json保存真实正式记录、原题/解析/必要原图定位；materials/按实际内容哈希去重存放源字节，source-map.json保留所有发布路径映射。
- runtime-eligibility.json单独绑定当前私有曝光和候选资格指纹、明确ESR、拒绝原因；不冒称这些私有运行时资料属于source_commit。导入时仍重查，未选唯一代表、不消费题面。
- batches.json、progress.json和return-template/。小批次只是网页执行/保存的组织方式，不截断完整目标集合。
- START_HERE.md、START_WEB_D.md、RETURN_CONTRACT.md、contracts/及全文件manifest。

范围是更新后的整个正式库的完整到期集合与点级请求并集，不仅是本次新入库题。未正式Capture不被冒充正式历史；没有网页个人成稿的点保留not_authored和可用机械事实，不现场虚构完整概况。

网页主要承担选题判断、必要变式、求解自检和个性化成稿。输入包已有的资料直接读取，MCP只作必要补查，不去GitHub找只在本地发布的提交，不反复扫大索引。既有变式私有评分capsule不导出；原始题面/解析属于教师备题材料，不能作为未作答页面。

回包仍使用原408_morning_review格式及原图引用合同；任何完整、PARTIAL或BLOCKED回包都必须保存实际成功文件、执行报告和具体失败调用，继续其他独立项。真实零成功如实写零。普通全量输入包不能改名成晨间回包。

正式批次首次固定目标日期，默认上海次日，可用--morning-date指定；恢复时不得静默换日。已正式提交但导出失败返回FORMAL_COMPLETE_MORNING_INPUT_PENDING，只补导出，不重放writer/profile。已有缺项或读取维护不因资料包生成成功被消掉。

## 并行分片

超过40个目标默认由split_web_input_408导出互斥分片和partition-set.json。父包保持原始完整集合；每片仍使用cs408-morning-input-v1，coverage只统计assigned_unit_ids，并在partition保存父ZIP哈希、父input_version、part_id/index/count及完整本片ID列表。同版来源共享，实际材料只包含可达依赖和公共合同。186点固定6片各31点；小批不强制凑6片。

每片独立网页会话，batches.json每5点仅作保存和阶段下载；不反查全量集合。输入目录与输出目录明确分开。return-template带真实分片绑定，本片回包仍不可直接prepare。

merge_morning_parts_408从本地partition-set和各片ZIP恢复父批完整due/request集合。校验片身份、固定版本、实际题目文件及声明覆盖；有缺片、冲突或缺题保留原ZIP和可用内容，输出PARTIAL_LOCAL_COMPLETION_REQUIRED，由本地补齐后再原生prepare。MERGED_PENDING_NATIVE_PREPARE仅证明合并结构完整，不证明语义审核或学习ready。

## Local due representative substitution

A D delivery may include optional `substitute_items.jsonl` for an explicitly
selected same-point source that is not a due formal node. Each row retains the
normal source/teaching/image fields, sets `execution_kind` to
`due_representative_substitute`, and supplies a nonempty `substitution_reason`,
exact `true_item_id`, and `surface_sha256`. The row has exactly one
`review_target_concept_ids` point and its complete original due group in
`suppressed_same_concept_question_ids`. No `request_id` is accepted from the
package. The source must be in the current point's formal candidate set and have
that exact main point; existing EVB or verified canonical EXAM sources are
supported. Canonical EXAM identity is a same-point source, not a claimed original
identity for the candidate formal node.

Native D import derives a content-bound `DREP-...` execution request only inside
its immutable receipt. These rows appear in `supplemental_items` with
`request_kind=due_representative_substitute` and `execution_kind=concept_supplement`.
They never enter native `concept_requests`, learner events, or scheduler
`due_items`. Their original due nodes remain unanswered carry-over; the union of
ordinary representatives, normal supplements, and substitutes must still cover
the exact frozen review concept set once. The handoff includes all execution IDs
in `supplemental_request_ids` and separately lists substitute IDs.

At actual display, substitutes must bind the current native import receipt and
unchanged source files; candidate membership, main point, exact surface hash and
exposure eligibility are checked again. A canonical source retains its true
`canonical_original` surface kind and verified original images while its learning
execution remains `concept_supplement`. Its exact source exposure is recorded.
Per-episode v2 Capture preserves source images and private grading evidence; no
linked original receives a redo date, error increment, score or FSRS update.


## 2026-09-20 真题限定

新生成输入的selection_policy为true_exams_only_exact_item_cooldown_7_days：仅允许已核验EXAM408原题；近7天同题曝光继续拒绝，普通来源原题和既有/新变式不作后备。无合格同点真题时保留目标并输出preparation_gaps/unresolved，不虚构题目或删除目标。候选包只包含仍合格的真题候选；scope保留完整原生范围及原到期身份。当前D项目完整指令为v7.0。
