# 408 standalone question intake v1

## Admission boundary

This is an explicit quick-intake operation, not a routine teaching transaction.
Ordinary standalone questions, image reading, explanations, hints, corrections,
restatements, and direct explanations are read-only and must not call this script or
create a package, Capture, pending index, or formal write.

The route is admitted only when the current user explicitly requests `快速入库` or an
equally explicit quick-intake command. The model-facing teaching worker routes that
request to `kaoyan-408-wrong-intake`; vague learning language, understanding claims,
or a request for explanation is not authorization.

## Input

`scripts/standalone_question_intake_408.py` accepts exactly one JSON object with
`schema=cs408-standalone-question-intake-v1`.

Required fields:

- `request_id`: 3-160 answer-safe identifier characters.
- `study_date`: a real `YYYY-MM-DD` date.
- `authorization`: exactly
  `{"schema":"cs408-explicit-fast-intake-authorization-v1",`
  `"kind":"explicit_fast_intake","user_message":"..."}`. The message must be
  exactly the latest user turn in `conversation`. Its NFKC-normalized content must
  pass the explicit quick-intake command gate.
- `source`: an object with required `source_id` and optional `title`, `year`,
  `subject`, `question_type`, and `locator`.
- `conversation`: a non-empty ordered array. Each row contains `role`, `text`,
  optional `kind`, and optional `observed_at`.

Question evidence:

- `timezone` may be omitted; omission means `Asia/Shanghai`. If present, it
  must be exactly `Asia/Shanghai`.
- `question` may contain the complete text. For the compact CLI form, the same
  field may instead be nested as `source.question`.
- `options` is optional and contains strings or exact `label`/`text` objects.
  The same value may instead be nested as `source.options`. If both forms are
  present they must normalize to identical bytes.
- `question_identity_hint` is optional private metadata. It is request-bound
  and preserved in the package, but never replaces or changes the stable
  identity derived from question, options, and source.
- At least one non-empty `question` or `question_image` attachment is required.
- `frozen_feedback` is optional.
- `attachments` is optional. Each row contains `path` and optional `role`,
  `mime_type`, and `label`. The source file must be a bounded non-symlink regular
  file. Supported roles are those of `cs408-conversation-package-v1`.
- `missing_fields` is optional. Missing solution or explanation evidence does
  not block the intake.

Unknown fields, including `session_id`, display receipts, answer-operation
receipts, and navigation fields, fail closed.

Outside the private package, durable request, Capture, and operation evidence stores
only the authorization schema/kind, `user_message_sha256`, and byte count. The
authorization body is not duplicated outside the private conversation package and
the summary and operation receipt both record `authorization_body_stored=false`.

## Stable identities

The `SQI-...` item identity is the SHA-256 identity of `study_date`, normalized
question text, question-image hashes, normalized options, and normalized source. It does not
depend on a managed session and every standalone package records
`session_id=null`.

Package and Capture identity additionally bind the explicit-fast-intake authorization
kind and `user_message_sha256`. Different request IDs for the same item and identical
authorization reuse the same sealed package and Capture; a changed authorization or
payload cannot silently rebind either identity. The same question on another
`study_date` is a distinct learning episode and receives a distinct identity.

`request_id` is stored only as a SHA-256. Before package creation it is bound to
the canonical private payload hash. Reusing the same request with different
question, source, options, conversation, feedback, attachment bytes, date, or
missing fields fails closed.

The authorization object and its message hash are part of that request binding.
Missing authorization, a message that differs from the latest user turn, or a message
that does not express explicit quick intake fails before package or Capture creation.

## Ordered transaction and recovery

The only valid order is:

1. persist or verify the safe request binding;
2. create and seal the complete private package;
3. reopen the package and verify every manifest-bound byte;
4. append exactly one answer-safe Capture at `awaiting_sol_formalization`, bound
   directly to the final package locator and canonical SHA-256;
5. reopen the exact capture event and verify its package locator/hash binding;
6. write and reopen one deterministic combined operation receipt.

