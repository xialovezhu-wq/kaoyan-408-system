---
schema: cs408-formal-enhancement-contract-v1
status: active
timezone: Asia/Shanghai
---

# 408 formal enhancement v1

This is the typed handoff from the web-authored final review to the local 408
writers.  It is data only.  The receiver never executes package scripts, trusts
package-selected output paths or expands the Capture set. Verified completed web
semantics are reused. Under the user's authorized local closeout, the owning Agent
may complete failed/missing units from original evidence in a derived repair package;
the immutable web return and any prior formal receipts must remain intact.

Execution/progress preservation and true-call diagnosis use
[web-execution-report-408-v1.md](web-execution-report-408-v1.md). The optional
manifest field web_execution_report names that manifest-listed report. Successful
formal/profile/publication closeout automatically exports the next target's input
through [morning-input-408-v1.md](morning-input-408-v1.md); export failure never replays
formal writes. PARTIAL returns are staged for local completion, not discarded.

## Archive layout

The input is one ZIP or one directory with exactly these root files plus optional
evidence assets:

```text
manifest.json
enhancement.json
knowledge_updates.json
START_HERE.md
assets/...
```

`manifest.files` lists every member except `manifest.json`.  Each row is
`{path, role, sha256, size_bytes}`.  Paths are normalized relative POSIX paths;
absolute paths, `..`, links, devices, encrypted ZIP members, duplicate names and
unlisted bytes are rejected.  The fixed receiver limits are 256 files, 128 MiB per
file and 1 GiB total uncompressed bytes.

## manifest.json

```json
{
  "schema": "cs408-formal-enhancement-manifest-v1",
  "subject": "408",
  "run_id": "stable-web-run-id",
  "study_date": "YYYY-MM-DD",
  "timezone": "Asia/Shanghai",
  "source_commit": "40 lowercase hex",
  "snapshot_id": "same verified 40-hex Study Library snapshot identity",
  "local_version": "published 64-hex local content version",
  "publication_target": "local",
  "capture_set_sha256": "sha256(canonical sorted capture_ids)",
  "captures": [
    {
      "capture_id": "CAP-...",
      "capture_payload_sha256": "64 lowercase hex",
      "episode_id": "stable episode identity",
      "execution_kind": "original_review|concept_supplement|standalone_question",
      "evidence_locator": "cs408-conversation-package://...",
      "evidence_sha256": "64 lowercase hex"
    }
  ],
  "affected_points": ["OS03-17"],
  "files": [
    {
      "path": "enhancement.json",
      "role": "enhancement",
      "sha256": "64 lowercase hex",
      "size_bytes": 123
    }
  ]
}
```

`captures` is sorted by `capture_id`, contains each Capture exactly once, and must
equal the exact local Capture subset at prepare and apply.  The receiver reopens
each payload hash and evidence locator/hash.  All Captures must have the one stated
study date.  It starts or reuses only that exact frozen batch; a different active
batch is a hard conflict. `source_commit` must reopen through the verified local
Study-Pro-Bridge publication receipt. `snapshot_id` equals that 40-hex snapshot
identity; `local_version` is a separate 64-hex content version and must equal the
resolved publication manifest.

## enhancement.json

```json
{
  "schema": "cs408-formal-enhancement-v1",
  "run_id": "same run_id",
  "source_commit": "same source_commit",
  "items": [
    {
      "item_id": "stable item id",
      "capture_id": "one manifest Capture",
      "action": "new|redo|correct_review|concept_only|already_applied|needs_user",
      "logical_question_id": "required for new, otherwise null",
      "formal_id": "existing formal ID when applicable, otherwise null",
      "expected_formal_state": {
        "formal_row_sha256": "published question_refs.formal_state value",
        "safe_card_sha256": "published question_refs.formal_state value"
      },
      "decision": {
        "decision": "new|redo|correct_review|concept_only|already_current|needs_user",
        "formal_id": null,
        "canonical_package": null,
        "evidence_refs": [{"locator": "...", "sha256": "..."}],
        "basis": ["source-grounded final basis"],
        "counterevidence": [],
        "confidence": "high|medium|low",
        "unresolved_gaps": [],
        "knowledge_observations": {}
      },
      "concept_review": null,
      "needs_user_reason": null
    }
  ]
}
```

