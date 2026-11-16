---
name: kaoyan-408-daily-intake-curation
description: "408 正式日终入口。‘将今天快速入库的题全部正式入库’一次完成当日快存入库、错题讲解摘要与图解验回、错点真题预处理和全天 HTML 复盘；也负责累计截止日、精确集合及原批恢复。"
---

# Kaoyan 408 Daily Intake Curation

本机路径和解释器先按[共享路径约定](../_shared/kaoyan-408/local-paths.md)定位；下文仓库命令在已定位的项目目录执行，历史回执保留原字节。

所有用户可见的进度说明、判断依据摘要、错误说明和正文使用中文；代码、命令、标识符及必要原文保留原样。过程说明简短，不以英文进度或长篇内部思考代替教学。

## 当日快存全部正式入库

用户说「将今天快速入库的题全部正式入库」或同义整日指令时，先读[当日正式收尾](references/daily-closeout.md)。本轮授权覆盖上海原学习日的全部快存、错题讲解所用的个性化摘要与图解验回、错点专项预处理和当天 HTML 复盘。发来的学习会话用于核源和确认年度范围；当天账本决定全量集合。正式事务完成后同轮交两份可用成果。只有用户明确限定为单项、单题、精确集合或只读时走相应窄入口。讨论或修改 Skill 只改流程。

2026-10-28：本地明确授权的摘要质量修订保留真实历史，按具体机制维护 `teaching_focus` 的独立表现、明确缺口、提示后修复、未验证范围；同步完整概况、短摘要、小点与讲题投影。新资料只提供客观机制/关系，不能制造个人不会或已会。详略由真实证据决定，频次只决定应留意的考试重点。

## 本机按原日期正式编纂

有实际内容的新正式批次统一走 enhancement-intake 的本地正式质量合同（`<repo>/schema/408-formal-quality-v1.md`）。每批用 OpenAI embeddings 在全库合规资料中找候选，用 Decisions 核具体机制、必要条件与终稿用途，再由原写者串行提交。提交前审核本批观察点与已登记的跨点依赖；提交后用本机原生入口回读整库父点、小点和向量路由。沿原 prepared 恢复 COMMITTED_QUALITY_PENDING。下列旧 start-batch、逐题 seal、外部建议和历史档案条目供查证与恢复已有事务。

授权正式入库后，当前会话直接核读本地原件并完成语义成稿。先用 intake_fact_capture_408.py backlog-plan 按原日期与用户范围读取两条本地写入路线，按原批身份核对已有 prepared/partial/COMMITTED。历史状态 awaiting_web_enhancement 交 kaoyan-408-enhancement-intake 的本地作者处理；已有回执按原身份恢复；下文 Gate and freeze 到 Direct decision 仅用于旧状态。backlog-plan 返回每个 processable item 的 intake_owner。local_enhancement_capture_ids 交增强作者；start_batch_capture_ids 只用于旧编纂项。同日两种状态共存时先恢复已有 active/partial 批，其他精确集合按原日期串行，不把两类放进一个写者。本机作者从原始对话和固定本地快照读取，编写逐题增强决定、父点/KSP、精确题教案与可视方案，再交既有唯一写者。真实 API 核证走本机已配置的增强入口；已有外部成稿只作兼容输入。

2026-10-07高频优先：正式个人摘要更新先按同版personal_context.syllabus_context.exam_frequency定位高频规范点，优先核对真实首答、机制断点、提示依赖、修复条件和next_check；频次是客观考试统计，不是个人不会或掌握的证据。保持全部受影响点及原日期的完整overview、short_summary、timeline和小点证据，低频记录不删、不漏、不改写为已会；不能仅凭频次自动修改现有掌握度。 同一原学习日期内优先审阅高频点，跨日期仍按原日期串行；完整集合和正式事务门禁不变。

