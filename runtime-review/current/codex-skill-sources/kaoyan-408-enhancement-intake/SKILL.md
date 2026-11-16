---
name: kaoyan-408-enhancement-intake
description: "408 本地正式增强入库。核读原始 Capture，用 OpenAI embeddings 和 Decisions 审核本批与跨点依赖，经唯一写者提交，刷新并用本机原生入口验回整库检索、个人概况及答案视图。"
---
# 408 本地正式增强入库

本机路径和解释器先按[共享路径约定](../_shared/kaoyan-408/local-paths.md)定位；下文仓库命令在已定位的项目目录执行，历史回执保留原字节。

用户明确要求正式入库时，由当前会话直接完成学习证据审核、正文和本地提交；编纂和提交在本机。每个有实际内容的新正式批次须使用 OpenAI embeddings 与 Decisions，验回两类真实请求及其用途才可提交。只请求方案或只读检查时不正式提交。保留旧 Capture 的 awaiting_web_enhancement 状态和已有证据身份，通过同一原生事务正式处理，不为切换本地通道改历史记录。

当日快存全部正式入库由 [daily-intake-curation 的当日正式收尾](../kaoyan-408-daily-intake-curation/references/daily-closeout.md) 统筹。本 Skill 负责正式数据与讲题读回，回执通过后由同一任务继续备好错点专项及全天 HTML。

1. 按上海原学习日期 replay 原生 Capture，核对用户指定日期/集合、各题原对话和补充版本，并先查本批 prepared-receipts、operations、apply-receipts 及现行批次；有写入或未竟事务则沿原回执恢复，不能重造。原始证据用脚本 conversation_package_408 / 本地归档指针重新核验。没有正式成绩、真实首答或已知题源时分别保留边界。
2. 同一日期和精确集合使用已登记的完整原生作者输入及其 source_commit。没有该集合的完整输入时，以本地 export_capture_day_408.py --date YYYY-MM-DD --capture-id CAP... 生成并登记，内部冻结的ZIP只供作者获得源文件/哈希/题模板要求，用户无需传送；本机已核定年度题另核原图及固定版本的canonical映射。既有输入、部分成稿、固定快照、原包有且仍可核验时直接复用。处理当日跨片同点只合成一次。
3. 新本地作者输入用冻结的本机来源版本核对当前教案、原图与父点/KSP。正式历史出处、已登记旧包和旧原图沿原固定来源身份核验。用首答、原话顺序和实际题设逐点区分学员表现、教师讲解、提示后变化以及未验证的范围。对照2027原纲和真题原图核定考法。
4. 先按本地正式质量合同（`<repo>/schema/408-formal-quality-v1.md`）对已冻结、脱敏的机制材料运行 begin，打开返回的旧知识来源，核对实际 Decisions 判据。当前本地作者亲自逐题按 schema/408-formal-enhancement-v1.md、408-knowledge-profile-v1.md、408-portable-question-teaching-v1.md 和408-portable-visual-teaching-v1.md写 enhancement.json / knowledge_updates.json / portable_question_teaching_updates.json 和所需教法方案。允许脚本生成索引、绑定、哈希与格式，不可用模板填充代替原话裁决。逐项写到已核验T9冷目录，保持旧概况、时间线、KSP、版本指纹和原日期；用本地作者来源 local_explicitly_authorized_semantic_content，不虚构网页报告或工具调用。无证据的关联不造新学情，学生材料过滤2013等用户隔离范围。
5. 本机成稿目录直接用脚本 prepare --source-dir <目录> 进入既有增强校验，再由 requirements 列举终稿声明，按证据完成 finalize。验回 QUALITY_PREPARED 后用 apply --prepared <回执> 逐项走唯一 writer，归档、资格和调度、完整概况、短摘要、KSP 和精确教案按本机原生查询验回。正确题、已应用项、concept_only 各走原动作，不追加错误次数。T9产物先check-output后register，值不符/盘离线不新建假目录。准备结果与实际正式提交分别报告。

## 全库刷新与跨考点审核

- 确认本批实际观察点后，用 `python3 scripts/runtime_paths_408.py --run formal_quality_408.py inventory --point <本批规范点>` 核对现库，多点重复 `--point`。逐个重开 `affected_dependents` 中已登记的证据依赖，核对原记录、当前概况与本批可改变的条件。机制相似只能提出候选。已核清的跨点概况在本地 `manifest.dependent_profile_points` 声明，与直接观察点合入同一 `knowledge_updates.json`，按上一版本、真实证据和双 API 核证走唯一写者。题级观察、评分和 FSRS 仍按实际作答。无法核实的历史概况在覆盖回执保留复核原因。
- `apply` 收尾以当前全部合规目录重建向量路由。相同投影、模型、维度和文本复用已验 OpenAI 向量，新增、变化或缺失才补调。回读所有父点和 KSP 路由，核对 `formal_quality.vector_coverage`、`personalization_coverage` 和本机来源版本。报告实际重算与复用数量以及过期原因。旧库证据积压不能仅凭向量结果批量改写成个人判断。修复现有索引错位时按质量合同的 `refresh-index` 复用原库，不重开已完成入库。