Items are sorted by `capture_id` and cover the manifest Capture set exactly once.
The action mapping is strict:

| web action | native decision and write |
|---|---|
| `new` | sealed `new`; one native auto-ID formal package under RepoLock/WAL |
| `redo` | sealed `redo`; one native existing-node formal package |
| `correct_review` | sealed `correct_review`; verified current node plus the delayed review schedule and point observations; no wrong-node recurrence |
| `already_applied` | sealed `already_current`; verified current state and idempotent delayed review/binding only |
| `concept_only` | sealed point decision plus one native `concept_review_408` session; no formal node, original-question date, error count or question FSRS write |
| `needs_user` | sealed unresolved decision and an answer-safe Capture terminal; zero formal/profile claim for unresolved points |

For `new` and `redo`, `decision.canonical_package` is the existing
`coordinator_approved_intake_v1` payload.  A new package keeps
`formal_id=null, auto_id=true`; the receiver binds `logical_question_id` to the ID
allocated inside RepoLock.  A redo package uses the existing ID.  The canonical
payload must bind the same batch/Capture/payload when it is executed.  The receiver
adds only the sealed decision transport locator/hash; all semantic fields come from
the web package.

The web page does not fill `batch_id`, `capture_id`, `capture_payload_sha256`,
`capture_set_sha256`, `curation_evidence_binding`, `capture_binding`,
`sol_decision_locator`, `sol_decision_sha256`, a local receipt path, or a final new
formal ID. The receiver derives those transport fields from the prepared receipt and
the active exact Capture batch. They are absent from the web-authored object. The web
page also does not run local preflight, read source code/help output, choose a local
destination, or claim that an ID has already been allocated.

### New canonical package

This is the complete current web-authored shape for `action=new`. Fields described as
exact labels come from the fixed Study Library taxonomy/contract and must not be
invented. `{{...}}` denotes content, not a literal placeholder accepted by apply.

```json
{
  "contract": "coordinator_approved_intake_v1",
  "schema_version": "coordinator_approved_intake_v1",
  "decision": "pass",
  "mode": "new",
  "formal_id": null,
  "auto_id": true,
  "job_id": "JOB-{{stable item or logical-question id}}",
  "subject": "DS|CO|OS|CN",
  "year": "YYYY|UNK",
  "source_id": "{{verified source identity or VISUAL_PENDING}}",
  "module": "{{one exact module code and label}}",
  "main_knowledge": "{{one exact leaf code and label}}",
  "sub_knowledge": ["{{exact leaf code and label}}"],
  "hit_knowledge": ["{{exact leaf code and label; include main point}}"],
  "question_type": "{{answer-safe type}}",
  "core_point": "{{answer-safe core mechanism, at least 8 characters}}",
  "safe_summary": "{{answer-safe question skeleton, at least 10 characters}}",
  "key_parameters": "{{only parameters needed to redo, at least 8 characters}}",
  "ask_type": "{{answer-safe requested quantity/type, at least 4 characters}}",
  "user_error_entry": "{{verified learner error entry, at least 10 characters}}",
  "redo_first_action": "{{first protected redo action, at least 10 characters}}",
  "fuzzy_concepts": "{{verified fuzzy concept, at least 5 characters}}",
  "error_tags": ["{{one or more exact E01-E10 labels, or 暂无明确错因 alone}}"],
  "detail_entry": "{{durable detail description; receiver appends the generated Obsidian card URI}}",
  "attachment_registry": {
    "题图": "{{provided or not provided}}",
    "解析图或解析正文": "{{provided or not provided}}",
    "作答痕迹": "{{provided or not provided}}",
    "可视化详情入口": "{{durable status, never a temporary path}}",
    "当前附件来源": "{{exact package/source description}}"
  },
  "first_done_date": "{{date reopened from the source package}}",
  "latest_review_date": "{{manifest.study_date}}",
  "latest_error_record": "{{answer-safe verified current error, at least 10 characters}}",
  "date_source": "{{source-bound date basis}}",
  "redo_action": "{{optional answer-safe action label}}",
  "relation_candidates": [],
  "relation_review": {
    "status": "reviewed_no_reliable_edge",
    "basis": "{{source-specific review basis, at least 10 characters}}",
    "reviewed_by": "{{same value as reviewed_by below}}"
  },
  "topic_chain_candidates": [],
  "review_unit": {
    "内容名称": "{{optional answer-safe mechanism title}}",
    "难度等级": "A|B|C"
  },
  "detail_card_source": {
    "package_path": "assets/{{final detail Markdown file}}",
    "sha256": "{{manifest-bound file SHA-256}}"
  },
  "evidence": [
    {
      "package_path": "assets/{{evidence file}}",
      "sha256": "{{manifest-bound file SHA-256}}"
    }
  ],
  "display_asset_plan": {
    "schema_version": "cs408-display-asset-plan-v1",
    "subject": "cs408",
    "package_id": "{{reopened conversation package id}}",
    "package_locator": "{{manifest capture evidence_locator}}",
    "package_sha256": "{{manifest capture evidence_sha256}}",
    "mode": "new",
    "formal_id": null,
    "stable_path_policy": "kaoyan-408-details-formal-id-content-hash-v1",
    "items": [],
    "image_attachment_count": 0,
    "no_display": true
  },
  "review_backflow_assessment": {
    "schema": "formal_intake_direct_schedule_assessment_v1",
    "source": "daily_practice|evening_d0|morning_review",
    "source_id": "{{verified learning source id}}",
    "source_identity": "{{verified scheduling identity}}",
    "review_unit_id": null,
    "formal_node_id": null,
    "first_result": "wrong|partial|uncertain|blank",
    "confidence": "low|medium|high",
    "prompt_level": "none|minimal|progressive|full",
    "reasoning_break": true,
    "first_break": "{{answer-safe verified first break; use a real nonempty value}}",
    "first_break_provenance": "user_report|visible_evidence|derived_classification",
    "event_time": "{{exact learner observed_at, or null for local runtime fallback}}"
  },
  "reviewed_at": "{{ISO-8601 timestamp with timezone}}",
  "reviewed_by": "web-gpt-6-pro",
  "coordinator_checks": {
    "evidence_hashes_verified": true,
    "answer_leak_check": "pass",
    "knowledge_tags_verified": true,
    "error_tags_verified": true,
    "duplicate_check": "pass",
    "historical_mapping_check": "pass_or_not_applicable"
  }
}
```

