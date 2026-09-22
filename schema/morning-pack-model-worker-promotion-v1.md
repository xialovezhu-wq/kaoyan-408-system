Morning pack model-worker promotion v1

Status

Legacy read-only compatibility only. The promotion, acceptance and challenge
control plane is retired and is not an input to a new preparation job, `run-worker`,
`prepare-next-morning`, the Guardian precheck, or a prepared-pack identity.

Historical boundary

Existing promotion and rollback receipts may be retained and inspected as immutable
historical evidence. They must not authorize a model call, create a job, alter a
queue, publish a pack, or be rewritten to match the new workflow.

Active contract

The active next-morning preparation contract is
`schema/morning-review-preparation-job-v1.md`. A new job binds only the immutable
queue, canonical review-ledger cutoff, answer-safe current-question source set,
builder hash and model-output schema hash. Independent author and verifier receipts
are added by their stages.
