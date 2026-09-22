# Daily curation output

Follow workspace chat formatting and use plain visible labels.

```text
日终编纂结果

截止日期：{YYYY-MM-DD, Asia/Shanghai}
计划哈希：{canonical plan SHA-256}
执行日期顺序：{oldest date -> newest date}
逐日批次：{date -> CUR-... and capture-set SHA-256}
冻结 Capture 总数：{N}

处理结果

正式编纂完成：{count and CAP -> formal ID mappings}
无需变更：{count and CAP -> formal ID plus verification hash}
需要你确认：{count and exact immutable fields}
失败待恢复：{count and receipt-bound recovery facts}
历史证据待归档：{count and exact legacy Capture IDs plus preview/apply action}
未授权 Capture：{count, not processed}

验收

事实账本：{PASS | FAIL}
正式全局门禁：{PASS | FAIL}
批次状态：{COMPLETE | PARTIAL | NO_PENDING_CAPTURE}
最终 backlog gate：{PASS | PENDING plus exact dated items}
残项：{date, capture ID, status, reason, next action}
模型说明：{confirmed model/reasoning metadata | 未确认，不作强模型声明}

下一步

{none, one compact needs-user decision list, or exact recovery command}
```

Never include complete stems, answers, options, full learner responses, receipt
absolute paths in the tracked ledger, or invented user-error explanations.
