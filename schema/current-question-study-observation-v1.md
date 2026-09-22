# Current question study observation v1

Status: retired read-only compatibility object as of 2026-08-30. New managed terminal
answers, including high-confidence independent correct, use final package → one
Capture → durable receipt verification. New standalone teaching is read-only. No new
turn may create this observation; the remainder describes historical bytes only.

## Purpose

Historically, `current-question-study-observation-v1` was a private,
content-addressed record for a high-confidence unprompted answer. The following object
shape is retained only for byte-exact replay and audit; no current outcome, Capture,
navigation, formal decision, or background process may consume it as new authority.

## Exact object

The object contains exactly:

```text
schema_version
observation_id
context_id
request_id
session_id
item_id
source_id
study_date
event_time
first_result
evidence_locator
evidence_manifest_sha256
first_answer_receipt_sha256
processing_status
formal_write_count
```

Required constants are:

```text
schema_version=current-question-study-observation-v1
first_result=independent_correct
processing_status=awaiting_background_analysis
formal_write_count=0
```

`evidence_locator` uses `current-question-evidence://sha256/`; both SHA-256 fields are
lowercase 64-hex digests. The observation ID is deterministically bound to the current
context, evidence-manifest hash, and first-answer receipt. Publication is idempotent
because the canonical object bytes determine its private object address.

## Evidence boundary

The current `answer-current-and-next` route binds the observation to a private
`current-question-evidence-bundle-v2`, which may contain the bounded
`current-question-interaction-trace-v1`. The observation itself does not duplicate the
trace, complete question, answer, options, full learner reply, feedback, grader,
attachment bytes, or private filesystem path.

`processing_status=awaiting_background_analysis` means only that durable source
evidence exists for later background analysis. It does not prove a Luna invocation,
completed report, knowledge classification, history match, or formal ingestion.

Correct-only observations remain outside explicit-date failure-capture batches. Sol
may later use one as independently verified study-history evidence, but a Luna
suggestion cannot turn it into a wrong capture or formal wrong node.
