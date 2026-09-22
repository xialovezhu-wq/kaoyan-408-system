---
schema: campaign_node_admission_contract_v1
timezone: Asia/Shanghai
status: active
effective_date: 2026-08-31
---

Campaign node admission v1

Purpose

This successor binds one already completed formal-intake result to the node-level
coverage campaign. It is neither a formal writer nor a campaign scheduler. It
never mutates a frozen or active run.

Authority chain

One admission freezes all of the following identities:

- the canonical intake `curation_item_result` event and its content hash;
- `capture_id`, `batch_id`, and the capture's explicit `study_date`;
- the committed formal result receipt, or the exact already-current
  verification receipt;
- the successful `formal_intake_review_binding_receipt_v1` and its formal
  review binding event;
- `formal_node_id` and the receipt-bound real primary `review_unit_id`;
- the campaign ID and one content-addressed formal-node cohort snapshot.

The normal formal receipt is authoritative for `mode=new|redo`.
`already_current` is always a refresh. The resulting event type is therefore:

- `campaign_node_admitted` for `curated + new`;
- `campaign_node_refreshed` for `curated + redo` or `already_current`.

No command guesses a review unit as `RU_<formal_node_id>`.

Append-only ledger

The canonical ledger is:

`wiki/study_vaults/408-full/state/review-campaign/formal-admission/events.jsonl`

Allowed event types are:

- `campaign_node_admitted`;
- `campaign_node_refreshed`;
- `campaign_forecast_refresh_requested`;
- `campaign_forecast_refresh_bound`.

Events are immutable. Repeated requests return the existing content-addressed
receipt. A reused idempotency key with different authority material fails
closed. The writer never edits the intake ledger, review ledger, formal tables,
campaign scheduler, forecast, run, segment, prepared pack, or session.

Frozen-run boundary

Every admission and refresh request states:

- `target_run_policy=next_unfrozen_run_only`;
- `frozen_run_mutation_allowed=false`;
- `active_run_mutation_allowed=false`.

An admission only guarantees eligibility for the next not-yet-frozen run.
Already frozen or active runs remain byte-identical. A scheduler/forecast
consumer may acknowledge the content-addressed handoff request with a separate
receipt; this writer never calls that consumer or edits its state directly.

Status model

Status is derived from evidence, never used as authorization:

- `FORMAL_COMMITTED_REVIEW_BACKFLOW_PENDING`: a canonical formal-success result
  exists, but its final formal-review binding is absent, pending, invalid, or
  inconsistent;
- `FORMAL_BOUND_CAMPAIGN_ADMISSION_PENDING`: formal review binding is verified,
  but no exact campaign admission event exists;
- `CAMPAIGN_ADMITTED_FORECAST_PENDING`: admission is verified, but no exact
  scheduler/forecast handoff receipt is bound;
- `CAMPAIGN_ADMISSION_COMPLETE`: the admission and its exact handoff receipt are
  both verified.

Recovery only fills the first missing successor layer. It never invokes or
replays formal apply, mark-result, review backflow, postcommit, or learner
outcome recording.

Content-addressed handoff

`refresh` writes a `campaign_forecast_refresh_request_v1` object. The request
binds the admission receipt, node, RU, study date, cohort snapshot, and the
next-unfrozen-run-only boundary. Without a consumer receipt, the status remains
`CAMPAIGN_ADMITTED_FORECAST_PENDING`.

A consumer receipt must bind the exact request SHA-256 and admission receipt,
report zero frozen/active-run mutations, and identify the same campaign cohort.
The accepted schema is `campaign_forecast_handoff_receipt_v1`. Its successful
status is `accepted` or `refreshed`.

Scheduler bridge

`scripts/campaign_admission_scheduler_bridge_408.py` is the local consumer for
the existing node-level scheduler. It accepts only a validated native admission
receipt, rechecks the current formal row and canonical review-unit mapping, and
appends one idempotent `review_campaign_admission_event_v1` to the scheduler
admission ledger. A new node maps to `campaign_node_admitted`; a refresh extends
the prior node chain, while a first refresh is accepted as a baseline member only
when the live formal cohort still equals the configured baseline count.

The bridge then runs `load_projection`, `build_due_run`, and `build_forecast` for
the requested future date in a derived output directory. Only after both products
are written and read back does it create `campaign_forecast_handoff_receipt_v1`
and bind that receipt through `refresh_campaign_projection`. It never rewrites a
frozen run or touches active, paused, or closed runtime state. Example:

```text
python3 scripts/campaign_admission_scheduler_bridge_408.py \
  --repo /Users/your-user/Documents/kaoyan-408 \
  --admission-receipt-sha256 <verified-receipt-sha256> \
  --target-date 2026-09-15 \
  --output-dir <new-derived-directory>
```

History audit

`audit-history` is read-only and audits two successive set boundaries.

The first boundary is the canonical current formal-success Capture-ID set
against the verified `formal_review_binding_v1` Capture-ID set. Formal result
attempts are replayed through the intake ledger and de-duplicated by the
Capture's current batch; retry/carry attempts cannot inflate the denominator.
A binding must match that current batch/Capture, exact formal proof digest,
formal node, real RU, and one canonical ensure event. When an immutable result
also stores a final binding receipt, that receipt selects the exact binding
event and resolves any historical duplicate candidate.

The second boundary is the verified-binding formal-node set against the
campaign disposition set. The only dispositions are `admitted`, `refreshed`,
`authority_excluded`, and `blocked`; no node disappears merely because it
cannot be scheduled.

Both boundaries report exact counts, sorted identities, content hashes, and
both set differences. Missing bindings and missing campaign dispositions whose
current evidence predates the contract effective date are listed separately as
historical residuals and are not silently counted as current successor
failures. Orphan, conflicting, or unverifiable evidence remains explicit.

CLI

```text
python3 scripts/campaign_admission_backflow_408.py --repo ROOT ensure \
  --batch-id BATCH --capture-id CAPTURE

python3 scripts/campaign_admission_backflow_408.py --repo ROOT bind \
  --ensure-receipt-sha256 SHA

python3 scripts/campaign_admission_backflow_408.py --repo ROOT refresh \
  --admission-receipt-sha256 SHA

python3 scripts/campaign_admission_backflow_408.py --repo ROOT refresh \
  --admission-receipt-sha256 SHA --handoff-receipt FILE \
  --handoff-receipt-sha256 SHA

python3 scripts/campaign_admission_backflow_408.py --repo ROOT status \
  --batch-id BATCH --capture-id CAPTURE

python3 scripts/campaign_admission_backflow_408.py --repo ROOT recover \
  --batch-id BATCH --capture-id CAPTURE

python3 scripts/campaign_admission_backflow_408.py --repo ROOT audit-history
```

All outputs report `formal_apply_count=0`, `formal_write_count=0`,
`review_backflow_write_count=0`, `learner_outcome_write_count=0`, and zero
external model/service calls.
