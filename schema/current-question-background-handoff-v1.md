# Current question background handoff v1

Status: retired read-only compatibility object as of 2026-08-30. The active route has
no background Luna, worker, consumer, handoff, or preterminal Capture. Existing bytes
may be reopened only for exact historical audit or recovery; the remainder does not
authorize new writes.

## Purpose

Historically, this private gate prevented a failure capture from entering Luna or
another semantic background consumer before the teaching exchange was complete. The
following verbs describe historical replay validation only; current code must reject
every attempt to publish, advance, wake, or recover a handoff.

The retired hot path formerly published
`status=teaching_pending`, or `status=evidence_pending` when bundle v3 lacks a
required image role. A background consumer must defer that capture until the same
capture ID resolves to `status=ready`.

## Content-addressed object

The immutable object schema is `current-question-background-handoff-v1` and contains
exactly:

```text
schema_version
capture_id
context_id
item_id
evidence_manifest_sha256
capture_receipt_sha256
status
completion_kind
created_at
updated_at
trace_supplement_locator
trace_supplement_object_sha256
interaction_trace_sha256
resolution_receipt_sha256
formal_write_count
```

`formal_write_count` is always zero. The private locator is:

```text
current-question-background-handoff://sha256/<object_sha256>
```

Objects are stored with mode `0600` under the private current-question evidence
root. Their canonical bytes determine `object_sha256`.

## States

`teaching_pending` is published before capture commit. Its `completion_kind`,
capture receipt, trace supplement, interaction trace hash, and resolution receipt are
all null. It is a fail-closed state: the capture is ineligible for semantic background
processing.

`evidence_pending` has the same null ready-only fields. It is a stronger fail-closed
gate: the producer must not enqueue a model, and teaching completion alone cannot
promote it to ready. Promotion requires the future core-owned authoritative
evidence-readiness receipt.

`ready` requires a capture receipt, a resolution receipt, and exactly one completion
kind:

- `first_turn_complete`: the first choice is correct but capture is still required
  because confidence, prompt use, or a verified reasoning break makes it fragile.
  Trace supplement fields are null; the private evidence manifest binds the first-turn
  bundle and its available interaction trace.
- `teaching_resolved`: the first result was `wrong`, `partial`, or `uncertain`, and a
  later correction was correct. The gate must bind the resolved trace supplement
  locator, supplement object hash, interaction trace hash, and the final private
  teaching-resolution receipt. This state has no mastery, retention, or independent
  repair effect.
- `document_complete`: one verified `formal_problem_document` scope already contains
  the full historical free-response dialogue, learner answer, first break, final
  correction, source DOCX, and ordered images in bundle v4. It requires the full
  dialogue SHA-256, binds no trace supplement, and becomes ready only after the raw
  Capture hot receipt and document-completion receipt exist.

State can move from `teaching_pending` to one exact `ready` object when teaching and
evidence are complete. `evidence_pending` can move to that exact ready identity only
after the core-owned authoritative evidence-readiness receipt verifies the completed
bundle v3. Exact replay is idempotent; regression, bypass, or a different ready
identity fails closed.

## Binding and reader

The mutable pointer is a small atomic binding with schema
`current-question-background-handoff-binding-v1` and exactly:

```text
schema_version
capture_id
status
object_sha256
locator
updated_at
formal_write_count
```

The binding path is keyed by `sha256(capture_id)`. The reader
`read_background_handoff_for_capture(capture_id, private_root=...)` verifies file
type, permissions, binding fields, locator/hash, object bytes, capture identity,
state, and update time before returning `(binding, object)`.

## Recovery and consumer rule

`recover-current-capture` follows the same gate transaction. It ensures pending
before committing a recovered capture; it leaves `wrong|partial|uncertain` pending,
and promotes a recovered first-turn fragile correct capture to
`first_turn_complete` ready. A crash after immutable object publication but before
binding promotion is safe because the prior binding remains pending.

For a recovered `wrong|partial|uncertain`, recovery also repairs or establishes the
same display-receipt lifecycle with the one recovered capture and turn receipt. That
receipt remains non-advanceable. A later corrected answer therefore resolves the
original capture and cannot create a second first outcome or capture.

Background consumers read this gate only by capture ID. `teaching_pending`, a
missing binding, an unsafe file, a hash mismatch, or an unknown state is a defer/fail
closed result. Only a fully verified `ready` object is eligible for semantic
processing. `evidence_pending` is never enqueueable.