`job_id` is needed only when a relation uses `from_job` or `to_job`. `review_unit`
is optional; when omitted the native writer derives its existing defaults.
`redo_action`, `date_source`, `evidence`, `relation_candidates`, and the shown empty
`topic_chain_candidates` are optional. The other shown fields are required by the
direct writer or its native new preflight. If there is no reliable
relation, keep `relation_candidates=[]` and the shown
`reviewed_no_reliable_edge`. A relation to an existing node uses the current R01-R09
fields `from, to, type, strength, reason, priority`. Because the new formal ID does
not exist yet, the new endpoint is written as `from_job` or `to_job` equal to this
package's `job_id`; the other endpoint is an existing formal ID. Then
`relation_review.status` is `connected`. Do not emit topic-chain candidates: the
current WAL deliberately rejects them instead of applying an untransactional edit.

`display_asset_plan.items` covers every attachment in the reopened conversation
package. Each row is exactly
`{package_relative_path, attachment_role, mime_type, byte_count, source_sha256,
visibility, disposition}`. Values are copied from that package. The complete role
policy is:

| attachment role | visibility | allowed disposition |
|---|---|---|
| `question_image` | `practice_safe` | `promoted` or `archive_only` |
| `source_article_image` | `practice_safe` | `promoted` or `archive_only` |
| `solution_image` | `protected` | `promoted` or `archive_only` |
| `explanation_image` | `protected` | `promoted` or `archive_only` |
| `user_work_image` | `protected` | `promoted` or `archive_only` |
| `other_attachment` | `protected` | `archive_only` only |

Every image attachment must be classified. `promoted` copies that image into the
stable details vault and includes it in the managed card embed block while preserving
the role-derived visibility. `archive_only` keeps it only in the sealed evidence
package and creates no stable path or embed. `other_attachment` can never be promoted.
`image_attachment_count` counts the five image roles and `no_display` is true exactly
when no row is promoted. The receiver derives stable output paths; the web page never
supplies them.

