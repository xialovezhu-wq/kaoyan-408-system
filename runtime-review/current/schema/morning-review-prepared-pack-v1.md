# Morning Review Prepared Pack v1

## Purpose

`scripts/morning_review_prepared_pack_408.py` creates an immutable delivery
sidecar for a 408 morning-review queue. Its purpose is to move question
construction, source checks, answer-key checks, distractor checks, feedback
preparation, and first repair preparation before the learner sees the first
question.

The sidecar is not canonical learning truth. It is not any of the following:

- the immutable scheduling queue;
- the morning session event ledger or state projection;
- the canonical review ledger;
- a fast capture;
- a formal wrong-question node, relation, index, or mastery update.

Pack preparation therefore has:

```text
learner_evidence_write_count = 0
formal_write_count = 0
```

## Control-plane ownership

The learner-facing start request is owned by
`scripts/start_morning_review_408.py`, not by the current-question hot path. Its
ordered boundary is:

```text
active/paused session gate and canonical closeout
-> dated action queue
-> unique Guardian-owned preparation job
-> four-item MCP evidence chunks
-> resumable Sol author chunks and independent Sol verifier chunks
-> immutable prepared manifest
-> unique session
-> first answer-safe display receipt
```

The learner-facing control entry only reads preparation status. It reuses a named same-date job and may explicitly rebind a pre-model
job to a newer preparation contract. It never deletes historical packs, sessions, or
receipts. Once a display receipt exists, every learner answer belongs to
`managed_408_current_turn.py answer-current-and-next`; that hot path cannot generate
or verify a pack.

Both preparation stages request `model=gpt-5.6-sol` and
`reasoning_effort=max`. Requested identity and runtime-confirmed identity are separate
fields. Luna is not a pack author, verifier, current-item worker, Capture processor,
or later background analyzer.

## Structured model output

`schema/morning-review-preparation-model-output-v1.json` is the strict Codex CLI
response schema. Every object, including nested mappings and union branches, closes
unknown fields with `additionalProperties=false` and requires every declared
property. Its branches cover one author draft, one bounded `needs_source` result, or
one verifier result. CLI schema acceptance is necessary but not sufficient:
`prepare_next_morning_408.py` still submits the returned draft and verification to
the deterministic normalizer, path/hash validator, answer-isolation checks, complete
item-order checks, and independent actor/execution checks before publication.

An MCP, author or verifier process failure publishes only an answer-safe, content-addressed
failure receipt. It records `error_code`, stage, requested model and effort, return
code, retryability, a sanitized error type/summary, and zero learner/formal writes.
It must not include queue contents, question or answer text, a private evaluator,
the model prompt, or raw stderr.

The complete item order is split into contiguous chunks of at most four. Every chunk
is grounded through the isolated `cs408_morning_preparation_bundle` MCP tool before a
model call. Chunk outputs are resumable, but nothing is learner-visible until all
author and verifier chunks pass the full-queue deterministic validator and one pack is
atomically published. A single model attempt may run for at most 1,800 seconds;
retryable failures enter `retry_wait` and preserve completed chunks.
Rejected author candidates are retained only in the private content-addressed job
store with a safe validation category. An independent verifier rejection does not
publish a partial pack. One explicit bounded recovery may rewrite only the rejected
chunk using failed-check labels while retaining all accepted chunk receipts; repeated
rejection remains fail closed.

## Two independent inputs

Publication requires two separately identified inputs.

The author draft uses:

```text
schema = morning_review_prepared_pack_draft_v1
```

It binds the queue SHA-256 and prepares every selected item in queue order. Each
item contains:

- stable `item_id`, `source_id`, and `item_kind`;
- answer-safe `source_label` plus `source_id_provenance`, keeping a human source title separate
  from the machine transaction identity;
- an answer-safe current-question target;
- three standalone A/B/C/D question surfaces: main, guided, and spaced repair;
- one private grading capsule for each surface role;
- one or more repository-relative evidence references with SHA-256.

The independent verification uses:

```text
schema = morning_review_prepared_pack_verification_v1
```

It binds the normalized draft, all three public-surface hashes, and all three
private grading-capsule hashes for every item. Author and verifier actor IDs must differ, and author and
verifier execution IDs must differ. Every item must affirm all of these gates:

1. standalone surface;
2. exactly four options;
3. mutually exclusive options;
4. one unique best answer;
5. source grounding;
6. same target;
7. answer isolation;
8. sufficient reasoning rubric;
9. safe feedback.

