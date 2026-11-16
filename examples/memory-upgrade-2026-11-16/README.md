# 408 原生摘要预算修复审查

[当前运行源码、Skill 和本机验回](../../runtime-review/current/README.md)已补齐。以下保留 B2 上线前的补丁与验收记录。后续启用见[上线报告](reports/rollout-acceptance.md)。

本审查基于已送审的 408 LongMemEval-V2 分支。公开主线的运行快照早于 Mac 的当前源码。本目录采用上一轮的脱敏补丁交付，原生基线与候选的逐文件哈希见 `native-source-manifest.json`。

在拥有相应本机基线源码的隔离目录，先执行 `git apply --check /path/to/native-candidate.patch`，通过后用 `git apply /path/to/native-candidate.patch` 应用。测试含全部合成的多点、条件变化、来源版本、授权和预算场景，现有本机开发依赖继续由所属项目管理。

本机候选 `65cb4b320be15374669bf3ce97e9bae17e49b790` 是隔离被测源码。基线和候选的使用状态、真实讲题缺项与回退证据见 [报告](reports/report.md) 和 [验收](reports/acceptance.md)。
