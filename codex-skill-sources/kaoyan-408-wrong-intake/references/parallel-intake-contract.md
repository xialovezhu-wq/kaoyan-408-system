# Fast-package and formal-read separation

The current-question learner turn never creates child tasks. Managed non-terminal
turns update only one private draft and create no Capture or pending row. A managed
terminal, or a standalone request with explicit quick-intake authorization, seals one
final package and creates one durably verified Capture; only that Capture enters
`awaiting_sol_formalization`. No learner turn performs a processing-library read,
background work, T9-Data archive, or formal write.

Native read-only children are allowed only in a later user-authorized formal-intake
turn owned by the same Sol. Ultra routes natively without the `orchestrate` Skill.
Non-Ultra first invokes the existing `orchestrate` Skill when at least two
independent processing-library reads may benefit from parallel work.

Every selected leaf uses `gpt-5.6-luna`, reasoning `max`, Fast service tier, and
`fork_turns="none"`. It receives one frozen package SHA, explicit read roots, one
query, and bounded return fields. It cannot delegate, write files, mutate packages,
allocate IDs, run formal scripts, touch ledgers/receipts/Wiki, or archive data.

The same Sol verifies and integrates leaf results once, then owns every decision and
the strictly serial batch-size-1 writer. Failed reads become gaps or
`needs_user`; there is no alternate processing route.
