# Learning-to-intake handoff

This reference exists only for a current-task handoff. The canonical route is in
`../../_shared/kaoyan-408/runtime-contract.md`.

When the current result is capture-eligible during active study:

1. freeze the current-question feedback;
2. let the managed current turn select `kaoyan-408-wrong-intake` fast capture for
   exactly this question;
3. pass stable protected evidence locators/hashes plus verified answer-safe source and
   user facts with provenance;
4. preserve unresolved identity, new/redo, main-knowledge, or error-taxonomy fields
   as explicit gaps rather than delaying learning to infer them;
5. show the frozen feedback after the bounded transaction succeeds or returns an
   exact capture-only recovery state;
6. do not create a formal package, job, queue receipt, or coordinator message.

The handoff completes fact preservation only. Formal completion belongs to a later
user-specified-date daily-curation task. A date-scoped curation request bypasses
evening D0 and routes to `kaoyan-408-daily-intake-curation`.