核对考纲内完整考法、逐点证据强度和文字/图解教案时，按 [本地质量编纂合同](references/local-quality-author.md) 做版本审查与逐点合并。

## 当前题与已有证据

当前题尚未封存、用户明确要求正式入库时，用 mark_study_episode_408.py record --intent formal --start-anchor <本题最早用户锚点> --end-text <真实正式请求原话> --compact 保存原始证据，精确年度题加 --question-id <ID> --bind-annual。命中已有同一证据即复用原 Capture。封存回执后，在同一任务直接继续本地质量候选、编纂、prepare、终稿核证、apply 和 status。用户单独说快速录入或快速保存时只沿 wrong-intake 封存并给本题速记；普通讲解只读最新热视图。

截止日清单读取 intake_fact_capture_408.py backlog-plan 的 local_enhancement_capture_ids；日期下仍有 active 或已有prepared/partial/COMMITTED 先查原回执并恢复。只读计划不能直接启动旧 start-batch 消费新待增强项。年度standalone的封包若有规范原题图且与固定版本逐字节吻合，本地输入额附该身份候选的题模板和相关父点/KSP；需核本题原话后走 recent_contact_408.py bind-capture --allow-pending-review 等既有来源绑定，不能把候选或图像存在直接当成个人学情。

原生 backlog-plan 的 final_gate.pending_enhancement_operations 给出未收尾的 prepared_receipt 和原批范围。题级已消费时用这个身份恢复概况、归档、预测或本机读回；已有完成继任的旧失败不重开。若精确请求仅覆盖原批的一部分，inspect_original_scope_only 只读核对，不能扩到其他Capture提交。

## 编纂与读回

正式作者用 `resolve --personal-scope full --materials core` 获取同版父点或 KSP 的完整语义。需要历史或出处时用 `memory history|compare|trace` 回读冻结版本和真实来源。日常讲题取 `resolve --personal-scope teaching --materials core`；旧 wiki 和短投影只提供其实际覆盖范围。

需定位身份未知的旧学习证据时，用discover的experience意图与实际actor查找，再沿trace_request核原出处；用户表现用user，助手解释用assistant，不将两者混成同一种观察。区分native_record_summary与核过原包/turn的verified_original_turn，片段不替代本批完整原件；相关、最近或高分命中均不直接构成复错或掌握判断。

新写或实际修订的个人判断按 `schema/408-knowledge-profile-v1.md` 核对稳定身份、适用条件、首答和提示前后原话。`content` 把条件、已成立步骤及未验证边界写成可独立保留的一段。确有同条件反证时用 `supersedes` 与 `change_reason`，不同条件分别写，未裁决冲突保持 `conflicted`。只有直接观察到的断点才作为该点错因。沿原 writer 保存完整轨迹，更正、撤回用既有追加链。旧格式仍可精确核源。

把概况写成下次讲错题可直接用的内容。记录四项：明确讲解或图解偏好及适用范围、本人的第一个具体断点和已成立步骤、仍缺或尚未独立验证的条件、下一次只补什么或怎样检查。用 `overview`、`short_summary`、`teaching_focus`、`teaching_plan` 与 `next_check` 的现有字段。全科讲法来自用户当前约定，具体题型偏好只有原话支持时写入并核定范围。没有可定位的个人表现时保留事实条目。自报理解、点击、提示后修复、独立成立各按真实证据记载。

提交后验回本机完整个人读取、相关依赖与更正引用及 Obsidian 当前阅读页。同一来源版本的摘要、下一检查及三种答案视图通过后完成质量回执。索引或读回失败只补该层。待审 Capture 仍按待审事实定位。只有用户明确要求远程访问或外发时，另走发布。

本地冻结包是真实源索引；使用其 input_id 与原始 requirements 字节 SHA。当前作者在本机冷目录逐题落已核成稿，依本地正式数据合同准备目录。检查既有WAL和已封材料后，new/redo用原生题卡写者，correct_review保留正确记录，concept_only只写关联点表现，already_applied只恢复缺层。原生分配新ID。保留时间、题源、原图及补充链，重做首次日期取旧节点，旧详情仅追加本包展示区块。

在增强目录使用 prepare --source-dir，原地恢复用 --resume-prepared。新正式批次走上述双 API 质量合同。apply/status 验回原件归档、准入/预测、父点三视图及每个KSP关键词、精确题讲法和本机来源版本。抽取本批确实更新的精确题及相关 KSP，按同版 question-worker 的真实 `resolve` 入口验回新断点、讲解切入、`next_check` 与适用的图解方案，核查 protected 的隐藏边界。预测日期用原学习日次日或已明确的 --forecast-date，恢复沿原身份。看板刷新后核对本批待处理交集，登记T9产物并读回。已正式提交但未完整验回时只恢复相应层。

只有原事务已提交且需查证历史回执时才读[旧包兼容流程](references/existing-web-return.md)。本机既有资料和已审稿可直接读取，不能让用户手动搬运内部文件来完成正式入库。
