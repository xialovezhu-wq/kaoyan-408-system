# CS408 complete conversation package v1

New private root:

```text
/Users/your-user/.codex/kaoyan-408-conversation-packages/YYYY-MM-DD/PACKAGE_ID/
  manifest.json
  conversation.json
  source.json
  attachments/
  receipt.json
```

`conversation.json` preserves every observed user/assistant turn in order and sets
`truncated=false`. New packages do not use the historical 24-event or 32-KiB
first/last projection. A high safety ceiling rejects rather than silently omits.

Managed lifecycle

A managed morning item has one stable package identity across its first answer,
hints, explanations, restatements, and all correction rounds. Before the item is
terminal, every immutable revision is a private `buffering` revision. Buffering is
not Capture admission: it creates no Capture, no pending-formal index row, and no
formal write.

Only a terminal verdict that binds a correct option/conclusion, sound reasoning, no
known contradiction, accurate prompt dependence, the full interaction trace, and the
frozen feedback may seal the final revision. Seal is one-way. The final canonical
SHA-256 covers the complete conversation, source, attachment manifest, missing fields,
and terminal-verdict binding.

`source.json` preserves the complete current surface, options, learner evidence,
evaluation source and source identity. It is private and never enters formal Markdown.

Attachment roles are `question_image`, `solution_image`, `explanation_image`,
`user_work_image`, `source_article_image`, and `other_attachment`. Later turns may
add attachments. Optional solution/explanation/media gaps remain in `missing_fields`
and do not block buffering. A missing terminal-required answer/conclusion binding,
semantic reasoning verdict, contradiction check, prompt-dependence check, complete
trace digest, or frozen-feedback digest blocks final seal and Capture admission.

The manifest binds JSON and attachments by relative path, role, MIME, byte count and
SHA-256. The fast path writes only local SSD and returns
`buffering` for a nonterminal managed revision. A sealed terminal managed package, or
a sealed standalone package admitted by explicit quick-intake authorization, may then
become the stable evidence for exactly one `awaiting_sol_formalization` Capture.
Package seal alone does not create a pending index. Every path keeps
`formal_write_count=0` and `background_processing=none`.

Capture binding

The Capture stable evidence reference must equal the sealed final package locator and
canonical SHA-256. The durable Capture receipt must be reopened and verify that exact
binding before managed successor publication, last-item session completion, or an
explicit hold/close. A session item has at most one Capture. Recovery reuses the same
package and Capture identity; it cannot attach a second Capture or replace the final
package hash.

The active managed path has no first-answer Capture plus later trace supplement. The
final package itself includes every correction and attachment. Historical package
revisions and supplements remain byte-immutable and read-only compatible.

Historical evidence locators stay read-only. Migration recursively copies existing
user evidence trees `objects`, `attachments`, `manifests`,
`answer-display-lifecycles`, `answer-operations`, `evaluation-capsules`,
`formal-problem-document-receipts`, `recoveries`, `trace-supplements`, `turns`, and
`observations` into local `legacy-verified/`, preserving every relative path, byte and
SHA. `background-handoffs` and `knowledge-snapshots` are explicitly reported as
`retired_excluded` and are never silently omitted.

After a successful formal terminal, archive verifies the canonical T9-Data sentinel,
stage-copies to `01_408/资料库/原始会话资料`, verifies tree and manifest, atomically
publishes, writes and rereads the Obsidian locator note, writes a receipt, then leaves
a local archive pointer after heavy-byte cleanup.

The archive command independently reopens the content-addressed Sol terminal object;
the caller cannot choose its outcome or omit a formal ID bound by that proof. An
immutable local archive intent fixes `archive_verified_at` and locator bytes before
copy. After archive receipt and locator validation it writes a `cleanup_pending`
pointer before deleting any local heavy bytes; retry resumes from that pointer.

Future reads use `archive_408_conversation_package.py reopen-verify` for exactly one
PACKAGE_ID and explicit manifest-bound relative paths. It rereads the locator note,
T9 sentinel, fixed 408 archive root, manifest-file SHA, canonical package SHA and all
manifest rows. It never scans the volume or enumerates unrelated packages.

Successful historical Captures that have only a verified
`current-question-evidence://` locator do not fabricate this v1 package. They use the
separate archive-only contract in `schema/cs408-legacy-evidence-archive-v1.md`, remain
`canonical_package=false`, and stay `FORMAL_COMMITTED_ARCHIVE_PENDING` until the
legacy receipt, locator, and local pointer all verify.