The script mechanically verifies structure, hashes, bounds, path safety, option
distinctness, and common answer leaks. The semantic assertions remain verifier
attestations; actor labels are not cryptographic proof of human identity.

## Atomic publication

Publication is all-or-nothing. A pack with one missing item, source, key,
rubric, verifier row, or passing check is rejected before a ready manifest is
published.

The queue-to-pack source contract is checked against the exact
`CurrentQuestionContext v1` stable-ID grammar. `source_id_provenance=formal_node_fallback` is
valid only when `source_id` equals the queue's one canonical `formal_node_id`; the original human
title remains in `source_label`. New publication never repairs an ambiguous queue in place. An
invalid source without one exact formal-node fallback fails before any pack object is written.

The content-addressed manifest uses:

```text
schema = morning_review_prepared_pack_manifest_v1
interaction_mode = frozen_prepared_pack
binding_status = pack_bound
```

For a generation-aware queue the manifest also binds `generation_id`,
`generation_input_sha256`, `eligible_due_action_set_sha256`,
`formal_due_source_sha256`, and `scheduling_policy_version`. Every manifest item
copies the exact queue `action_id`, `stage`, and `trigger_event_id` when present,
plus the queue SHA-256 and one common `pack_commitment_sha256`. The commitment is
recomputed from the generation, due-action set, queue hash, ordered identities,
all public-surface commitments, all private-grading commitments, evidence, and
verification rows whenever the manifest is opened. A queue hash alone is only
`queue_bound`; it is not evidence that any item is `pack_bound`.

The default managed root is:

```text
wiki/study_vaults/408-full/state/morning-review-prepared-packs/
```

Public and private objects are physically separated:

```text
objects/surfaces/<sha256>.json
private/evaluators/<sha256>.json
private/verifications/<sha256>.json
objects/manifests/<sha256>.json
pointers/<pack_id>.json
```

Managed directories use mode `0700`; JSON objects use mode `0600`. Symbolic
links, absolute evidence locators, `..` traversal, files outside the repository,
and hash drift fail closed.

## Public and private boundaries

The public surface contains only:

- `item_id`;
- `surface_role=main|guided|repair`;
- stem;
- exactly A/B/C/D option text;
- response instruction;
- `answer_safe=true`.

It cannot contain the correct option, decisive reason, feedback, or grading
rubric. `show-item` returns only this public object.

The private capsule contains:

- the unique correct option;
- decisive reason;
- accepted reasoning points;
- controlled first-break candidates;
- feedback for every controlled first-result class;
- optional prepared boundary checks.

The guided and repair branches are not private prompt strings. Each is a separate
public A/B/C/D surface plus a separate private grading capsule, with independent
surface and grading commitments. The manifest also binds one content-addressed
verification object; cold audit rechecks its digest, identity separation, all item
hashes, and every semantic gate.

`open_grading_context()` is a private in-process compatibility reader with no
public CLI. A trusted UI/adapter may use it only to publish the current opaque
`current-question-evaluation-capsule-v1`; the full evaluator must never enter the
main-model context, learner ledger, renderer, session event, formal repository,
or user-visible question surface.

## Session binding

The normal start command is:

```text
python3 scripts/start_morning_review_408.py --repo . \
  --date <Asia/Shanghai-YYYY-MM-DD> \
  --preparation-job <existing-job-id>
```

The lower-level session binding remains:

```text
python3 scripts/morning_review_session.py start \
  --repo . \
  --queue <queue-relative-path> \
  --prepared-manifest <manifest-relative-path>
```

The start command validates the complete pack before any session write. The
`session_started` event and derived state retain only:

- pack ID, manifest reference, and manifest SHA-256;
- interaction mode;
- each main, guided, and repair surface SHA-256;
- each main, guided, and repair grading-capsule SHA-256.

They never retain stems, options, answer keys, feedback bodies, or the learner's
raw response. This creates a durable chain from session/item to the exact
delivered-question and grading commitments without putting protected content in
learning truth.

Source compatibility is a producer-consumer gate, not merely a display concern. Publication,
cold audit and pre-session start each verify that every manifest `source_id` can be consumed by
`CurrentQuestionContext v1`. Cold readers may still open an older immutable manifest to diagnose
it, but an older pack with a human title in `source_id` cannot start or record a new answer. Its
explicit legacy recovery must use a separately verified unique formal-node binding; historical
bytes are never rewritten.

