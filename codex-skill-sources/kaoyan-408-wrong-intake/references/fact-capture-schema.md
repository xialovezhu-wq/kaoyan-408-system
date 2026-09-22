# 408 complete conversation package index

Managed non-terminal turns produce only an unsealed private draft. A managed terminal
or explicitly authorized standalone quick intake produces one sealed
`cs408-conversation-package-v1`, exactly one Capture bound to its final SHA, and one
answer-safe local index event after durable receipt reopen.

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

Only a verified Capture reaches `awaiting_sol_formalization`. Ordinary standalone
teaching and non-terminal managed drafts never enter this state. It means only that an
explicit formal Sol turn may consider the Capture; formal identity, taxonomy,
relation, mastery, and whether a formal write is appropriate remain undecided.

No background handoff, producer attestation, report, consumer, model artifact, or
adoption token is emitted. Legacy evidence locators remain read-only compatible.

After a proven formal terminal, archive is an independent receipt-bound operation.
Failure becomes `FORMAL_COMMITTED_ARCHIVE_PENDING`; formal apply is never replayed.
