# Cross Shadow Activation v3

## Boundary

This pipeline changes only the disposable cross-subject routing projection. It
never creates, promotes, or edits a formal relation edge. A `shadow_candidate`
cannot receive an evaluator assignment and remains ineligible regardless of any
unmanaged favorable text.

The public `cross_subject_candidates_408.py` CLI has no live-enable option and
its public builder always emits `enabled_for_live_routing=false`. Public
`write_projection` rejects a payload containing a live edge. The private live
writer is called only by `cross_shadow_activation_408.py` after cumulative
policy validation.

## Bounded status and nightly plan

`status --limit N` recomputes the current full source fingerprint and returns at
most `N` current `verified_existing + activation_eligible` records. Each record
has one deterministic action:

- `issue_assignments`: this current fingerprint needs a new bounded evaluation
  round on the current Asia/Shanghai date;
- `evaluate_assigned`: signed assignments already exist and must be completed;
- `wait_next_local_date`: the first date is complete and cannot be backfilled;
- `activate_ready`: the cumulative threshold is satisfied;
- `blocked_by_failure`: a current-fingerprint fail prevents activation.

The status surface excludes all answer-bearing question content and performs no
formal write. A changed source fingerprint starts a fresh partition; prior
assignments and evaluations stay auditable but do not count in the new status.

## Managed evaluator assignment

`issue-assignments --request FILE` accepts a bounded
`cross_shadow_assignment_request_v1` from the fixed nightly orchestrator actor.
The request contains the current fingerprint, one to four eligible edge IDs, an
orchestrator `actor_id + execution_id`, and two to four evaluator identities.
Evaluator actor IDs and execution IDs must both be distinct.

For each planned edge the command issues at least two
`cross_shadow_evaluator_assignment_v1` receipts. Each receipt binds:

- the current source fingerprint, eligible edge and answer-safe candidate view;
- the actual current Asia/Shanghai date;
- structured orchestrator and evaluator `actor_id + execution_id`;
- the assignment-request SHA-256 and zero formal writes;
- a content-addressed assignment ID and local HMAC signature.

The signing key is local derived-state authority, is created with mode `0600`,
and is never put in evaluator prompts. Editing an assignment, changing identity,
moving it outside the current managed partition, or changing the candidate view
invalidates the signature or content address.

## Evidence and append-only event

An evaluator receives only its signed assignment and the embedded answer-safe
candidate view. It writes a bounded `cross_shadow_evaluator_evidence_v1` object
with its assigned identity, pass/fail verdict, four boolean checks, a short
reasoning summary, and a counterexample check. Protected question, option,
answer, solution, response, first-break, image, and attachment fields are
rejected; answer-bearing text and evidence over 16 KiB are rejected.

`record-evaluation --submission FILE` accepts only assignment and evidence
references plus their SHA-256 hashes. It derives the edge, evaluator,
orchestrator and date from the signed assignment. The assignment date must equal
the actual current Asia/Shanghai date, so callers cannot backfill a second date.
One assignment can be consumed once. The stored `cross_shadow_evaluation_v2`
ID is the SHA-256 content address of the normalized event; callers cannot choose
an evaluation ID or self-assert evaluator/date fields.

## Automatic live policy

An edge reaches the private live allowlist only when all of the following hold
under the same current source fingerprint:

- lifecycle is `verified_existing`, `activation_eligible=true`, current formal
  evidence count is at least one, and confidence is medium or high;
- relation code is one of R01, R02, R04, R05, or R06;
- at least three passing events have three distinct evidence hashes and three
  distinct evaluator execution identities;
- those executions include at least two evaluator actors and at least two real
  Asia/Shanghai dates;
- no failing event exists.

Before `activate` writes anything, the existing cross projection must be a current
complete rebuild and the current personalization root manifest must bind its exact
SHA-256. The writer stores content-addressed snapshots of both files. The resulting
`cross_shadow_activation_receipt_v3` binds those previous objects, the activated
projection, complete source manifest/fingerprint, evaluation ledger, policy,
decisions, enabled IDs, and hashes of both runtime scripts. Source, ledger, script,
candidate-view, projection, or root-manifest drift fails closed and restores the
previous projection if activation publication did not finish.

The activation receipt is immutable under `activation-receipts/<sha256>.json`; the
fixed `activation-receipt.json` is only the latest pointer. A rollback must name that
exact latest receipt:

```bash
python3 scripts/cross_shadow_activation_408.py --repo . rollback \
  --activation-receipt-sha256 ACTIVATION_RECEIPT_SHA256
python3 scripts/cross_shadow_activation_408.py --repo . validate-receipt \
  --kind rollback --sha256 ROLLBACK_RECEIPT_SHA256
```

Rollback accepts exactly two root-manifest states: `pre_rebuild`, whose bytes still
equal the previous-manifest snapshot, or `post_rebuild`, whose manifest is a legal
answer-safe zero-formal-write personalization manifest and whose cross hash exactly
binds the activated projection. The latest activation must still have the same current
source fingerprint, source manifest, evaluator ledger, policy and script hashes, and
the activated projection must remain exact. It restores only the content-addressed
previous projection; it never edits the root manifest. Therefore a post-rebuild
rollback deliberately leaves a manifest/projection hash mismatch, which makes the hot
path fail closed until an immediate personalization index rebuild binds the restored
projection.

Immediately before restoring the projection, rollback writes the observed root
manifest bytes to a managed content-addressed snapshot. The immutable, replayable
`cross_shadow_rollback_receipt_v1` binds that snapshot by `ref` and SHA-256, its
`pre_rebuild|post_rebuild` state, both old and restored projection hashes,
dependencies and `personalization_rebuild_required=true`. Receipt validation replays
the rollback-before manifest from that snapshot; it does not require the current root
manifest to remain in the rollback-before state. The required later personalization
rebuild may therefore bind the restored projection without invalidating receipt
validation or idempotent rollback replay. The current cross projection must still
equal the restored projection exactly. A repeated command validates and returns the
same receipt without another write. A non-latest activation, legacy v2 receipt,
malformed or unrelated rollback-before manifest snapshot, changed dependency, or
tampered snapshot is rejected. Receipt publication failure restores the activated
projection and leaves the root manifest unchanged.

Successful activation returns `personalization_rebuild_required=true`; the
personalization index must then be rebuilt immediately so its manifest binds the new
cross artifact.

## Nightly sequence

```bash
python3 scripts/cross_shadow_activation_408.py --repo . status --limit 8
python3 scripts/cross_shadow_activation_408.py --repo . issue-assignments \
  --request PROTECTED_ASSIGNMENT_REQUEST.json
python3 scripts/cross_shadow_activation_408.py --repo . record-evaluation \
  --submission PROTECTED_EVALUATION_SUBMISSION.json
python3 scripts/cross_shadow_activation_408.py --repo . activate
# On a validated rollback, rebuild again whether rollback occurred before or after
# this first root rebuild.
python3 scripts/personalization_index_408.py build --repo . --as-of CURRENT_LOCAL_DATE
```

The skill dispatches each assignment to its named independent evaluator in a
separate execution. On the first date two completed passes are still not enough;
status returns `wait_next_local_date`. On a later nightly run, new same-day signed
assignments produce the second-date evidence. Once the threshold is reached,
`activate` runs automatically. No step writes `关系边表.md`.