原生 backlog-plan 的 final_gate.pending_enhancement_operations 给出未收尾的 prepared_receipt 和原批范围。题级已消费时用这个身份恢复概况、归档、预测或本机读回；已有完成继任的旧失败不重开。若精确请求仅覆盖原批的一部分，inspect_original_scope_only 只读核对，不能扩到其他Capture提交。

## Gate and freeze

Read `references/runtime-contract.md`, `references/fact-capture-schema.md`, and
`references/backlog-cutoff-plan.md`, plus `references/display-asset-contract.md` for
canonical conversation packages. When a successful historical Capture has no
canonical conversation-package archive proof, also read
`references/legacy-evidence-archive.md`. For cumulative requests the default cutoff is
the current `Asia/Shanghai` day. A named cutoff includes older dates. Requests for all
quick captures from one named day use `--only-today`, including the same-day closeout
above. In cumulative mode `backlog-plan` selects every canonical processable capture whose
original `study_date <= cutoff_date`, ordered by original date then capture ID.
For the legacy direct-curation lane below, processable means
`formalization_authorized=true` and `quality_status=awaiting_sol_formalization` (or a
retryable `curation_failed` successor). The explicit formal request and single-date
`start-batch` are the authorization boundary; do not require a second
`capture_authorized` event. Legacy `captured_unconfirmed` authorization is a separate
historical recovery path only.
Only `--only-today` or an exact `--capture-id`/`--package-locator` subset narrows it.

Replay before every date. Resume the exact active/partial batch or call the existing
`start_batch` once for that date, optionally with planner-returned exact capture IDs.
Never create a cross-date batch. Freeze ordered capture IDs plus capture-set SHA;
same-date additions wait for a later replay.

Reopen each `cs408-conversation-package://PACKAGE_ID` directly from
`<package-root>`; verify the complete
conversation, source, attachments, manifest, current canonical SHA, date, and capture
binding. Legacy `current-question-evidence://sha256/...` remains read-only
compatible and never gains invented missing turns.
It may be reopened only for an explicitly named historical recovery/archive. It never
starts a background consumer/Luna route and never substitutes for the sealed
`cs408-conversation-package-v1` required by a current Capture.

## Bounded read-only assistance

The current root agent owns direct judgment and all writes. Ultra uses the client's
native proactive orchestration without fixing a child model, count, reasoning level,
fork mode or service tier. Non-Ultra uses one agent unless the user explicitly authorizes
delegation for the current task. A delegated read receives
one frozen package SHA, allowed roots and a concrete query; it writes nothing, allocates
no ID and starts no writer. The root checks evidence and integrates once; a failed read
does not revive the retired background system.

## Direct decision and serial writer

Use `scripts/sol_curation_decision_408.py seal` to validate frozen order, reopen
direct evidence, and seal `new|redo|already_current|needs_user|skip`. There is no
retired background/model curation preprocessor, Dispatcher, consumer, model package,
or adoption token. Bounded material extraction/uncertain-page review and the existing
web-return evidence_prescreen are advisory source checks only (see below), never a
curation decision or an additional writer. Keep the deterministic, zero-model nightly
exam-tutoring refresh required below after formal terminals.
Immutable user/source/date facts cannot be overwritten. New keeps
`formal_id=null` until RepoLock; redo binds one verified existing ID.

For each item: seal decision; run exact identity/preflight; call `intake_apply_408.py` exactly once at batch-size-1; reopen RepoLock/WAL/COMMITTED
receipt or already-current verification and the returned display-asset closure; then run existing postcommit, review backflow, audit and `mark-result`.
Reopen the exact result/review binding; call `campaign_admission_backflow_408.py` `ensure`, then `bind`, then `refresh`, and bind the exact scheduler
`campaign_forecast_handoff_receipt_v1`. New emits `campaign_node_admitted`;
redo and `already_current` emit `campaign_node_refreshed` for the existing node.
Complete requires all four layers: formal result, verified review backflow, exact
campaign admission/refresh, and bound forecast handoff. Preserve exact pending states
`FORMAL_COMMITTED_REVIEW_BACKFLOW_PENDING`, `FORMAL_BOUND_CAMPAIGN_ADMISSION_PENDING`,
and `CAMPAIGN_ADMITTED_FORECAST_PENDING`; recovery fills only the first missing layer.
Never rerun apply, postcommit, review backflow, `mark-result`, or an existing admission.
List pre-contract `historical_residual` separately; never count it as current completion.
Then run `sol_curation_decision_408.py terminal-binding` against the exact result line.
Do not start the next writer until terminal or receipt-bound recoverable. Close the
batch only after all items are recorded, admitted/refreshed, handed off and audited.

