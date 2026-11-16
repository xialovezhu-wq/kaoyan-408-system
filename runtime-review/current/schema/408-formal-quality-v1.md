# 408 本地正式入库质量合同

本机位置与解释器沿 `codex-skill-sources/_shared/kaoyan-408/local-paths.md` 定位；下列仓库命令通过标准库引导入口选择具备运行依赖的现有 Python，不绑定旧电脑路径。

适用范围是有实际内容的新 408 正式批次，包括单题、原日期编纂、正确复做、同题核重和 concept_only。当前会话打开原件、裁决语义、写成稿并调用唯一写者。旧状态名 awaiting_web_enhancement 只标识已有 Capture。已有正式事务保留原身份，只恢复已提交的缺层。快录、日常讲题和准备阅读只读本地证据及索引。

## 作者顺序

1. 查 backlog-plan 或当前精确 Capture，优先接原回执。冻结同一上海学习日、原件、补充链和本机来源版本。历史出处按其已登记的固定来源身份核验。空队列沿原生 NO_PENDING_CAPTURE 收尾。确认所有应处理题面和附件是否可读。
2. 在经核验的 T9 408 作者工作区创建质量 scope。读取原件后只填已核、脱敏、去答案的机制、条件和候选线索。附件逐件核实。纸面表格和机制图写确实读取的行列、字段、方向或状态。完整对话、原附件、真实身份和绝对路径留本机。
3. 运行 begin 得到候选回执。检查候选原文和实际 Decisions 回答，落实到本批编纂。批次内重试使用原 scope。
4. 用 inventory 的 `--point` 核对每个直接观察点的已登记反向依赖。重开具体原记录，确认概况应更新或有依据可保留。按现行 enhancement、knowledge profile 和精确教案合同写本地成稿，运行 prepare --source-dir。用 requirements 枚举终稿需支持的声明和哈希。逐条填脱敏声明及支撑它的已绑定机制单元。
5. 运行 finalize。遇缺证、拒答或支持不足保留已有回执，修复对应条目。终稿变动用新的原生 prepared SHA，仍沿原 scope。只有 QUALITY_PREPARED 能开始 apply。
6. 区分正式写者 model_call_count 和 formal_quality 的真实调用覆盖。提交后先核准原生来源版本，对全库合规公开条目核向量、重建映射并读回全部父点和 KSP。COMPLETE 需要两种 API、批次覆盖、全库路由验回，以及本机原生讲题视图读回摘要、下一检查和同一来源版本。`protected` 隐藏个人内容，授权视图匹配已接受概况。读回成功即封 `VERIFIED_CURRENT`。COMMITTED_QUALITY_PENDING 表示已有正式提交，沿原 apply/status 补齐。

~~~text
python3 scripts/runtime_paths_408.py --run formal_quality_408.py begin --scope /经核验T9作者目录/scope.json
python3 scripts/runtime_paths_408.py --run enhancement_intake_408.py prepare --source-dir /经核验T9作者目录/成稿
python3 scripts/runtime_paths_408.py --run formal_quality_408.py requirements --prepared /原prepared回执
python3 scripts/runtime_paths_408.py --run formal_quality_408.py finalize --run-id Q408-... --prepared /原prepared回执 --reviewed /经核验T9作者目录/reviewed.json
python3 scripts/runtime_paths_408.py --run enhancement_intake_408.py apply --prepared /原prepared回执
python3 scripts/runtime_paths_408.py --run enhancement_intake_408.py status --prepared /原prepared回执
~~~

## 输入和凭证

scope 采用 cs408-formal-quality-scope-v1，redacted=true。manifest 与本地增强 manifest 的身份、原日期、快照和 captures 相同，写稿前可用空 files 和初步 affected_points。units 数组覆盖每个 Capture，给 id（唯一短 ID）、capture_id、mechanism、conditions（可空数组）、query、anchors、points 和 related（已核候选 ID，可空）。anchors 以 package_sha256 绑定原包，再用原 conversation.json 的 ordinal 或已查验 attachment_sha256 指向片段。旧结构化证据用已绑定源对象的 ordinal 0。attachment_reviews 按附件 SHA 分类为 verified_text、verified_figure、archive_only 或 required_missing。exclude_years 可额外收窄，系统合并当前年度保护。

