# 408 Skill sources

These versioned Skill sources mirror the installed copies under
`/Users/your-user/.codex/skills`. The directory is not a discoverable skill root.
Update only the changed source/installed files and `_shared/kaoyan-408` together;
keep unrelated global shared contracts and historical snapshots unchanged.

- enhancement-intake authors pending Captures locally, commits them through the native writer and verifies profiles and teaching material.
- evening-review prepares the whole-day interactive HTML or, when requested, a local question plan with separate student and teacher materials.
- morning-control prepares the requested selection from local sources and resumes an existing queue when applicable.
- daily-study-loop owns an already displayed campaign/legacy item and its exact transaction.
- question-worker preserves current-evidence teaching and the latest bounded personal context.
- wrong-intake captures a real current standalone question only after explicit quick intake.
- daily-intake-curation owns same-day formal closeout, including a verified tutoring readback, a prepared weak-point set and the daily HTML. It also routes cumulative cutoff requests and recovery.
- intake-coordinator, qa-template and rollback-review are limited legacy entrypoints.

The common current contract is `_shared/kaoyan-408/runtime-contract.md`.
For matching step-by-step mechanisms, question-worker uses the local shared
visual guide and bundles at `skills/_shared/visual-explanations` under the active
Codex home. The existing 408 visual entry remains usable if that optional
bundle is absent.
The installed one-call helper has a real current-turn consumer; preserve source labels,
complete dialogue, honest prompt dependence and verified Capture-before-navigation.

Static validation (no model or production write):

```text
python3 codex-skill-sources/_shared/kaoyan-408/scripts/validate_408_skills.py --skills-root codex-skill-sources --schema-root schema
```

Validate the changed source/installed resources and relevant isolated behavior tests.
Do not run retired model preparation, background reports or broad live benchmarks as
routine Skill validation. Check the current user-selected model and
visible-response latency at the supported teaching entry.
