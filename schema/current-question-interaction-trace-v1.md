# Current question interaction trace v1

## Shape

`--trace-json` accepts a JSON list. Every input event has exactly:

```text
role, kind, text
```

`role` is `learner` or `assistant`. `kind` is `utterance`, `first_action`,
`reasoning`, `hint`, `correction`, `restatement`, or `answer`. Text must be a
nonempty UTF-8 string copied from the already observed current-question exchange.

The normalized private object is exactly:

```text
schema=current-question-interaction-trace-v1
events=[{ordinal,role,kind,text}, ...]
event_count=<length of events>
truncated=false
```

Ordinals start at 1 and are contiguous.

## Bounds and failure behavior

- at most 24 events;
- at most 2048 UTF-8 bytes of text per event;
- at most 32768 canonical JSON bytes for the normalized trace.

Invalid per-call shape, enum, ordering, or size fails validation. The runtime does not
silently trim that input. A caller may pass a
pre-normalized object only when its four top-level fields, event count, ordinals, and
enums already match this contract.

## Same-item accumulation

For `answer-current-and-next`, the caller passes only this turn's events. The runtime
adds the current choice as a learner `answer` event when absent, then merges events
into the same display/item private buffer. Each accepted occurrence is bound to its
answer-operation/revision identity and ordinal; identical text spoken twice remains
two events. Only an exact idempotent replay of the same operation/revision is
deduplicated. The
24-event and 32-KiB limits apply to one submitted turn object, not to the cumulative
conversation package. When cumulative history exceeds those legacy limits, the
runtime retains every accepted event across immutable package revisions; it never
keeps only the first/last events and never sets `truncated=true`. An oversized single
submission is rejected so the caller can provide a valid bounded revision without
losing any already accepted event.

## Privacy and semantics

The normalized trace is stored only inside the private
current-item buffer and final `cs408-conversation-package-v1`. Public Capture rows
never contain its event text. The trace never mixes another item, reconstructed
history, or model-inferred learner speech. Only the final package's complete
interaction digest may enter the terminal-verdict and Capture receipt binding.
