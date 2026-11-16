# 408 网页精确题讲解与路线回流 v1

本合同更新派生教师材料。完整学习事实和个人概况仍由 `enhancement.json`、`knowledge_updates.json` 及 `portable_visual_teaching_plans.json` 的原写者处理；本文件不形成第二套掌握度或教学效果记录。

## 1. 输入承诺与固定来源

新输入含 `TEACHING_ALIGNMENT_REQUIREMENTS.json`，schema=`cs408-teaching-alignment-requirements-v1`、`required=true`，本机按原 ZIP、requirements 字节 SHA-256 登记到 `.private/study-bridge/teaching-alignment-inputs/<input_id>.json`。登记保存 requirements 完整对象、`requirements_sha256`、`input_zip_path`、`input_zip_sha256`。回包不得自行取消 required、修改原 Capture 集合、换快照逃逸新合同。新 prepare 时，返回集合只要与同日任何 required 注册输入相交，就不能通过删去 portable 文件或缩成未登记 subset 降级为 legacy；合法分片必须沿已登记分片或先合回原总包。

requirements 含 `input_id`、`source_commit`、`study_date`、排序的 `capture_ids`、原 `capture_set_sha256`、候选 `affected_points`、`question_targets`、`route_targets`、`source_files`。source_files 是原输入成员的 path/sha256 清单。候选点不是已观察学情；真正受影响点由原 enhancement 校验的逐 Capture 真实 observations 确认，必须等于回包 manifest.affected_points。

question_targets 每项含 `capture_ids`、`question_identity`（未知为 null）、`knowledge_points`（未分类为空数组）、`expected_previous_sha256`、`expected_source_plan_sha256`。route_targets 每项含 `identity`、`parent`（规范点为 null，小点为父规范点）、`expected_previous_sha256`。版本必须取同版 MCP 的 `template_native_version`、`source_visual_plan_version`、`linear_teaching_route.native_route_version`，不得用 effective route 或可移植图对象的 hash 冒充原生版本。旧快照缺字段就报告缺口，不猜测。

已核精确题的knowledge_points是备课候选范围；同一Capture正式增强决定中已经核验的observed点允许补入该题教案。其他Capture的新增观察不能借此扩本题范围，原精确身份、Capture集合及版本约束不变。

资料可以来自项目上传原件，但回包必须携带原件或其已登记输入路径与原始字节 hash；一句“已在项目记忆中”不能代替可重开的来源。MCP 读取固定发布快照，不直接写本机正式数据库。

## 2. 回包文件

新增 `portable_question_teaching_updates.json`，在原 manifest.files 内登记 SHA-256、大小、`role="supplemental"`；不扩 manifest 顶层。

```json
{
  "schema": "cs408-portable-question-teaching-v1",
  "source_commit": "固定40位快照",
  "study_date": "2026-10-23",
  "input_binding": {"input_id": "原输入ID", "requirements_sha256": "原文件SHA256"},
  "questions": [],
  "routes": []
}
```

questions 每项必须完整提供以下字段，不增删字段：

```json
{
  "capture_ids": ["本批Capture"],
  "question_identity": "已核EXAM408/Pxx/正式题身份，或null",
  "logical_question_id": null,
  "knowledge_points": ["本批实际观察规范点"],
  "disposition": "retain",
  "reason": "依据本批原始作答、原题和同版资料，具体说明保留、修订或受限原因",
  "expected_previous_sha256": "原精确题模板SHA256，首次为null",
  "expected_source_plan_sha256": "原source_visual计划SHA256，不存在为null",
  "source_refs": [],
  "template": null,
  "source_plan": null
}
```

