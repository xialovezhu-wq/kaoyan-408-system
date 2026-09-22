# 408 complete conversation package index

This contract covers only authorized Captures: managed failed terminals and
standalone questions explicitly requested for quick intake. Campaign screened-correct
and Good successes create no Capture. Ordinary standalone teaching stays read-only.
Each admitted Capture binds a private `cs408-conversation-package-v1` and an
answer-safe local index event. Explicit same-episode supplements append immutable
cumulative packages to that Capture; they preserve its date and do not add recurrence.

The private package root is fixed at
`/Users/your-user/.codex/kaoyan-408-conversation-packages`. Each logical package
contains `manifest.json`, `conversation.json`, `source.json`, `attachments/`,
and `receipt.json`. It preserves complete ordered turns without the historical
24-event or 32-KiB first/last truncation.

Attachment roles are `question_image`, `solution_image`, `explanation_image`,
`user_work_image`, `source_article_image`, and `other_attachment`. Later turns
may supplement attachments. Missing evidence is recorded and does not suppress the
package.

The tracked ledger stores only the package locator, canonical SHA, answer-safe source
and learner facts, explicit gaps, study date/timezone, authorization policy, and
`formal_write_count=0`. It must not contain question, answer, options, full turns,
feedback, attachment bytes, or local paths.

The local state is `awaiting_sol_formalization`. This means only that an explicit
formal Sol turn may consider it. Correctness, formal identity, taxonomy, relation,
mastery, and whether any formal write is appropriate remain undecided.

No background handoff, producer attestation, report, consumer, model artifact, or
adoption token is emitted. Legacy evidence locators remain read-only compatible.

Formal sealing and the writer both reopen the frozen Capture and package to bind
`latest_review_date` to the actual episode date. New `first_done_date` comes from that
episode or an explicit earlier date in the reopened source; redo preserves the
existing first date. Historical backfill never moves the latest formal date backward.
Frozen supplements must all be cited; the latest cumulative package supplies display
assets. A supplement to an already formalized episode updates that same node without
adding another recurrence.

After a proven formal terminal, archive is an independent receipt-bound operation.
Failure becomes `FORMAL_COMMITTED_ARCHIVE_PENDING`; formal apply is never replayed.
For every successful Capture without verified normal or legacy archive proof, the planner reports
`legacy_archive_pending` until a verified legacy receipt, locator and pointer exist;
absence of a conversation-package locator is not a trusted-terminal shortcut.

The read-only backlog planner uses replayed `pending_by_date`, active batches,
PARTIAL batches and canonical item results. Its default cutoff is the current
`Asia/Shanghai` date; an explicit date includes all original study dates at or before
that cutoff. It does not create a ledger or queue. Joint-index proof status is a
diagnostic field, not a selector that may hide canonical pending.

An explicit user deletion of a closed PARTIAL batch's complete `needs_user`
non-success set appends `needs_user_retired`; it never rewrites prior facts. Replay
uses administrative status `user_removed`, preserves formal identity/history, and
excludes the item from future processable/residual backlog counts. This status is not
formal completion, consumption, mastery, skip, or archive eligibility.
