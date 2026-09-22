# Existing node maintenance v1

`existing_node_maintenance_v1` is the batch-size-one transaction contract for
high-confidence maintenance of an already existing formal 408 wrong-question
node. It is not a new-question, redo, merge, deletion, or identity-migration
route.

## Safety boundary

- Exactly one existing `node_id` is the transaction item.
- `answer_verified` must be `true` and the package must bind the current safe
  card, detail card, question image, and solution image by SHA-256.
- Formal ID, subject, year, first/latest dates, latest error record, user error
  entry, review scores, mastery, schedule, and historical events are immutable.
- The writer may update only explicitly enumerated model-derived fields,
  answer-safe detail metadata, direct relation rows, and guarded review-unit
  projections.
- Every existing relation patch carries the complete current row as `expected`.
  Every add/remove/replace must be `confidence: high` and touch `node_id`.
- Source or detail-entry changes must update the original-question track in the
  same transaction. Main-knowledge and mechanism changes must explicitly update
  the relevant review-unit contribution.
- The package and all evidence are revalidated under the repository lock.
  Production writes use the shared WAL and idempotency receipt engine.
- A committed transaction is never reapplied merely because closeout failed.
  Use the `closeout` subcommand with the original receipt.

## Package shape

```json
{
  "schema_version": "existing_node_maintenance_v1",
  "node_id": "CO_2099_901",
  "review_date": "2026-07-22",
  "answer_verified": true,
  "evidence": [
    {
      "role": "safe_card",
      "path": "formal:CO_2099_901.md",
      "sha256": "64 lowercase hex characters"
    },
    {
      "role": "detail_card",
      "path": "details:cards/CO_2099_901.md",
      "sha256": "64 lowercase hex characters"
    },
    {
      "role": "question_image",
      "path": "details:assets/CO_2099_901/question-01.png",
      "sha256": "64 lowercase hex characters"
    },
    {
      "role": "solution_image",
      "path": "details:assets/CO_2099_901/solution-01.png",
      "sha256": "64 lowercase hex characters"
    }
  ],
  "expected": {
    "master": {
      "ID": "CO_2099_901",
      "年份": "2009",
      "科目": "计算机组成原理",
      "首次做题日期": "2026-07-04",
      "最近复做日期": "2026-07-04",
      "最近错误记录": "exact current value",
      "来源ID": "VISUAL_PENDING",
      "详情入口": "exact current value"
    }
  },
  "changes": {
    "master": {
      "来源ID": "EXAM408-2009-Q15",
      "详情入口": "answer-safe source and detail locator"
    },
    "safe_card": {},
    "details_metadata": {
      "来源定位": "规范真题语料 EXAM408-2009-Q15；章节别名 CO03-Q066",
      "来源 ID": "EXAM408-2009-Q15"
    },
    "relations": [],
    "review_unit": {
      "original_track": {
        "expected": {
          "来源ID": "VISUAL_PENDING",
          "完整来源位置": "exact current value",
          "详情入口": "exact current value"
        },
        "after": {
          "来源ID": "EXAM408-2009-Q15",
          "完整来源位置": "answer-safe source locator",
          "详情入口": "answer-safe detail locator"
        }
      }
    }
  }
}
```

`expected.master` must include every changed master field plus `ID`, `年份`,
`科目`, `首次做题日期`, `最近复做日期`, and `最近错误记录`.

## Allowed master fields

- `来源ID`
- `主模块`
- `主知识点`
- `副知识点`
- `命中知识点`
- `题型`
- `核心考点`
- `模糊概念`
- `错因标签`
- `详情入口`

The writer automatically projects these fields to the subject mirror, year and
date indexes, knowledge-hit index, and safe card where corresponding columns or
labels exist.

## Allowed explicit safe-card fields

- `题图`
- `解析图或解析正文`
- `复做保护说明`

These fields only reconcile answer-safe attachment registration and its redo
protection surface after the bound question and solution evidence have passed
SHA-256 validation.  They may not change `作答痕迹`, `当前附件来源`, user facts,
dates, or identity fields.
`复做保护说明` may only remove stale attachment/source-pending language or
refresh answer-safe redo instructions after the same bound evidence passes.

## Relation patch

```json
{
  "action": "replace",
  "a": "CO_2099_908",
  "b": "CO_2099_905",
  "confidence": "high",
  "expected": {
    "type": "R06",
    "strength": "强",
    "reason": "exact current reason",
    "priority": "高"
  },
  "after": {
    "type": "R06",
    "strength": "中",
    "reason": "answer-safe question-specific reason",
    "priority": "中"
  }
}
```

For `remove`, omit `after`. For `add`, `expected` must be `null` or `{}` and an
`after` object is required.

## Review-unit patch

- `mapping.after` may contain only `证据等级`, `实际考点`, `覆盖方式`,
  `复核状态`, or `待补事项`.
- `original_track.after` may contain only `来源ID`, `定位标记`, `段落号`,
  `完整来源位置`, or `详情入口`.
- `unit.after` may contain only `模块`, `内容名称`, `闭卷问题`, or
  `派生自查口径`, and is rejected unless the review unit covers only the target
  node.
- `card_replacements` are exact, unique string replacements in one `card_id`.
  The `card_id` must be a primary or secondary review unit currently mapped to
  `node_id`. In a shared review unit, both replacement strings must explicitly
  contain `node_id`. Replacements may not touch schedule, mastery, dates, error
  counts, or protected YAML.
- Detail-card metadata may not change the formal node ID, subject, year, dates,
  user error entry, or user answer facts.

## Commands

Dry run:

```text
python3 scripts/apply_existing_node_maintenance_408.py publish PACKAGE.json \
  --repo /Users/your-user/Documents/kaoyan-408 \
  --details-root /Users/your-user/Documents/kaoyan-408-details
```

Apply exactly one node:

```text
python3 scripts/apply_existing_node_maintenance_408.py publish PACKAGE.json \
  --repo /Users/your-user/Documents/kaoyan-408 \
  --details-root /Users/your-user/Documents/kaoyan-408-details \
  --idempotency-key existing-node-maintenance:NODE:DATE:VERSION \
  --apply
```

Resume only the derived closeout for a committed receipt:

```text
python3 scripts/apply_existing_node_maintenance_408.py closeout \
  --repo /Users/your-user/Documents/kaoyan-408 \
  --details-root /Users/your-user/Documents/kaoyan-408-details \
  --review-date 2026-07-22 \
  --receipt /absolute/path/to/receipt.json
```
