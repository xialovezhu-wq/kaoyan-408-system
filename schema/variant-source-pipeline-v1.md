# Variant Source Pipeline v1

## Purpose and safety boundary

The pipeline turns an unscheduled `variant_request` into a selectable source
only after independent authoring and verification. It never infers an answer or
distractors from a safe node summary and never writes a formal node or relation.

Nightly generation demand is selected separately by
`variant-demand-planner-v1.md`. That planner can call `prepare-generation`, but it
cannot weaken or replace any request, identity, HMAC, source, or attestation gate in
this pipeline.

## Stage 1: generation request

`variant_authoring_evidence_v1` must bind a verified answer-isolated original
question manifest and a different, durable authoritative solution file by
SHA-256. Its evidence scope is
`full_question_and_authoritative_solution`. The generated
`variant_generation_request_v1` carries locators and hashes, not solution text,
and requires a changed A/B/C/D surface with the same target.

The evidence must also preassign two structured
`variant_actor_execution_identity_v1` records:

- `author_identity`, with role `variant_author`;
- `verifier_identity`, with role `variant_verifier`.

Each identity contains both an actor ID and an execution ID. Both IDs must be
different across the two roles. The verifier cannot be invented at finalize
time by changing one display string.

`prepare-generation` is the only request-issuance boundary. It creates a local
mode-0600 issuance key when needed, MACs the complete request (including both
preassigned identities and every source hash), and persists the request at:

`state/linked-practice/variant-work/<request-id>/requests/<request-sha256>.json`

The path is content-addressed by the exact persisted JSON bytes. The issuance
key is never returned and must never be committed. `finalize-verified` rejects
an otherwise well-formed request when it is copied outside this managed path,
renamed, edited, unsigned, signed by another key, or created without
`prepare-generation`.

`validate_authoring_evidence()` is a read-only adapter for the nightly planner. It
performs the same original-source, sealed-solution and identity checks used to
rebuild a request, then returns only answer-safe hashes and controlled identifiers;
it never returns solution content or the sealed-solution locator.

## Stage 2: independent verification

The authored question surface is checked by a verifier whose identity differs
from the author. `variant_independent_attestation_v1` binds the generation
request hash, question-surface hash, original-manifest hash, authoring-evidence
hash, sealed-solution hash, both preassigned execution identities, and a
content-addressed authoring-chain commitment. It also carries affirmative
checks for:

- answer isolation;
- one unique answer;
- valid distractors;
- the same knowledge/mechanism target;
- a genuinely changed surface.

Before accepting that attestation, finalize reopens the prepare-issued request,
verifies its MAC and exact content-addressed path, rebuilds the expected request
from the original `variant_authoring_evidence_v1`, and revalidates the current
original question manifest and sealed solution files against their prepared
SHA-256 values. Any source drift is a hard failure. A self-authored request plus
a self-authored attestation is therefore not a finalize input, even if their
displayed actor strings differ.

For Markdown surfaces, the local lexical gate rejects common Chinese and English
answer disclosures in addition to the attestation checks. For image surfaces, that
lexical gate does not inspect pixels; answer isolation therefore depends on the
independent verifier's bound attestation and must not be reported as a local text-scan result.

## Stage 3: durable source and catalog

Only after the attestation passes does the pipeline publish:

- one question-only `variant-question-01` asset;
- `variant_answer_verification_receipt_v1`;
- `variant_question_source_manifest_v1`;
- a fully rebuildable `variant_source_catalog_v1`.

Durable variant assets live under
`state/linked-practice/variant-sources`. The catalog is a derived sibling
projection named `variant-catalog.json`, is SHA-256-bound by the root
personalization manifest, and is rebuilt by scanning and deeply validating all
durable variant manifests. The selector accepts only `verified_ready` entries
whose anchor, subject, knowledge point, mechanism, source hash, and receipt all
match. A variant outcome updates point/mechanism evidence but not the anchor
original's cooldown.

Catalog rebuild is not a weaker path. It repeats the request MAC, managed-path,
live original/evidence/solution hashes, preassigned actor and execution IDs,
attestation commitment, asset hash, receipt, and manifest checks. Deleting the
issuance key or any durable provenance object makes the variant unavailable;
the catalog must not silently grandfather an unverifiable source.

The local issuance MAC proves that this repository's prepare tool issued the
request and makes accidental or API-level request fabrication detectable. It
does not claim to protect against an operating-system account owner who can
read and replace every local file, including the mode-0600 key.
