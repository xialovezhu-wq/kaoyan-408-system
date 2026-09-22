# Morning coverage campaign v1

This contract defines a high-throughput successor to the legacy managed
one-question morning session. It does not migrate, edit, close, or otherwise
operate on a legacy queue, prepared pack, active session, current-turn receipt,
or Capture.  Its only canonical review-loop write is the dedicated, answer-safe
campaign outcome described below.

## Immutable daily manifest

The input schema is `morning-coverage-campaign-v1`. It binds one
`campaign_date` in `Asia/Shanghai`, a stable content-derived `run_id`, all item
commitments, and contiguous segments of at most 16 items. Every item carries the
existing prepared-pack commitment fields:

- `item_id`, `source_id`, optional `formal_node_id` and `action_id`;
- `surface_sha256`, `grading_sha256`, and `pack_commitment_sha256`;
- exact `true_item_id`, nullable `variant_id`, and `surface_kind`;
- `campaign_item_commitment_sha256`, which binds the complete prepared item;
- frozen `first_variant`, `recent_recurrence`, `mandatory_audit`, and the
  deterministic `stable_hash_audit` assignment.

Historical manifests fix
`review_outcome_contract_version=canonical-review-event-v1`; v2 manifests fix it
to `none-saving-contract-v2`. A canonical original must have an
`EXAM408-YYYY-QNN` true-item identity and no variant ID. A
verified variant must have the same `EVB-*` value in `true_item_id` and
`variant_id`.

New manifests also bind `saving_contract` from
`morning-campaign-saving-contract-v2.md` and its canonical
`saving_contract_sha256`. The saving contract participates in the content-derived
`run_id`. Historical manifests without those two fields remain legacy-readable;
an existing run is never rewritten to add them.

The preparation-side item commitment also binds `acceptance_kind`. Existing
`verified_ready` variants retain author/blind/adversarial hashes. A
`web_self_checked_ready` variant has nullable blind/adversarial commitments,
`independent_reviewed=false`, and separate non-null web solution and same-actor
self-check commitments. Segment verification checks the branch selected by
`acceptance_kind`; it never places web self-check hashes in verifier fields.

`segment_size` is always 16. A final segment may contain 1 through 16 items.
Each segment binds its ordinal, ordered item rows, `commitment_sha256`, readiness,
and an upstream `verification_sha256`. `start` fails closed unless every segment
is ready. There is no partially published campaign.

The runtime never derives or fabricates `verification_sha256`. A producer must
provide the hash of a real upstream verification receipt for every ready
segment. Missing verification produces a non-ready manifest, or fails manifest
construction when the caller simultaneously claims `all_ready=true`.

The stable hash audit is selected over the complete run. Sort items by
`SHA-256(run_id + NUL + item_id)` and require the first
`ceil(item_count * 0.20)` items. `mandatory_audit=true` adds a forced audit and
does not replace the stable sample. Low confidence, recognized original,
first-time variant, and frozen recent recurrence also force one short reason
audit. `recognized_original` is a user fact and is never inferred from speed or
confidence.

## Runtime boundary

The CLI is:

```text
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT create-run --manifest MANIFEST.json
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT start --run-id RUN_ID --idempotency-key KEY
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT status --run-id RUN_ID
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT record-outcome --repo REPO --run-id RUN_ID ...
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT finalize-capture --repo REPO --run-id RUN_ID --capture-receipt RECEIPT.json --capture-receipt-sha256 SHA256 ...
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT pause --run-id RUN_ID ...
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT resume --run-id RUN_ID ...
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT carryover --run-id RUN_ID ...
python3 scripts/morning_campaign_runtime_408.py --runtime-root ROOT audit --repo REPO --run-id RUN_ID
```

The runtime writes only below `ROOT/runs/RUN_ID/`. `events.jsonl` is the
campaign's append-only evidence ledger. Receipts are content addressed and each
idempotency key is bound to one normalized request. Same-key/same-request replay
returns the original receipt; a changed request fails closed.

`active-state.json` contains aggregate counters plus only the current segment,
never the full run or completed item identities. Its current item list is
therefore bounded by 16 for 30, 40, 100, or larger runs. Segment transition
loads exactly the next segment object.

`pause` freezes the current frontier. `resume` reopens that same frontier.
`carryover` requires a paused run, seals the source run as `carried_over`, and
writes a new immutable later-date manifest for only the remaining items. It does
not start the successor automatically or rewrite the source manifest.

## Canonical event and write authority

Campaign operations always return `capture_write_count=0` and
`formal_write_count=0`. The runtime never imports or invokes a Capture writer,
the managed current-turn writer, or `morning_review_session.py`. Before its
private execution event can advance, `record-outcome` calls only the dedicated
`record_campaign_outcome_hot` canonical writer, then verifies that writer's
durable commit receipt. The canonical event fixes
`scheduling_mode=campaign_fsrs_successor`, `generated_actions=[]`, no trigger,
no organic baseline, no legacy retention, and no legacy candidate route.

The canonical event records raw facts: response mode, screened/audited state,
recognition fact, exact true-item and variant identity, response time, choice,
reasoning, confidence and prompt dependency. FSRS rating and predicted
probability are derived data and are not stored as raw learning facts. The
private scheduler receipt deterministically maps screened correct to Hard,
audited sound medium/high confidence to Good, and every error, uncertainty,
prompt dependence or reasoning divergence to Again. Low-confidence audited
sound remains Hard. No automatic Easy exists.

Correct outcomes have `capture_required=false` and no capture request. Failures
have `capture_required=true` and an answer-safe
`rapid-review-capture-authorization-request-v1` with
`capture_authorized=false` and `direct_capture_write=false`. This is a request
for the owning successor; it is not a Capture and does not authorize a formal
write.

A failure first commits one canonical Again evidence event, then appends
`rapid_review_failure_pending`, changes state to
`failure_pending_capture`, and keeps the item frontier unchanged. The external
owner is `morning_campaign_failure_terminal_408.py`: it buffers exact turns, seals and
reopens one package, commits and reopens one package-bound Capture, and emits a
`rapid-review-durable-capture-receipt-v2`. `finalize-capture` verifies its file SHA-256,
receipt content SHA, canonical Capture binding, hot joint proof, package ref, and exact
run, segment, item, request and pending-outcome bindings, appends the final
failure outcome, and only then advances. Finalization reopens the same canonical
commit receipt and never writes a second canonical outcome. Same-key replay
returns the original finalization receipt and cannot duplicate the Capture or
outcome.

Historical `rapid-review-durable-capture-receipt-v1` files remain read-only compatible;
new terminal operations emit v2 only. The full command/input/recovery contract is in
`schema/morning-campaign-failure-terminal-v1.md`.

Every outcome also contains `rapid-review-scheduler-handoff-v1`. It is
`admission_independent=true`: pending campaign failure evidence is committed immediately even
when the item already has a `formal_node_id` and the result is failure/Again.
The pending handoff keeps `campaign_target_complete=false`; verified Capture
finalization changes it to true. Downstream formalization may only bind or
refresh identity. It must not repeat the already recorded campaign target.
