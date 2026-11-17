# 408 记忆系统当前运行审查

2026-11-16 · B2、实答判断替代门禁与普通讲题查询修复均已在本机生效。

## 2026-11-17 V2 增量

同一精确点的并列证据按稳定标识展示。个人来源、日终续办和答后浮点对照更新在对应 Skill 与合同。[V2 逐文件清单](memory-integrity-v2-source-manifest.json)给出本机与审阅哈希。三份 Skill 文件只导出 V2 修改，沿用审阅分支的既有正文。以下源版本和验收指 11 月 16 日基线。

## 普通查询与 Skill

- 精确 Pxx 复用同版课件的逐问小点绑定。显式选择只接受本问绑定。没有有效绑定时沿原答案保护使用本题及当前作答。
- 有界 core 保留精确课件指针。对于状态未变化的文件，查询复用编制时核过的摘要。实际打开课件时继续核对完整内容、版本和权限。
- 2015 年已有的 23 个逐问绑定全部命中。实库定点检查确认精确题号可一次读到 3 条已绑定个人判断，兄弟小点被拒绝，受保护视图保持隔离。38 项相关测试通过，另有 5 条旧预算断言在基线也同样失败。[当前读回](query-alignment-readback.json)

## 运行与发布对应

当前本机 source_version 为 5ff7b3551eaa1a280bebd8c8d4f0561f9ee6405fe04d3c93372f649a131a793a。现行质量索引为 VERIFIED_CURRENT。实际运行入口：

~~~sh
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py resolve --repo . \
  --current-question-id <已核题号或独立小问> --view <本轮授权视图> \
  --personal-scope teaching --materials core
~~~

[逐文件清单](runtime-source-manifest.json)记录 315 个文件的运行与发布路径、用途、依赖和两侧 SHA-256。311 个文件与本机原件逐字节一致。4 个既有脱敏文件分别记录运行和发布哈希。原生 manifest 校验的查询模块、课件编译器和已安装的错题讲解 Skill 与本次发布字节一致。个人原始记录、答案、部署绑定及凭据留在本机。

[Python 和启动入口](scripts/) · [接口合同](schema/) · [Skill 与科内引用](codex-skill-sources/) · [合成测试](tests/)

## 历史与范围

本次衔接上一份审查提交 95b50be7ffe38feb8f352874cc453eda44f6fc85。既有 [B2 上线报告](../../examples/memory-upgrade-2026-11-16/reports/rollout-acceptance.md) 和 [替代门禁验收](before-after.json)保留原版本数据。当前本机读取与公开快照的对应关系由本页的源版本、路径和文件哈希确认。

本轮覆盖已具备逐问绑定的实际入口及发布读回。真实讲题首段耗时、全库语义质量和旧桌面会话热重载未测。新增正式学习写入和模型调用均为 0。