Do not synthesize this plan by guessing attachment roles or paths. Reopen the exact
conversation package and use the native deterministic
`display_assets_408.build_default_plan(...)` entrypoint. Its default promotes every
image role, assigns the visibility above, and makes every `other_attachment`
`archive_only`; `archive_only_paths` may demote named images when the evidence should
remain package-only. This entrypoint supports both `new` and `redo`; the latter uses
the verified existing formal ID.

### Study Library read examples

Reuse the input package's pinned 40-hex `source_commit`. Only if the input has no
pinned version, read `Study_Library.library(subject="408")` once and then keep that
version unchanged. To reopen the published binding and both required formal-state
hashes for an existing node, query the dedicated manifest-backed domain:

```text
Study_Library.query(
  subject="408",
  domain="question_refs",
  source_commit="<pinned 40-hex commit>",
  ids=["CO_2099_902"],
  offset=0,
  limit=1,
  max_bytes=24000
)
```

The record's `formal_state.formal_row_sha256` hashes the canonical formal-node row;
`formal_state.safe_card_sha256` hashes the published safe-card file. The enclosing
record `source_sha256` hashes the verified pinned `PUBLISHED_LIBRARY.json`. Keep the
same commit and advance `next_offset` until `eof=true` when paging more than one
binding. This domain is served from the root manifest and does not scan all published
files.

For personal context, use `protected`, `after_attempt`, or `direct`; `full` is not a
valid view. The 408 default byte budget is 8192 and an explicit budget must be from
1024 through 65536 bytes. Use `after_attempt` only after the current attempt has been
supplied, and `direct` only when answers are explicitly authorized:

```text
Study_Library.query(
  subject="408",
  domain="personal_context",
  source_commit="<same pinned 40-hex commit>",
  ids=["CO04-17"],
  contains="{\"view\":\"after_attempt\"}",
  max_bytes=65536
)
```

If `personal_context.next_cursor` is nonempty, put that cursor in the next call's
`contains` JSON and keep the commit, knowledge-point IDs, view and byte budget fixed.

Never guess an asset path from year or question number. Query `question_refs` by the
verified formal ID, select an exact returned `question_images` or `solution_images`
path according to the current answer-visibility rule, then call
`Study_Library.view_image` with that returned path and the same `source_commit`. A
`PATH_NOT_IN_PUBLISHED_LIBRARY` response means the guessed or stale path was not
recovered; it is not a network error and must not be reported as successful.

### Redo canonical package

This is the complete current web-authored shape for `action=redo`. Redo is a real
repeat error on an existing original question. A correct review uses
`action=correct_review` and has no canonical package.

```json
{
  "contract": "coordinator_approved_intake_v1",
  "schema_version": "coordinator_approved_intake_v1",
  "decision": "pass",
  "mode": "redo",
  "formal_id": "{{existing formal ID}}",
  "auto_id": false,
  "latest_review_date": "{{manifest.study_date}}",
  "latest_error_record": "{{answer-safe verified current error, at least 10 characters}}",
  "error_tags": ["{{zero or more exact additional error-tag labels}}"],
  "redo_action": "{{optional answer-safe action label}}",
  "relation_candidates": [],
  "relation_review": {
    "status": "reviewed_no_reliable_edge",
    "basis": "{{source-specific review basis, at least 10 characters}}",
    "reviewed_by": "{{same value as reviewed_by below}}"
  },
  "topic_chain_candidates": [],
  "evidence": [
    {
      "package_path": "assets/{{evidence file}}",
      "sha256": "{{manifest-bound file SHA-256}}"
    }
  ],
  "display_asset_plan": {
    "schema_version": "cs408-display-asset-plan-v1",
    "subject": "cs408",
    "package_id": "{{reopened conversation package id}}",
    "package_locator": "{{manifest capture evidence_locator}}",
    "package_sha256": "{{manifest capture evidence_sha256}}",
    "mode": "redo",
    "formal_id": "{{same existing formal ID}}",
    "stable_path_policy": "kaoyan-408-details-formal-id-content-hash-v1",
    "items": [],
    "image_attachment_count": 0,
    "no_display": true
  },
  "review_backflow_assessment": {
    "schema": "formal_intake_direct_schedule_assessment_v1",
    "source": "daily_practice|evening_d0|morning_review",
    "source_id": "{{verified learning source id}}",
    "source_identity": "{{verified existing formal/review identity}}",
    "review_unit_id": "{{verified review unit or null}}",
    "formal_node_id": "{{same existing formal ID}}",
    "first_result": "wrong|partial|uncertain|blank",
    "confidence": "low|medium|high",
    "prompt_level": "none|minimal|progressive|full",
    "reasoning_break": true,
    "first_break": "{{answer-safe verified first break}}",
    "first_break_provenance": "user_report|visible_evidence|derived_classification",
    "event_time": "{{exact learner observed_at, or null for local runtime fallback}}"
  },
  "reviewed_at": "{{ISO-8601 timestamp with timezone}}",
  "reviewed_by": "web-gpt-6-pro",
  "coordinator_checks": {
    "evidence_hashes_verified": true,
    "answer_leak_check": "pass",
    "knowledge_tags_verified": true,
    "error_tags_verified": true,
    "duplicate_check": "pass",
    "historical_mapping_check": "pass_or_not_applicable"
  }
}
```