## Current tutoring evidence and publication

408 日常讲题用 `exam_tutoring_context_408.py resolve --personal-scope teaching --materials core`。正式编纂用同入口 `--personal-scope full` 和 `memory` 只读命令精查冻结原件及更正链。用户更正沿原更正 owner 追加。当前、历史、偏好、来源及关系以 `schema/memory-access-408-v1.md` 的版本和范围为准。

机制发现与经历发现分开；补找旧经历按experience、实际actor和真实时间范围定位后trace核源，不能将助手讲解或编辑摘要转成用户独立表现。最近候选不保证全历史最后一次，查询和备课不写学习事件；本批判断仍依据完整原始对话。

新修订判断依`schema/408-knowledge-profile-v1.md`维护逐项claims与原始回合来源，分别记录依据、证据状态和有效状态。旧未结构化概况保留旧格式标签，不自动抽取成确定事实。正式收尾检查personal_read_receipt的完整范围、更正引用与相关视图失效；本机查询同版验回通过即完成质量回执，失败沿原回执恢复。

正式个人摘要更新同时审阅本批真实文字讲解、辅助图解、用户反馈与已有教法版本。按2026-10-17明确纠正，同一实质讲解以线性文字讲清步骤和因果，同时用可点击图解辅助抽象概念与状态；依据该点真实断点调整文字起点、解释桥梁与图解重点，不把主线藏进点击或只复制通用偏好句。以完整对话和实际成品为依据，分别记录明确偏好、观察到的倾向、解释后真实变化和未验证范围；本轮偏好不倒写成旧日学习效果，助手自评、点击或看懂不等于独立掌握。保留有效版本，依据新证据小步调整，不启动递归后台评测。

短摘要保留最小形式偏好、适用范围和下次检查；详细依据写入既有 `teaching_plan.visual_feedback` 与概况/原日期时间线。旧父点及小点方案须按 `teaching_plan_update` 明确保留、修订或退役，不因完整替换无声丢失。网页输入/回包使用便携视觉方案合同（`<repo>/schema/408-portable-visual-teaching-v1.md`），本地直接维护仍由原生profile写者核验原件；不另建偏好库、不修改客观模板为已验证有效。

按概况合同的衔接约束（`<repo>/schema/408-knowledge-profile-v1.md`）核对overview、short_summary与各级next_check/plan.check一致；图阶段不代替本题推理步骤。适度拓展可沿同点区别、考纲上下级与经审核关联，目标须同时纲内且有真实题目考查依据，优先高频目标，当前断点未解决不展开，未实际讲过不写学习事实。`lesson_bridge`由冷构建派生，不另存为个人掌握记录。

完成时验证相关父/KSP的本机 after_attempt/direct确实返回方案与反馈、protected隐藏，旧版本缓存失效。原始新学习尚未正式提交时仅是待审教法线索；已COMMITTED后只补摘要或本机读回缺层，不重放题级事件。

New `start-batch` events carry `knowledge_observation_contract=formal-point-observations-408-v1`.
Before `seal`, the formal owner must include `knowledge_observations` with
`schema=formal-point-observations-408-v1`, `scope_confirmed=true`, and one `points[]`
entry per actual knowledge point opened in the complete source. Every canonical
main/sub/hit point needs a disposition too: `observed` with concrete records or
`no_observation` with a source-grounded reason and `records=[]`. A question's tags
never imply an observed learner error. Read `schema/formal-point-observations-408-v1.md`
for the exact typed record and original-turn hash format. Inspect the entire frozen
conversation and available attachments before confirming scope; retain every actual
error, correction/restatement and hint, including points absent from the original tags.

