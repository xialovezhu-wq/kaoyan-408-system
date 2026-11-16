> 新非空正式批次由[本地质量合同](408-formal-quality-v1.md)授权原写者，已有结果和原 WAL 按旧身份恢复。

# 408 fact capture and daily curation v1

## Purpose

Separate low-latency preservation of first-hand study facts from high-quality formal
curation. The capture ledger is not the formal wrong-question repository.

## Truth sources

- `wiki/study_vaults/408-full/state/intake-curation/events.jsonl`: append-only
  capture and curation event truth.
- `wiki/study_vaults/408-full/state/intake-curation/state.json`: deterministic
  replay projection; never edit by hand.
- normal formal receipt and WAL: truth for each completed new/redo transaction.
- formal Markdown tables/cards: current curated graph state.

## Capture event

Input schema is `intake_fact_capture_v1`; event schema is
`intake_fact_capture_event_v1`. Required semantic fields are:

- explicit `study_date` and `timezone=Asia/Shanghai`;
- idempotency key;
- for every new managed-terminal or standalone quick-intake Capture, exactly one
  sealed `cs408-conversation-package://` ref with its canonical SHA-256; historical
  captures with other/multiple refs remain read-only compatibility inputs only;
- separated source facts and user facts with provenance;
- identity hint, answer-safe context anchor, missing fields;
- explicit `formalization_authorized` boolean.

Protected content and temporary paths are rejected. A capture may preserve unknown
identity or main knowledge because it creates no formal node.

## Daily batch identity

Before any dated batch, `backlog-plan` performs a read-only canonical replay. With no
cutoff argument it uses the current `Asia/Shanghai` date. An explicit date is a
cutoff and includes every original `study_date` at or before it; only `only_today` or
an exact capture/package selector narrows the plan. The planner creates no ledger,
queue, batch, or receipt and publishes a canonical plan SHA-256.

The foreground Sol replays and processes planner dates in ascending order. Every
actual `start-batch` remains single-date. Exact selectors resolve to repeatable
`--capture-id`; PARTIAL retries must still preserve their complete original frozen
set and prior-success carry events.

`start-batch --date DATE` is itself the explicit formal authorization boundary. It
selects exactly the `formalization_authorized=true` captures whose replayed state is
`awaiting_sol_formalization` or retryable `curation_failed`. The batch stores
sorted capture IDs, a canonical capture-set SHA-256, and a positive attempt number.
Only one batch may be `active` globally. The inventory is immutable; new same-date
captures wait for the active batch to close and then create a later batch.

New batch IDs use `CUR-YYYYMMDD-<set-sha-prefix>-A<attempt>`. Legacy v1 batches without
an attempt replay as attempt 1 with their original ID. Repeating the same start,
item-result, or close idempotency key returns the original event even after the batch
state changes or closes. A failed set may start attempt 2 only after attempt 1 closes.

The current `awaiting_sol_formalization` route needs no second authorization event;
the explicit scoped formal request plus `start-batch` supplies authority. Later
authorization of one exact historical `captured_unconfirmed` item is an append-only
`capture_authorized` event produced by `authorize --capture-id ...`. It does not
rewrite the original fact event or authorize any other capture.

A closed `PARTIAL` batch's `needs_user` result is resolved only by an append-only
`needs_user_resolved` event. The caller must supply the original explicit study
date, exact capture ID, one confirmed formal ID, and the exact canonical
`candidate_confirmation` event that records the user's choice. The original
capture, item result, batch close, and capture-set hash remain immutable.

There are two resolution kinds:

- `confirmed_identity` proves only the identity. The item enters the next
  same-set attempt and may then run the normal serial preflight/apply flow.
- `confirmed_identity_with_existing_receipt` additionally proves that a normal
  COMMITTED redo already occurred. Its byte-exact normal receipt and committed
  WAL must bind the repository, original batch/capture/payload, canonical review
  event hash, confirmed formal ID, transaction, and confirmation chronology. The
  next attempt must not replay apply; it seals current formal state and records
  `already_current`.

