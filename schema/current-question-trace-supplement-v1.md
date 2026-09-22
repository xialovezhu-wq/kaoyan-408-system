# Current question trace supplement v1

Status: retired read-only compatibility schema as of 2026-08-30.

No new managed or standalone item may publish this object. Nonterminal turns remain
only in the same private conversation buffer; terminal Capture admission binds the
sealed final `cs408-conversation-package-v1`, which already contains every correction,
restatement, feedback turn, and attachment. Existing supplement bytes may be reopened
for exact historical audit or recovery but cannot authorize navigation, a new Capture,
or a replacement final package.

The remainder of this document describes the historical object shape only.

## Purpose

`current-question-trace-supplement-v1` is the private content-addressed cumulative
teaching trace published exactly once when an initial `wrong|partial|uncertain`
current question is corrected successfully. It extends evidence without rewriting the
immutable first-turn bundle or public capture.

## Exact object

The object contains exactly:

```text
schema_version
capture_id
context_id
item_id
evidence_manifest_sha256
created_at
supplement_kind
resolution_receipt_sha256
events
interaction_trace_sha256
formal_write_count
```

Required constants are
`schema_version=current-question-trace-supplement-v1`,
`supplement_kind=resolved_trace`, and `formal_write_count=0`. Events have exactly
`role`, `kind`, `text`, and `observed_at`; current runtime requires
`observed_at=null`. Roles and kinds match `current-question-interaction-trace-v1`.
There are 1 to 24 events and their canonical array is at most 32768 bytes.

## Binding

The private `current-question-trace-supplement-binding-v1` binds the capture, context,
item, evidence-manifest hash, object locator/hash, creation time, supplement kind, and
teaching-resolution receipt. A capture can resolve to only one exact binding.

The teaching-resolution attestation and session receipt separately establish that
the correction choice was correct and that session resolution is
`relearn_required`, with `mastery_effect=none`, `retention_effect=none`, and
`independent_repair=false`. The supplement never upgrades the immutable first result
to independent correct.

## Consumers

For an unresolved failure capture, semantic background analysis remains gated. A
supplement alone is not readiness proof. Background input and Sol must also resolve a
`current-question-background-handoff-v1` for the same capture and verify
`status=ready`, `completion_kind=teaching_resolved`, the supplement locator/hash, and
the interaction trace hash. A truncated cumulative trace remains an evidence gap.
Neither trace text nor the private locator is copied into the public capture.