- disposition 只用 `retain`、`revise`、`source_limited`。retain 明确保留已有对象，template/source_plan 为 null，但仍需本轮真实来源核对记录；讲法有效且无需变化时不强制改写。
- revise/source_limited 交完整 template 与同步 source_plan。template 复用 `cs408-question-visual-template-v1`；effectiveness 永远 unverified，source_review_status 分别为 reviewed/source_limited。这表示来源审查状态，不表示学习效果已验证。
- 新题只有 enhancement 同 Capture 的 action=new、logical_question_id 才能使用 logical 身份；question_identity 为 null，两个 previous hash 为 null。本地 prepare 不分配正式 ID；正式 writer 成功后把逻辑身份绑定到它实际分配的正式 ID。
- 普通 concept_only 可能真实做过既有真题而尚未建立正式节点。本地先核对原题并使用现有 `recent_contact_408.py bind-capture` 登记，随后可在同批接受该 canonicalID；适配器读回本地绑定的 Capture payload hash 和 fixed publication original mapping。网页声明不能代替该绑定。注册时未知身份而本地后来已核时，还需引用固定发布的 exact template JSON，重开核对 template_native_version/source_visual_plan_version。
- 身份未闭合且没有合法 new logical 身份时，question_identity/logical_question_id 都为 null，disposition=source_limited，template/source_plan=null，reason 写明具体缺口；只记受限审查结果，不伪造节点或替换为关联原型。
- 2026-10-26补充：本地已逐题核原图与同版规范题的待增强standalone，可先用 `recent_contact_408.py bind-capture --allow-pending-review` 补题源身份再prepare。该显式入口要求awaiting_web_enhancement、standalone、source_id与核定canonical一致、完整已封原包及明确reviewed_by；只记录来源映射、原Capture哈希及原学习日期，不提交学习、错误次数、FSRS或概况。网页候选不能自行调用或代替本地原图审核；旧默认入口仍仅接受concept_only。
- 每个本批实际相关 question_target 都要明确审查，不能用一条重复 target 凑齐数量。不能修改本批 Capture 以外的题。

routes 每项为：

```json
{
  "identity": "DS01-01",
  "parent": null,
  "capture_ids": ["本批Capture"],
  "disposition": "retain",
  "reason": "本轮原题与真实断点支持的处理理由",
  "expected_previous_sha256": "原生route SHA256，首次为null",
  "source_refs": [],
  "route": null
}
```

每个 manifest.affected_points 至少一条父点路线审查；KSP 只处理本轮实际需要变化或复核的小点。retain 的 route 为 null；另两种交原 `cs408-derived-teaching-route-v1` 完整对象，identity/parent 必须一致、teacher_release_level=5、frontend_ready=false、effectiveness=unverified、learning_write_count=0，保留 ordered_blocks、visual_insertions、limitations。review_status 仅 proposed/source_limited/source_reviewed_not_effectiveness_verified。真实资料之外的教法保持 proposed/unverified。

原输入没有分类到的新观察点可以接纳，但其旧 hash 必须同时匹配当前原生对象与 source_refs 引用的固定发布 JSON 中精确 identity 的 `linear_teaching_route.native_route_version`；无证明不猜。禁止全库重新 install，也不改非本批路线。

## 3. 可重开来源与禁止本机路径

纯旧教案或原生版本证明不能冒标为考纲、真题或教材。可另附 `native_teaching_version_proofs.json`（manifest.files 的 supplemental），对象仅含 `schema="cs408-native-teaching-version-proofs-v1"`、同版 `source_commit` 与 `refs`；每条ref仅含id、uri、sha256、locator。只接受同版已发布JSON原件的study-library URI及完整字节SHA，最多512条。接收器独立核身份与原生版本；这些证明不参与下述四类教学来源的齐备性，也不能替代本地Capture题源绑定。

source_refs 每项只有 `id`、`uri`、`sha256`、`role`、`locator`。id 在本项内唯一。每个审查项至少包含 capture、syllabus、current_question 和 exam/textbook 之一四类来源。role 是待审核用途说明，不是自动证明资料内容正确。允许 URI：

