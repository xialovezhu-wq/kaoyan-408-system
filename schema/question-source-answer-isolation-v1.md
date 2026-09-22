# 原题题面答案隔离证明 v1

`question-*.png` 只是文件命名约定，不是答案隔离证据。原题进入可展示状态前，必须完成以下冷路径：

1. `prepare` 先真实解码每个明确列出的 PNG、JPEG 或 WebP 题面资产，再计算路径、字节数、逐文件 SHA-256 和资产集合 SHA-256，生成随机 nonce 与受管签名的 review request；扩展名正确但无法解码的文件必须 fail closed。
2. 与题面 producer 不同的 verifier 独立审阅资产字节，并提交精确 request ID、request SHA-256、nonce、资产集合 SHA-256，以及题面不含答案或解析的明确确认。
3. `finalize` 重放 request 与 submission，重新解码并核对当前资产，确认 producer/verifier 的 actor 和 execution 标签均不同、资产未漂移，再写入固定位置 `assets/<正式ID>/answer-isolation-attestation.json`。
4. bridge 和 personalization index 只在完整证明链可重放时设置 `surface_verified=true`、`answer_isolated=true` 和 `original_question_eligible=true`。缺失、漂移或格式错误一律 fail closed。

身份字段是受管执行标签，不是对现实人物身份的密码学证明。流水线证明的是两个绑定标签和 execution ID 不同，以及 request、nonce、submission 与精确资产集合一致。

准备请求：

```bash
python3 scripts/question_source_attestation_408.py prepare \
  --details-root /path/to/kaoyan-408-details \
  --formal-node-id DS_2099_915 \
  --question-asset assets/DS_2099_915/question-01.png \
  --asset-producer-identity-json '{"actor_id":"surface-producer","execution_id":"producer-run-001","role":"question_surface_producer"}' \
  --verifier-identity-json '{"actor_id":"isolation-reviewer","execution_id":"reviewer-run-002","role":"answer_isolation_verifier"}' \
  --verification-method independent_visual_review
```

verifier 按 request 创建独立 submission 后，再执行：

```bash
python3 scripts/question_source_attestation_408.py finalize \
  --details-root /path/to/kaoyan-408-details \
  --request .answer-isolation-work/QAIR-.../request.json \
  --submission .answer-isolation-work/QAIR-.../review-submission.json \
  --output /path/to/kaoyan-408-details/assets/DS_2099_915/answer-isolation-attestation.json
```

日终流程可以为明确冻结批次准备 request，但不得自动替 verifier 提交结论，也不得为现有生产资产批量补发证明。
