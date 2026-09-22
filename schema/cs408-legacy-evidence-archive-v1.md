# CS408 legacy evidence archive v1

This contract archives every successful historical Capture without a verified
`cs408-conversation-package://...` archive. It is archive-only compatibility and
never upgrades legacy evidence into a canonical conversation package.

## Command and authorization

```text
python3 scripts/archive_408_conversation_package.py \
  archive-legacy-evidence-after-formal-commit \
  --repo <repo> \
  --capture-id <CAPTURE_ID> \
  --decision-root <root> \
  --evidence-root <root>
```

The default invocation is a zero-write preview. It replays the canonical ledger,
derives the capture/result/closeout rows, resolves the content-addressed Sol decision
and terminal, reopens the exact legacy manifest/object/attachments, and returns one
content-derived authorization value. Mutation requires both `--apply` and that exact
value as `--authorization`. Any source, decision, terminal, or proof-byte drift
changes or invalidates authorization before a write.

The caller cannot provide the evidence locator/SHA, outcome, formal IDs, terminal
locator/SHA, study date, package ID, or conversation completeness claim.

## Archived package

The fixed destination is:

```text
/Volumes/T9-Data/01_408/资料库/原始会话资料/<study_date>/<legacy_package_id>/
```

`manifest.json` has schema `cs408-legacy-evidence-archive-package-v1` and binds:

- `archive_package_type=legacy_current_question_evidence`;
- `canonical_package=false`;
- `conversation_complete` and its verified source-evidence basis;
- the legacy evidence locator, schema, manifest SHA, outcome, formal IDs, decision,
  and terminal;
- exact original `manifests/`, `objects/`, and referenced `attachments/` bytes;
- exact canonical capture/result/closeout JSONL lines and decision/terminal JSON
  proof bytes;
- archive-only counters with no formal apply, mark-result, or terminal-binding call.

For v3, `conversation_complete=true` only when the verified trace has no omitted
events or ranges. For formal-document v4 it follows the verified source dialogue's
`complete=true`. No missing turn is invented.

Historical current-question evidence without a content-addressed Sol terminal uses
`current_question_evidence_ledger_terminal`. The full evidence tree is still copied,
but `terminal_locator` and `terminal_sha256` are null. A separate exact ledger
result-event locator/SHA binds the canonical terminal facts. Outcome and formal IDs
come only from replayed capture/result/closeout bytes; no terminal is synthesized.

When no current-question evidence locator exists, the package mode is
`ledger_only_reference_files`. It binds exact capture/result/closeout JSONL lines and
current canonical terminal facts. It copies every safely resolvable stable-ref source
file: repo-relative paths are resolved before fragments, and absolute files are
restricted to `/Users/your-user/Documents/kaoyan-408-details`. Unresolvable refs keep
their exact kind, locator, declared SHA-256 and failure reason. This mode fixes
`conversation_complete=false`, `canonical_package=false`, and `cleanup_count=0`.

## Publication and pointer

The command verifies the canonical T9 sentinel, volume name, and UUID and rejects
lookalike mounts such as `T9-Data 1`. It stage-copies, verifies every path/size/SHA,
atomically publishes, writes and rereads the locator note, writes and rereads the
archive receipt, then writes and rereads
`legacy-archive-pointers/<evidence-manifest-sha>.json` before any local cleanup.

The locator note records `canonical_package: false` and the exact
`conversation_complete` truth flag. The pointer binds the receipt, locator,
archived manifest, source-file inventory, terminal, authorization, cleanup candidates,
and retained files. Evidence readers may reopen only an exact missing manifest,
object, or attachment through this verified pointer and fixed archive path; they do
not scan T9.

Local deletion is allowed only for a private content-addressed file proven to have
one canonical Capture reference and one local manifest reference. Shared objects,
ambiguous ownership, unsafe inventories, and `source_document` objects are retained.
External DOCX/corpus paths are never deletion targets. A failure after formal commit
or during archive returns `FORMAL_COMMITTED_ARCHIVE_PENDING`; retry resumes only this
archive operation.

Ledger/reference mode never deletes repo, session, shared or external reference
files. Its receipt and pointer both record zero cleanup.

Duplicate refs may create multiple archive descriptors for one source file. The
descriptor inventory remains per-reference, while the cleanup partition is normalized
by unique source path. Each unique path is retained once. Resume accepts and
normalizes an already-written duplicate retained descriptor in memory without
rewriting the existing pointer or receipt bytes.
