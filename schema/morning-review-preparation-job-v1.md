Morning review preparation job v1

Schema identifier: `morning_review_preparation_job_v1`.

Purpose

prepare_next_morning_408.py moves complete frozen-pack authoring and independent
verification outside the learner turn. Its managed state is a rebuildable delivery
sidecar, never canonical learning evidence.

The content-addressed job identity binds the local review date, immutable queue and
item order, preparation policy version, exact answer-safe current-question source IDs,
canonical review-ledger cutoff, preparation-worker hash, prepared-pack builder hash,
and model-output schema hash. The same preparation-worker hash is also present in
`source_versions`, so changes to prompts, MCP binding, recovery, or publication logic
cannot silently reuse an older execution contract. It does not bind or rebuild
personalization, history preferences, cross-question
activation, recommendations, variants, or a personalization/question-bank manifest.
The worker rechecks that identity before authoring, after author return, before and
after verifier return, and after the all-or-nothing pack publisher. Any drift moves
the job to stale and no ready job pointer is returned.

For current generation queues, job identity additionally carries the queue's
`generation_id`, generation-input hash, eligible due-action set hash, formal-due
source hash, and scheduling-policy version. Queue generations are stored at unique
immutable paths and selected through a per-date current pointer. A fixed-date queue
without these fields remains a read-only legacy generation.

Execution contract v2

The logical job schema stays backward-compatible, while current workers add a
`morning-preparation-chunked-v2` execution contract. The immutable item order is
partitioned into contiguous chunks of at most four. Every chunk has independent MCP,
author, verifier, attempt, failure and content-addressed receipt bindings. Completed
chunks survive process restart and transient retries.

Each chunk must first call the isolated `cs408_morning_preparation_bundle` MCP profile.
The response is bound to the queue SHA-256, ordered item IDs, MCP server release,
generation, authority fingerprint, evidence-scope hash, exact evidence content and
evidence file hashes. Missing or invalid MCP state fails closed; no shell or broad
repository-read fallback is permitted.

States

- pending_author: a queue snapshot is bound and awaits one isolated author run.
- needs_source: the author found a precise source gap; no question is invented.
- pending_verifier: a structurally valid complete draft awaits an independent run.
- ready: every item passed publication and the job binds one prepared-pack manifest.
- stale: a bound queue or dependency changed; the job is not displayable.
- stale_pre_session: a queue/job/pack had completed or was in flight, but the
  executable action set changed before durable `session_started`. Immutable objects
  remain intact and one replacement generation is prepared.
- retry_wait: a transient MCP or model failure is waiting for bounded exponential
  backoff. It is resumable and is not a complete-pack terminal failure.
- failed_author / failed_verifier: a non-retryable deterministic or semantic gate
  rejected the relevant chunk. Existing immutable evidence remains available. An
  explicit adoption may perform one bounded recovery: requeue a pre-output process
  failure, retry one deterministic author rejection, or clear and rewrite only one
  verifier-rejected chunk with its answer-safe failed-check labels. Accepted chunks
  are preserved; a repeated rejection stays closed.

When a terminal `failed_author` or `failed_verifier` job already contains model
artifacts and the preparation-worker identity later changes, the old job is never
rebound. A manual enqueue may create exactly one clean content-addressed successor for
the same queue, with `predecessor_job_id` and
`successor_reason=execution_contract_changed_after_terminal_failure`. The predecessor
state and objects remain byte-for-byte unchanged; the successor starts with empty MCP,
author, verifier, failure, and pack fields and zero learner/formal writes. Replaying the
same new identity reuses that successor. Pending, ready, packed, or retryable jobs
cannot use this transition.

Triggers

- nightly_closeout requires a named, closed batch whose global_closeout is pass.
- guardian_precheck may generate only the derived dated queue when no active session
  exists, then enqueue the same content-addressed job.
- prepare-next-morning is the supported manual or nightly wrapper. A named nightly
  trigger validates the closed PASS batch, may create the next dated queue, and may
  run the isolated author/verifier worker without starting a learning session.
- manual is a diagnostic and migration-safe operator entry.

The guardian integration may reuse its existing five-minute scan, pending state and
single-instance lock. It does not create another LaunchAgent or Scheduled task. The
retired promotion/challenge release gate is not part of a new job, worker invocation,
or job identity. Existing promotion receipts remain historical read-only evidence.

Author and verifier

run-worker launches separate ephemeral, read-only Codex executions per chunk. Plugins,
subagents, shell and web search are disabled. The author and verifier receive distinct
managed actor and execution labels. Model output never publishes directly: the
existing prepared-pack normalizer, hash binding and all-or-nothing publisher remain
the authority. A model attempt has a 1,800-second watchdog. Retryable timeouts and
transient Provider failures retain checkpoints and enter retry_wait; there is no
ten-minute complete-pack deadline.

Safety invariants

- learner_evidence_write_count is always 0.
- formal_write_count is always 0.
- no command starts, grades, advances, pauses, cancels or closes a session.
- guardian precheck for the current day stops when another active session exists.
  Future-date nightly preparation may continue because it only writes rebuildable
  sidecars and never changes that session.
- private drafts, evaluators, verification objects and receipts use mode 0600.
- publication failures and source gaps remain explicit; the learner hot path never
  regenerates the pack and never invokes run-worker.
- source versions contain only queue SHA-256, review-ledger cutoff, current-question
  source-set SHA-256, builder SHA-256, and schema SHA-256. Author and verifier receipt
  hashes are added by their independent stages.
- job, worker and CLI inputs contain no promotion, option-trial, challenge or
  personalization receipt.
- old preparation jobs and prepared packs remain read-only compatible. New jobs use
  `prepare-next-morning-v2-current-sources` and never rewrite historical objects.
- a ready generation may tolerate an append-only review-ledger suffix only when the
  bound cutoff is still an exact prefix and the eligible action set, formal due source,
  policy, renderer, schema, queue, and pack commitments remain unchanged;
- once a same-date session is active or paused, generation replacement and dynamic
  insertion are forbidden. Newly eligible actions remain pending with their original
  due date and receive an idempotent `morning_late_action_deferred_v1` receipt for the
  next unfrozen generation. “Newly eligible” is the exact difference between the
  current due-action set and the frozen generation manifest's complete
  `eligible_due_actions`, not the smaller selected session item set. A legacy session
  has no exact frozen eligible set, so it returns
  `legacy_generation_late_action_unverifiable` with zero receipts rather than
  classifying old quota backlog as late.
