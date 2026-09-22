# Legacy summary input ledger

Normalize the supplied artifact without upgrading its authority.

```text
source_type: external_gpt | tutor | old_rollback | unknown
source_locator: stable path, note ID, or user-provided label
scope: one question | concept | session | unknown
source_date: YYYY-MM-DD | unrecorded
explicit_user_request: classify | learn | repair_candidate | formal_intake | recover
claimed_facts:
  - claim
  - provenance inside the artifact
primary_evidence_available:
  - locator or none
protected_content_present: yes | no | unknown
```

Keep answers and protected content out of the normalized ledger. A claim copied from
a summary remains `derived` until primary evidence or the user confirms it.
