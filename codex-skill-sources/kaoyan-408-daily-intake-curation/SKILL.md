---
name: kaoyan-408-daily-intake-curation
description: "408 截止日 backlog 或精确集合的正式编纂、review backflow 后的 campaign admission/forecast handoff，以及 canonical needs_user residual 的显式退休；默认逐日串行，only-today 或精确 subset 才缩小。"
---

# Kaoyan 408 Daily Intake Curation

所有用户可见的进度说明、判断依据摘要、错误说明和正文使用中文；代码、命令、标识符及必要原文保留原样。过程说明简短，不以英文进度或长篇内部思考代替教学。

## 网页加强包优先

提供408正式加强包时，直接使用 `kaoyan-408-enhancement-intake` 的prepare/apply，按网页最终语义与精确Capture集合写入并同步个人概况/短摘要。新Capture的awaiting_web_enhancement状态不能被普通cutoff编纂提前消费；没有加强包时不在本地重做这批网页语义。下面的直接编纂合同只用于明确选择的原始/历史兼容维护，不替代新闭环。

## Gate and freeze

Read `references/runtime-contract.md`, `references/fact-capture-schema.md`, and
`references/backlog-cutoff-plan.md`, plus `references/display-asset-contract.md` for
canonical conversation packages. When a successful historical Capture has no
canonical conversation-package archive proof, also read
`references/legacy-evidence-archive.md`. Ordinary formal intake without a date uses the
current `Asia/Shanghai` day as `cutoff_date`; an explicit date is also a cutoff, not
a single-day set. `backlog-plan` selects every canonical processable capture whose
original `study_date <= cutoff_date`, ordered by original date then capture ID.
For the current managed-terminal and standalone quick-intake route, processable means
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
`/Users/your-user/.codex/kaoyan-408-conversation-packages`; verify the complete
conversation, source, attachments, manifest, current canonical SHA, date, and capture
binding. Legacy `current-question-evidence://sha256/...` remains read-only
compatible and never gains invented missing turns.
It may be reopened only for an explicitly named historical recovery/archive. It never
starts a background consumer/Luna route and never substitutes for the sealed
`cs408-conversation-package-v1` required by a current Capture.

## Bounded read-only assistance

The current root agent owns direct judgment and all writes. Ultra uses the client's
native proactive orchestration without fixing a child model, count, reasoning level,
fork mode or service tier. Non-Ultra keeps small work in one agent and uses the existing
`orchestrate` Skill only for substantial independent work. A delegated read receives
one frozen package SHA, allowed roots and a concrete query; it writes nothing, allocates
no ID and starts no writer. The root checks evidence and integrates once; a failed read
does not revive the retired background system.

## Direct decision and serial writer

Use `scripts/sol_curation_decision_408.py seal` to validate frozen order, reopen
direct evidence, and seal `new|redo|already_current|needs_user|skip`. There is no
background/model preprocessor, Dispatcher, report, consumer, model package, or
adoption token. The only permitted derived preprocessing is the deterministic,
zero-model nightly exam-tutoring refresh required below after formal terminals.
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
and requests the shared publisher after its locks are released. Cloud failure preserves
local learning and returns pending; it never authorizes another apply or review event.

The same cold refresh publishes every affected knowledge-point learning path and the
protected/after_attempt/direct prepared teaching summaries. New formal evidence must
change the resolve version so a previous known-version cannot reuse stale text.
Re-read the refreshed summary/version before treating the postcommit as complete;
missing pages/summaries remain pending and recovery never replays the formal write.

For historical sources without the new formal point contract, the native
formal owner verifies the source and splits all touched points into safe initial reasoning,
correction/restatement and hint-dependent records. Assistant explanations remain distinct. Seal once with
`python3 scripts/seal_knowledge_observations_408.py --repo <repo> --input-json <path>`
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
closeout. Formal intake includes updating the knowledge-point personal understanding projection
and its MCP local publication copy; the user need not remind the owner again. Semantic
annotations require actual opened source evidence and preserve role, original date and
hint dependence. Never manufacture observations just to satisfy publication coverage.
After this final refresh-current and audit (including any later semantic seal), run:

```text
python3 scripts/study_commit_followup_408.py --repo <repo> --event-id <original-verified-business-event-id>
```

Reuse the verified commit/terminal business event. This postcommit-only owner refreshes
idempotently and keys the publication request by business event plus personal version,
so an earlier PUBLISHED receipt cannot swallow the final semantic version. The native
descriptor exports only the verified exam-tutoring manifest and its closed immutable
references under `personalization/408/`, including point summaries, records and source
coverage; it never exports all state or private drafts. Confirm the published manifest
personalization version and a real knowledge-point query readback from the MCP local
copy before declaring personalization synchronized. Wait for the deterministic publisher
to finish, then run `/usr/local/bin/python3 /Users/your-user/Documents/Study-Pro-Bridge/study_publication.py verify-current --subject 408`.
Only `PUBLISHED_CURRENT` passes; `STARTED`, an old receipt or `SYNC_PENDING` is not
completion. A failed refresh, snapshot or readback
is pending: retry only this postcommit publication/readback, never apply, scoring or
semantic evidence fabrication. Confirm the shared publication receipt separately before
claiming the cloud is current or admitting dependent B/D work.

For canonical conversation packages, seal one normalized
`cs408-display-asset-plan-v1` inside the canonical formal package. It must classify
every attachment from the verified manifest. Question/source images may be
`practice_safe`; solution, explanation and user-work images are `protected`;
`other_attachment` is always `archive_only`. Sol selects disposition only and never
supplies a target path. The writer derives
`kaoyan-408-details/assets/<FORMAL_ID>/...`, handles both new and redo, and updates
only a package-scoped managed embed block in the details card. Temporary package,
inbox and T9 absolute paths are forbidden in all formal surfaces.
Start from `python3 scripts/display_assets_408.py build-plan --package-locator <locator> --package-sha256 <sha> --mode new|redo [--formal-id <ID>]`; demote only explicit paths with `--archive-only-path`.

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
python3 scripts/archive_408_conversation_package.py archive-after-formal-commit --repo <repo> --package-locator <locator> --package-sha256 <sha> --formal-ids-json '<JSON list>' --formal-terminal-locator <locator> --formal-terminal-sha256 <sha> --decision-root <root>
```

It only accepts `/Volumes/T9-Data`; verifies sentinel SHA
`f086b32b29b2b38f1a28dffb8fde4fa850078332d8a23ae269cc8e857a6bd2ca`,
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
python3 scripts/archive_408_conversation_package.py archive-legacy-evidence-after-formal-commit --repo <repo> --capture-id <capture> --decision-root <root> --evidence-root <root>
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

For an external A handoff only, read `/Users/your-user/Documents/kaoyan-408/schema/study-bridge-408-v1.md`. Advice adjudication, local formal decision and verified publication are separate results.
