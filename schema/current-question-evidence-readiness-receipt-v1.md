Current Question Evidence Readiness Receipt v1

Status

Retired interface-only compatibility contract. The 2026-08-30 route has no
preprocessor enqueue, background handoff, worker, consumer, or semantic-model queue.
No new managed or standalone item may create this receipt. Existing receipt bytes may
be validated only as historical evidence.

Required binding

The future receipt schema is `current-question-evidence-readiness-receipt-v1` and
must bind the exact capture ID, bundle v3 manifest SHA-256, bundle object SHA-256,
`question_mode`, ordered attachment-row digest, required-role assessment, trace v2
full digest, controlled-contract path and SHA-256, producer build SHA-256, creation
time, and `formal_write_count=0`.

Terminal rule

Only `status=ready`, with `question_mode=image_question` and a verified
`question_image` and `solution_image` rows or `question_mode=dialogue_only`, may
set `model_enqueue_allowed=true` and authorize semantic model enqueue.
`evidence_pending`, missing receipt, receipt drift, unknown fields, a ninth
attachment, MIME/magic disagreement, or non-v3 evidence fails closed. This receipt
never authorizes a formal write.