`resolve-needs-user` never treats a tool parameter as user authority and cannot
resolve an item from a guessed identity, another date, another candidate, an
unbound receipt, or an open batch.

An explicit administrative removal is a different terminal, not a formal item
outcome. `retire-needs-user --date DATE --capture-id ID ... --reason user_removed
--user-confirmed --idempotency-key KEY` appends exactly one batch-level
`needs_user_retired` event. The repeated capture IDs must already be sorted and
unique and must equal the complete current non-success set of the explicit date's
latest closed `PARTIAL` batch. Every member must still be `needs_user`, every
non-success result in that batch must be `needs_user`, and no batch may be active.
Partial subsets, another date, resolved/failed/successful items, or missing explicit
confirmation fail closed.

The retirement event binds the original `study_date`, `prior_batch_id`, full-batch
`capture_set_sha256`, each retired capture's immutable payload SHA-256 and original
result-event ID/hash/result hash, and the original close-event ID/hash/close hash.
It records `reason=user_removed`, `explicit_user_confirmation=true`, and
`formal_write_count=0`. Replay does not edit the original capture, result, close,
formal ID, current batch ID, history, or answer-safe result reason. It only projects
`quality_status=user_removed` and the immutable retirement material. Repeating the
same idempotency key or the same already-recorded retirement is a no-op; key reuse
with different material is a conflict.

`user_removed` is not a member of the formal result outcomes or success outcomes.
`backlog-plan`, including exact capture/package selectors, never makes it
processable or residual and reports it under
`administrative_terminal_excluded`. `audit` revalidates the bound event evidence
and reports `user_removed_capture_count`.

No selector may use receipt UTC time, mtime, Git diff, conversation memory, or topic
similarity.

## Formal item result

Each capture is formally processed at most once per successful new/redo transaction,
serially through `kaoyan-408-daily-intake-curation` and the public batch-size-1 apply path.
The ledger stores only:

- batch ID and capture ID;
- outcome `curated`, `already_current`, `needs_user`, or `failed`;
- formal ID and normal receipt SHA-256 for `curated`;
- formal ID and verification SHA-256 for `already_current`, with no fake receipt;
- the preflight `review_schedule_ensure_receipt_sha256` and, for a formal
  success, the post-commit `formal_review_binding_receipt_sha256`;
- the validated backflow terminal, canonical action/due date when applicable,
  and the receipt-bound review-unit identity;
- an answer-safe reason for other outcomes.

It does not store absolute receipt paths or formal semantic deltas. A successful
formal receipt with failed closeout is resumed; it is never reapplied.

The ensure receipt is created before formal preflight from the frozen capture,
its original Asia/Shanghai study date, and the current scheduling policy.  It
may reuse an existing `D1`/`D1_rescue` family action, create the one missing
action, or prove a maintenance no-op.  It is not a learner outcome and cannot
write retention, baseline, mastery, or formal knowledge state.

After a committed normal receipt/WAL or exact `already_current` verification is
reopened, `formal_intake_review_backflow_408.py bind` appends the non-outcome
formal binding and publishes the final content-addressed receipt.  Only
`satisfied` or `maintenance_noop` permits a new `curated`/`already_current`
result.  `needs_user` and pre-apply failures retain the ensure action as
`scheduled_unbound` and must not claim a final binding receipt.

If apply committed but binding, item-result recording, or later closeout fails,
the recoverable state is `formal_committed_backflow_pending`.  Recovery reopens
the original receipt/WAL and continues only bind, mark-result, and closeout;
formal apply count remains zero during recovery.

The hash fields are references, not self-authenticating proof. A `curated` hash must
resolve to the byte-exact normal receipt in the current repository's fixed 408
runtime receipt directory. An `already_current` hash must resolve to the byte-exact
`already_current_verification_v1` entity in the fixed content-addressed verification
directory. That entity binds batch ID, capture ID, study date, formal ID, capture
payload hash, current formal-row hash, and current safe-card hash. Sol terminal
binding and the aggregate closeout independently revalidate these entities and
identities; an arbitrary 64-hex value is never sufficient.

