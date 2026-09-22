# Visible intake output

Follow workspace chat formatting. Use the template matching the actual terminal
state; never merge fast capture and formal completion wording.

## Fast fact capture

```text
快速捕获结果

Capture ID：{CAP-ID}
学习日期：{YYYY-MM-DD}
稳定证据：{saved locator count and hash status}
身份提示：{existing formal ID | new candidate | unknown}
事实状态：已保存
质量状态：{待日终编纂 | 已捕获未授权}

待补充

{none or facts left for daily curation}
```

Do not say “正式入库完成”, “高质量入库完成”, or “全部审计通过”. Do not include
related old questions, relations, topic chains, or a global audit inventory. If the
stable evidence write failed, report the persistence blocker and no capture ID.

## Explicit formal request

Route to `kaoyan-408-daily-intake-curation` and use its output contract. This quick
capture Skill never claims formal completion, runs full-now, or records a formal result.
