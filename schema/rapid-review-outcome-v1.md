# Rapid review outcome v1

`rapid-review-outcome-v1` is an answer-safe, append-only campaign execution
record. Before it is appended, the dedicated campaign hot writer must commit
and verify exactly one canonical `review_event_v1 outcome`. Neither object is a
Capture, formal intake item, or mastery update.

## Required evidence

Each outcome binds the immutable run, segment and item commitments and includes:

```text
formal_node_id: canonical node ID or null
true_item_id: exact EXAM408 or EVB identity
variant_id: EVB identity or null
observed_date: YYYY-MM-DD
choice_result: correct | incorrect | partial | uncertain
reasoning_result: sound | partial | diverged | not_observed
confidence: low | medium | high
prompt_dependency: none | minimal | progressive | full
response_mode: choice_only | choice_plus_reason
screened_correct, reason_audited, recognized_original: boolean
response_seconds: positive number up to 7200 or null
```

The private operation receipt contains the deterministic Again/Hard/Good
scheduler mapping. Rating and predicted probability are not copied into the
raw canonical event. The same raw fact object is returned as `scheduler_replay`.
The `scheduler_handoff` additionally states that campaign evidence is already
committed independently of Capture admission. It fixes downstream formalization
to `identity_binding_or_refresh_only` and sets
`must_not_repeat_campaign_target=true`.

## Deterministic classification

`screened_correct / Hard`

The conclusion is correct, there is no prompt dependence and no known partial
or diverged reasoning, but a semantic audit was not performed. A stable or
mandatory audit item cannot take this path.

`audited_sound / Good`

The conclusion is correct, `reasoning_result=sound`, prompt dependence is
`none`, confidence is medium or high, and a semantic audit was performed. Audited
sound with low confidence remains `Hard`.

`failure / Again`

The conclusion is incorrect, partial, or uncertain; reasoning is partial or
diverged; or prompt dependence is not `none`. Failure evidence is committed to
the campaign ledger immediately, including when `formal_node_id` is already
known.

Failure recording does not advance the frontier. It first appends pending
evidence and a Capture request. The managed failure owner buffers exact turns and the
final failure outcome is appended only after `finalize-capture` verifies a real
package-bound Capture receipt. New receipts use this v2 shape:

```text
schema: rapid-review-durable-capture-receipt-v2
status: durable
run_id, segment_id, item_id
capture_request_id, pending_outcome_sha256
conversation_package_locator, conversation_package_sha256
conversation_package_root
capture_id, capture_payload_sha256, capture_idempotency_key
capture_event_id, capture_event_sha256, capture_ledger_event_sha256
capture_joint_receipt_sha256, capture_joint_manifest_sha256
capture_bounded_manifest_sha256
capture_status: awaiting_sol_formalization
formalization_authorized: true
producer_schema: cs408-campaign-package-capture-proof-v1
receipt_content_sha256
formal_write_count: 0
```

Runtime must recompute the receipt content hash, reopen the canonical Capture and hot
joint proof, and require its unique stable evidence ref to equal the sealed package.
Historical v1 receipts remain immutable and readable; new owners do not emit v1.

An audited correct outcome cannot use `reasoning_result=not_observed`. A stable
hash audit or `mandatory_audit=true` item must set `audit_performed=true`.

## Capture boundary

Correct paths set `capture_required=false`, have no capture request, and keep
`capture_write_count=0`.

Failure sets `capture_required=true` and includes a deterministic request ID
bound to the outcome SHA-256. The request states
`capture_authorized=false`, `direct_capture_write=false`, and
`formal_write_authorized=false`. No campaign operation may turn that request
into a Capture directly.

While that request is pending, runtime state is
`failure_pending_capture`, `completed_items` and the Again counter are unchanged,
and the same item remains current. A hash-verified, identity-bound
`finalize-capture` commits the final outcome, increments Again, and advances once.
The canonical Again event was already committed before the pending state;
finalization re-verifies that same commit receipt and never appends a second
canonical outcome.
