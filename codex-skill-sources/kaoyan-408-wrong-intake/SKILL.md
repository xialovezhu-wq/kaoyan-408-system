---
name: kaoyan-408-wrong-intake
description: "408快速保存完整学习证据。新晨间每题终态Capture；普通题明确快速入库时对错都封完整包。只事实/曝光/进度，等待网页加强包正式判分与更新摘要；可日终汇总已保存Capture。"
---

# 408 完整快速保存

所有用户可见的进度说明、判断依据摘要、错误说明和正文使用中文；代码、命令、标识符及必要原文保留原样。过程说明简短，不以英文进度或长篇内部思考代替教学。

Capture是证据容器，不是错误或掌握度判断。正确、提示后完成、wrong/unresolved均可保存；不能因为正确就省掉明确要求的完整包，不能因为有Capture就加错次数。

## 唯一写者

健康保存以10秒内、通常10–20秒内返回为目标，不承诺模型端到端固定时延。复用当前display、原话、附件路径和稳定episode ID；已知合同一次构造输入并提交，成功回执即结束保存。不要每题查源码/help/枚举、搜索全账本或重新审核已有来源。跨日续题检查属于下一题导航，不能把已成功Capture说成仍在入库；遇到具体错误才定点补读一次相关字段。不得用省略原文、附件或校验换速度。

- 新campaign v2：daily-study-loop的[固定学习入口](../kaoyan-408-daily-study-loop/references/learning-entry.md)拥有完整事务，程序衔接首答/追加/封存，不由模型手工串联，不另调standalone。每题终态封包→唯一Capture验证→导航；讨论只同题草稿。
- D独立补充：按morning-control的[D固定入口](../kaoyan-408-morning-control/references/d-concept-supplement.md)，先给已核实的教学反馈，再用v11助手的d-supplement-turn提交本轮完整用户原话和observed_outcome。程序从真实线程读取本题完整对话、填时间、按已绑定计划确定后继，完整收集已发反馈，校验保存后同次返回后继题图链接；不手抄旧对话、不填后继ID、不单独查date/help/源码。真实保存故障复用learning_request_file；学习中不读图、裁切或修图，不因视觉缺陷启动image-handoff。旧显式恢复仍保留原请求合同。
- 普通standalone：只有明确快速入库才用下述共享合同；普通讲解不自动扩大保存授权。已存在的旧light记录保持其原事实，不伪造为历史Capture。

## 普通题的完整入口

首次需要构造输入才读 [episode输入](references/study-episode-capture.md)。使用：

`/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/standalone_question_intake_408.py record --input-json <私有输入.json>`

该入口接受schema=cs408-study-episode-capture-v1、execution_kind=standalone_question，并额外验证authorization.user_message与最新真实用户快速入库命令完全一致。有managed display时不使用此入口。

使用真实episode_id，重试保持相同ID/字节；同日同题另一次独立学习用另一个episode，不按日期+题面合并成一次。新题没有外部来源ID可保留null，本地生成明确标注的技术键，不猜正式或可视化ID。附件用实际绝对path/role/mime_type/label，未知时间为null。

保存所有已发生的相关user/assistant轮次、题面/选项、解析、自检、提示、复述、用户工作图及原始字节。正确题的错因/首步未观察到就明确未观察到，不为过格式校验捏造。新turn只追加，不反复重抄旧全文，不截成首尾摘录。

原始消息与turn一一对应；同一消息兼含选项和理由仍保存完整text，不拆分伪轮次、不trim、不删除末尾换行。结构化response和reasoning_result单独填写。晨间固定入口支持真实message_refs读取及去重；原文传入模式明确标记caller_supplied，不冒充独立逐字核验。package_verified只验证提交包。已封存旧包保持原字节，后到更正按下方不可变补充保存。

共享writer完成原包、Capture及必要证明。正常回执的capture_verified/package_verified、capture_id/package locator就足够确认保存，不再由Agent手工调用第二次打包/索引/回执扫描。

## 等待网页增强

