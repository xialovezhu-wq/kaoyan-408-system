# 408 增量验收

2026-11-16 · `KAOYAN-MEMORY-20261116` · 训练投影 B2 保持隔离

## 冻结身份与判分

- `baseline_sha`（本机 B1 范围）`a126d2056d0acda2086e0c8e9331c3734e4997f2`。`tested_sha`（本机 B2 范围）`65cb4b320be15374669bf3ce97e9bae17e49b790`。B2 的 16 项变更文件逐一比对被测工作树字节。完整运行依赖来自同批冻结的 v1.1／LongMemEval-V2 本机快照。
- 同机 B1/B2 查询使用合成源，固定日期与权限。交错基准两臂账本 SHA-256 相同，为 `c6eb1d0332a74302405e3c44b4f3128d3b8832120bbd690b11a1f2fc87bd69b2`，均有 12 行。本机现行 B1 额外执行一次权限明确的两点只读验回。B0 只用于必要诊断，本轮无 B0 模型输出。
- 合成保持主科隔离、同题和独立 Pxx 权限。验收检查返回的确切订正文、尾部条件、并存条件、依据版本、提示依赖和否定样本。可用状态和字节预算同时检查，模型自报不计分。
- 开发集 10 项测试，覆盖 12 个以上的组合输入。首次留出 v1 为 23/24，普通点因关联题的订正带入待审状态。改用显式绑定后将此例纳入开发回归。留出 v2 使用不同题族，16 个四分科条件变动及 8 个控制项，24/24，数据 SHA-256 `4bb29be390f742192a78be784e054aeaed8af55d5e391c2459106d63d9f6ce6e`。

## 实际执行

命令在各自隔离根执行。`B1`、`B2`、`FIXTURE`、`STATE` 为同批已核验的冻结、候选及合成路径。测试临时根同属 408，`PYTHONDONTWRITEBYTECODE=1`。

| 层 | 命令 | 退出码与结果 |
| --- | --- | --- |
| 附件 helper | `python3 408_compaction_diagnostic.py --scripts-root B1/scripts --source-sha BASELINE_SHA`，B2 同形 | 两臂均 0。B1 `OBSERVED`，914 → 814 字节，有旧误解、缺订正。B2 `NOT_OBSERVED`，279 字节，附件输入本身无审核字段，整段争议文字隐藏。 |
| 现有知识、路径、概况 | `python3 -m unittest discover -s tests -p 'test_knowledge*408.py'` | B1 0，B2 0，各 103 项。试运行缺本机固定分词依赖，使用原已安装 jieba 0.42.1 的隔离副本后通过。 |
| C1 开发及真实 CLI | `python3 -m unittest discover -s tests -p 'test_correction_budget_408.py'` | 0，10 项，含 UTF-8、4500/8192 两／三点、审阅与缺审、顺序反转、保护、原生 source guard。 |
| C1 留出 | `python3 -m unittest discover -s tests -p 'test_correction_budget_holdout_408.py'` | 首版 1，冻结 v2 0，24 个投影场景通过。 |
| 原有完整查询／源修订／账本 | `PYTHONPATH=tests python3 -m unittest test_exam_tutoring_context_408 test_seal_knowledge_observations_408 test_local_quality_408 test_learning_evidence_408 test_legacy_408_writer_retirement` | 0，43 项。 |
| 提交后恢复 | `PYTHONPATH=tests python3 -m unittest test_study_commit_followup_408` | 0，9 项。覆盖外层锁、重复原操作、旧 cache 失效、发布故障和派生仅补续。 |
| Skill 静态 | `python3 codex-skill-sources/_shared/kaoyan-408/scripts/validate_408_skills.py --skills-root codex-skill-sources --schema-root schema` | B1、B2 均为 1。35 → 34 个原有断言，本轮替换过时的模型必须固定为 Astra 的断言，无新增失败。 |
| Skill 快检／Mac 发现 | `python3 <skill-creator>/scripts/quick_validate.py <installed-skill-dir>`；`codex app-server --listen stdio://` → `skills/list(forceReload=true)` | 10 次快检均 0。新进程诊断 0，发现 10 项，资源哈希一致。见 Skill 对齐。 |
| 答案范围及共享 | 在隔离 Bridge 根 `python3 -m unittest discover -s tests -p 'test_study_mcp_english_context.py'`。在已核验 `study-intake-v2-backend` 根 `python3 scripts/verify_projection_rebuilds.py --subject cs408 --cs408-repo FIXTURE --artifact-root ARTIFACTS --output RECEIPT` | 0、0。Bridge 10 项。后端 CLI 核隔离派生恢复，现成合成 verifier 单测 4 项通过。无公共写入或运行绑定调整。 |

