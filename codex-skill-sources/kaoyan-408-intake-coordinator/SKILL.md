---
name: kaoyan-408-intake-coordinator
description: "LEGACY 408 历史事务检查与恢复技能。仅用于明确点名 2026-07-12 前 JOB/WAL/receipt 的检查、恢复或对账；不得启动连续入库、创建新 job 或重新 apply。当前题学习中提交转 wrong-intake 快速事实捕获，立即完整正式入库转 daily-intake-curation，指定日期集中编纂转 daily-intake-curation。"
---

# Legacy 408 Intake Recovery

## Goal

Inspect, recover, or reconcile one named historical transaction without changing its
identity, broadening its scope, or reviving the retired queue workflow.

## Entry gate

This skill may run only when the request supplies or clearly identifies at least
one existing artifact: job ID, WAL, transaction ID, receipt, or a legacy state tied
to one transaction. Read the named artifact and its necessary historical contract.
Use `../_shared/kaoyan-408/runtime-contract.md` only when routing is uncertain.
Do not invoke retired personalization pre-answer or rebuild historical projections.

Do not use this skill for:

- a current question, learning-time capture phrase, immediate full-now phrase, or
  dated daily-curation request;
- “start continuous intake”, a generic queue scan, or a new batch;
- a DRAFT/READY item with no WAL or successful receipt;
- teaching or current-question correction.

If the entry gate fails, do not mutate legacy runtime state. Route a current
question to `kaoyan-408-wrong-intake`, a dated capture inventory to
`kaoyan-408-daily-intake-curation`, or report that the historical item is retired.

For an inspection-only request, read the named artifact and
`references/question_submission_v1-evidence-map.md`, report verified evidence, and
stop with zero mutations.

## Success criteria

Recovery is complete only when:

1. the original job, transaction, manifest, actor/generation, and receipt bindings
   are verified from disk;
2. the original WAL is resolved or a successful receipt is reconciled;
3. no second apply occurred after a successful normal receipt;
4. required coverage, bridge, review-unit-v2, and global audits pass for the
   original formal ID;
5. the legacy coordinator remains inactive and all historical evidence remains.

## Evidence order

Read `references/coordinator_contract.md` before mutation. Treat local runtime,
WAL, receipts, hashes, and formal files as authoritative. Chat summaries, worker
candidates, and stale queue labels cannot override them.

Use targeted reads for the named transaction. Do not scan or claim unrelated jobs.
Read current project validation rules only to determine the completion bar; never
reinterpret them as permission for a new submission.

## Recovery decisions

### Existing WAL or COMMITTING without a successful normal receipt

Use the repository's recover-only operation with the original identities. It may
finish or roll back the original transaction; it may not create a new manifest or
new idempotency identity. Hash or binding conflict is a hard stop.

### Successful normal receipt but stale queue state

Verify receipt path, SHA-256, transaction, formal ID, and formal after-hashes. Reconcile
queue metadata only through a supported fenced transition of the original active
generation; otherwise report that metadata step as blocked. Proceed to receipt-bound
closeout and never call apply again.

### Successful receipt with incomplete derived surfaces

Use `intake_parallel_orchestrator_408.py --resume-receipt ...` for receipt-bound
postcommit repair. Refresh and validate only surfaces belonging to the original
transaction. A failed required branch remains incomplete.

### Historical DRAFT or READY without WAL/receipt

Leave it unchanged and report it as a retired unsubmitted item. Cancellation is a
separate explicit user action; do not delete evidence.

## Hard boundaries

- Do not register or reactivate a coordinator, create/claim/review a job, create an
  approved package or manifest, or assemble a batch.
- Do not edit state versions, SHAs, or idempotency keys to bypass binding checks.
- Do not wait for the user, Tutor, HTTP, Obsidian, or another task while holding a
  formal lock.
- Do not delete jobs, inbox evidence, manifests, WAL, journals, receipts, or
  already published formal data.
- Recovery concerns one named historical transaction at a time.

## Output

Report the target artifact, evidence verified, action taken, resulting state,
validation status, and any remaining blocker. If no mutation was allowed, say why
and identify the correct route. Stop after the named transaction reaches a verified
terminal state or a hard evidence conflict is found.
