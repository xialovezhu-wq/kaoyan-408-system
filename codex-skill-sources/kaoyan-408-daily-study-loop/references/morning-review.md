# Morning review current-question contract

Preparation and session start remain owned by `kaoyan-408-morning-control`.
Daily study begins only after one answer-safe display receipt exists.

For every first answer and correction, use exactly one
`answer-current-and-next` operation. Freeze feedback first, keep the same display
while correction is unresolved, and publish at most one successor after the exact
navigation frontier receipt passes. Default to `navigation_policy=publish_successor`.
When the learner explicitly ends today's questions or says not to publish the next
item, use `navigation_policy=hold_current` in that same operation. The hold path
finishes the current item without a successor lookup and returns
`next_item_published=false` and `next_item=null`.

Before terminal, update only one private draft for the displayed item under
`/Users/your-user/.codex/kaoyan-408-conversation-packages`. Create no Capture,
pending row, canonical outcome or navigation. Terminal requires the correct option or
conclusion, no known reasoning contradiction, and accurate hint dependence. Then seal
the complete final package, create and durably reopen exactly one Capture bound to its
SHA, and only afterward publish, hold, or close.

Only the verified terminal Capture enters `awaiting_sol_formalization`; this is a
local pending-formal index, not a judgment that the item is a formal mistake.
`formal_write_count=0`.

First/session receipt failure hides answer-bearing feedback and blocks advancement.
Final-package/Capture failure keeps frozen feedback visible and retains the item.
Recovery reuses the original identities and does not repeat an outcome, package,
Capture, index row, or successor. It also reuses the navigation policy frozen in the answer operation:
first-answer completion, teaching resolution, capture recovery, and idempotent replay
cannot upgrade a held operation into successor publication.

`hold_current` is not a session-close receipt. If unanswered items remain, closeout
must use the session controller's authorized append-only stop/disposition route; do
not claim the session is closed merely because no successor was displayed.

The learner turn has no semantic child, worker, consumer, report, adoption, service
poll, T9-Data write, Obsidian locator write, or formal writer. Only a later explicit
formal request routes to `kaoyan-408-daily-intake-curation`.

managed_morning_route_v9=entry:answer-current-and-next-only;nonterminal:draft-zero-capture;terminal:final-package-one-capture-durable-reopen;index:verified-terminal-only;feedback:frozen;navigation:publish-close-or-frozen-hold;background:none;formal:explicit-only
