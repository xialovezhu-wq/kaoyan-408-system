# 408 网页加强包成稿分工

以下规则来自实际回包问题。先复用输入包固定的 source_commit、snapshot_id 与 local_version；仅未提供固定身份时调用 Study_Library.library 一次。不要让各分片重新选择不同基线。

## 网页本轮完成

- 完整读取本片原件，独立核对首答、理由、提示后改变、助手错误和实际题源；Capture 不等于错题。按日期、run_id、item_id、capture_id 共同识别题目，不能把另一天同号 MCQ 的说明套入本题。
- 逐 Capture 写完 enhancement.items、逐点观察及对应完整概况/短摘要/时间线/下一次检查；同点各题事实先在本片合并，跨片贡献标明来源。解释不确定性，但从最终个人概况中移除打包进度、未正式写入等临时流程措辞。
- 已有正式 ID 用 `Study_Library.query(subject="408", domain="question_refs", ids=[formal_id], source_commit=固定版本, limit=1, max_bytes=24000)` 取得真实来源、两项 formal_state hash 和实际图像路径。不得用关联 ID、年份或“已审核”标签代替实际题面身份。
- redo 的 first_done_date 从同版 formal_nodes 的“首次做题日期”原样保留，latest_review_date 才是本次真实学习日期；不要把首次日期改成本次日期。若省略首次日期，本地只可按已核实的原节点补入，不能凭模板猜测。旧review/outcome存在不等于本Capture已正式入库，不得据此随意使用already_applied。
- 独立正确且没有实际推理断点时，调度字段使用 first_result=independent_correct、reasoning_break=false、first_break=none、first_break_provenance=not_observed；正向理由写进逐点观察，不把“正确选项”等答案措辞写进安全断点字段。过程未完整展示不能单凭这一点制造错误。
- 已存在正式卡的 profile.question_refs 使用 `source_identity_status="verified_published"`。`verified_capture` 是待分配新题逻辑身份的证据，不能只给已有 formal_id 却省略所需 logical_question_id。关联旧题的链接不证明本次做的就是该原题。
- personal_context 的 view 仅为 protected、after_attempt、direct；408 的 max_bytes 为 1024–65536。按实际 next_cursor 分页，复用固定版本。先用 question_refs 等真实返回查路径，不能猜路径，也不能将参数错误解释为网络故障。
- 发布历史的 source_refs 使用 `study-library://<固定commit>/<实际发布路径>` 及已核实 hash；不得把裸发布路径误作回包内文件。回包自身证据使用实际相对路径或对应 enhancement-package 定位，文件必须列入 manifest.files。
- new/redo 的 display_asset_plan 要在网页完成所有附件的确定性分类：question_image、source_article_image 为 practice_safe；solution_image、explanation_image、user_work_image 为 protected；图像的 disposition 可为 promoted 或 archive_only。other_attachment 必须 protected + archive_only。逐项沿用原 manifest 的路径、角色、mime、大小和 hash，不写 public/private，不猜枚举，不因这些已给字段退回 needs_user。
- return-template 是起始模板，不是已完成结果。补全每个 Capture，核对 actions、点集、证据 hash、字段与文件闭包；不交空 items/updates 冒充成功。不要求在网页运行只能访问本机账本的正式 apply。
- 先列出真实文件再读取：输入清单在根目录 `manifest.json`，不要假设存在 `return-template/manifest.json`。Capture 的规范载荷 SHA 与 `capture.json` 文件字节 SHA 是不同字段，分别沿用输入绑定与文件清单，不互换。
- MCP fetch 返回的 `source_ref` 可直接用作来源，`sha256` 是完整原文件哈希，`returned_sha256` 只对应当前分段。`PUBLISHED_LIBRARY.json` 的哈希来自同版发布回执验证的原字节，不能对展开后的 library 对象重新序列化求哈希；该清单的固定版本 URI 与其他真实发布成员均可引用。
- 不要把整个输入原封复制到回包；保留正式语义实际引用的证据文件、执行报告与本片来源证明即可，输入原件已在本地按身份保全。不得因精简回包丢掉实际引用的补充原话或用户附件。
- 真实失败记录工具名、参数、原始错误及恢复结果。单项失败继续独立项，最终始终交成功文件和真实 PARTIAL 范围；参数/路径修正成功后才标 recovered。

## 本地保留的职责

核对精确 Capture 集合、跨片与跨日期版本衔接，处理只有本机可验证的旧回执与行政结束，锁内分配正式 ID，原生事务写入、幂等恢复、发布和导出。网页能确定的语义、来源引用和传输枚举应在网页先完成；不把缺传输字段误写成需要用户重答。

明确跳过且不要入库的记录，只保全原话和问题反馈，不伪造概念表现。真实原题与关联旧节点不同，保留真实来源；没有错误证据不能为了分配节点写 new/wrong。实际无匹配正式节点的正确原题，可提出仅记录正向知识点表现的本地接续方案，不能重标为变式或改错关联原题的 FSRS。
