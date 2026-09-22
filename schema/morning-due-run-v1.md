# 408 节点级晨间到期 run v1

`morning_due_run_v1` 是某一天全部到期正式节点的确定性只读快照。生成它不创建 session、题面、package、Capture 或正式写入。

## 到期与全量覆盖

一个 included 节点在 `next_due_date <= run_date` 时到期。不得用语义分数、题数上限或时间预算截断：

```text
due_count == scheduled_count
skipped_due_count == 0
semantic_cap == null
```

排序只决定展示顺序：有 D/S/R 的低 retrievability 在前，再按逾期天数和正式 ID 稳定排序。排序不改变集合。

## 16 题 segments

到期列表按 16 个节点切段。每段保存顺序稳定的 `formal_node_ids`、item-set SHA-256 和 segment SHA-256。最后一段可以少于 16；segments 拼接后必须与完整 due list 完全相等。

## 冻结和 admission

run 保存：

- admission cutoff：ledger ref、byte size、event count、文件 SHA-256、末事件 ID/hash。
- admission event bindings。
- cohort、memory projection、config 和 source bindings 的 SHA-256。
- 可选 refresh handoff 的触发事件 ID/hash。

一旦 run 作为不可变 JSON 冻结，`--frozen-run` 只验证并重开原 `run_sha256`，且拒绝 `--refresh-handoff`。冻结后的 admission 不得插入该 run；下一次不带 `--frozen-run` 的 dry-run 才读取新 cutoff。

## CLI

```bash
python3 scripts/morning_coverage_campaign_408.py forecast \
  --repo . --as-of 2026-08-31

python3 scripts/morning_coverage_campaign_408.py dry-run \
  --repo . --as-of 2026-08-31 --date 2026-08-31
```

可选参数：`--config`、`--admission-ledger`、`--refresh-handoff`。冻结重开另用 `dry-run --frozen-run RUN.json`。全部命令输出 JSON 到 stdout，错误输出 `status=unavailable` 并以 2 退出；不会写入仓库或学习账本。

