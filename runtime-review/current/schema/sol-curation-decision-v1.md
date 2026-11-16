# Current direct decision contract

Current `sol-curation-decision-408-v2` remains unchanged in version. New batch events
add `knowledge_observation_contract=formal-point-observations-408-v1`; current
complete-package decisions require the typed `knowledge_observations` property.
See `formal-point-observations-408-v1.md` and the current daily-intake-curation Skill.
The historical v1 material below is retained only for named historical recovery.

# Sol curation decision v1

## Purpose

`sol_curation_decision_v1` is the private, content-addressed decision boundary
between one frozen capture, its direct evidence, an optional Luna report, and the
existing batch-size-1 formal intake transaction. It records Sol's independent
review. It never calls Luna, grants formal-write authority, or replaces the capture
ledger, receipt, or WAL.

A failure capture does not bind a separate correct-observation object. Its current
private bundle is `current-question-evidence-bundle-v3`; the bounded
`current-question-interaction-trace-v2` is part of that exact content-addressed
bundle. For an initial `wrong|partial|uncertain`, the corrected cumulative exchange
is separately bound by `current-question-trace-supplement-v1`. A correct-only
`current-question-study-observation-v1` without a failure capture is not a formal
batch item and cannot become a wrong node from a Luna suggestion.

Decision sidecars default to
`~/.codex/study-intake-preprocessor/private/sol-curation`. Directories are `0700`
and objects are `0600`. Formal transaction state remains separately rooted at
`~/.codex/kaoyan-408-intake`.

## Required sequence

1. The user explicitly names one `Asia/Shanghai` study date.
2. `start-batch` freezes one ordered capture list. Sol replays the canonical batch
   and verifies the supplied set SHA-256 over that original order.
3. For the current ordered item only, the external consumer is called once. It
   reads a completed background result and never starts, waits for, polls, or
   retries Luna.
4. Sol reopens the raw capture and its bundle-v3 private manifest and object. Locator,
   manifest hash, object hash, source/date/context binding, permissions, exact
   `dialogue_only|image_question` mode, one-to-eight ordered image roles, actual
   PNG/JPEG/WebP magic, declared/detected MIME, byte count, and SHA-256 must pass. A
   ninth image fails closed. A required image-role gap is `evidence_pending` with
   zero model enqueue. A legacy capture is explicitly `legacy_evidence`; hashes are
   never invented.
5. For bundle v3, validate trace-v2 order, enums, bounds, original/included/omitted
   counts, exact omitted ranges, truncation reason, and full-trace SHA-256. If the
   first result is `wrong|partial|uncertain`, reopen and validate
   the capture-bound `resolved_trace` supplement, teaching-resolution attestation,
   session receipt, and `current-question-background-handoff-v1` at
   `ready/teaching_resolved`. Verify `relearn_required`, `mastery_effect=none`,
   `retention_effect=none`, and `independent_repair=false`. A missing supplement is
   unresolved teaching evidence, not a completed Luna input. Any omitted trace range
   is an explicit gap and is not reconstructed. When the consumer supplies a verified
   pinned deterministic knowledge/history snapshot, verify its content address,
   manifest binding, source cutoffs, and direct candidate refs. An absent runtime
   snapshot remains absent evidence; no locator or schema is inferred.
6. For Luna v2, Sol resolves the content-addressed package and independently opens
   `private/reports/objects/<report_json_sha256>.json`. The JSON root must be
   `study-intake-luna-analysis-v2`; the package does not embed analysis. Sol also
   verifies report refs, Markdown hash, renderer build, evidence hashes, processing
   fingerprint, publication ID, stage state, and quality receipt. The supplied
   preprocessor root must resolve to the active immutable `current` release. Sol
   dynamically loads that release's verifier and independently validates the
   HMAC-sealed unit, fence, receipt, completion, release, frozen input fingerprint,
   and critical-review revised report.
7. Sol records one field decision for every claim in the report's
   `formalization_candidates`, then seals its decision and optional canonical
   package.
8. Exactly one formal item reaches a canonical result. `terminal-binding` reopens
   the exact JSONL result line and verifies formal completion evidence. Only then
   may the external adoption recorder receive the binding.
9. The next frozen capture cannot begin formal publication until the current item
   is `curated`, `already_current`, `needs_user`, or `failed`.

## Decision request

