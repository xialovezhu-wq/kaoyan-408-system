# Exact archived evidence only


Normally use the current surface, bounded teaching slice, formal node, and existing
Obsidian information. Only when those are insufficient and the note exposes an exact
`raw_archive_locator`, reopen that one package. If only a formal/source identity is
known, first call `archive_408_conversation_package.py resolve-locator --repo <repo>
--retrieval-key formal:<ID>|source:<ID>`; exactly one locator note must resolve.

Then call `archive_408_conversation_package.py reopen-verify --repo <repo>
--package-id <PACKAGE_ID> --read-relative-path conversation.json`, plus only an exact
manifest-bound attachment path when necessary. Verify the canonical T9 sentinel,
fixed 408 relative path, locator note, manifest-file SHA, package SHA, and every
manifest row. Never scan T9-Data or enumerate other packages. Keep answer-bearing
archive content private and preserve the current answer-protection level.
