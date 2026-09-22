Current Question Context v1

Purpose

`current-question-context-v1` is the complete model-facing input for one 408
question. It contains only the visible current question, the learner's current
reply, and answer-safe metadata. The active turn must not augment it from
personalization, history, formal tables, related questions, variants, Luna, or
another item in the prepared pack.

Required fields

- `schema`: `current-question-context-v1`
- `source`: `morning_review`, `ordinary_question`, `daily_practice`, or `evening_d0`
- `request_id`, `session_id`, `item_id`, `source_id`, `idempotency_key`
- `event_time`, `study_date`, `timezone=Asia/Shanghai`
- `source_stable`: boolean
- `source_binding_sha256`: required exactly when `source_stable=true`
- `grader_capsule_id`: opaque private-grader identity only
- `question_valid=true`
- `question_evidence`: exact public surface fields `public_text`, `options`,
  `response_instruction`, `public_surface_sha256`, and declared
  `attachment_sha256s`
- `learner_evidence`: exact current-reply fields only
- `assessment`: choice, reasoning, confidence, prompt, first break, and provenance
- `formal_write_count=0`

`BLANK` and `UNCERTAIN` are explicit non-terminal learner responses. They remain on
the same item/display draft and never synthesize an A-D choice.

The private evaluator supplies only the controlled option/conclusion result. Root Sol
supplies the frozen semantic assessment. The prepared operation binds
`semantic_assessment_sha256`, the complete interaction-trace SHA-256, frozen-feedback
SHA-256, and `terminal_verdict_sha256`; a choice-only result cannot overwrite them.

Source identity compatibility

`source_id` is always the machine identity accepted by the stable-ID grammar.
New producers must normalize source metadata before publishing a prepared pack.

One narrow compatibility path exists for an already-published immutable legacy
morning pack whose raw `source_id` is a human-readable title. The managed
consumer may substitute the scalar `formal_node_id` only when the current
content-addressed display receipt contains a scheduling binding that exactly
matches the binding recomputed from the same immutable session and prepared
pack. The formal node must satisfy the canonical formal-ID grammar. Missing,
invalid, non-unique, or conflicting bindings fail closed as `source_id_invalid`.

The compatibility decision is frozen once in the private prepared answer
operation. Crash replay validates that frozen value and does not derive another
identity. The normalized context carries `source_identity_provenance` with:

- `raw_source_label`
- `machine_source_id`
- `normalization_basis=verified_display_scheduling_formal_node_fallback`
- `scheduling_binding_sha256`
- `binding_verification`, either `verified_embedded_scheduling_binding` or
  `reconstructed_legacy_display_session_pack_binding`

A valid original `source_id` is never rewritten and receives no fallback
provenance field.

Privacy boundary

The context recursively rejects private grading keys, including answer keys,
correct options, graders, evaluators, standard solutions, decisive reasons,
and prepared feedback maps. Private evaluation is published as a physically
separate grader capsule. Production accepts only the capsule locator and hash;
answer or explanation JSON is never accepted in the public turn payload.

Conversation-buffer, terminal, and local-index matrix

- Every managed current item uses one stable private conversation-buffer identity.
  Nonterminal first answers, hints, explanations, restatements, and corrections append
  complete immutable revisions but create no Capture and no local pending-formal row.
- Correct with medium or low confidence, a prompt, or an observed break remains
  `fragile_correct`; partial reasoning, wrong answers, blanks and explicit
  uncertainty retain their own current assessment. None of these classes is a
  formal-node or mastery decision during the learner turn.
- Terminal requires a correct option/conclusion, `reasoning_result=sound`, no known
  contradiction, and prompt dependence that matches the observed trace.
  `partial|diverged|not_observed` reasoning is nonterminal even when the option is
  correct. Prompted terminal resolution remains prompt-dependent and is not upgraded
  to independent mastery.
- On terminal, the transaction seals and reopens the complete final package, creates
  exactly one Capture bound to its canonical SHA-256, then reopens the durable Capture
  receipt. Only that verified receipt admits one `awaiting_sol_formalization` pending
  row and permits successor, last-item close, or explicit hold/close.
- Unstable source, final-package failure, Capture failure, or receipt drift returns an
  exact recoverable state and cannot advance. Recovery reuses the same item, display,
  finalization idempotency key, package, and Capture identity.

Standalone boundary

This context does not authorize ordinary standalone persistence. A standalone
teaching turn is read-only. Only a separate explicit-fast-intake authorization may
invoke `standalone-question-intake-v1` and create one final package plus one Capture.