`error_tags`, `redo_action`, `evidence`, and the shown empty relation/topic arrays
are optional. Redo cannot replace the
existing detail-card Markdown, cannot carry `detail_card_source`, cannot add a
relation, and cannot write a topic chain. A verified transfer practice may add only
`practice_context={"kind":"transfer","source_id":"...","formal_node_id":"..."}`;
the receiver accepts it only when the sealed source package proves a distinct source
bound to that same existing node.

For both templates, literal pipe-separated examples such as `DS|CO|OS|CN` are
documentation choices: the actual JSON contains one allowed value. Empty optional
arrays contain no placeholder strings. Safe formal fields must not contain the
correct answer, answer option, user's old wrong option, full stem, full explanation,
temporary file paths, or hidden answer material. Full text and images remain in the
hash-bound details/evidence layer.

### Outer decision object

For `new` and `redo`, the outer `decision` object has this exact field topology; the
complete canonical package above replaces `{{canonical package object}}`. The
delayed assessment appears inside that canonical package and is omitted at the outer
level.

```json
{
  "decision": "new|redo",
  "formal_id": null,
  "canonical_package": {},
  "evidence_refs": [
    {
      "locator": "{{manifest capture evidence_locator}}",
      "sha256": "{{manifest capture evidence_sha256}}"
    }
  ],
  "basis": ["{{one or more source-grounded reasons}}"],
  "counterevidence": [],
  "confidence": "high|medium|low",
  "unresolved_gaps": [],
  "knowledge_observations": {}
}
```

The empty objects shown above are replaced by the complete objects from the linked
contracts. For `redo`, `formal_id` is the existing ID; for `new`, it is null. Every
new/redo item has no unresolved gap; a material gap uses `action=needs_user`.

For `correct_review` and `already_applied`, `canonical_package` is null, the existing
formal ID is present, `knowledge_observations` is complete, and the exact
`review_backflow_assessment` object is a top-level member of this outer decision.
`correct_review` uses `decision=correct_review`; `already_applied` uses
`decision=already_current`. These actions verify the current formal row/card and
write the delayed review/point evidence without running redo or adding a recurrence.

For `concept_only`, `decision=concept_only`, `formal_id=null`,
`canonical_package=null`, no review-backflow assessment is present, and complete
`knowledge_observations` accompanies the item-level `concept_review`. For
`needs_user`, `decision=needs_user`, `formal_id=null`, `canonical_package=null`, and
`unresolved_gaps` plus `needs_user_reason` state the exact answer-safe gap; it does
not claim affected-point/profile coverage from unresolved evidence.

The live published companion contracts are
`support/d408/formal-enhancement-contract.md`,
`support/d408/formal-point-observations-contract.md`, and
`support/d408/concept-review-contract.md`. Use those published records for the exact
Capture turn hashes, point observations, concept-review observations, attachment
roles, and source bindings; do not use the retired Luna workflow prose in
`schema/sol-curation-decision-v1.md` as an authoring contract.