新Capture为awaiting_web_enhancement；普通后台cutoff编纂不提前消费。它已经保存，不是needs_user，也不是正式记录。学习阶段不写review事件、FSRS、正式个人摘要或正式题卡，不做后台模型/独立审核/T9归档。

原题保存original_review，系统变式和点级补充保存concept_supplement；后者不改关联原题日期、错误次数或FSRS。正式内容由GPT-6 Pro读取完整总包和MCP历史后写成加强包，再由kaoyan-408-enhancement-intake按精确集合执行。

## 日终原始总包

用户明确要求结束当日408学习并打包时，先让当前原事务完成必要封存；结束未解题用hold，不再展示新题。然后调用：

`/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/export_capture_day_408.py --date <真实上海日期>`

导出当日全部已封Capture及其补充版本，原件和真实身份不变；不要求先正式入库或把记录发布成已接受历史。未封展示/缺原件明确报告，不能删掉题凑成全量ZIP。原始总包给网页408正式增强业务，下载和导出不是学习完成或正式入库。

大批原始Capture导出超过40项时，export_capture_day_408自动返回分片和配套启动词，默认每片约30项，大批通常5—6片；每个Capture的完整对话、附件与补充链不能拆开。`partition_status=PENDING`表示原始总包已保存、只需恢复拆分，不重新封Capture。也可调用 `scripts/split_web_input_408.py --kind enhancement --zip <原始总包> --parts <数量>`。导出时一次固定正式source_commit，全部网页会话只读同版资料；一会话一包。小批保持原单包入口，不启动网页或模型。

用户明确指定看板待处理数量或精确集合时，先冻结真实Capture ID并集，核对跨日构成；关闭当前未答题新产生的Capture单独保留，不混入原指定集合。按Capture真实study_date先分组，再按知识点和体量分片，每片保持单日；不能用父包日期替换原学习日期。MCP来源优先沿用fetch的source_ref，输入manifest在实际根目录，不猜不存在的return-template/manifest.json。

不同片涉及同一知识点时，网页各交真实证据及本片候选成稿，本地收齐后合成该点唯一最终概况/短摘要。全部正式决定先按父Capture并集核对并合成一个原生增强包，再串行正式写入、发布一次、导出下一轮晨间资料；不可并发apply各片，也不可后片覆盖前片摘要。分片身份放partition.json并列入files，正式manifest不添加未支持字段。

交给网页的启动说明必须要求逐项保全、失败后继续其他独立项，最终始终交实际成功文件、EXECUTION_REPORT.md和web_execution_report.json；明确失败调用的真实工具名/参数/原始错误。尾部失败不清零前面成果，真实零成功才报告零。PARTIAL加强包回本地由enhancement-intake保全、补全失败部分和正式写入，再维护真实读取问题并导出下一轮晨间资料。不要让用户因网页局部失败重新打包或重做已成功学习。

## 恢复

新v2 campaign封存后的真实更正/口误/补充，仍走固定学习入口intent=correct-late并绑定被更正题的原display SHA。程序重新验证原Capture与v2授权，保留原包、原日期和原观察，追加补充证据进入同一Capture补充链及日终导出；不重交首答、不重做导航、不改FSRS或正式摘要。普通standalone仍使用原显式补充合同，不能混用身份。

同episode保存不明先用原receipt/请求核对，只补失败层，不换key重建Capture。旧standalone supplement仍用既有补充合同，不能改旧日期/来源或复写历史包。

事实保存证明不会因后来正式处理而失效：current_quality_status与原保存receipt分开，curated/concept_only等表示后续已处理，不把它重置待增强。完整正式包的恢复由增强入口负责，不重放正式apply或原作答。

新导出附带 WEB_AUTHORING_GUIDE.md、正式回包合同、enhancement/knowledge_updates 起始模板和各Capture的冻结local_status。网页按固定版本完成来源绑定、合法附件可见性和本片逐点成稿；不能把旧review/outcome存在当成正式入库回执。只给裸formal_id的引用用verified_published，时间/题号说明必须同时限定日期、run和Capture。
