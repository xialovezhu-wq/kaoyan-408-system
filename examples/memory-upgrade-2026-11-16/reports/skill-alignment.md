# 408 Skill 安装及发现验收

源根 `codex-skill-sources/`，安装根 `~/.codex/skills/`。表中名称是两侧相同的目录名。新 Mac Codex app-server（`codex-cli 0.154.0`）调用 `skills/list(forceReload=true)`，按记录返回路径读回 `SKILL.md` 和全部资源，10 项全部发现、0 加载诊断错误。源码和安装包逐文件等同。Skill 序列化包哈希以相对路径与文件 SHA-256 的有序 JSON 计算。全部 10 项 frontmatter 快检通过。

| Skill | 状态 | 正常入口／权限 | 主引用 | `SKILL.md` SHA-256 | 资源包 SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `kaoyan-408-daily-intake-curation` | 活跃 | 当日／确切范围正式收尾、同版原生读回 | daily-closeout.md | `9ff9aa09d80b0179585e235702e908ca5f723fa0a1a459c99ad85803dc446e99` | `42fd298e425ba994c40cc8cee0d82eeb195f849043743a8319eec7429d9c465b` |
| `kaoyan-408-daily-study-loop` | 活跃 | 固定 v2 学习入口与 Capture 后导航 | learning-entry.md、one-call-turn.md | `81589bf5161dc5592514a3458c5701015150b2a3fa02c5bb9cadb0fbcc6a84f3` | `cc3fcc6261c6bc70ace35f465dca2fa3fa9ca457864d254e95f540b810912c9d` |
| `kaoyan-408-enhancement-intake` | 活跃 | 既有唯一 writer 和本机派生恢复 | local-quality-author.md、existing-web-return.md | `8712267294a02da5ed3a8af58c80eba75b011aba35d7b07dacbf920515e6ed53` | `e11f1a67fff5dd682e40702c1e00d44c0a106772f3faef21f23ab074c7861ccb` |
| `kaoyan-408-evening-review` | 活跃 | 交互日复盘与获准的逐问入口 | 晚间准备／答案保护资源 | `146c889aa2c75f1a98b89dec4afee083d9dcbe11ee8055afc019ad8173556adc` | `89d9655a8619e6f68dfed81dddd669513f79b0f6a79acb37959030da607ac77a` |
| `kaoyan-408-intake-coordinator` | 历史 | 点名旧 JOB、WAL 与回执核对及恢复 | coordinator_contract.md | `1d3186ebb30afbf6ebe51abc5722899d1435fe3e9ac5908e363ece1adc5a5bac` | `27230341baefb2c9f2b156a23d92ff631facb183994ab6275416285ce1078c8e` |
| `kaoyan-408-morning-control` | 活跃 | 本机 D 准备、日期门禁及已有队列续办 | d-concept-supplement.md、native-campaign-preparation.md | `22ab4f4d4bbd2a20f57c5570ec4ada4bb5f1ce5ab3935700ac77651a6dff9caf` | `1623f40f3ab1c2c60c9f3c82cca4f71116daa27017ac9a1be7c84fe2de02fae3` |
| `kaoyan-408-qa-template` | 历史 | 显式旧网页模板适用范围 | 原 QA 模板与输入约定 | `f810fd703311b1f8e7880364b0cd1272cf6df5cbd713575b30a89145aa02c831` | `f390fafa9a0936e8f008ce4c54ade5a6d988821e9c685c348e2278e520402ece` |
| `kaoyan-408-question-worker` | 活跃 | 本机精确 resolve，当前题与现有权限 | personal-learning-context.md、full-teaching-contract.md | `cfac99b30d71794995ebe713978a4ed3592e86500b05b7f10e630fa3626d8ec3` | `14f97513a88d9c9798efd8d5815d22b099d27a9015e04382cc01d0ef14aa1314` |
| `kaoyan-408-rollback-review` | 历史 | 明确旧保护复盘、原事务与归属 | rollback-decision-matrix.md | `2dc7c9249013d81915df4ab905176d4a4a821597690fd9fd194fe01368eb9dfc` | `04eec188df0a4ada8660c5c76a8a6c6c8c74a672d95fcb5d8d4b74b34d01ba29` |
| `kaoyan-408-wrong-intake` | 活跃 | 显式原回合快存及一份 Capture | route-map.md、fact-capture-schema.md | `ccac798db5c77b6024baf5fe59486817908fbec90c417981c894c0b7a5cf62e8` | `4c21d0476dee886cddc656536899493cc692e3a67803b3bd01698f5a4516af49` |

科内共用 `_shared/kaoyan-408` 的源／安装包 SHA-256 为 `ed85b3ee4c7092a9acaa4c61f5966ae4b5e0ad91fdfdffb59b0adb6f995332ff`。它由上表 Skill 引用。

本轮安装后同路径文件 SHA-256 如下。

- `~/.codex/skills/_shared/kaoyan-408/runtime-contract.md` · `1e7c54046da4f118cc480da3779ea6a8202fd282643a052e3f399172d3c901f9`

- `~/.codex/skills/_shared/kaoyan-408/scripts/validate_408_skills.py` · `01b02ba0bc242566de440dc4513c0ad6b2d29609345b167a1e38a3ebb7e06cbc`

- `~/.codex/skills/kaoyan-408-question-worker/SKILL.md` · `cfac99b30d71794995ebe713978a4ed3592e86500b05b7f10e630fa3626d8ec3`

- `~/.codex/skills/kaoyan-408-question-worker/references/personal-learning-context.md` · `45a58091f10d64f324aa39c984b36fd35d87463b15f5fbc72aeff0734759a22b`

此次证明新本机进程发现并解析安装资源。旧桌面会话中已载正文的替换以及 B2 实际讲题执行中的使用均未测。本地规则中的模型选择和耗时目标与配置实际生效及模型时延分层验证。

日终与正式编纂、学习循环、快存、晨间、晚间与历史入口核对后沿用现行资源。旧网页模板仍消费已提交的原身份；历史语料和索引文档新增只读状态与现行替代链接。全局 validator 仍退出 1，基线 35 项、候选 34 项，消除硬编码教学模型断言，余下均为原有 v8 路由、评测名和旧回执字样断言。
