# CS408 display asset plan and closure v1

`cs408-display-asset-plan-v1` is sealed inside the Sol canonical package and bound to
one exact `cs408-conversation-package-v1` canonical SHA. It classifies every package
attachment as `promoted` or `archive_only`. Sol may choose only disposition and the
role-derived `practice_safe|protected` visibility; it never supplies a filesystem
target.

The formal writer derives stable targets under
`kaoyan-408-details/assets/<FORMAL_ID>/`, validates PNG/JPEG/WebP bytes, and publishes
the assets plus one package-scoped machine-managed embed block in
`cards/<FORMAL_ID>.md` in the same RepoLock/WAL transaction. This applies to both
`new` and `redo`. Existing targets are reused only when their complete SHA-256 is
identical; conflicting bytes fail closed and are never overwritten.

The same transaction publishes `cs408-obsidian-asset-closure-receipt-v1` below
`.cs408-display-assets/closures/<PACKAGE_ID>/<FORMAL_ID>.json`. It binds the package,
plan SHA, formal ID, source attachment metadata, stable relative paths and hashes,
Markdown path/hash, visibility, final disposition, embed resolution and reread
result. `archive_only` rows have no stable path or embed. A zero-display plan records
an explicit `no_display` proof.

Before local package cleanup, the archiver must reread the closure, every stable
asset, the card and every embed, then scan the formal repo and details vault for
temporary package, inbox or absolute T9 image references. The archive intent,
receipt and pointer bind the closure SHA, formal-reference scan SHA, stable asset
count, no-display proof and `pending_component`. Any failure remains
`FORMAL_COMMITTED_ARCHIVE_PENDING` with `pending_component=display_assets`; formal
apply is never replayed and local heavy bytes remain intact.