```json
{
  "schema": "sol_curation_decision_request_v1",
  "intended_terminal": "apply",
  "identity": {
    "status": "resolved",
    "mode": "new",
    "formal_id": null,
    "basis": "Sol independently resolved a new candidate"
  },
  "field_decisions": [
    {
      "proposal_sha256": "sha256 of the complete Luna claim identity",
      "field": "main_knowledge",
      "action": "modify",
      "luna_value": "the exact Luna text",
      "sol_value": "the exact value placed in the canonical package",
      "evidence_refs": ["capture.source_facts.source_id"],
      "basis": "direct-evidence basis",
      "counterevidence": "counterevidence or boundary checked by Sol",
      "confidence": "high",
      "unresolved": []
    }
  ],
  "immutable_conflicts": [],
  "canonical_package_path": "/private/scratch/intake-package.json",
  "decision_reason": "answer-safe decision basis"
}
```

`intended_terminal` is `apply`, `already_current`, or `needs_user`. Every field
decision must preserve the actual Luna value and actual Sol value and use exactly
one of `adopt`, `modify`, or `reject`. Its `evidence_refs`, and those of every
immutable conflict, may name only paths that actually exist in the raw capture or
reopened direct bundle. Luna-only prose is not evidence.

All formalization claims must receive exactly one decision. Any immutable conflict,
unresolved identity, or material unresolved adopted field forces `needs_user` and
zero apply. New packages retain `formal_id=null` and `auto_id=true` until RepoLock
allocates the ID. Redo packages require one verified existing formal ID.

For every Luna-proposed classification, historical match, learning break, formal-node
link, or relation, Sol must reopen the proposed direct source and record one of
`adopt`, `modify`, or `reject`. Neither a Luna evidence ref nor deterministic-snapshot
membership is sufficient by itself. A truncated interaction trace is an explicit
gap; Sol and Luna must not reconstruct omitted learner speech.

## Luna isolation

Only `ready` with `pipeline_status=two_pass_ready` is consumable. The package,
consumer, analysis receipt, and critical-review receipt must all independently bind
`gpt-5.6-luna` and `reasoning_effort=max`. Single-stage or degraded output is a
recovery checkpoint only. Any batch, set, payload, manifest, bundle, package, report,
renderer, processing, authority, or quality drift is quarantined as
`rejected_report`: the adoption token and claims are cleared, while Sol continues
from the raw capture and direct bundle. Raw capture or direct-bundle corruption fails
closed. A missing or non-consumable report never delays formal curation.

## Sealed entities

The decision binds repository identity, date, batch attempt, original ordered
position, capture-set hash, payload and capture hashes, stable evidence, direct
manifest/bundle hashes, the bundle-v3 trace-v2 disclosure, required background-handoff,
resolved-supplement, and teaching-resolution bindings, any verified pinned-snapshot
binding supplied by runtime, Luna package/report/quality hashes when valid, all field
decisions and conflicts, identity, intended terminal, and `formal_write_count=0`.

An apply decision emits a separate content-addressed canonical package. The original
scratch package is not edited. An already-current decision first creates a private
verification sidecar binding the exact Sol decision and current formal row/card
hashes. `terminal-binding` rechecks that state.

## Commands

```bash
python3 scripts/sol_curation_decision_408.py --repo . seal \
  --study-date YYYY-MM-DD --batch-id BATCH \
  --capture-set-sha256 SET_SHA --capture-id CAPTURE \
  --consumer-result /private/consumer-result.json \
  --decision-request /private/sol-decision-request.json \
  --preprocessor-root ~/.codex/study-intake-preprocessor/current

python3 scripts/sol_curation_decision_408.py --repo . seal-already-current \
  --decision-sha256 DECISION_SHA --formal-id FORMAL_ID

python3 scripts/sol_curation_decision_408.py --repo . terminal-binding \
  --decision-sha256 DECISION_SHA
```

For `curated`, terminal verification requires a committed normal receipt and WAL
that bind the sealed canonical package. For `already_current`, it requires the
decision-bound verification sidecar. The item-result commitment is the SHA-256 of
the exact raw JSONL line, including its newline, not merely a reserialized object.
The output separates `sol_review_status=sealed` from
`formal_curation_status=<terminal>` for the read-only workbench.

## Failure isolation

Luna availability and adoption-observability never gate formal publication. A
preflight failure writes no formal state. A committed formal receipt whose
postcommit closeout failed is resumed by its original receipt and never reapplied.
Failure to record an adoption sidecar cannot invalidate or replay the capture
result, formal receipt, WAL, or batch closeout.
