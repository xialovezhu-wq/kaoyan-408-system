---
name: kaoyan-408-rollback-review
description: "LEGACY 408 外部 GPT/Tutor 总结回流审查。仅在用户明确要求分析旧 GPT 总结、旧 rollback-review、外部 GPT/Tutor 回流，或判断旧总结能否用于修复空白安全卡时使用。总结是派生证据，默认只读；任何正式节点、详情或 bridge 变更必须转入对应 formal maintenance 路径。日常复盘、当前题学习、正式入库和历史事务检查或恢复分别使用各自技能。"
---

# Legacy 408 Rollback Review

本机路径和解释器先按[共享路径约定](../_shared/kaoyan-408/local-paths.md)定位；下文仓库命令在已定位的项目目录执行，历史回执保留原字节。

## Goal

Classify one legacy summary into a safe next action without treating generated text
as primary evidence or silently mutating the formal system.

## Success criteria

The review is complete when it identifies:

1. what the source actually proves;
2. what remains inference or missing;
3. the narrowest safe destination;
4. whether user confirmation or primary evidence is required;
5. that no unauthorized formal write occurred.

## Routing gate

Use only for an explicitly named legacy GPT/Tutor summary or rollback artifact.
Read its targeted primary evidence; never call retired personalization pre-answer.
Daily/current teaching uses the relevant learning skill; explicit quick intake uses
wrong-intake; explicit formal completion uses daily-intake-curation; a named historical
WAL/receipt recovery uses the legacy coordinator.

Do not reinterpret those requests as summary classification.

## Evidence policy

Read the supplied summary and only the targeted primary source needed to verify a
claim. A GPT or Tutor summary may propose a concept, error entry, or safe-card field,
but it cannot prove user behavior, identity, answer safety, or mastery by itself.
During daily curation it may support only model-derived field candidates. It cannot
override captured user facts, source/details identity, study dates, new/redo facts,
redo history, or evidence hashes; a conflict becomes `needs_user`.

Use StudyVault, Tutor, Obsidian, LLM Wiki, Chronicle, or details only when the source
or requested destination depends on it. Do not start a Tutor quiz, full-vault scan,
or dashboard update by default.

## Classification

Read `references/rollback-decision-matrix.md`. First apply blocker flags. A summary
containing a complete answer, user answer, wrong option, or other answer-bearing
content is both `derived-source` and `blocked-protected-content`: do not repeat it,
do not turn it into a generic evidence-request workflow, and stop after the safe
classification unless the user separately supplies a redacted primary source.

When no blocker ends the review, choose exactly one result from the decision matrix.
`intake-readiness candidate` reports evidence readiness only; it neither confirms a
candidate nor writes. An explicit current intake phrase bypasses this classification
and routes to wrong-intake for quick capture or daily-intake-curation for formal work.

When evidence conflicts, prefer primary local evidence and preserve the conflict.
Do not vote or average incompatible facts.

## Actions

This skill may produce a read-only classification, compact evidence ledger, and
handoff packet. It does not directly edit formal nodes, safe cards, details,
coverage, bridge, mastery, or dated reviews.

If the user explicitly asks to repair an existing empty safe card, hand the verified
answer-safe candidate to the existing `scripts/apply_existing_node_maintenance_408.py`
maintenance owner; its source/evidence contract owns validation and writes. If the summary would create a new formal
node, require the normal explicit intake route.

## Output

Use `references/output-template.md` and report:

- source and scope;
- verified facts, inferred claims, and missing evidence;
- classification and reason;
- permitted next action or handoff;
- write status.

Stop after classification and the authorized handoff. Do not continue into a quiz,
formal write, or broad cleanup unless the user separately requested that outcome.
