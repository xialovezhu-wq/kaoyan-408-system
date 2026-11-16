# 408 Bridge adapter

Owner: `scripts/study_bridge_408.py`. It reuses the existing package, sealed local
decision, formal proof, full-due scheduler and managed answer owners. It does not
author a question, create a learning outcome from a document, or call a model.
The FSRS cold routes use the interpreter verified by `scripts/runtime_paths_408.py`
with the existing FSRS dependency; a bare `python3` from another environment may
not have it. Do not bind the route to one machine's interpreter location.

## Read-only sources

- `descriptor --repo .` returns the complete source whitelist, actual learning
  entrypoints, question/history mappings and original gaps for the shared publisher.
  details/package/T9 roots have their existing local defaults. It excludes machine
  metadata, secrets, nonterminal drafts and rebuildable runtime caches.
- `validate-package --package-root <exact YYYY-MM-DD/CS408-ID directory>
  [--expected-sha256 SHA]` verifies the complete local package. An archive pointer is
  reopened through the existing T9 sentinel/locator/manifest verifier. Return fields
  include `actual_root`, original `manifest`, canonical SHA and conversation completeness.
- `question-source --formal-id ID` resolves one already identified question and its
  public question-image paths/hashes. It returns no solution/key and records no display
  or learning event. Use only when the current image is not already available.

## A local adjudication and formal proof

`seal-a --input-json A.json --local-decision LOCAL.json [--package-root <container root>]
[--decision-root <isolated root>]`

A.json: `subject="408"`, `project="A"`, `task_id`, `source_commit` or `library_version`,
`batch_id`, `capture_id`, `evidence_locator`, `evidence_sha256`, `proposal`.
The batch/Capture must already be frozen by the existing explicit formal-intake route.

LOCAL.json: `reviewed_proposal_sha256`, `advice_adjudication=accepted|modified|rejected`,
and `decision` containing the existing `sol_curation_decision_408` decision shape.
The proposal SHA is SHA-256 of UTF-8 JSON with sorted keys, `ensure_ascii=false`,
compact separators and no trailing newline. Bridge must not manufacture LOCAL.

`seal-a` returns an immutable A binding plus the existing sealed decision. It always
reports `formal_complete=false`; this is not an apply or an A success claim.

After the normal local formal owner completes, use:

`verify-a --input-json PROOF.json [--decision-root ...] [--formal-runtime-root ...]`

PROOF.json: `a_binding_path`, `a_binding_sha256`, `terminal_locator`, `terminal_sha256`.
The verifier reopens the exact local decision, terminal/result event bytes and normal
WAL/receipt or already-current verification. It returns the formal outcome separately
from advice adjudication, original package/Capture identity and `publication_event_id`.
New successful terminal bindings own `formal-terminal-408:<item_result_event_id>`;
already-current does not invent a transaction. Missing fields in historical bindings
remain missing. Bridge separately verifies this event's publication before unlocking B/D.

## D coverage and native consumption

原图引用交付见[d-asset-delivery-408-v1.md](d-asset-delivery-408-v1.md)。新ZIP声明`asset_delivery=snapshot_refs_v1`并带asset_refs.jsonl；prepare先在独立派生目录装配固定快照字节，再把asset_delivery/asset_refs传给import-d。原生导入在任何变式接纳前核对资产所属题目、哈希及public/private用途；复制不清除真实缺项或升级单点检查包。原网页ZIP保持不变。

`due --date YYYY-MM-DD` returns the native full due run and its primary-knowledge groups:
`due_version`, `due_concept_ids`, `due_concepts`, `coverage`, `native_due_run`.
The full node denominator, unknown-source blockers, due/carry-over and FSRS evidence
remain authoritative; no top-N cutoff is introduced.

`import-d --input-json D.json` accepts:

- `subject="408"`, `project="D"`, `task_id`, `source_commit`, `target_date`, `due_version`;
- `due_concept_ids`, `selected_items`, `unresolved`, `coverage`, `output_root`;
- each selected item: `question_id` (formal ID), `review_target_concept_ids`,
  `suppressed_same_concept_question_ids`, `source_label`, `source_refs`,
  `problem_path`, `solution_path`, optional `image_paths`, `history_path` and
  `teaching_path`.

