# 408 记忆系统当前运行审查

2026-11-16 · MEMORY-DELTA-20261116 / 408 · 本机 B2 已生效，替代校验已更新。

## 当前源码

R-C1 已补齐 311 个文件，含现行 [Python 和启动入口](scripts/)、[接口合同](schema/)、[Skill 与科内引用](codex-skill-sources/)、必要自有依赖及一份定点回归。可直接读完整 [claims 校验器](scripts/knowledge_profile_408.py)、[B2 教学包取舍](scripts/teaching_memory_408.py)、[正式提交后刷新与恢复](scripts/study_commit_followup_408.py) 和 [质量索引](scripts/formal_quality_408.py)。这里的源码供复审，实际运行继续使用既有本机测试库。

[逐文件清单](runtime-source-manifest.json)列出运行路径、发布路径、用途、依赖和两侧 SHA-256。307 个文件与原件逐字节相同。4 个文件将本机用户目录或归档卷身份与哨兵值改为占位，路径与归档校验分支保留。会话、个人学习数据、原题和答案、部署绑定与凭据均留在本机。科外 Bridge、读 MCP、入库后端的审查版本及本机 Bridge 相关文件哈希见清单。共享变更请求为 NONE。

## 本机正常入口

```sh
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py resolve \
  --repo . --concept <已核规范点或KSP> --view <本轮授权视图> \
  --personal-scope teaching --materials core
```

从现有 408 库根运行。解释器由路径引导器解析。私有详情、运行状态及正式数据仍由现有配置定位。该次正常读回使用默认 8192 字节预算。当前 `source_version` 为 `1359438276f219408ca7a7a4483592cec017c7a4b47c93694542c6947c19d147`。原生 manifest 绑定实际导入的 `knowledge_profile_408.py`，SHA-256 为 `d2e458570af0d6bfc5125b35160b042aabb33e03397bfb3c93f3295343f9fb81`。已安装正式编纂 Skill 的作者参考与源文件一致，SHA-256 为 `7a5f58d323e5b60e67c90079f23ceb5daeeeb67c3789483b4809239db695ea71`。本机 Codex 0.154.0 重新发现 10 项安装入口，零诊断。正在运行的旧桌面会话是否重新读取了此前缓存的正文未单独验证。

## 定点验证

- 替代边界的合成反例中，旧校验接受了带 `supersedes` 的待核推断，原实答判断退到历史。更新后拒绝该替代，仍允许保留原判断并记录未决冲突。12 项 claims 校验和 1 项既有原生更正回归通过。
- 本机原生刷新后，答后双点读回 8,153 字节，返回两条核过的定点补读引用。受保护读回 1,825 字节，个人证据和答案正文未返回，答后 `known-version` 未被复用。节点总表、节点映射、两份账本和个人概况指针五项哨兵一致。质量索引读回 `VERIFIED_CURRENT`，复用 3,926 个向量输入。模型调用和正式写入均为 0。[读回与前后记录](before-after.json)
- R-C2 保留。真实双点样本未触发影响判断的订正缺口。B2 仍按原预算返回省略、待审及来源状态，Skill 在缺口会改变讲法时沿同一题和权限补读。原有提示依赖、撤回、来源校验和权限范围均沿用。

## 版本关系

审查基准为 `209b2e76ee7df50f72c38bfe77c3329fea2a5e3b`。本轮被验证的当前运行源码对应审查提交 `763be1f9d0da74d88beac5d03366fa61246fdd3a`，文档和精简回执另提交。实际运行库含工作树字节，对应关系以逐文件 SHA-256 和原生代际为准。

[原 B2 上线报告](../../examples/memory-upgrade-2026-11-16/reports/rollout-acceptance.md)保留。其三份 B2 模块与本机当前字节一致。旧 `native-candidate.patch` 和当时的报告记录上线前审查，保留供追溯。

本轮验证覆盖现行原生入口、替代校验和文件映射。全库测试、端到端正式写入、讲题准确率及生成时延未测试。
