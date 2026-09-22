# Variant Demand Planner v1

## Purpose

`scripts/variant_demand_planner_408.py` is the bounded cold-path bridge between
recent learner evidence and `variant_source_pipeline_408.py`. It runs only in a
user-triggered dated nightly closeout. It does not run before an ordinary answer,
does not author a question, and has `formal_write_count=0` throughout.

## Deterministic demand gate

A canonical review outcome can create one demand only when all of these hold:

- the selected `observed_date` equals the nightly study date;
- `event_kind=outcome` and `first_result` is `partial`, `wrong`, or `uncertain`;
- the current event mapping proves exactly one formal original-question identity;
- the outcome is not a variant attempt;
- the projection's effective original-attempt date is less than seven days before
  the current Asia/Shanghai closeout date;
- no `verified_ready` catalog entry already matches the same anchor, knowledge
  point, and mechanism.

When both the event mapping and `question_index` provide non-empty knowledge-point
or mechanism sets, the selected target must come from their non-empty
intersection. An empty intersection is recorded as `knowledge_point_conflict` or
`mechanism_conflict`; it is never resolved by preferring either source or by the
knowledge-point mechanism fallback. A conflict demand remains `source_gap` and
`prepare-demand` rejects it even if a supplied evidence package is shaped to match
the answer-safe placeholder target. A singleton intersection remains valid, and a
singleton from one source remains valid when the other source has no value.

An independent-correct event, a variant event, an event without exact original
identity, or an original already seven days old cannot create demand. The planner
deduplicates by anchor plus point plus mechanism, keeps at most eight trigger-event
commitments per visible record, defaults to three demands per night, and rejects a
limit above eight. It never pads the plan.

## Verified inputs

Before planning, the tool verifies the root personalization manifest and the exact
manifest-bound hashes of:

- `event_mappings.json`;
- `question_index.json`;
- `variant-catalog.json`;
- the canonical `review-loop/events.jsonl` source.

It deep-validates every original source selected for demand and fully rebuilds the
verified variant catalog before trusting it. The standard protected evidence
inventory is bounded to 256 files of at most 128 KiB each under
`state/linked-practice/variant-authoring-evidence/`.

## Answer-safe durable artifacts

The planner writes only rebuildable workflow state under
`state/linked-practice/variant-demand/`:

- `plans/VDP-*.json`: content-addressed bounded demand plan;
- `receipts/VDP-*.json`: plan hash and source commitments;
- `current.json`: exact current plan projection;
- `events.jsonl`: append-only, hash-chained workflow events.

Artifacts may contain controlled formal IDs, point and mechanism IDs, event IDs and
hashes, source manifest locators and hashes, prepared request locators and hashes,
and structured actor-identity hashes. They never contain a question stem, options,
an answer, a solution, a full response, feedback, or `first_break`.

## Source gaps

The plan keeps a selected demand visible as `source_gap` instead of improvising
material when the original surface, exact target, protected authoring evidence, or
sealed authoritative solution is absent or invalid. Multiple matching evidence
packages are an ambiguity, not a tie to be resolved silently.

Adding a protected evidence package does not mutate a prior plan. Rerun `plan`, or
pass that exact package to `prepare-demand`; the command revalidates its original
surface, solution hash, target, and prebound author/verifier identities.

## Recoverable nightly workflow

After the personalization index is rebuilt for the current local date:

```bash
python3 scripts/variant_demand_planner_408.py --repo . plan \
  --date STUDY_DATE --as-of CURRENT_LOCAL_DATE --limit 3
python3 scripts/variant_demand_planner_408.py --repo . status
```

For each `ready_for_prepare` demand, use a protected evidence package whose
`variant_actor_execution_identity_v1` author and verifier were assigned before
request issuance:

```bash
python3 scripts/variant_demand_planner_408.py --repo . prepare-demand \
  --demand-id VDM-... --evidence PROTECTED_EVIDENCE.json
```

The command delegates to `variant_source_pipeline_408.py prepare-generation`, so
the existing mode-0600 HMAC issuance key, request content addressing, and structured
identity chain remain authoritative. A dedicated author creates only protected
staging output; a different verifier produces the independent attestation. Then run
`finalize-verified`, which publishes only the question surface and verification
chain.

After publication, record a terminal event only if the deep-validated catalog has
one exact matching entry:

```bash
python3 scripts/variant_demand_planner_408.py --repo . mark \
  --demand-id VDM-... --outcome finalized \
  --reason verified_variant_published --variant-source-id VAR-... \
  --idempotency-key finalize:VDM-...
```

`source_gap` and retryable `failed` marks use fixed reason codes. Repeating an
idempotency key with the same payload returns the existing event; reusing it for a
different payload fails. After all finalized variants, rebuild the personalization
index once more so the next hot-path slice can select them.

Formal batch completion is not rolled back if this derived workflow fails. Resume
from `status`; never replay formal apply and never generate a variant in the
single-question hot path.
