# Legacy evidence archive-only recovery

Use this branch after canonical replay shows any successful terminal without a
verified `cs408-conversation-package://...` archive.

The backlog planner must keep the item under `legacy_archive_pending`, reason
`FORMAL_COMMITTED_ARCHIVE_PENDING`, until all three durable surfaces verify:

1. `cs408-legacy-evidence-archive-receipt-v1` in the T9 package;
2. `wiki/sources/raw_archives/<legacy_package_id>.md` with
   `canonical_package: false` and the source-proven `conversation_complete` flag;
3. the local manifest-keyed legacy archive pointer.

Planner verification and every pointer resume must also reopen the current canonical
capture/result/closeout/decision/terminal bytes, recompute the exact authorization
and evidence reference count, and reject any drift before cleanup.

Run the archive command once without `--apply`. This preview writes nothing and
derives its authorization from the exact Capture, result, closeout, decision,
terminal, legacy manifest, bundle and referenced attachment bytes. Then rerun the
same command with `--apply --authorization <preview value>`.

The caller supplies only repo, Capture ID, decision root and evidence root. Do not
pass or infer a replacement evidence locator, terminal, outcome, formal ID, study
date, package ID, or completeness claim. The archive package is explicitly legacy
and `canonical_package=false`; it preserves exact evidence and proof bytes without
creating missing turns.

For v3, completeness requires zero omitted trace events. Formal-document v4 uses its
verified complete source dialogue. Other legacy shapes remain incomplete unless the
source itself proves completeness.

If current-question evidence exists but predates content-addressed Sol terminal
objects, use `current_question_evidence_ledger_terminal`. Reopen and archive the full
evidence tree, but derive outcome and formal IDs only from exact canonical
capture/result/closeout lines. Keep terminal locator and SHA explicitly null, record
the ledger result-event locator/SHA separately, and never synthesize a terminal.

If there is no current-question evidence locator, use
`ledger_only_reference_files`. Bind exact capture/result/closeout JSONL lines and
current canonical terminal facts. Copy every safely resolvable stable-ref source:
repo-relative files are resolved before `#` fragments, and absolute files are allowed
only below `/Users/your-user/Documents/kaoyan-408-details`. Record each unresolved ref
with its original kind, locator, declared SHA and reason. This mode always has
`conversation_complete=false`, `canonical_package=false`, and `cleanup_count=0`;
all repo, session, shared and external reference files remain in place.

Several refs may point to the same source file. Preserve each ref descriptor in the
archive audit inventory, but compute cleanup/retained ownership from unique source
paths. Ledger mode retains each unique path once, keeps cleanup at zero, and resumes
an already-written duplicate descriptor pointer without rewriting pointer or receipt
bytes.

After sentinel/name/UUID verification, the command stage-copies and verifies the
fixed dated 408 path, publishes atomically, rereads the locator and receipt, and
writes/rereads the local pointer before cleanup. Retry only archive. Never replay
formal apply, mark-result, terminal-binding, or batch closeout.

Every archive, locator, intent and pointer path component must remain inside its exact
authorized root and be free of symbolic links. Archive verification requires an exact
file-and-directory tree; extra files, directories, links or special files fail closed.

Cleanup is conservative. Delete only content-addressed private bytes with proven
exclusive Capture and manifest ownership. Keep shared objects, ambiguous inventories,
`source_document` objects, and every external DOCX/corpus source. Evidence readers
may reopen an exact missing manifest/object/attachment only through the verified
pointer; no T9 scan or path guessing is allowed.