reviewed 采用 cs408-formal-quality-reviewed-v1，redacted=true，给 prepared_sha256、statements 和 candidates。statements 以 requirements 返回的 ID 为键，值给 sha256、去标识的 text 和所用 units。candidates 以 begin 的 judgment ID 为键，值给 action、location、reason。action 为 adopt、revise、reject、keep_conditions 或 unresolved。采用、改写、保留并列绑定实际终稿声明。拒绝绑定对应声明或 item/<Capture>/decision。未解决冲突保留 needs_user。

新批次两类 API 使用本批真实材料和同批终稿。历史缓存只重用未变旧内容。同批可续接已验请求。写入前基线漂移时，用原作者 run_id、原 Capture 与相同机制投影重新运行 begin，旧快照候选和请求保留。逐项输入哈希相同的请求复用，已变输入重算；原 prepared 已失效时用同一运行的新成稿。失败、部分返回、拒答和缺项留凭证。候选用精确来源、词法、向量和一次有界补查。相似度只找材料，Decisions 判断给定证据。正式归属、条件、事实与评分由本地原生证据和作者核定。

写前 proof 固定原件、旧库基线、prepared SHA、所用模型、真实请求、覆盖及采用。写后按原生结果和索引签发同版凭证。私有账本和向量留在 .private/formal-quality-v1，QMD 资格独立。

## 每批整库验收

- 合规目录枚举公开机制、题目安全投影、正式卡、父点和 KSP。年度保护及已给前置在选入前执行。向量键包含投影、模型、维度和文本。全库重算路由和来源绑定，对新增、变更、缺失或失效向量用 OpenAI 补齐，其余读取已验缓存。`vector_coverage` 分别记条目数、去重输入、缓存命中、回执恢复和本次新嵌入。`statements` 仍是本批终稿核证凭证。
- `personalization_coverage` 回读当前原生全部父点与 KSP，指出新旧证据不一致、跨考点依赖变化、只有事实记录及没有记录等状态。过期历史保留真实状态并列待复核原因，不能用重打向量将旧结论标成当前。通过 Decisions 且本地实际采用的关联提示，须有可核验原题范围、两端机制和出处才随版本续接。提示不写正式关系边、学情或掌握度。
- `manifest.affected_points` 仍是本批真实观察点。另声明 `dependent_profile_points` 可将确实依赖这些点的历史概况按证据纳入本批唯一写者。依赖来自目标的不可变上一版 `cross_point_sources`，复核时引用实际原记录，保证本批证据能支持保留或改变的具体条件。同一正式写入后读取直接点和已更新依赖点。仅章节近似、向量相似或有关系链的远端点进入候选，不因此新增学情。

## 只读与恢复

memory coverage 可用 --expected-source-version 固定分页版本。精确定位仍用 resolve，歧义或必要条件缺项时用同范围 memory discover --evidence-gap 补查一次。保留候选与省略项。

`python3 scripts/runtime_paths_408.py --run formal_quality_408.py inventory [--point <规范点>]` 只读覆盖和依赖。已完成批次之后若原生快照或保护范围单独前移，经授权用同入口 `refresh-index` 对齐完整向量索引及新路由。它不提交新学习，也不回放旧 apply。用户明确要求远程访问或外发时，才可用 `Study-Pro-Bridge/study_publication.py publish --subject 408 --event-id <refresh-index 返回的 publication_event_id>`，再读回 `verify-current --subject 408` 和发布回执绑定的索引 SHA；现有正式事务打开时依原回执恢复。

旧 --zip 和 --evidence-prescreen 是历史协议。新 408 正式批次用本地作者目录。已提交批次用原 prepared、WAL 和 terminal 恢复，保留原历史。

终稿可附 evidence_by_unit，将各单元的去标识原回合分别列出。每项只给 package_sha256、ordinal 和 text。程序只接纳 begin 已冻结的回合，由该回合提供角色和次序。先核事实或复测建议的证据性质，再调用 Decisions。完全相同的文本和证据共用一次核证，完全相同的嵌入文本共用一条实际向量回执。每个终稿位置仍有自己的来源与证明。
