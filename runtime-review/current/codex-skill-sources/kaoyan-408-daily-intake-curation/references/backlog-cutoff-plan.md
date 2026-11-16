# Canonical cutoff backlog plan

普通正式请求先运行此原生只读入口。每行 intake_owner 区分本地增强与旧直接编纂；历史名 awaiting_web_enhancement 要交本地增强作者。local_enhancement_capture_ids 保持日期及精确证据集合，start_batch_capture_ids 仅供下文旧写者。一个日期内有两种状态时 mode=separate_local_intake_owners，各自提交后重放清单。已开始的增强事务按原prepared回执恢复，不重新创建Capture或开始第二个批次。

Use the read-only planner before formal work and after every dated terminal:

```text
python3 scripts/runtime_paths_408.py --run intake_fact_capture_408.py --repo . backlog-plan
```

For a cumulative cutoff request, omitting the date uses the current `Asia/Shanghai`
day. `--cutoff-date YYYY-MM-DD` on its own includes all eligible original
`study_date <= cutoff_date`. A request for all quick captures from one day,
including 「今天快速入库的题全部正式入库」, uses `--cutoff-date YYYY-MM-DD --only-today`.

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
3. for each new nonempty group, preserve the exact original date and Capture IDs and run the local quality contract（`<repo>/schema/408-formal-quality-v1.md`）;
4. the validated native apply owns start-batch and binds the quality proof before writing;
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
