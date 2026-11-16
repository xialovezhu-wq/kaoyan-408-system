# Current personal learning context

Use the same exact current question, point/KSP and allowed view as the teaching turn. The current attempt owns the diagnosis; history changes emphasis only when its evidence is relevant. Do not label every tested point as weak.

## Selected personal semantics

```text
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py resolve --repo . --current-question-id <exact-current-id> --concept <known-point-or-KSP> --view <protected|after_attempt|direct> --personal-scope teaching --materials core
```

Supply the verified current identity and the necessary concepts or mechanisms in one request. Reuse campaign/D `teaching_context` when it already covers the explanation. Read `records[].personal_context` and `personal_read_receipt`. A bounded record preserves whole claim statements, their basis and any known validity dates. Use statements only under the stated conditions. `omitted_claim_count`, `omitted_fields`, `omitted_points` and `omitted_subpoints` describe this delivery. Read `full/core` in the same exact permitted scope if a specific missing condition, conflict or source would change the explanation.

For a multi-point explanation, check whether the returned claims or bounded summaries cover every required point. `records[].omitted_fields` names optional definitions, navigation and syllabus details omitted for this delivery. Use the receipt's profile version plus `claim_id` to anchor a claim. Interpret `event_at` as occurrence and `recorded_at` as the recorded judgment revision.

Apply a correction only when the current stem satisfies its conditions. Keep competing conditions distinct and carry prompt dependence into the explanation. When a repair or its condition is missing, rely on the current stem and attempt. If the gap changes the explanation, use the existing same-scope source reference. A historical recorded repair supports a check of that mechanism, and a prompted response supports a later independent check.

An independent Pxx with unbound parent personal history returns `withheld_scope`, `unscoped_parent_personal_history`, `complete=false` and `current_question_only`. Select a KSP only when the current public question supports the link.

- `complete=true` confirms the selected full personal objects were read and version-checked.
- `bounded` confirms a checked object with limited teaching content. `summary_only` is the existing navigation view.
- `evidence_only`: usable records without a current reviewed semantic judgment. Do not infer mastery or reconstruct an old personal conclusion.
- `stale/unavailable`: leave the affected personal judgment unknown, name the material gap, and continue from the current attempt. Repeating a corrupted read cannot replace repair by its owning maintenance workflow.
- `withheld`/protected: the current scope intentionally withholds content. Do not increase view to recover it without the appropriate answer permission.

For `--known-version` use the native `version` of the response held in this chat. The scope includes the question/Pxx, mechanisms, view, materials, read scope and teaching budget. `unchanged` has `complete=null`. Use `context_version` for a portable snapshot.

`--personal-scope full --materials visual` selects the exact visual material in the same permission scope. Its personal receipt is `not_requested`. Keep same-turn prose and clickable visual delivery scoped to the permitted mechanism.

## Evidence, preferences and time

Direct learner observations, question-associated sources, assistant explanations and reviewed interpretations retain different roles. `point_binding=explicit_point` with learner evidence can support a narrow personal claim; `question_associated`, formal summaries or assistant explanation alone cannot prove a learner error or independent repair.

Use current claims according to their basis, evidence_status, lifecycle and source turns. `legacy_unstructured` preserves the old wording without inventing claim-level evidence. Superseded/retracted claims remain historical, not current tutoring premises. Reviewed means the author checked the evidence; it does not mean independent mastery.

Explicit preferences, observed effects, self-reported understanding and independent transfer stay separate. Apply the preference only to its recorded scope; a new user instruction governs this turn. Unknown dates or ambiguous same-day order cannot establish a newest error. Import, file modification and recorded time do not replace actual event time.

For an explicit historical, comparison, source or preference question, use the same entry's `memory history|compare|trace|preferences` operation with exact identities. trace accepts record-key or claim-id. history/trace after_attempt returns metadata only; original evidence and preference quotes require the current authorized direct scope, never a view escalation merely to fill missing material. `--event-after/--event-before` filter occurrence; `--known-before` limits what was recorded by that cutoff. Where a historical state cannot be reconstructed, preserve the returned insufficient_evidence/unknown boundary instead of showing today's conclusion as the past.

`source_kind=concept_review` remains sealed B evidence and `semantic_formal_intake` remains sealed ordinary per-point evidence. Reading them does not start a B workflow, create an attempt or update FSRS. Correction refs explain changed interpretations while preserving raw originals.

## Discovery and missing evidence

Exact IDs go directly to resolve. Unknown public mechanism wording uses bounded `memory discover --query ... --intent concept --view protected --limit 8`, then exact resolve. A remembered personal attempt or statement uses --intent experience --actor user; locating an assistant explanation uses assistant. Follow its trace_request and retain all current question/Pxx, point/KSP, allowed-ID, year, time and view restrictions. auto is a routing default, not evidence of a correct match.

Protected refuses experience search. After_attempt historical lookup without a current question returns metadata only and reads no body. Direct distinguishes native_record_summary from verified_original_turn; the former is an authored summary, never a verbatim quote. Source-verified original turns are available only for unconstrained history lookup with no current-question, point/KSP or allowed-ID scope, and a truncated snippet is not a complete turn or dialogue. Do not remove scope to obtain it. Use event-after/before and latest/no-latest for requested chronology; retain unknown dates and ties. If known-before needs revision replay, use the precise history operation rather than today's interpretation. A latest ranked hit does not prove the last event in all history.

Discovery uses Chinese word segmentation and fielded BM25. The default backend follows the shared local qualification policy; exact identity, experience and protection boundaries remain unchanged, reranking is not default, and there is no paid API fallback. Retrieval, preparation and hits do not create learning events or prove weakness or mastery.

Use the result's distinct statuses: not_found is a retrieval miss, no_record is absence in the selected source scope, insufficient_evidence is an unsupported conclusion, and unavailable is a freshness/source failure. Missing or omitted content is not evidence that the user never studied it.

Old wiki pages without current version bindings are supplementary. Follow exact source references only when needed; do not scan full tables, the review ledger, the entire vault or T9. Do not rebuild while the learner waits, call retired personalization writers, create learning evidence, or add a background model request.

缺少必要证据时用同范围的 --evidence-gap 做一次只读本机补查，随后精确回读。新批已核关联只用于定位，保留答题权限和待审证据的独立状态。