## Aggregate global audit receipt

After every frozen item has one result, a new batch must run
`seal-global-audit --batch-id ...` before it can close with
`global_closeout=pass`. The sealer runs the fixed read-only
`intake_validate_408.py --full --json` command, requires a normalized PASS report,
and rejects any change to the bounded audit tools or audited formal surfaces during
that run.

The resulting `intake_daily_global_audit_receipt_v1` entity lives only at the fixed
content-addressed path
`state/intake-curation/global-audit-receipts/<sha256>.json`. It binds the repository,
batch identity, study date, attempt, frozen capture-set hash, complete ordered item
results, audit contract/version and command, PASS report, audit-script hashes, and
bounded formal-table/card path, size, and SHA-256 commitments.

The entity is attested with a repository-local 32-byte HMAC key whose path must be a
non-symlink 0600 regular file. A bare hash, forged JSON, receipt from another batch
or repository, changed item result, substituted path, symlink, or changed audit
object cannot close an active batch. A successful new close records
`close_contract=daily_curation_close_v3` and the receipt hash inside `close_sha256`.

`global_closeout=failed` must not claim a PASS receipt. Historical close events
without a close contract remain immutable and replayable as `legacy_unsealed`; new
PASS closes cannot emit that form. Ledger audit revalidates content address,
attestation, command/report commitments, and historical batch/result binding.
Likewise, a batch containing any per-item `failed` result cannot close with
`global_closeout=pass`; it must close as failed/`PARTIAL`. A `needs_user` result may
coexist with a technically valid aggregate audit receipt, but the batch remains
`PARTIAL` and cannot become the completed formal batch used for next-morning
preparation.
Mutable formal objects are rechecked against the snapshot at close time, while later
legitimate curation does not rewrite or invalidate an older receipt.

A historical `legacy_unsealed` COMPLETE close remains read-only replayable. It is
not upgraded, republished or used to rebuild personalization, recommendation,
cross-activation or variant projections. New batches must use the current aggregate
audit receipt contract.

## Field ownership

Curatable model-derived fields: safe summary, key parameters, question type, core
point, main/sub/hit knowledge, fuzzy concepts, evidence-backed error tags, redo
first action, relation/topic-chain candidates, and review-unit wording.

Immutable without separate explicit authorization: formal/source/details identity,
study dates, new/redo facts, user response/error/first action, hint or correctness
facts, redo history/count, attachment provenance, mastery, schedule state, and all
receipt/WAL facts. Conflicts become `needs_user`; merge, deletion, and ID reassignment
are outside this schema.

## Recovery

- Ledger projection drift: `reconcile`, then `audit`.
- Formal preflight failure: zero apply.
- Formal apply success plus closeout failure: receipt-bound resume only.
- Item failure: record `failed`; later batch may retry from live evidence.
- Active batch conflict: resume the exact `active_batch`; never open another batch.
- Same-set retry: a v3 retry preserves the complete ordered inventory and
  capture-set hash, points to the immediately preceding attempt, and increments
  the attempt by exactly one after close. Previously successful items receive
  explicit result-carry events bound to their exact prior result events; carrying
  is evidence reuse and never a second apply. Only prior `failed` items and
  append-only `needs_user_resolved` items are processed again. An unresolved
  `needs_user` keeps that same-set retry blocked rather than being silently
  dropped or reclassified.
- Batch closes `COMPLETE` only when every capture is `curated` or
  `already_current` and aggregate formal/ledger closeout passes. A new PASS close
  must bind the content-addressed receipt returned by `seal-global-audit`; the receipt
  commits the frozen set, per-item results, audit tools, audited formal surfaces and
  normalized PASS report. Any `needs_user` or `failed` item makes the batch `PARTIAL`.
- A verified PASS audit may coexist with a `PARTIAL` batch caused by explicit
  `needs_user` results; the receipt still binds the full result set. It never turns
  those items into successful curation and never triggers personalization,
  recommendation, cross-activation or variant rebuilding. A later explicit attempt
  must resolve the remaining items under the same frozen-set rules.
