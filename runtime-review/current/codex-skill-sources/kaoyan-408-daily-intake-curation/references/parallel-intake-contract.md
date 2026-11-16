# Native read-only formal-turn contract

This contract applies only inside an authorized 408 formal-intake turn after the
canonical cutoff planner has frozen one exact dated batch. A cutoff may cover several
dates, but child reads and the formal writer remain bound to one current dated batch.
The parent remains the same current Sol.

Ultra uses native proactive routing directly and does not invoke the `orchestrate`
Skill. Non-Ultra first reads and invokes the existing `orchestrate` Skill to decide
whether at least two genuinely independent processing-library reads exist. A single
read stays in Sol.

Each useful child is one native read-only leaf using `gpt-5.6-luna`, reasoning
`max`, Fast service tier, and `fork_turns="none"`. The leaf receives only the
frozen package SHA, explicitly allowed package or processing-library roots, one
concrete query, and required return fields. It has no child and performs no file
write, package mutation, ID allocation, formal script, ledger, Wiki, receipt, or
archive operation.

The same Sol waits for every started leaf, rejects wrong-scope, wrong-hash, mutation,
or incomplete results, resolves conflicts, and integrates once. Count is dynamic and
has no fixed Skill-level ceiling. Leaf failure never routes to another processing
system; Sol continues from primary package evidence, records a gap, or chooses
`needs_user`.

All identity, duplicate, taxonomy, relation, answer-safety, approval, RepoLock, WAL,
formal writer, postcommit, mark-result, terminal binding, archive and final acceptance
remain owned by Sol. Formal publication is strictly serial and batch-size-1. Proven
COMMITTED work is never reapplied, and receipt-bound recovery never reruns child
reads.

The learner-turn package route always has zero children and never reads this contract.