If the package exists but capture is missing, replay reopens the same sealed
package and adds only capture plus receipt. If capture exists but the combined
receipt is missing, replay adds only the receipt. A complete replay returns the
same receipt bytes.

## Storage and exclusions

Complete question, options, conversation, feedback, and attachment bytes exist
only below the explicitly supplied conversation-package root. Repository intake
state stores only safe identifiers, opaque locator, hashes, and zero-write
counters.

The route performs no managed session, display, answer-operation, navigation,
T9-Data, Obsidian, formal Markdown, formal-ID, background model, router, or
background write. Capture commit uses the same bounded hot-writer joint commit and
durable receipt reopen contract as managed terminal finalization.
`formal_write_count=0` and `background_processing=none` are part of the combined
receipt.

Exactly one sealed package and one Capture are permitted for one explicit request.
Replay reuses both identities. This quick intake does not authorize formal intake;
formal curation still requires a separate explicit request. That request runs
canonical `backlog-plan`, then a single-date `start-batch` which selects
`formalization_authorized=true` captures in `awaiting_sol_formalization`; the quick
turn never emits a second authorization event or formal write.

The CLI form is
`python3 scripts/standalone_question_intake_408.py record --input-json INPUT.json`.
`--repo` defaults to the current directory and `--package-root` defaults to the
canonical local 408 conversation-package root. Tests and controlled recovery
may override either path.

The combined receipt publishes `captured_at` in `Asia/Shanghai`, bound to the
canonical capture event commit time, with
`time_quality=capture_event_committed_at`. Replay never derives this time from
file mtime and never changes it.

## Current episode route

New standalone saves use `schema=cs408-study-episode-capture-v1` through the
same CLI. The facade requires `execution_kind=standalone_question` and still
validates the exact `cs408-explicit-fast-intake-authorization-v1` object against
the latest user turn before invoking the shared episode writer. Its complete
payload and receipt are defined in `study-episode-capture-v1.md`.

This current route records correct, screened-correct, prompted-correct, wrong,
and unresolved terminal outcomes as complete private packages and Captures. It
uses the web-enhancement waiting policy, so ordinary local cutoff curation does
not consume it. The older `cs408-standalone-question-intake-v1` path remains
readable and retryable for operations already started under that byte contract;
it is not migrated or rewritten.

## Explicit supplement of a sealed standalone episode

For an explicit “补充入库 / 补充这道题 / 更新这道题” request, use the same `record`
entry with `schema=cs408-standalone-question-supplement-v1`, a new `request_id`,
`capture_id`, `parent_package_locator`, `parent_package_sha256`, exact `new_turns`
and the same authorization object bound to the latest user turn. A normal explicit
quick-intake command also authorizes this schema. Optional fields are new `attachments`,
`frozen_feedback`, and `missing_fields`; omitted missing fields inherit the parent.
Do not supply a new date, source, question or options. Code inherits the original
identity, copies the entire parent dialogue and attachments, and seals a new cumulative
package. Original packages, event bytes and the unique Capture remain intact.

The owner appends one `capture_supplemented` event to the existing Capture ledger.
Identical request/payload retries (including after package, event or receipt failure)
recover the missing step; the same evidence under another request ID is deduplicated.
Use the latest returned package as parent for another supplement. A stale parent,
changed request payload or active formal batch fails closed. Do not disguise a supplement
as a new source or study date. This applies to both pending and already formal episodes.

An explicit formal request freezes `batch.supplement_evidence[capture_id]` and
`supplement_prior_formal_ids[capture_id]`; the last ref is the cumulative package.
Successful `mark-result` consumes only the frozen refs. Existing formal identity and
learning date stay fixed; supplementation does not add a recurrence. Old committed
result retries cannot consume a later revision. No separate pending-state database is used.

Question, option, dialogue and frozen feedback text preserve exact whitespace and CRLF.
Normalization is used only for question identity comparison, never stored evidence.
