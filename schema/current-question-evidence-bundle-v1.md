Current Question Evidence Bundle v1

Status

This is a read-only historical schema for pre-2026-08-30 objects. New managed items
use one `cs408-conversation-package-v1` private buffer and seal only its final terminal
revision. New standalone teaching is read-only; explicitly authorized quick intake
also uses the final conversation package. No new item publishes an immutable
first-answer bundle, background handoff, observation, or trace supplement.

The remainder of this document defines historical object validation only. Its Capture
and recovery directions are not active routing authority.

Storage

The default private root is:

`~/.codex/study-intake-preprocessor/private/current-question-evidence`

It is configurable for tests and deployment. Directories are mode `0700`;
objects, manifests, attachments, recovery objects, and turn receipts are mode
`0600`. Symlinks are rejected. Nothing in this tree enters Git.

Bundle

The canonical top level is exactly `schema_version`, `context_id`, `request_id`,
`session_id`, `item_id`, `source_id`, `source_kind`, `study_date`, `event_time`,
`timezone`, `source_binding_sha256`, `current_question`, `learner_evidence`,
`evaluation_evidence`, `assessment`, `frozen_assistant_feedback`, `provenance`,
`missing_fields`, `attachment_objects`, and `formal_write_count=0`.

Large attachments are stored as independent content-addressed binary objects.
The manifest binds each object's SHA-256, MIME type, label, and size. Declared
attachment hashes must exactly match supplied bytes in order and count. The
same attachment rows appear in the bundle, manifest, and provenance.

The bundle JSON limit is 256 KiB. There are at most eight attachments, each at
most 16 MiB. Directories use `0700`; all files use `0600`.

Bundle v2

`current-question-evidence-bundle-v2` preserves every v1 field and adds exactly one
`interaction_trace` field using `current-question-interaction-trace-v1`. The trace is
bounded to 24 events, 2048 UTF-8 bytes per event, and 32768 canonical bytes. It is
private and never copied into the public capture ledger or observation metadata.

The `answer-current-and-next` route normalizes the first-answer trace before prepared
grading and binds that object into the private bundle. Invalid or oversized per-call
traces fail validation.

For an initial `wrong|partial|uncertain`, the bundle remains immutable while the same
display receipt stays active for correction. The runtime privately accumulates the
bounded current-item trace. Once correction is correct, it publishes one
`current-question-trace-supplement-v1` bound to the original capture, context, item,
evidence-manifest hash, and teaching-resolution receipt. The supplement does not
rewrite bundle v2 or the public capture.

Public locator

The capture ledger stores only:

- `kind=current_question_evidence_bundle_v1`
- `locator=current-question-evidence://sha256/<manifest_sha256>`
- `sha256=<manifest_sha256>`

It never stores the private root, an absolute path, full question, learner
reply, answer, evaluator, or feedback.

For bundle v2 the public manifest still uses the same answer-safe locator and binds
the private object's actual `bundle_schema_version`; it does not expose trace text.

Recovery

First-answer/session receipt recovery and capture recovery are separate.
`recover-current-receipts` never commits capture. `recover-current-capture`
never repeats an outcome or session write. Stable bundle publication failure
persists the canonical bundle plus staged attachment rows when possible; if
even recovery storage is unavailable, the caller retries the same original
input using the returned idempotency key and context hash.

Capture recovery ensures the private background handoff before commit. For an
original `wrong|partial|uncertain` display it repairs or establishes the display
lifecycle and returns `display_lifecycle_repaired=true`, preserving one first outcome
and one capture across the later same-display correction.

Private grader input is published separately. The CLI accepts it only from
stdin via `publish-evaluation-capsule --capsule-json -`; literal private JSON
in argv is rejected.
