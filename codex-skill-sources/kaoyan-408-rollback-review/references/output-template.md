# Rollback review output

Use plain visible labels compatible with the workspace chat-formatting rules:

```text
来源与范围

{source type, locator, date, and requested outcome}

证据判断

已验证：{facts grounded in user or primary evidence}
派生判断：{claims from GPT/Tutor summary}
缺失或冲突：{material gaps}

分类结果

{one primary classification and reason}

安全去向

{read-only next action or owning skill handoff}

写入状态

未写入正式 408 数据；{or state that the owning formal workflow completed it}
```

Do not include a broad Tutor plan, dashboard update, or old-question list unless the
user explicitly requested that separate outcome.