- `study-library://<同一source_commit>/<发布清单相对路径>`：必须匹配该固定版本清单 hash，复用 Published.materialize 验回字节。
- `input://<原输入相对路径>`：必须来自本机登记的原 ZIP 与 source_files，capture 角色必须位于本项 Capture 的 captures/<id>/ 下。
- `enhancement-package://<manifest.run_id>/<回包相对路径>`：必须匹配本包 manifest 哈希清单，可携带网页读取过的上传原件；不允许把新回包伪装成原始 capture 证据。

相对路径禁止绝对路径、反斜杠、空组件和 `..`。网页不得提交本机 path/pdf_path/published_path/ref/historical_plan_ref。模板、路线或 exam_visual_binding 内需要引用来源时，以 `{"source_id":"对应source_refs.id"}` 表示；资产还可加 role、question_identity、pdf_page/pdf_pages。本地解析后才绑定真实路径，固定源字节以内容 hash 保存到原教案热目录。

## 4. 真题图、source_visual 与提示保护

template.exam_visual_binding 复用 `exam_figure_binding_408.validate_binding`：保留本题有辨识意义的布局、对象、编号、字段、方向和初态，focus_by_step 绑定 Q1/Q2 等实际解题步骤。teacher_only=true、frontend_ready=false、effectiveness=unverified。无图则 basis=text_only_mechanism 并明确机制示意；原件未核则保留受限，不伪造图源。

同步 source_plan 使用 `cs408-source-visual-plan-v1`，identity 与 template 一致、exam_visual_binding 完全一致、teacher_only=true、frontend_ready=false；`source_visual_plan_review.decision="retire_old_design"`。不得携带旧 teaching_design/narrative_plan/overview/region_bindings/teacher_states；这些对象保存在历史 immutable store，本地 writer 自动加 historical_plan_ref，查询 projection 明确剔除。否则 materialize 会重新覆盖本轮教案。

-Pxx/teaching_part 必须 scope=current_exact_question_only，只用当前问和已核前置；不导入父题整图、兄弟问答案、parent_solution/sibling_answers。完整路线和模板是教师 direct/L5 材料，after_attempt 不代表向用户开放完整答案；提示轮必须另编当前获准的安全局部。protected 查询不返回模板或原生版本 hash。

## 5. 本地事务与完成条件

prepare 只检查本地登记、固定版本、来源字节、Capture/题/点范围、previous hash、模板和binding，并用原 question_visual.import_catalog(activate=false) 校验；不写正式库、不分配ID、不写个人摘要或FSRS。

apply 先完成原正式 item、profile、batch；再以独立 operation receipt 幂等更新 exact template、同步source_visual question_plan、bounded route 对象，最后才 postcommit refresh/publish。原模板与路线对象不删除。某层失败返回 QUESTION_TEACHING_PENDING，只恢复未完成层；已提交 item/profile/评分/FSRS 不重放。

发布后保留原个人摘要核验，同时用 current_question_id 读回 direct/after_attempt/protected，核对 exact identity、template/hash、exam binding、无historical_plan_ref泄漏；路线核对当前native hash和protected不披露。失败为 QUESTION_TEACHING_READBACK_PENDING，不能称闭环完成。source_limited 身份缺口独立报告，不能称该题已更新。

旧输入缺requirements、已prepared或COMMITTED旧包保持原合同，不新设正式写入或重放。结果明确 LEGACY_LOCAL_TEACHING_REVIEW_REQUIRED / automatic_exact_question_update=false，由既有Skill执行有界本地补齐审查；不得把旧包成功偷换成“自动更新了精确教案”。

## 6. 最小完整 revise 形状样例

下例只展示字段与引用形状，不是完成稿、真实真题事实或学习记录。EXAMPLE-NEW-QUESTION 是说明用 logical ID；真实回包必须替换为 enhancement 同 Capture 的实际 new logical ID、真实 Capture、来源 URI/字节 hash 与本题内容。示例不含任何真实题答案。source_refs 中 q 是本批原题文件；图片 role=question_figure。若真实原题没有图，改用 text_only_mechanism、source_assets=[] 并明确机制示意，不能伪装有原图。

