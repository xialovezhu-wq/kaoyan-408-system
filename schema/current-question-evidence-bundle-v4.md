Current Question Evidence Bundle v4

Purpose

`current-question-evidence-bundle-v4` is the private successor contract for one
verified historical free-response `formal_problem_document` scope. It is not an
A-D display receipt and must not fabricate options, a correct option, or a learner
choice. The answer-safe public Capture continues to store only the content-addressed
evidence-manifest locator and SHA-256.

Source-document boundary

The only supported production source is the contract-bound regular, non-symbolic-link
OOXML DOCX. The producer reads and hashes the bytes through one file descriptor,
checks the package member closure, parses `word/document.xml` and its local image
relationships, and verifies the document title, selected section heading, next
section boundary, block range, render-page range, image captions, image magic, and
every embedded-image SHA-256. Caller-supplied titles, block/page ranges, hashes,
Capture IDs, release IDs, activation IDs, or authority are not accepted.

One invocation selects exactly `part1` or `part2`. The two scopes share the same
private source-document object but have independent content, evidence manifests,
Capture payloads, background handoffs, and producer receipts. A merged scope fails
before publication.

Top-level contract

The bundle contains the existing private question/evaluation/assessment/provenance
fields plus exactly these v4 additions:

```text
capture_kind = formal_problem_document
question_mode = formal_problem_document
source_document
document_scope
document_content
interaction_trace
model_call_count = 0
luna_call_count = 0
formal_write_count = 0
```

`source_kind` is `formal_problem_document`. `source_id` is
`formal-problem-document:<full-source-document-sha256>:<scope_id>`.
`context_id` and the public Capture `source_facts.details_id` are both
`FPDOC-<first-24-uppercase-hex-of-scope_sha256>`.

Document scope

`document_scope` contains exactly:

```text
scope_id
scope_title
scope_title_sha256
source_document_sha256
scope_sha256
render_page_start
render_page_end
block_start
block_end
question_sha256
dialogue_sha256
learner_answer_sha256
first_break_sha256
final_correction_sha256
ordered_embedded_image_sha256s
```

`scope_sha256` binds the scope identity, title, full source hash, page and block
ranges, all ordered content blocks, and the ordered embedded-image hash collection.
Every other content digest is recomputed on reopen. A changed scope under the same
source hash and scope ID conflicts with the existing Capture payload; it cannot mint
a different raw Capture ID.

Complete dialogue and real answer

`interaction_trace.schema` is `formal-problem-document-dialogue-v1`. It preserves
all verified learner and assistant turns from the selected OOXML section in order,
including text blocks and content-addressed image references. Every turn and the full
trace have SHA-256 bindings. `learner_evidence.answer_text` and `reasoning` contain
the verified learner turns; `assessment.first_break` contains the explicit source
table entry; `frozen_assistant_feedback.text` contains the final retained correction.
All are private and independently hash-bound by `document_scope`.

Attachments

`attachment_objects` is ordered and contains the source DOCX first, followed by all
images in source order. Each row contains exactly:

```text
ordinal
role
object_ref
sha256
media_type
detected_format
byte_count
source_part
label_sha256
```

The first role is `source_document`, with OOXML DOCX media type and
`current-question-attachment://sha256/<source_document_sha256>`. Image roles are
`question_image`, `solution_image`, `learner_code_image`,
`interaction_diagram`, or `interaction_diagram_corrected`. At least one question
image and solution image are required. Stored private objects use content-addressed
names and mode `0600`; the original host path is never persisted.

Publication and handoff

Publication writes the verified DOCX/image objects, v4 bundle object, and v1
answer-safe manifest before the release-neutral public Capture. The background
handoff is published pending before the Capture commit and becomes `ready` with
`completion_kind=document_complete` only after the hot Capture receipt exists. It
binds the full dialogue hash and has no trace supplement. No model, Luna, formal
writer, release, activation, or Dispatcher authority is invoked by this producer.