Immediately before the `session_started` append, the session writer recomputes
generation freshness while holding the same runtime lock used for that append.
If an eligible action or another generation-defining source changed, start fails
with `queue_generation_stale_pre_session`; the control plane retains the old queue,
job, and pack, appends a stale disposition, and prepares a complete replacement.
After `session_started`, the pack stays frozen and late actions are deferred rather
than inserted.

## Frozen live path

For a healthy bound session, the live turn is:

```text
exact current public surface + opaque private grading capsule
-> CurrentQuestionContext v1
-> freeze current-question feedback
-> managed_408_current_turn.py answer-current-and-next
-> append the same-item private buffer while nonterminal
-> terminal: seal final package -> one Capture -> reopen durable receipt
-> retained display, one receipt-gated successor, hold, or session complete
```

The displayed public-surface SHA-256 and the current learner choice are mandatory
ephemeral inputs to a prepared recorded turn. Before any session, review, or capture
write, the current-question adapter verifies the displayed main-surface commitment
and derives `choice_result` from the bound private key, then publishes only an opaque
capsule locator/hash and the controlled result. `managed_408_current_turn.py` verifies
that binding; the session writer reuses it and does not reopen the old prepared-pack
grader. A caller-supplied choice result is only an assertion and must match. `BLANK`
is the controlled no-choice value. Neither the
learner's A/B/C/D value nor the ephemeral display commitment is copied into the
session or canonical ledger.

The current choice can deterministically establish only `choice_result`; it does
not establish `reasoning_result`. Absent reasoning remains `not_observed`. Spoken
reasoning is judged against the prepared reasoning points, and the first real
divergence is recorded separately. A correct option with diverged reasoning may
therefore remain partial or wrong, while an incorrect option can never be written as
an independent or fragile correct result.

Prepared mode keeps the canonical first-answer, hot receipt, terminal finalization,
and feedback-reveal gates. It changes only delivery and grading sources. The current
turn does not read personalization, history, formal tables, another pack item, Luna,
or port 8767. The sole external answer operation keeps the same display receipt for
same-item correction and returns at most one successor after the exact prior receipt
passes the current navigation-frontier check. `current-turn`, `continue-current`,
`next-item`, and the retired direct router are not public learner-turn routes. Guided
and repair surfaces remain distinct and retain their separate capsules.

The healthy item path does not rebuild a queue, generate a question, search a
source, scan history, replay a full ledger, inspect relations, or run a global
audit. Full audits remain session-close or explicit-recovery work.

## Backflow boundary

The canonical review ledger remains the learning-evidence source of truth. `wrong`,
`partial`, `blank`, `uncertain`, verified reasoning breaks, hints, explanations,
restatements, and correction rounds remain only in the same private item buffer. They
create no Capture or pending index until the item is terminal.

Terminal requires the private choice result and root Sol semantic assessment to prove
a correct answer/conclusion, `reasoning_result=sound`, no known contradiction, and
accurate prompt dependence. Then the managed writer seals the complete final package,
creates exactly one `awaiting_sol_formalization` Capture bound to its canonical
SHA-256, and reopens the durable receipt before navigation, hold, or session complete.
A high-confidence unprompted first answer follows the same gate; a correct option with
contradictory reasoning remains nonterminal.

The prepared pack never performs formal intake. Formal nodes, relations,
special-topic chains, Wiki surfaces, and formal mastery are still updated only
during the separately authorized, explicit-date daily curation workflow.

## Commands

Compute normalized authoring commitments:

```text
python3 scripts/morning_review_prepared_pack_408.py --repo . draft-hashes \
  --queue <queue-relative-path> \
  --draft <draft-json>
```

Publish after independent verification:

```text
python3 scripts/morning_review_prepared_pack_408.py --repo . publish \
  --queue <queue-relative-path> \
  --draft <draft-json> \
  --verification <verification-json>
```

Read one public question:

```text
python3 scripts/morning_review_prepared_pack_408.py --repo . show-item \
  --session <session-id> --item <item-id> --surface-role main
```

Cold audit and exact-item microbenchmark:

```text
python3 scripts/morning_review_prepared_pack_408.py --repo . audit \
  --manifest <manifest-relative-path>

python3 scripts/morning_review_prepared_pack_408.py --repo . benchmark-item \
  --session <session-id> --item <item-id> --iterations 100
```
