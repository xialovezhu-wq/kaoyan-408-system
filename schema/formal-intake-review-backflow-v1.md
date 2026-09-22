---
schema: formal_intake_review_backflow_contract_v1
timezone: Asia/Shanghai
status: active
---

Formal intake review backflow v1

Purpose

This contract closes the gap between one frozen learning capture, an already
completed formal result, and the canonical review schedule. It is not a second
formal writer. A new `study_episode_capture/original_review` morning Capture
intentionally has no earlier review outcome; its enhancement-time `ensure` may
append exactly one Capture/episode-bound deferred morning outcome before creating
or reusing its schedule. Other branches do not create learner outcomes.

Boundaries

- The intake capture ledger remains the source of capture, study-date, batch,
  and formal-result facts.
- `review-loop/events.jsonl` remains the source of review actions and formal
  review bindings.
- `ensure` appends `review_schedule_ensure_v1`; only the sealed deferred-morning
  branch may first append its one `outcome` with
  `deferred_morning_binding=cs408-deferred-morning-outcome-binding-v1`.
- `bind` may append only `formal_review_binding_v1`.
- A binding event always has `generated_actions=[]`.
- No command calls formal apply, Luna, Provider, MCP, Dashboard, or a Capture
  writer. The deferred-morning branch calls the existing review outcome writer
  with a fixed episode/Capture idempotency identity.
- Existing queue, prepared pack, and morning session objects are never edited
  by this command.

Ordering

The supported day-end order is:

1. Freeze the canonical capture and the scheduling policy version.
2. Run policy-aware `ensure` before formal preflight. A new morning episode first
   writes or reopens its one deferred outcome inside this step.
3. Run existing preflight and formal apply exactly once.
4. Reopen a COMMITTED normal receipt/WAL, or an `already_current`
   verification.
5. Run `bind` and obtain a successful final receipt.
6. Pass the ensure and final receipt SHA-256 values into intake `mark-result`.
7. Close the batch, then prepare a new morning generation.

If formal commit is already proven but binding or downstream closeout cannot
finish, publish `formal_committed_backflow_pending`. Recovery resumes binding
and closeout only. Formal apply must not be replayed.

Scheduling policy

- Ordinary daily/evening independent correct evidence is a maintenance no-op
  only when confidence is high, prompt level is none, and there is no
  reasoning break.
- Every other ordinary failure-standing result ensures one D1-family action.
- D1 and D1_rescue share one reuse slot for the same identity and due date.
  When both exist, D1_rescue is authoritative and equivalent pending actions
  are closed by append-only dispositions.
- Existing morning outcomes are not reinterpreted. `ensure` requires and reuses
  their one canonical successor action. A new deferred morning episode preserves
  its private web-reviewed result, real learner timestamp and `morning_review`
  source, then creates that outcome/action once and immediately reuses it.
- A non-null learner-turn `observed_at` is the preferred time. When all user turns
  retain null timestamps, the receiver may use only the hash-verified
  `campaign_response_observed.event_time` from the same run/item after matching its
  episode, display commitment and private first response. The binding records
  `time_quality` and the runtime event ID/SHA. Enhancement/apply time is forbidden.
- A correction/repair event never creates a second schedule.

Receipt-bound formal proof

Curated result

The proof is the exact `intake_batch_receipt_v1` bytes with status COMMITTED,
plus its canonical committed WAL. The verifier checks repository identity,
transaction identity, payload hash, one formal item, package identity, and the
published review-unit mapping stage. The primary RU is parsed from that stage;
`RU_<formal_id>` is never guessed. A shared RU is valid.

New formal packages must bind their batch and capture in the WAL package. For
historical migration only, an immutable canonical `curation_item_result` may
bridge an older package that lacks this field, provided its batch, capture,
formal ID, outcome, and receipt SHA all match.

Already-current result

The proof is the exact `already_current_verification_v1` receipt with status
VERIFIED_CURRENT and `formal_write_count=0`. Its batch, capture, and formal ID
must match. The real RU is resolved from a hashed canonical mapping snapshot;
missing or conflicting identity fails closed.

Ensure receipt

Schema: `review_backflow_ensure_receipt_v1`.

Required identity fields:

- `batch_id`
- `capture_id`
- `study_date`
- `policy_version`
- `schedule_input_sha256`
- `ensure_event_id`
- `action_id` (nullable only for maintenance no-op)
- `due_date` (nullable only for maintenance no-op)
- `receipt_sha256` in returned/validated output

Allowed terminal statuses:

- `created_action`
- `reused_existing_action`
- `maintenance_noop`

Final binding receipt

Schema: `formal_intake_review_binding_receipt_v1`.

Required identity fields:

- `ensure_receipt_sha256`
- `batch_id`
- `capture_id`
- `formal_id` and `formal_node_id`
- receipt-bound `review_unit_id`
- `action_id` and `due_date` (nullable for maintenance no-op)
- `formal_proof_kind`
- `formal_proof_sha256`
- `formal_binding_event_id` for a successful terminal
- `receipt_sha256` in returned/validated output

Successful terminals are only:

- `satisfied`
- `maintenance_noop`

`formal_committed_backflow_pending` is recoverable but is not formal-backflow
satisfaction. Intake `mark-result` must not accept it as a successful binding.

All receipts report zero for formal writes, Capture writes, and external
model/service calls. Final binding receipts and ordinary ensure receipts report
zero learner-outcome writes. A deferred-morning ensure reports exactly one when
its bound canonical outcome was created by this enhancement identity; idempotent
recovery keeps the same causal count and never appends a second outcome. Reuse of
an older canonical outcome reports zero.

History audit and repair

`audit-history` is read-only. It freezes the latest intake ledger SHA-256,
review ledger cutoff SHA-256 plus its byte and event count, scheduling policy
version, and the exact count of all formal-success result events. Every result
enters exactly one terminal:

- `reuse_existing_action`: one causal source outcome or exact backflow proof
  owns the action.
- `create_missing_action`: learning evidence, date, formal proof, and RU are
  complete, but no causal action exists.
- `maintenance_noop`: explicit policy evidence proves no action is required.
- `needs_user`: evidence, date, receipt, identity, RU, or action conflicts.

The four terminal counts must sum to the exact formal-success denominator.
Same-formal-ID co-occurrence alone is never reuse evidence. Scheduling
reconciliation and superseded outcomes remain immutable and are not formal
backflow proof.

`repair-history` is dry-run by default. `--apply` may append verified ensure
and binding events plus content-addressed receipts. It never calls formal
apply, edits old queue/pack/session objects, deletes events, or rewrites dates.
Repeated execution is idempotent. A frozen manifest fails closed if either
ledger prefix changes or if the review-ledger suffix contains anything other
than its own receipt-bound ensure/binding events.

CLI

```text
python3 scripts/formal_intake_review_backflow_408.py --repo ROOT ensure \
  --batch-id BATCH --capture-id CAPTURE --policy-version POLICY

python3 scripts/formal_intake_review_backflow_408.py --repo ROOT bind \
  --batch-id BATCH --capture-id CAPTURE \
  --ensure-receipt-sha256 SHA --formal-result-status curated \
  --formal-result-receipt-sha256 SHA --formal-id DS_2099_915

python3 scripts/formal_intake_review_backflow_408.py --repo ROOT validate \
  --receipt-sha256 SHA

python3 scripts/formal_intake_review_backflow_408.py --repo ROOT audit-history

python3 scripts/formal_intake_review_backflow_408.py --repo ROOT repair-history
python3 scripts/formal_intake_review_backflow_408.py --repo ROOT repair-history --apply
```