`terminal-binding` automatically reopens the exact result/decision/package and seals
these records through `seal_knowledge_observations_408.py` using the existing
`knowledge_history_semantic_annotations_v1` envelope. Outcome and repair remain
separate; assistant hints/explanations cannot become learner success. Original dates,
full package SHA and exact original turn hashes are bound. A semantic failure after
the formal terminal is `FORMAL_COMMITTED_KNOWLEDGE_OBSERVATIONS_PENDING`: rerun only
this same `terminal-binding` to complete/reuse its semantic closure, never apply.
Identical source+SHA observations are idempotent and conflicting rewrites fail closed.
The ordinary point query and morning priority read only verified formal observations;
they never add question attempts or create B concept-review sessions. New point
observations remain required even for an already-current formal card. Historical
batches without this additive contract remain readable without invented observations.

Verified formal and review commits invalidate the old teaching version immediately.
The public commit owner refreshes the existing `exam_tutoring_context_408.py` projection
after its locks are released. Resume a missing local projection step under the original event.

The same cold refresh publishes every affected knowledge-point learning path and the
protected/after_attempt/direct prepared teaching summaries. New formal evidence must
change the resolve version so a previous known-version cannot reuse stale text.
Re-read the refreshed summary/version before treating the postcommit as complete;
missing pages/summaries remain pending and recovery never replays the formal write.

For historical sources without the new formal point contract, the native
formal owner verifies the source and splits all touched points into safe initial reasoning,
correction/restatement and hint-dependent records. Assistant explanations remain distinct. Seal once with
`python3 scripts/runtime_paths_408.py --run seal_knowledge_observations_408.py --repo . --input-json <path>`
using `knowledge_history_semantic_annotations_v1`: exact package locator as `source_id`,
verified `canonical_sha` as `source_sha256`, leaf codes, evidence roles and source fragments.
The sealer binds cold sources and writes only immutable private annotations; conflicting
reviewed records are rejected, identical records are idempotent. For COMMITTED items, retry
only missing annotation/refresh, never formal apply. Score-only review events already
update the projection and do not require a fabricated annotation. Ordinary teaching
never scans full history or performs this cold sealing step.

After all planned terminals, review backflow and `mark-result` complete, run the existing
`exam_tutoring_context_408.py refresh-current` for the current Shanghai date, then cold
`audit` the returned receipt once. This reuses an already current version or refreshes
from actual formal cards, mapping and review ledger. Original study dates stay unchanged.
A failed refresh/audit is `FORMAL_COMMITTED_TUTORING_CONTEXT_PENDING`: recover that
projection only and keep stale history disabled. It is not a successful personalized
closeout. 本地正式收尾在原 prepared 回执上用原生 `resolve` 验回父点与 KSP 摘要、下一检查、同一来源版本和答案视图。`protected` 隐藏个人内容，许可的两个视图匹配已接受概况。验过生成 `VERIFIED_CURRENT` 质量回执，再继续本地备题和复盘。只有明确要求远程访问或外发时才发布副本，按同一业务事件显式调用 `study_commit_followup_408.py --event-id ... --publish` 或相应外发入口。已存在的发布回执按原身份保留。

For canonical conversation packages, seal one normalized
`cs408-display-asset-plan-v1` inside the canonical formal package. It must classify
every attachment from the verified manifest. Question/source images may be
`practice_safe`; solution, explanation and user-work images are `protected`;
`other_attachment` is always `archive_only`. Sol selects disposition only and never
supplies a target path. The writer derives
`kaoyan-408-details/assets/<FORMAL_ID>/...`, handles both new and redo, and updates
only a package-scoped managed embed block in the details card. Temporary package,
inbox and T9 absolute paths are forbidden in all formal surfaces.
Start from `python3 scripts/runtime_paths_408.py --run display_assets_408.py build-plan --package-locator <locator> --package-sha256 <sha> --mode new|redo [--formal-id <ID>]`; demote only explicit paths with `--archive-only-path`.

