# Bounded JSONL Index v1

## Purpose

`scripts/bounded_jsonl_index_408.py` is a generic, answer-safe exact index and
commit-receipt layer for an append-only JSONL ledger. The ledger remains the
only fact source. The index is a rebuildable projection and has
`formal_write_count=0` throughout.

This component is deliberately not wired into the current review, morning, or
capture writers. A future integration must make its writer use the lock-held API
as the sole append path; merely building this index does not change current
transaction authority.

## Stable Python API

Cold operations:

```python
rebuild_index(ledger_path, index_dir, ...)
audit_index(ledger_path, index_dir)
recover_tail(
    ledger_path,
    index_dir,
    max_tail_bytes=1_048_576,
    max_tail_events=256,
)
```

Exact lookup:

```python
lookup_event(ledger_path, index_dir, event_id)
lookup_idempotency(ledger_path, index_dir, idempotency_key)
```

Lock-held writer integration:

```python
with locked_writer(ledger_path, index_dir) as writer:
    result = writer.append(event, idempotency_key="stable:key")
    writer.recover_tail()
```

The convenience wrapper is:

```python
append_event(ledger_path, index_dir, event, idempotency_key="stable:key")
```

When an explicit `idempotency_key` is supplied, it must exactly equal the
event's `idempotency_key` field. This ensures a crash-recovered tail still
contains enough durable evidence to reconstruct the idempotency index.

All writers that adopt this component must share the lock file managed by
`locked_writer`. An unrelated process that appends without this lock is outside
the transaction contract and will appear as an unindexed tail.

## Event contract and answer safety

An event must be one newline-terminated JSON object with a safe non-empty
`event_id`. The default maximum encoded event size is 65,536 bytes. The index
does not copy the event body; it stores only:

```text
event_id
canonical event SHA-256
raw JSONL line SHA-256
byte offset
byte length
idempotency-key SHA-256, when present
```

The ledger may contain bounded ordinary metadata needed by its owner, but this
component rejects protected question and answer fields recursively. This
includes English and Chinese forms of question stem, options, answer, correct
answer, solution, full response, complete response, and complete explanation.

The index, receipts, and manifests never contain a question stem, option,
answer, solution, full learner response, or the event's free text.

## On-disk structure

```text
index-dir/
  .bounded-jsonl-index.key
  .bounded-jsonl-index.lock
  manifest.json
  objects/<sha256>.json
  receipts/<sha256>.json
```

The signing key is mode `0600`. `manifest.json` is HMAC-bound and published by
atomic replacement. Shards and receipts are immutable, content-addressed JSON
objects; objects left by a crash before manifest publication are harmless and
unreferenced.

Exact indexes use four hexadecimal hash characters as a shard prefix:

- event IDs are placed by `SHA-256(event_id)`;
- idempotency keys are never stored raw and are placed by their full SHA-256.

Only the affected shard is loaded and rewritten for a normal append. A shard
has a hard two-MiB serialized limit.

## Ledger commitment

Every manifest binds the resolved ledger identity and state:

```text
resolved path SHA-256
device and inode
committed byte size
mtime_ns
one-MiB chunk size
ordered SHA-256 for every committed chunk
root commitment over size, chunk size, and chunk hashes
```

It also binds all event and idempotency shard hashes, counts, field names,
maximum event size, previous manifest SHA-256, generation ID, and the latest
commit receipt SHA-256.

A normal exact lookup performs constant-file-count reads, verifies the signed
manifest, selected shard, ledger identity/size/mtime, the event's byte range,
its raw line hash, canonical event hash, and every bounded chunk intersecting
that event.

The chunk list makes the commitment incrementally maintainable. An append
re-reads and validates at most the previously committed final chunk, then hashes
the changed final chunk and new chunks. Cold audit re-hashes every chunk.

## Lock-held append transaction

`LockedJsonlWriter.append` performs this sequence under one exclusive lock:

1. Verify the current signed manifest and exact committed ledger state.
2. If a prior bounded complete tail exists, recover it first.
3. Resolve event ID and idempotency key through exact shards.
4. Return `already_committed` when both idempotency key and canonical event hash
   match an existing event.
5. The same event ID with the same canonical event hash is also an idempotent
   retry; the same event ID or key with different content fails before writing.
6. Append one canonical JSONL line, flush it, and `fsync` the ledger.
7. Index the tail, update affected content-addressed shards and chunk hashes,
   and create a content-addressed commit receipt.
8. Atomically publish the new HMAC-bound manifest and `fsync` its directory.

The receipt binds the previous manifest hash, new generation ID, ledger size and
root, event and idempotency counts, and answer-safe per-event location/hash
commitments. It does not contain event bodies.

If the process stops after ledger `fsync` but before manifest publication, the
old manifest remains authoritative and the appended bytes form an explicit
unindexed tail. No partially written index generation becomes current.

## Bounded tail recovery

Tail recovery starts at the exact previously committed byte size. It verifies
ledger identity and the prior final chunk, then reads no more than:

```text
1,048,576 tail bytes
256 complete JSONL events
```

Both limits may be reduced by a caller but not increased through the public
API. Recovery fails without publishing when:

- the tail exceeds either budget;
- the tail lacks a final newline;
- any event is malformed, oversized, or answer-unsafe;
- an event ID repeats;
- an idempotency key conflicts;
- the committed final chunk, inode, path, or prefix size has drifted.

Recovery publishes all accepted tail records in one new generation. It never
truncates or rewrites the ledger.

## Drift behavior

The component fails closed for:

- manifest HMAC drift;
- content-addressed shard or receipt drift;
- ledger replacement or truncation;
- unexplained same-size mtime change;
- selected event byte, line hash, canonical hash, or intersecting chunk drift;
- incomplete or over-budget tails;
- count or idempotency cross-index disagreement.

`audit_index` is the cold full verification path. It re-hashes the complete
ledger, loads every referenced shard, validates every byte range and exact
cross-index mapping, checks counts, and verifies the latest commit receipt.

No bounded hot lookup claims to be a substitute for this full cold audit.

## CLI

```bash
python3 scripts/bounded_jsonl_index_408.py \
  --ledger /path/events.jsonl --index-dir /path/derived-index rebuild

python3 scripts/bounded_jsonl_index_408.py \
  --ledger /path/events.jsonl --index-dir /path/derived-index audit

python3 scripts/bounded_jsonl_index_408.py \
  --ledger /path/events.jsonl --index-dir /path/derived-index recover-tail \
  --max-tail-bytes 1048576 --max-tail-events 256

python3 scripts/bounded_jsonl_index_408.py \
  --ledger /path/events.jsonl --index-dir /path/derived-index lookup-event \
  --event-id RE-EXAMPLE
```

The CLI exists for cold rebuild, audit, controlled testing, and future writer
adapters. This version does not modify any current production state or core
writer.
