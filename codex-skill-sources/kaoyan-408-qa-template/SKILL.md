---
name: kaoyan-408-qa-template
description: "LEGACY 408 外部 GPT 网页端问答模板生成器。仅在用户明确要求生成或调试可复制到外部 GPT 的旧版 408 prompt 时使用；只生成提示词，不执行学习、测验、恢复或正式写入。"
---

# Legacy 408 QA Template

## Goal

Generate one compact, copyable prompt for an external GPT session using the
smallest verified study context. This skill does not run the prompt itself.

## Success criteria

The delivered prompt contains:

1. the learner's requested scope and outcome;
2. only the evidence needed for that scope;
3. answer-protection and one-question-per-turn behavior;
4. a clear completion condition and stopping rule;
5. no formal-write instruction, obsolete queue workflow, or unsupported mastery
   claim.

## Routing gate

Use this skill only for an explicitly requested legacy external-GPT template.
Read only the supplied scope and the references needed for it; never call retired personalization pre-answer. Current teaching routes to question-worker, explicit quick
intake to wrong-intake, and explicit formal completion to daily-intake-curation.
This legacy generator is not the A/B/D production importer.

## Evidence selection

Start with the user's supplied scope. Then read only the source needed to make the
prompt grounded:

- a targeted StudyVault or LLM Wiki page when named or required;
- an Obsidian note when the user identifies it;
- recent screen context through Chronicle only when the user refers to recent work
  that is not represented in files;
- Tutor state only when the requested legacy template is specifically for a Tutor
  handoff.

Do not preload all knowledge systems, scan the full vault, invoke Tutor, or create
dashboard/process artifacts merely because they are available.

## Build the prompt

Read `references/qa-template-format.md` and choose only the blocks needed for the
requested session. Prefer this structure:

- Role and learning outcome
- Verified context and evidence limits
- Interaction behavior
- Answer-protection and write boundaries
- Completion criteria
- Output shape and stop rules

Use the canonical interaction block from `references/qa-template-format.md`; do not
restate or extend it with a second competing learning protocol.

Do not embed complete stems, answers, wrong options, protected screenshots, private
attachment paths, or large source excerpts. Prefer short evidence summaries and
stable local identifiers.

## Validation

Before output, check:

- the prompt serves one stated outcome;
- no instruction conflicts with answer protection or read-only boundaries;
- no obsolete job/queue action appears;
- every conditional tool has a trigger;
- the prompt has an explicit stopping condition;
- placeholders are either filled or clearly labeled.

Use `references/output-template.md` for the visible handoff. Return the copyable
prompt, a short evidence note, and any unresolved placeholder. Do not execute the
generated session. Stop once the prompt passes the checks.