`review_backflow_assessment` is the delayed typed result used by the native review
policy.  It has the exact `formal_intake_direct_schedule_assessment_v1` fields:
`schema, source, source_id, source_identity, review_unit_id, formal_node_id,
first_result, confidence, prompt_level, reasoning_break, first_break,
first_break_provenance, event_time`; the optional correction triple is
`supersedes_source_event_id, source_event_sha256, supersession_reason`.  This is the
single enhancement-time review/scheduling input.  A pre-existing bound outcome is
reused or explicitly corrected; it is never replayed as another learner attempt.
For an independently correct result with no reasoning break, use
`first_result=independent_correct`, `reasoning_break=false`, `first_break=none`, and
`first_break_provenance=not_observed`. Wrong, partial, uncertain, and blank results
must carry a real nonempty answer-safe first break.
For `source=morning_review`, the receiver reopens the exact
`study_episode_capture/original_review` conversation package, its campaign-original
display binding and private observed outcome. If a learner turn has `observed_at`,
that exact timestamp is used and `time_quality=user_turn_observed_at`. If every user
turn preserves `observed_at=null`, the web leaves `event_time=null`; the receiver
reopens the same run/item's canonical `campaign_response_observed` runtime event,
verifies its episode, display commitment and exact private first response, and uses
that event's recorded time with `time_quality=runtime_observation_recorded_at` plus
its event ID/SHA. It never substitutes enhancement/apply time. If neither source has
a real timestamp, or the timestamp's Shanghai date differs from the Capture study
date, the item is rejected as a concrete time-evidence gap. `source_id` is the
nested real question/source identity, not the technical `EPISODE-...` package
identity. `source_identity` equals the verified review unit, existing formal node,
or real source ID in that order.

If the Capture already binds a canonical morning outcome, the receiver reuses it.
Otherwise enhancement apply writes one outcome with a deterministic hash of
`episode_id + capture_id`, binds the Capture payload, conversation SHA, display SHA,
private-outcome SHA and sealed decision SHA, and immediately reopens that event for
backflow. A retry returns the same event; a different event for the same session/item
is a conflict. Thus deferred morning review preserves `source=morning_review`, the
original time and one FSRS transition. The web page does not invent an event ID and
must not relabel the episode as `daily_practice`.

When the learning-time `observed_outcome` aggregate conflicts with the complete
original conversation, keep that immutable aggregate and add the optional
`deferred_outcome_reconciliation` to this same assessment. It is an explicit
evidence-bound correction, not another learner attempt:

```json
{
  "schema": "cs408-deferred-outcome-reconciliation-v1",
  "observed_outcome_sha256": "canonical SHA256 of the unchanged observed_outcome",
  "first_response_ref": {"turn": 2, "sha256": "canonical original turn SHA256"},
  "prior_assistant_turns": [
    {"turn": 1, "sha256": "canonical original turn SHA256", "content_role": "question_only"}
  ],
  "choice_result": "uncertain",
  "reasoning_result": "not_observed",
  "reason": "Concrete evidence explaining the original first response and later prompts."
}
```

Use the existing canonical JSON hash algorithm, exact original one-based turns,
and every assistant/tutor turn before the selected first response. `content_role`
is `question_only`, `hint`, or `explanation`. If earlier user turns are explicitly
unrelated to the current question, the optional `earlier_nonattempt_turns` must
list every such turn in order with `turn`, its original `sha256`, and a concrete
`reason`; it cannot silently skip an earlier wrong attempt. All raw turns remain.
The receiver verifies the sealed assessment and per-point original-turn evidence;
`independent_correct` requires a matching independent first-response observation,
correct choice, sound expressed reasoning, `prompt_level=none`, and no preceding
hint or explanation. A later prompted correction cannot prove independence.
An independently valid option-elimination answer and later prompts about extra
concepts remain separate evidence; neither overwrites the other. Keep explicit
uncertainty or real reasoning errors where the original first response shows them.

`knowledge_observations` follows `formal-point-observations-408-v1` and cites exact
one-based original turns with their canonical hashes.  Correct and prompted results
are valid observations; presence in a Capture never implies an error.

For `concept_only`, `concept_review` is
`{session_id, started_at, ended_at, observations}`.  The receiver supplies the exact
reopened conversation and package artifacts.  Each observation uses the native
`concept_review_408` fields, has `question_ref` equal to this Capture ID, and may
refer to existing formal IDs only as sources.  Its point set must equal the observed
point set in `knowledge_observations`.

Set `concept_review.session_id=null`; the receiver derives the formal concept
event's stable session identity from the real Capture ID. A new web run cannot
count the same Capture again. Conflicting reinterpretations of a sealed event
require explicit correction, not a new session label. A Capture with
`execution_kind=concept_supplement` permits only `concept_only` or `needs_user`,
never original-question new/redo/FSRS actions.