If apply returns `COMMITTED_DISPLAY_ASSETS_PENDING`, the formal WAL is already
committed. Resume only the display closure and postcommit layers; never replay apply.

Continue to the next planned date after recording exact residuals. `needs_user`,
damaged canonical evidence, and `FORMAL_COMMITTED_ARCHIVE_PENDING` remain dated
residuals rather than stopping unrelated later dates. A COMMITTED apply with missing
backflow/result/closeout resumes only those layers. After the final date, rerun the
same canonical planner; accept only when processable pending through the cutoff is
zero and the mandatory nightly teaching-projection audit passes, otherwise list every
remaining date, capture ID, state, reason, and action plus any teaching-context debt.

## Explicit needs_user retirement

Only an explicit user request to delete/remove named or currently listed
`needs_user` residuals authorizes administrative retirement. Replay the canonical
planner first and resolve the exact capture IDs. For each original date, call
`intake_fact_capture_408.py retire-needs-user` with the complete non-success set of
the capture-bound frozen set's latest PARTIAL attempt, repeated `--capture-id`, fixed
`--reason user_removed`, `--user-confirmed`, and a unique idempotency key.

The command appends one `needs_user_retired` event and sets replay status to
`user_removed`. It never edits or deletes earlier ledger events, results, closeouts,
identity hints, formal nodes or packages; it performs no apply/archive and is not a
formal-success terminal. Partial batch subsets, active batches, non-needs_user items,
and implicit "all" selection fail closed. Group different dates/batches into separate
calls. A later unrelated same-date batch does not replace the capture-bound frozen
set. A fresh planner reports retired IDs only under
`administrative_terminal_excluded`, with zero processable/residual contribution.

## Required archive after formal terminal

Run:

```text
python3 scripts/runtime_paths_408.py --run archive_408_conversation_package.py archive-after-formal-commit --repo . --package-locator <locator> --package-sha256 <sha> --formal-ids-json '<JSON list>' --formal-terminal-locator <locator> --formal-terminal-sha256 <sha> --decision-root <root>
```

It only accepts `/Volumes/T9-Data`; verifies sentinel SHA
`0000000000000000000000000000000000000000000000000000000000000000`,
volume `T9-Data`, UUID `00000000-0000-0000-0000-000000000000`, and rejects
`/Volumes/T9-Data 1`. It stage-copies into
`01_408/资料库/原始会话资料`, verifies tree and manifest, atomically publishes,
writes and rereads `wiki/sources/raw_archives/PACKAGE_ID.md` with the exact locator
frontmatter, writes a receipt, then clears local heavy bytes and leaves an
archive-pointer. It never edits an Obsidian `.base` file.

Cleanup is permitted only after the archiver revalidates the display closure and a
zero-finding formal-reference scan. Intent, archive receipt and local pointer bind
the closure SHA, scan SHA, stable-asset count, no-display proof and
`pending_component`. A display failure is
`FORMAL_COMMITTED_ARCHIVE_PENDING/pending_component=display_assets`; keep the full
local package and resume only display/archive. Locator notes include exact
`retrieval_keys` for package, formal ID, source/item identity and study date.

Any archive or locator failure returns
`FORMAL_COMMITTED_ARCHIVE_PENDING`. Resume archive only; never rerun apply.
Archive completed-analysis outcomes `curated|created|updated|already_current|skip|skipped|noop|duplicate`; formal IDs may be empty. Keep the complete local package for `needs_user|failed` and do not clean it.
The archive command derives outcome and actual formal ID from the verified terminal object. `formal-ids-json` is required even when empty and must exactly match that terminal proof.