```json
{
  "capture_ids": ["CAP-EXAMPLE"],
  "question_identity": null,
  "logical_question_id": "EXAMPLE-NEW-QUESTION",
  "knowledge_points": ["DS01-01"],
  "disposition": "revise",
  "reason": "示例：当前题需要先确认输入对象，再核对操作边界；依据必须换为真实本批证据。",
  "expected_previous_sha256": null,
  "expected_source_plan_sha256": null,
  "source_refs": [
    {"id":"c","uri":"input://captures/CAP-EXAMPLE/packages/PKG-EXAMPLE/conversation.json","sha256":"实际64位SHA256","role":"capture","locator":"真实首答和后续纠偏回合"},
    {"id":"s","uri":"study-library://固定40位快照/support/syllabus-2027/实际发布文件.json","sha256":"实际64位SHA256","role":"syllabus","locator":"当前规范点的2027范围与必要前置"},
    {"id":"q","uri":"input://captures/CAP-EXAMPLE/packages/PKG-EXAMPLE/attachments/question.png","sha256":"实际64位SHA256","role":"current_question","locator":"当前独立题面"},
    {"id":"e","uri":"study-library://固定40位快照/details/corpus/408统考真题/实际文件.json","sha256":"实际64位SHA256","role":"exam","locator":"核对同一条件或机制的真实题面位置"}
  ],
  "template": {
    "schema":"cs408-question-visual-template-v1",
    "question_identity":"EXAMPLE-NEW-QUESTION",
    "identity_kind":"formal_question",
    "parent_identity":null,
    "subject":"DS",
    "knowledge_points":["DS01-01"],
    "given_objects":["示例输入对象及题设标签"],
    "initial_state":"用真实题设说明对象初态，不填兄弟题结果。",
    "target":"只写当前问要求确定的关系。",
    "visual_kind":"state_steps",
    "scene":"保留当前原图布局，只在相关对象上显示局部操作。",
    "steps":["逐项确认当前输入对象与约束。","沿题设操作检查相关对象如何变化。"],
    "solution_anchor":"用当前所求和原条件核对操作结果；此处须写经原题核验的解法依据。",
    "checks":["核对对象身份、边界及方向。"],
    "adaptation_rules":["依据当前真实首答决定是否展开第一步，不将阅读或点击当作掌握。"],
    "source_review_status":"reviewed",
    "effectiveness":"unverified",
    "exam_visual_binding": {
      "schema":"cs408-exam-visual-binding-v1",
      "basis":"current_question_figure",
      "source_assets":[{"source_id":"q","role":"question_figure","question_identity":"EXAMPLE-NEW-QUESTION"}],
      "preserve":["原题对象编号","原图连接方向和初态"],
      "focus_by_step":[{"step_id":"Q1","source_step_ref":"steps.0","what_to_show":"对应题设对象及其字段","state_change":"仅高亮本步正在核对的对象","return_to_reasoning":"回到文字说明为什么该对象是本步输入"}],
      "teacher_only":true,
      "frontend_ready":false,
      "effectiveness":"unverified"
    }
  },
  "source_plan": {
    "schema":"cs408-source-visual-plan-v1",
    "identity":"EXAMPLE-NEW-QUESTION",
    "scope":"current_exact_question_only",
    "teacher_only":true,
    "frontend_ready":false,
    "source_visual_plan_review":{"decision":"retire_old_design"},
    "exam_visual_binding": {
      "schema":"cs408-exam-visual-binding-v1",
      "basis":"current_question_figure",
      "source_assets":[{"source_id":"q","role":"question_figure","question_identity":"EXAMPLE-NEW-QUESTION"}],
      "preserve":["原题对象编号","原图连接方向和初态"],
      "focus_by_step":[{"step_id":"Q1","source_step_ref":"steps.0","what_to_show":"对应题设对象及其字段","state_change":"仅高亮本步正在核对的对象","return_to_reasoning":"回到文字说明为什么该对象是本步输入"}],
      "teacher_only":true,
      "frontend_ready":false,
      "effectiveness":"unverified"
    }
  }
}
```