直接 `resolve` 的多点测试内实际执行：

```sh
python3 scripts/exam_tutoring_context_408.py resolve --repo FIXTURE --output-root STATE --concept OS99-10 --concept CO99-10 --view after_attempt --personal-scope teaching --materials core --max-bytes 4500 --read-stats
```

同范围传 `--known-version` 返回 `unchanged` 并少读正文；换为 `protected` 时不复用已开放版本。三点 4500 压力输入显式整组省略，该点仍携带 `evidence_object`。8192 时压缩器返回三条完整订正。撤回、过期与未审以不同状态表达。

## 延迟、读取与成本

`python3 tests/benchmark_correction_budget_408.py --baseline B1 --candidate B2 --output RESULT`，每场景每臂 30 次并交错。热为进程内重复完整查询，冷为每次新 CLI 进程并含启动与 JSON 输出。表中顺序为 B1 → B2，单位 ms 或字节。

| 场景 | 热 p50 | 热 p95 | 冷 CLI p50 | 冷 CLI p95 | 返回包 | 物化读取件数及字节 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 普通单点 8192 | 2.14 → 2.08 | 2.53 → 3.18 | 126.05 → 126.78 | 179.80 → 166.81 | 2200 → 2200 | 4 → 4 件，54894 → 54894 字节 |
| 三点 4500 | 3.00 → 3.09 | 3.58 → 3.61 | 126.74 → 126.50 | 131.73 → 134.10 | 4309 → 3474 | 9 → 9 件，70243 → 72933 字节 |
| 三点 8192 | 3.05 → 3.32 | 3.40 → 3.63 | 127.58 → 129.71 | 138.87 → 139.94 | 4309 → 7430 | 9 → 9 件，70243 → 72933 字节 |

所有配对满足质量可用、UTF-8 上限、热 p95 不超过 max(B1×1.05，B1+20ms) 和现有 100/250 ms 门槛。基准普通单点包大小和读取量相同。三点 8192 下完整订正单元由 0 增至 3，增加的读取量为已物化对象内容，读取件数维持 9。夹具进程峰值 RSS 为 283.2 → 282.8 MiB，包含初始化和派生生成，无法拆出单个热调用内存。

原 `benchmark_knowledge_tutoring_408.py --repo FIXTURE --iterations 30` 独立运行，两臂 `all_queries_ready=true` 与 `local_budget_pass=true`，函数 p95 4.14 → 4.42，抽样 CLI 最大 130.30 → 131.48。该旧基准无法替代上面的有界讲题验证。

本轮候选检索外部模型调用 0、token 0、正式写入 0、新增服务和依赖 0。实际模型首段解释、完整答复时延、正式编纂耗时与成本、桌面旧会话重新加载正文、自动任务执行均未测。当前本机默认模型配置与用户实时可选模型分开记录。缺本批可核实的三科共享评估额度，真实讲题配对 0/24，生产启用 `NEEDS_VALIDATION`。没有对历史调用费用做估算。

## 回退与完整性

隔离探针使用同一串合成正式源和 `refresh-current --repo FIXTURE --output-root STATE --refresh-date 2026-08-30`。从 B1 切 B2 时旧缓存拒绝，刷新后完整订正可读。恢复 B1 三模块时反方向重复，旧 B2 版本同样失效，刷新后回到 B1。两次读回均为 `available`，各次查询 `write_count=0`。账本、映射和题表三项哈希相同。已提交后出现通知／发布失败的隔离测试保留原幂等身份。当前生产保护、正式写者、现行派生指针以及 7 项本机哨兵一致。

代码与 Skill 补丁以 `tested_sha` 相对 `baseline_sha` 导出。正式四份报告与补丁进入审查分支。审查提交的文件树读回和分支最终 SHA 另附交付回执。