When exact study-session timestamps are unknown, set both `started_at` and
`ended_at` to null. The receiver uses the actual display/capture recording
bounds, marks `time_quality=recorded_bounds_not_user_timestamps`, and retains
the Capture's original study_date. It does not fabricate user timestamps;
point-history records remain day-precision with event_time=null in this case.

`expected_formal_state` is copied from the Study Library
`question_refs[formal_id].formal_state`. It binds existing-node decisions to the
canonical master-row JSON SHA and safe-card byte SHA seen by the web run. Both the
published values and current local values are checked before any write. `needs_user`
has no canonical package or profile update for an unresolved point and supplies an
answer-safe `needs_user_reason`.

## knowledge_updates.json

```json
{
  "schema": "cs408-knowledge-profile-updates-v1",
  "updates": [
    {
      "concept_key": "OS03-17",
      "expected_previous_version": null,
      "overview": "complete final overview",
      "short_summary": "at most 2400 UTF-8 bytes",
      "next_check": "one concrete next check",
      "timeline": [],
      "progress": [],
      "question_refs": [{"logical_question_id": "q1"}],
      "source_refs": [{"path": "cs408-conversation-package://...", "sha256": "..."}],
      "aliases": []
    }
  ]
}
```

There is exactly one complete update for every `manifest.affected_points` entry.
`expected_previous_version` is the reviewed profile object SHA, or null if none;
it is not a mechanical-summary SHA. `source_refs` must match a verified binding.
Supported binding keys are package relative paths,
`enhancement-package://RUN/PATH`, `capture://CAPTURE_ID`, the exact conversation
package locator, `study-library://SOURCE_COMMIT/PUBLISHED_PATH`,
`study-library://SOURCE_COMMIT/question-ref/FORMAL_ID`, and
`study-library://SOURCE_COMMIT/original-mapping/QUESTION_ID`. Study Library record
hashes use canonical sorted-key compact JSON plus one newline. This lets a profile
cite its actual Capture or published history instead of citing enhancement prose as
proof of itself. The receiver validates raw updates during prepare, keeps their
bytes unchanged, and passes the same raw updates plus real logical-to-formal bindings
to `knowledge_profile_408.apply_updates` after all item writes.

Each `question_refs` row marks `source_identity_status` as `verified_published`,
`verified_capture`, or `unverified`. Published identities are checked against the
verified Study Library `question_refs`; capture identities are checked against the
reopened original package. An unverified row cannot claim a year, official number,
canonical IDs, or source label merely because a formal ID contains a year.

## Local CLI and recovery

```text
python3 scripts/enhancement_intake_408.py prepare --zip /exact/package.zip
python3 scripts/enhancement_intake_408.py prepare --zip /exact/package-directory
python3 scripts/enhancement_intake_408.py apply --prepared /exact/prepared-receipt.json
python3 scripts/enhancement_intake_408.py status --prepared /exact/prepared-receipt.json
python3 scripts/enhancement_intake_408.py describe
```

Prepare performs no formal, review, FSRS, Capture-result, profile or publisher
write.  Apply stores a local recovery checkpoint after every native receipt.  A
committed formal receipt followed by profile or publication failure resumes only
the missing layer; all owners use stable idempotency keys.  The profile update is a
completion gate.  Only after item writes and profiles succeed does the receiver
close the exact batch and request one current refresh/publication for the whole
enhancement operation.


## Local repair of unfinished exact batches

`prepare --resume-prepared <original receipt>` accepts a derived correction only before profiles or batch close have committed. It preserves the Capture set, original date and fixed snapshot, requires every completed item to be byte-equivalent as a JSON object, and carries the existing completed transaction receipts into the new prepared operation. Only unfinished items run. Originals and parent receipts remain immutable; subsequent retries use the new receipt.

Cumulative supplements are accepted only when the exact locator/SHA appears in the Capture supplement history; the sealed decision still proves every frozen supplement and uses the latest cumulative version.

For morning records without exact learner timestamps, runtime observation recording time may fall after the Capture day. The runtime payload must explicitly bind the original study date. Keep the original Capture/formal date, the actual recorded timestamp and its runtime event proof separately; never substitute a fabricated learner time. Ordinary standalone episodes have no prior morning outcome and do not need a nested campaign session/item identity.
