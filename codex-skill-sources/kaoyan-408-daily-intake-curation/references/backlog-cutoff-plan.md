# Canonical cutoff backlog plan

Use the read-only planner before formal work and after every dated terminal:

```text
python3 scripts/intake_fact_capture_408.py --repo <repo> backlog-plan
```

No date means `cutoff_date` is the current `Asia/Shanghai` day. An explicit
`--cutoff-date YYYY-MM-DD` still means every eligible original
`study_date <= cutoff_date`; it never silently means only that day.

The only narrowing controls are:

- `--only-today`, which keeps only captures whose original date equals cutoff;
- repeatable `--capture-id ID`;
- repeatable `--package-locator LOCATOR`, each of which must uniquely resolve to one
  canonical capture.

The planner is read-only and returns `canonical_plan_sha256`, ledger hash/count,
ordered `execution_order`, one `days[]` row per actionable/residual original date,
processable and residual item states, future exclusions, active-batch blocker, and a
`final_gate`. It creates no ledger, queue, batch, receipt, or formal write.

Canonical `pending_by_date` plus active/partial/result replay is the selection
authority. Joint-index proof status is diagnostic only and must not hide a replayable
pending capture. Exact package/evidence reachability is checked when Sol reopens the
item; a real failure then becomes a dated `needs_user` or damaged residual.

For each day in order:

1. replay the canonical ledger again;
2. resume an active batch before doing anything else;
3. otherwise call `start-batch --date DATE` once;
4. when `start_batch_capture_ids` is non-null, pass each value as a repeatable
   `--capture-id`; this is the real exact-subset API;
5. finish every item serially and close/replay before advancing to the next date.

PARTIAL retries preserve the original frozen capture set and carry prior successes.
The exact subset may not shrink that retry set. Existing canonical results are not
recreated. A validated COMMITTED receipt/WAL changes the action to closeout-only and
sets `apply_allowed=false`. Archive intent/pointer evidence may similarly produce
archive-only recovery.

A successful Capture without verified normal or legacy archive proof remains a dated
`legacy_archive_pending` residual with reason
`FORMAL_COMMITTED_ARCHIVE_PENDING`. It becomes a trusted terminal only after the
typed T9 receipt, truth-preserving locator and local pointer are all reread and
verified. The planner never creates those surfaces.

Residual `needs_user`, damaged evidence, and archive-pending items stay listed with
their original dates while unrelated later dates continue. Final acceptance requires
a fresh planner replay with `processable_pending_count=0`; otherwise return every
pending date, capture ID, status, reason, and next action from `final_gate`.

When the user explicitly asks to delete current `needs_user` residuals, do not use
`resolve-needs-user`, `mark-result`, a temporary filter, or physical ledger deletion.
Replay the plan, group exact IDs by the latest PARTIAL attempt of their own frozen
set, and invoke `retire-needs-user` once per complete non-success set. Later unrelated
same-date batches do not replace that capture-bound set. Replayed `user_removed` items
remain auditable under `administrative_terminal_excluded` and cannot re-enter a
formal batch or count as formal success.