新增网页变式时，顶层 `web_variants` 是已从 `web_variants.jsonl` 解析出的对象数组，完整格式见发布文件 `variant-bank/web-self-check-contract.md`。对应 selected/supplement item 用 `web_variant_id` 引用；native 导入后生成真实 `EVB-*` 与 surface SHA-256，并在派生 run 中锁定该 identity。复用已有 bank 变式时不重复提交对象，item 直接使用 `preferred_true_item_id=<EVB-...>`；可同时给 `true_item_id`、`surface_sha256` 做一致性断言。

Output paths are relative to `output_root`, existing and hash-bound. `source_refs` are
published paths or `{path,sha256}` objects; at least one binds the selected question.
The supplied commit must already have publisher-verified material. An unrelated newer
commit alone does not invalidate it, but current local due/version and selected-source
hashes must still match. A nonproduction fixture may explicitly supply
`fixture_publication_manifest`; production cannot substitute a caller-selected manifest.

Covered and unresolved concepts are disjoint and exactly cover the full due concept set.
Each concept appears once; suppression lists contain every other same-concept due node,
without duplicates. Suppression is display deduplication, never a learning result.

`NEEDS_SOURCE` and `NO_DUE_CONCEPTS` do not create a ready quiz. A valid import reports
`IMPORTED_FOR_NATIVE_PREPARATION`; identical retry returns its original receipt.
The existing `start_morning_coverage_campaign_408.py prepare` reads the imported view,
rechecks file/source versions and then performs its normal verified-source, exact-item
seven-day exposure, full-segment and answer-isolation gates. Its normal `start` publishes
the first question. Cloud question text never replaces the native grading source.

网页新增变式的 grading/solution/self-check 文件只进入私有、内容寻址的 variant bank；状态为 `web_self_checked_ready`。它由同一网页 actor 自检，不产生 blind/adversarial actor 或 receipt。prepare 仍重新执行当前节点 SHA、目标知识点、ESR、exact identity、7 天曝光、重复 surface 与 answer-isolation 门禁；锁定 identity 不可用时失败关闭，不静默改选另一道题。导入在任何 campaign pointer 前完成；异常中断可能留下不可变中间对象，相同输入可按内容哈希幂等补全，未形成 manifest/index 的对象不会被选择或消费。

Only selected representatives are answered today. The full due-node count and suppressed
unanswered nodes remain explicit. Native display/status/completion return `review_scope`,
`concept_coverage_complete` and `unanswered_node_count`; completing the concept tasks
does not mark suppressed nodes reviewed or mastered. Those nodes remain due/carry-over.

## Local generation when web D cannot produce a usable run

`local_concept_review_408.py plan --date YYYY-MM-DD --output <external-details>/review-deliveries/<date>/local-plan.json`
uses the same native `due` result and verified surface selectors, then chooses one
representative per primary concept in native due order. It does not depend on a
GitHub checkout inside the web execution environment or claim that web D succeeded.
The output must stay outside the formal repository because previewed variants may
contain complete learner text.

Any unresolved concept requires the existing ESR-bound `exam_variant_bank_408.py`
author / independent blind / independent adversarial / publish chain. Morning
preparation includes filling these source or exposure gaps with accepted V2/V3
variants. It must not offer omitted concepts as an automatic fallback.

After publishing the needed variants, invoke the same command with `prepare`.
The local owner rechecks current native due identity and prepares the full concept
view through `prepare_campaign`; ordinary `audit` and `start` then publish the first
question. Same-concept unanswered nodes and authority/source blockers stay in the
full denominator. Active or paused legacy/campaign sessions are protected.

## Actual B/D answers

`record-review --input-json RESPONSE.json` accepts `subject="408"`, `project=B|D`,
`task_id`, `source_commit` or `library_version`, `attempt_id`, `allowed_formal_ids`,
`operation` and `input`. Operations and dynamic input follow the existing four
`morning_campaign_turn_408.py` commands. The adapter owns the stable idempotency key.

Before any write, the exact current display must belong to `allowed_formal_ids` from
the verified external output. D also binds its imported task/selected representative.
No display returns `needs_local_display_and_native_turn_input`; a worksheet is not an
answer. Real reasoning and prompt dependence must be supplied to answer-choice when
observed. Failure turns preserve full exact dialogue; verified terminal Capture and one
distinct repair precede navigation. The original outcome/FSRS Again remains unchanged.

The source binding preserves the actual request; it is not a completion proof. The
canonical review ledger and native durable receipts own that proof. Nested owners
release their locks before local personalization refresh and a single shared publication
request. Failures resume only the missing layer, never replaying learning or formal apply.
