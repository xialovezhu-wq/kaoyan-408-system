# Legacy current-item contract


Legacy managed items keep their original `answer-current-and-next` transaction and
`CurrentQuestionContext` contract. `BLANK`, `UNCERTAIN`, errors, partial reasoning and
unresolved corrections stay in the same private draft with no Capture or navigation.
A choice-only grader cannot override semantic inconsistency. A terminal needs the
correct conclusion, sound reasoning and truthful hint dependence. Its one serial owner:
1. seals every user/assistant turn and original attachment, without the retired
   24 events or 32 KiB truncation;
2. creates/reopens exactly one package-bound Capture and verifies durable bindings;
3. then advances the navigation frontier, completes, or honors an explicit hold/close.
Use `navigation_policy=publish_successor` unless the user requests no next question.
Terminal hold does not thaw the old operation; continuing uses `resume-held-session`.
Do not use the campaign bridge for legacy items or migrate historical evidence.

Every path preserves source identity, answer protection and zero formal writes.
No T9 archive, formal write or background model processing occurs in the learner transaction.
The already authorized deterministic publication request runs after its locks release. A verified variant updates only its
bound node and does not become exact-original exposure. Only returned authorized
surfaces may be displayed; never pre-read or generate a replacement question.
