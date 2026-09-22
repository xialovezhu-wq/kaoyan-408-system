# Legacy coordinator recovery contract

This contract applies only to a named transaction that existed before 2026-07-12.
It preserves historical evidence and permits recovery of work that had already
entered WAL/commit or had a successful normal receipt. It never admits new work.

## Runtime evidence

Default historical runtime:

```text
~/.codex/kaoyan-408-intake/
  coordinator.json
  jobs/JOB-*.json
  approved/JOB-*.json
  manifests/
  transactions/{repo_key}/{transaction_id}/journal.json
  receipts/{repo_key}/*.json
```

Use the actual paths bound to the named job. Do not assume the default if the job
records another runtime.

Verify, when present:

- job ID, state, and state version;
- actor and coordinator generation;
- submission, approved, manifest, and idempotency hashes;
- transaction ID and formal ID;
- WAL/journal status and before/after hashes;
- normal or recovery receipt path, schema, status, items, and SHA-256;
- postcommit validation state.

## Allowed state decisions

| Evidence | Allowed result |
|---|---|
| DRAFT/READY only, no WAL or normal receipt | report retired and leave unchanged |
| WAL or COMMITTING, no successful normal receipt | recover-only using original identities |
| successful normal receipt, stale queue state | reconcile receipt-bound metadata, no apply |
| successful normal receipt, closeout incomplete | resume receipt-bound postcommit only |
| hash, identity, or ownership conflict | stop blocked; no mutation |

Do not use a queue label alone to decide. A valid receipt and verified formal
after-hashes outrank a stale COMMITTING label.

## Recover-only

For an unresolved historical WAL, the batch engine exposes recover-only:

```bash
python3 scripts/intake_batch_apply_408.py --repo . --recover-only \
  --runtime-root ~/.codex/kaoyan-408-intake --json
```

Use the original runtime and binding values. Inspect the resulting recovery receipt
and verify action, job/transaction, actor/generation, items, hashes, and file SHA.
If the historical runtime already contains the matching active generation, the
legacy queue's recover/reconcile operation may be used only for that generation and
named transaction, followed by deactivation. Never substitute a new manifest or
apply package.

Important scope limit: the current `--recover-only` implementation scans every
nonterminal transaction under `transactions/{repo_key}/`, not one transaction ID.
Before running it, inventory that repository runtime and prove that every transaction
it could mutate is within the user's authorized recovery scope. If unrelated
nonterminal transactions exist, stop blocked; do not use the broad command for a
single named job.

Current queue mutation also requires an active coordinator generation. The retired
contract forbids registering or reactivating one, so an inactive runtime has no safe
supported command for queue-state reconciliation or DONE transition. In that case,
the WAL/receipt may be verified or repaired at its own layer, but queue reconciliation
must be reported as a tooling blocker. Do not register a coordinator to bypass it.

## Receipt-bound closeout

For a successful normal receipt with incomplete derived surfaces:

```bash
python3 scripts/intake_parallel_orchestrator_408.py \
  --resume-receipt /absolute/path/to/normal-receipt.json --repo .
```

The resume path may refresh coverage, bridge, review-unit-v2, details validation,
network health, safe-card coverage, and other current required audits. It must not
call apply or launch semantic subagents.

The current resume path accepts exactly one receipt item. A legacy multi-item normal
receipt is not safely splittable into synthetic single-item receipts. Preserve it
and report a tooling blocker; do not reapply, rewrite the receipt, or pretend each
item can be resumed independently.

## Cancellation

Cancellation is allowed only when the user explicitly requests it and the old
contract supports a fenced transition for that exact state. Preserve all evidence.
Never cancel a postcommit `needs_user` item merely to hide an incomplete validation;
resolve only the permitted validation metadata.

## Completion

Report transaction closeout as verified only after the original receipt/WAL is
reconciled and every current required validation for the formal ID passes. Mark
queue metadata DONE only when an already-active historical generation exposes the
supported fenced transition; otherwise report that metadata transition as a tooling
blocker. Keep the coordinator inactive and do not delete or rewrite evidence.
