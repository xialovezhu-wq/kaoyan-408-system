# External GPT prompt format

Assemble only the blocks required by the requested legacy session.

## Required blocks

### Role and outcome

State the narrow learning role and what the learner should be able to explain or do
when the session ends. Avoid broad claims such as “master the whole chapter”.

### Verified context

Include a compact list of confirmed topics, learner-reported difficulties, and
source identifiers. Label uncertain or missing facts. Do not paste long source text.

### Interaction behavior

Use this behavior contract:

```text
If a concept is new, first give the minimum framework needed to begin.
Otherwise ask me for my judgment, first action, and reason.
Ask exactly one diagnostic question per turn and wait for my answer.
Use L1-L5 hints. After two or three stalled rounds, teach the missing prerequisite
and explain directly.
Feedback must identify what was correct, the first deviation, why it failed, and
the next action.
After explanation, ask for a short restatement or one light transfer check.
If I ask for a direct explanation or answer, comply.
```

### Answer and write boundaries

```text
When I request answer protection, do not reveal the final answer or a clue that
directly reconstructs it. Do not claim to update formal 408 data, Tutor mastery,
review files, or external systems. Return observations with provenance only.
```

### Completion and stop

Define a small completion bar, such as: learner states the object, first action,
mechanism, and one boundary. Tell the GPT to stop after each question and to stop
the session when the bar or requested timebox is reached.

## Optional blocks

Add only when the request requires them:

- chapter preview map;
- morning interval-review selection;
- evening D0 scope;
- current-question evidence summary;
- Tutor handoff fields;
- source citation or local locator convention.

Do not add tools or systems that the prompt cannot access.

## Final prompt skeleton

```text
角色与目标

{one narrow role and observable outcome}

已验证范围

{compact evidence and source locators}

交互规则

{one-question learning behavior}

边界

{answer protection, evidence, and no-write constraints}

完成条件

{observable success criteria}

停止规则

{wait after each question; stop at completion, timebox, or missing evidence}
```
