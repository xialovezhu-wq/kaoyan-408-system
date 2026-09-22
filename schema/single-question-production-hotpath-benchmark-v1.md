# Single-question Production Hot-path Benchmark v1

Status: retired read-only benchmark. Its script must reject execution before reading
or copying production study state. The following claim and transaction list describe
historical measurements only and are not current acceptance criteria.

## Claim

`scripts/benchmark_single_question_hotpath_production_408.py` accepts the full
ordinary single-question transaction against a disposable clone of current
production-scale bytes. It does not measure live production contention.

The measured transaction contains:

1. canonical outcome persistence;
2. exact hot commit receipt verification;
3. conditional answer-safe fast capture inside the same public study-turn
   orchestrator;
4. immediate bounded personalization overlay update;
5. a second preanswer proving that the exact new failure is visible in
   `direct_recent_errors`;
6. verified next-question variant selection and deep source revalidation.

## Real-data construction

The cold phase copies current production bytes for:

- review ledger and legacy state;
- capture ledger and state;
- personalization projection;
- the three formal review scheduling tables;
- linked-practice verified source and provenance artifacts;
- the anchor's question assets and answer-isolation chain.

All derived review, formal-projection, and capture hot indexes are rebuilt only
inside the disposable clone before timing starts. Copy hashes and byte counts
are recorded in the report.

The current signed production variant request contains one legacy absolute path
to its sealed solution. Deep validation therefore performs one exact,
hash-bound, read-only production-details evidence read per transaction. The
benchmark counts and discloses these reads. It rejects every other production
details read and every production learning-state or formal-database read.

## Acceptance gates

- at least 20 measured transactions;
- one finite nonnegative `elapsed_ms` sample retained per transaction, with the
  reported nearest-rank P95 reproducible directly from those rows;
- nearest-rank P95 strictly below 5,000 ms;
- combined returned JSON no larger than 49,152 UTF-8 bytes;
- every transaction commits a unique canonical outcome;
- every partial outcome commits one unique `captured_unconfirmed` capture;
- no benchmark wrapper performs a second external ordinary-capture call;
- one production-derived `independent_correct` control commits its review event
  but creates no capture;
- every transaction immediately selects and revalidates the known verified
  variant for `CO_2099_911`;
- a production multi-point negative control for `CO05-19` must not select the
  `CO05-18` variant;
- every measured failed outcome must be returned by the immediately following
  preanswer with the exact canonical event ID;
- zero measured cold rebuilds;
- zero measured review-ledger replays;
- zero measured capture-ledger replays;
- every permitted ledger read handle is capped at 65,536 bytes and at most two
  line reads; size-less reads and longer iteration fail closed;
- `readlines`, `readall`, unbounded `read1`, raw-buffer detachment, and raw-fd
  ledger reads are rejected;
- each transaction is additionally capped at 9 review-ledger opens, 6
  capture-ledger opens, and 700,000 aggregate ledger bytes;
- the benchmark records and merges the actual byte ranges read from each ledger
  during each transaction and rejects disjoint reads whose union covers the
  complete file; the capture hot index uses 4 KiB commitment chunks so a ledger
  smaller than the former 1 MiB default is not re-read in full merely to verify
  its tail chunk;
- zero measured formal-table reads;
- zero measured formal-table writes, with pre/post temp-table hashes equal;
- zero production learning-state reads or writes;
- zero formal writes;
- exact review and capture event growth after warmups and measured calls;
- all bound production hashes unchanged;
- temporary workspace removed.

The mechanism acceptance is narrow: `KP:CO05-18` and
`RU_CO_EXCEPTION_INTERRUPT` are equivalent only because both are explicitly
bound to the same event target and the variant is bound to that exact review
unit. The event-explicit point and mechanism take precedence over the anchor's
first primary point. A different point on the same formal question or a
conflicting review-unit alias fails closed.

The write guard instruments Python file APIs and compares before/after hashes
for the enumerated production learning and formal surfaces. It is not an
operating-system sandbox and does not claim to intercept arbitrary native code.

## History claim boundary

The report may state that real stored formal dates span at least 100 elapsed
calendar days when the parsed endpoints prove it. It separately reports
distinct explicit outcome/repair dates and must always set
`active_learning_days_100_plus_claimed=false` unless 100 distinct active dates
are independently proved by a future contract.

## Run

```text
PYTHONWARNINGS=error::ResourceWarning PYTHONDONTWRITEBYTECODE=1 \
python3 scripts/benchmark_single_question_hotpath_production_408.py \
  --repo . \
  --iterations 20 \
  --warmup 2
```

Any optional report output path must be outside the entire production
repository and outside the details vault. This deliberately prevents a report
from replacing an unenumerated formal table, index, node card, or future
production-state surface.
