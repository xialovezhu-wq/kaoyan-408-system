# Morning Personalized Router v1

## Purpose

`scripts/morning_personalized_router_408.py` is the single bounded routing gate
for one morning 408 practice item. It consumes only caller-supplied, already
verified projections and cooldown views. It never scans `节点总表.md`, a formal
subject table, `events.jsonl`, the Obsidian vault, a details vault, a reference
book, or a question corpus.

One call returns exactly one `selected_item`. It never recursively asks itself
for another item. `followup_depth > 0` is retained as audit evidence and sets
`recursion_blocked=true`; it does not create a second selection.

## Inputs

The function API is:

```python
route_morning_practice(
    question_index=...,
    variant_catalog=...,
    cross_variant_catalog=...,       # optional
    cross_projection=...,            # current manifest-bound live state
    learner_cooldown=...,
    variant_cooldown=...,
    target=...,
    as_of="YYYY-MM-DD",
    demand_dir=...,
    user_requested_original_id=...,  # optional
    followup_depth=0,
)
```

`question_index` must use `schema=personalization_index_v1` and
`kind=question_inverted_index`. An original is routable only when its
`reliable_source` proves all of the following:

- status is `verified_exam_locator` or `verified_question_surface`;
- `original_question_eligible=true`;
- `surface_verified=true`;
- `answer_isolated=true`;
- `source_ref` is present and `source_sha256` is a valid SHA-256 digest.

The ordinary catalog must use `schema=variant_source_catalog_v1`,
`kind=verified_variant_source_catalog`, and `formal_write_count=0`.

The optional cross-subject catalog must use
`schema=cross_variant_catalog_v1`,
`kind=verified_cross_variant_catalog`, and `formal_write_count=0`. This is the
complete catalog rebuilt by `cross_variant_pipeline_408.py`; it does not use a
second router-only schema. Each entry carries `original_formal_node_id`,
`verified_cross_edge_id`, and exactly two `targets`, where each target contains
one subject, point ID, and mechanism key. The catalog's deep rebuild already
replays the verified-live edge, independent blind answer, machine reveal, and
question-only publication chain. Shadow or merely proposed variants never enter
this catalog. A catalog entry is still insufficient by itself: every route
rechecks the same edge ID and exact two point endpoints against the current live
projection. A disabled, removed, ambiguous, or endpoint-drifted edge makes the
stale catalog entry unroutable.

The target is answer-safe and may contain only these bounded controlled lists:

```json
{
  "subjects": ["OS"],
  "point_ids": ["OS04-06"],
  "mechanism_ids": ["RU_OS04_LINK_COUNT"],
  "formal_node_ids": ["OS_2099_918"]
}
```

Cooldown views are rebuildable projections, not learning truth:

```json
{
  "schema": "morning_learner_cooldown_v1",
  "as_of": "2026-07-23",
  "questions": {
    "OS_2099_918": {"last_attempt_date": "2026-07-17"}
  }
}
```

```json
{
  "schema": "morning_variant_cooldown_v1",
  "as_of": "2026-07-23",
  "variants": {
    "VAR-OS-LINK-01": {"last_displayed_date": "2026-07-15"}
  }
}
```

An absent date, `null`, `unknown`, `unrecorded`, or `未记录` is explicit unknown
evidence. It is not rewritten as a date.

## Deterministic seven-day routing

The normal route applies these gates in order:

1. An original with a known age of at least seven days is eligible.
2. An original with an unknown date is eligible but ranks below an otherwise
   suitable original with a known due date.
3. An original with a known age from zero through six days is prohibited.
4. When the current exact original is prohibited, select a `verified_ready`
   variant with the same mechanism only if that variant itself was last shown
   at least seven days ago or has an unknown display date. Known due variants
   rank above unknown-date variants.
5. If no such variant exists, select another verified original that is due by
   the same seven-day rule.
6. If neither exists, return one `mechanism_check` and atomically write one
   content-addressed durable `morning_variant_demand_v1` request.

The exact boundary is therefore:

| Age in days | Normal original route |
| --- | --- |
| 0 | prohibited |
| 6 | prohibited |
| 7 | eligible |
| 8 | eligible |
| unknown | eligible, lower priority than known due evidence |

`user_requested_original_id` is the only cooldown exception. If it resolves to
a verified, answer-isolated original, the router returns it with
`gate_decision=user_requested_exception` even when its age is zero through six.
The exception is explicit in the receipt and must not be generalized into a
normal recommendation policy.

## Variant safety

The router never writes or invents a question stem, option, answer, explanation,
or first-break narrative. A routable variant must already carry a verified
question-only source locator and hash. An ordinary variant checks exact mechanism
equality. A cross-subject variant is considered only when exactly one of its two
target objects matches the current controlled point or mechanism; a point match
on one side and a mechanism match on the other is ambiguous and fails closed.
The selected receipt preserves `verified_cross_edge_id`, `matched_cross_target`,
`cross_targets`, and `cross_match_basis`. Same subject or keyword overlap is
insufficient.

The durable fallback request contains only controlled IDs, projection hashes,
the reason code, and required verification gates. It requires a reliable original
surface, distinct author and verifier, a unique-answer check, a same-mechanism
check, changed-surface evidence, and answer isolation before later publication.
The request has `formal_write_count=0` and cannot authorize generation in the
morning hot path.

## Output

Every successful call returns `schema=morning_personalized_route_v1` with:

```text
selected_item_count = 1
recursive_followup_allowed = false
formal_write_count = 0
runtime_source_policy.formal_repository_scan_count = 0
runtime_source_policy.review_ledger_scan_count = 0
```

The selected item is exactly one of:

- `original_question`;
- `verified_variant`;
- `mechanism_check` with a durable demand receipt.

The router does not return a ranked candidate list. This prevents one morning
turn from silently expanding into a multi-question chain.

## CLI

The CLI reads only the explicitly named JSON projections:

```bash
python3 scripts/morning_personalized_router_408.py \
  --question-index question_index.json \
  --variant-catalog variant-catalog.json \
  --learner-cooldown learner-cooldown.json \
  --variant-cooldown variant-cooldown.json \
  --target-json '{"point_ids":["OS04-06"],"mechanism_ids":["RU_OS04_LINK_COUNT"],"formal_node_ids":["OS_2099_918"],"subjects":["OS"]}' \
  --as-of 2026-07-23 \
  --demand-dir /durable/derived/morning-variant-demand
```

The caller is responsible for verifying and binding the supplied projection
files to the current personalization manifest before invoking this router.
