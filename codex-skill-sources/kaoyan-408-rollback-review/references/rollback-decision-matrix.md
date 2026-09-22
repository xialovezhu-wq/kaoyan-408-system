# Rollback classification matrix

Apply blocker flags before choosing one primary classification.

| Blocker evidence | Required flags | Result |
|---|---|---|
| complete answer, user answer, wrong option, or answer-bearing protected content | `derived-source`; `blocked-protected-content` | do not quote or paraphrase the protected fragment; report that direct reuse is blocked and stop |

| Evidence and user outcome | Classification | Next action |
|---|---|---|
| duplicate, stale, or unsupported summary | no action | report why; preserve source |
| useful misconception or recall target | learning follow-up | hand to daily loop or question worker |
| material identity/user-fact/answer-safety gap | evidence request | ask for smallest primary fact |
| known existing node plus verified answer-safe repair fields | safe maintenance candidate | hand to wrong-intake path E; no direct write |
| answer-safe evidence appears ready for intake, but no formal-write instruction exists | intake-readiness candidate | report readiness; no write or candidate confirmation |
| named old WAL/receipt transaction | legacy recovery | hand to legacy coordinator |

## Conflict rules

- Primary evidence outranks a derived summary.
- User-confirmed reasoning outranks model inference about the user's mistake.
- Conflicting identity, new/redo, main knowledge point, or answer-safety claims are
  blocking; do not average or vote.
- A useful learning insight can remain read-only even when formal evidence is
  insufficient.
- A blank safe card is not permission to synthesize a full card from a summary.
- An explicit current intake phrase bypasses rollback classification and hands the
  question to wrong-intake fast capture or daily-intake-curation formal work. A dated capture batch
  belongs to daily-intake-curation. Derived summaries never override the capture's
  user facts, identity, dates, or evidence hashes.

## Historical-question note

If the classification includes a learning follow-up with old-question candidates,
the owning learning or related-candidate workflow must apply the 7-day hard filter.
This legacy classifier does not improvise recommendations or locations.