For every successful terminal without a verified conversation-package archive, do
not treat the item as `not_locally_pending` or a trusted terminal. The planner must
report `legacy_archive_pending` with reason
`FORMAL_COMMITTED_ARCHIVE_PENDING` until the legacy archive receipt, locator note and
local pointer all verify. Preview with:

```text
python3 scripts/runtime_paths_408.py --run archive_408_conversation_package.py archive-legacy-evidence-after-formal-commit --repo . --capture-id <capture> --decision-root <root> --evidence-root <root>
```

The preview is zero-write. Apply only with the exact preview authorization by passing
`--apply --authorization <authorization>`. The command derives all evidence, terminal, outcome and formal-ID
facts; archives the existing object tree and canonical proof bytes with
`canonical_package=false`; records source-proven conversation completeness without
inventing turns; and cleans only proven-exclusive private bytes after verified
receipt, locator and pointer publication. It never calls formal apply, mark-result or
terminal-binding. Shared objects and external DOCX/corpus sources remain local.

When no `current-question-evidence://` locator exists, the same command uses
`ledger_only_reference_files`: it archives exact capture/result/closeout ledger bytes,
current canonical terminal facts, every safely resolvable repo-relative or allowlisted
details reference file, and an explicit list of unresolved refs. It always records
`conversation_complete=false`, `canonical_package=false`, and `cleanup_count=0`;
repo, session, shared and external source files are never deleted.

When current-question evidence exists but no historical Sol terminal object exists,
use `current_question_evidence_ledger_terminal`: preserve the full evidence tree,
leave terminal locator/SHA null, bind the exact ledger result event separately, and
derive outcome/formal IDs only from canonical capture/result/closeout bytes.

direct_408_curation_v2=parent:current-root;reads:bounded-optional-native;decision:direct-evidence;writer:serial-existing;archive:t9-sentinel-stage-verify-atomic-locator-receipt-cleanup;recovery:no-reapply

For an external A handoff only, read `<repo>/schema/study-bridge-408-v1.md`. Advice adjudication, local formal decision and verified publication are separate results.

## 按需资料提取与疑义复核（原入口）

本机普通核读先用清晰的原文、原图及离线提取。只有当前另有远程资料处理授权且原件辨识仍有疑义，才复用 `<bridge>/scripts/study_material_preprocess.py`：用 `<bridge>/.venv-study-mcp/bin/python` 执行，传 `--subject 408 --source 原件绝对路径 --pages 1,3 --purpose reference`，purpose按来源角色选reference/question/user_work/solution；公开网页可用 `--url`，不带浏览器凭证。先本地文字提取，扫描页/图像按需调用视觉 API；不为清楚的资料重复增加一层模型。输出原件副本、提取稿和缓存走本科已核验 T9 冷目录，来源/hash/页码保持可追溯，不修改 Capture 或正式库。

`--review auto` 是默认，仅对提取疑义页追加 Decisions；已有明确疑义可用 `--review-pages 1,3`，页码必须属于本次 `--pages`，`--review off` 只跳过此旁路。它比较原页图与提取稿，提示可能漏项、混区、角色或文字不一致；HTML/TXT 没有原页图时明确未筛查。无疑义不默认每页复核；空白/过少内容等可程序核查的情况先用代码。失败保留已提取结果与未筛查原因，不重跑成功正式写入。

默认 `protected`，`after_attempt` 也只返回元数据；只有当前任务已授权读取该内容及相应答案范围时，才用 `--view direct --allow-answer-text`。正式审核授权不扩展到未来题、兄弟小问或其他答案。机器提取、疑义复核和回包 `evidence_prescreen` 是不同的派生辅助：均不替代实际核读原图、完整真实对话与本科语义审核；未标记不是审核通过，提示后完成不是独立掌握，缓存不是新学习。只将原证据支持的结论交既有唯一正式 writer，保留原日期、身份、原评分/掌握度与幂等恢复规则。
