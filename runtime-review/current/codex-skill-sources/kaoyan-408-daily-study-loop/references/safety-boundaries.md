# Learning safety boundaries

When answer protection is active, never expose the final answer, correct option,
decisive clue, or equivalent intermediate result. A direct request for the answer
overrides this preference.

Use only current visible evidence and the controlled grader. Keep user statements,
visible facts, and inference distinct. Do not augment the learner turn from MEMORY,
history, formal nodes, other questions, personalization, processing-library reads, or
archived raw packages unless the question-worker's precise raw_archive_locator gate
is satisfied.

Non-terminal attempts write only the same item's private draft and create no Capture
or pending row. A terminal answer-and-reasoning verdict seals one final package and
creates one durably verified Capture; only then may its
`awaiting_sol_formalization` row exist. This is not a formal wrong-node decision.

First/session receipt failure hides answer-bearing feedback and blocks advancement.
Final-package or Capture failure keeps frozen feedback visible and retains the current
item. Recovery reuses the same item/display/package/Capture identities and does not
repeat the outcome. Only a verified terminal `answer-current-and-next` receipt may
publish one frontier-bound successor.