本轮 source_plan 更新以 exam_visual_binding 为主。即使旧 source-visual 文档前半还保留历史热点 renderer 的介绍，本回流也不得生成 region_bindings/overview/teacher_states 或重新启用 teaching_design/narrative_plan。旧对象由本地保全，网页无须复制旧私有 ref。

完整 route 值样例（填入 routes 项的 route 字段；该项还须提供前文列出的 identity/parent/Capture/disposition/expected hash/source_refs）：

```json
{
  "schema":"cs408-derived-teaching-route-v1",
  "identity":"DS01-01",
  "parent":null,
  "title":"示例：先核对题设对象，再判断操作关系",
  "policy_version":"linear-text-primary-408-v1",
  "teacher_release_level":5,
  "frontend_ready":false,
  "effectiveness":"unverified",
  "learning_write_count":0,
  "review_status":"proposed",
  "ordered_blocks":[
    {"step_id":"S1","teacher_text":"依据当前题逐一确认输入对象与相关约束。"},
    {"step_id":"S2","teacher_text":"只追踪本步发生变化的关系，并解释变化所需的条件。"}
  ],
  "visual_insertions":[
    {"step_id":"S2","recipe":"保留本题编号和连接方向，点击本步涉及的连接时只高亮该关系变化。","return_to_prose":"回到正文核对这一步的输入、条件和后果。"}
  ],
  "limitations":["这是字段形状示例；必须改成当前精确题和真实依据，不代表用户已学会或图解已有效。"],
  "personalization_binding":"current_query_profile_and_current_answer_only"
}
```

规范点的 portable parent 为 null；历史原生 route 的 parent 若等于它自身，retain 按相同规范点角色保留原值。KSP 的 parent 必须精确匹配父规范点，不能借回流重新归属。

## 7. 发布给 MCP 的来源闭合

本地适配后的每个固定源引用都带 `published_path=teaching-sources/408/<sha>.<ext>`，`verified_origin` 保留原 study-library/input/enhancement URI 与 hash。发布者调用 `publication_sources(repo)`，只从当前激活的精确题、source_visual question_plan、route 对象收集实际引用；验证来源位于 `.private/question-visual-408/sources/`、没有符号链接或路径逃逸、文件名 stem/声明 hash/实际字节 SHA 一致。未激活候选和历史对象不扩大发布范围。发布清单缺这些引用源时不能声称网页 MCP 可重开；原生对象图发布和来源文件发布均通过后才完成可读链。

## 8. 身份与恢复硬校验

既有模板的 identity_kind/parent_identity 由 expected hash 指向的原生对象锁定，不接受网页改写类型。规范 `EXAM408-YYYY-QNN-Pxx` 强制 subquestion 并绑定其父题；`#Pxx` 强制 teaching_part；不得把子问自报为 parent_exam 或 formal_question 绕过范围限制。

所有 retain 均从 expected hash 指向的不可变旧对象读取 desired。恢复时不得把当前对象反算为 desired；当前状态只能是原 expected 或本 operation 已提交的实际 desired。template/source_plan/route 任一并发替换都必须停在该教学层，不重放学习。

路线只接纳本批 affected 规范点及其已核 KSP。所有 KSP（含已登记旧点）必须出现在同批 knowledge_updates 对应父点的 subpoints 中，且 ID 与原 profiles.subpoint_key(parent,title) 一致；本批已通过 removed_subpoint_ids 退休的小点不得再进入教学路线更新与读回清单。本地仍使用原 profile writer 审核与写入。不能通过 parent=本批点更新另一规范点或 S27 条目。

读回同时检查当前 source_visual plan hash、返回的 source_visuals.exam_visual_binding 与精确模板绑定一致；路线 direct/after_attempt/protected 均读回，after_attempt 不返回完整 ordered_blocks/visual_insertions。身份、哈希或披露边界失败都保持 pending。
