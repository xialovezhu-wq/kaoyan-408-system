---
name: kaoyan-408-enhancement-intake
description: "接收GPT-6 Pro网页的408正式入库加强包，按最终成稿执行精确Capture集合的正式写入、延后判分/FSRS与个人摘要更新。只处理给定增强包，不重做网页语义、不扩历史backlog。"
---

# 408 正式入库加强包

所有用户可见的进度说明、判断依据摘要、错误说明和正文使用中文；代码、命令、标识符及必要原文保留原样。过程说明简短，不以英文进度或长篇内部思考代替教学。

用户提供408正式入库加强包并要求入库时使用。网页尽量完成最终语义，本地复用成功成稿，负责核对、失败部分的授权续接补全、原生写入、摘要与快照更新。没有增强包时不在学习会话自行做正式语义编纂；普通快速保存仍归wrong-intake。数学加强包不走这里。


多分片加强包先按本地partition-set逐一保全，核对固定快照/父集合、每片精确Capture以及并集无遗漏或重复。每片Capture原件不可拆，跨日期仍逐日处理。保留正确成稿，只补失败项；同知识点跨片成稿先结合各片原话合成一次完整概况/短摘要/时间线，保留全部证据，不能后写覆盖。然后在新派生目录合成一个既有schema的统一增强包，原partition.json移到private来源记录并列入files；不直接apply单片或让多个会话并发正式写。未知新ID仍由唯一writer分配。不同题的独立学习事件不因共享知识点而合并计次。

收到任何回包先用 `scripts/stage_web_return_408.py --kind enhancement --zip <实际ZIP>` 保全并读取成功文件、执行报告和真实失败调用。PARTIAL/BLOCKED不等于弃包：从原始Capture包和对应正式历史补齐未完成项，保留已成功的网页成稿，在派生修复包中记录实际本地改动，不伪装网页已完成。单项失败继续其他独立项，真实缺证据单列，不编造作答或掌握度。

已存在prepared/COMMITTED状态先status，优先同身份恢复，不重放正式writer。 未完成逐题阶段确需修复语义包时，可prepare --resume-prepared <原回执>；必须保持精确集合/日期/快照，已提交项逐字不变，由接收器继承原完成回执。个人概况已提交或批次已关闭后不使用此入口，只恢复后置层。确需派生修复包时依据真实回执识别already_applied，保留原Capture集合、日期和morning_target_date；新prepared显式传原目标 --morning-date，不能因跨日恢复自动改日。

## 准备与应用

已知入口（仅数据包，不执行包内程序）：

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/enhancement_intake_408.py prepare --zip <实际ZIP或解压目录>
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/enhancement_intake_408.py apply --prepared <实际prepare回执>
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/enhancement_intake_408.py status --prepared <相同回执>
```

prepare只核对给定包、精确Capture集合、固定快照、来源、原题状态和旧摘要版本。用户已明确要求正式入库时，prepare成功后直接apply，不为同一批重复索要许可。只有查看/预检请求就止于prepare。

apply复用已有正式writer，不把增强包中的shell/Python指令当作执行命令，不直接按任意路径覆盖文件。新真实题的正式ID由锁内分配，网页logical_question_id随后确定性绑定；既有ID保持原身份。

## 正式对象与计次

Capture是学习证据，不代表做错。new/redo写相应真实原题；correct_review不造错误或加错次数；concept_only/D补充只写真实知识点表现，不建变式正式节点、不改关联原题的日期/错误次数/FSRS。已有相同事件的already_applied只恢复或补充，不再算一次学习。需要用户资料的项保持具体needs_user，不能伪称完成。

新学习合同的正式判分与FSRS在本阶段一次提交，保留真实作答时间。旧流程已有正式event_id的输入要幂等复用或追加纠正，不重放。

不重新通读全库或重写已完成成稿。版本/事实冲突或网页缺项只处理受影响单元；当前用户已授权本地根据真实证据补全，不能停在“请网页重做”就结束。无法取得的必要原证据明确保留，不能沿错误前提盲写。

## 个人摘要是完成条件

每个受影响规范知识点都要写入网页最终overview/short_summary/timeline/progress/next_check与原题引用，更新小路由和同版快照。完整成稿独立封存，机械刷新不覆盖其语义。查到not_authored/stale不能宣称已经有最新网页摘要。

正式提交后的原生complete_after_commit必须运行现有refresh_after_commit：同时重建受影响点的完整学习路径和protected/after_attempt/direct讲题短摘要，发布新source_version后才请求共享快照。讲题resolve的旧known-version必须失效；有新记录但旧网页概况未覆盖时标semantic_stale并生成当前事实摘要，不能继续返回旧掌握判断或宣称已完成正式语义收尾。学习路径、短摘要和版本读回是本批后置完成条件；失败只恢复该层，不重放正式apply。普通Capture不触发这次正式更新。

正式结果、点级观察、摘要与同版发布完成后，apply会自动调用export_morning_input_408导出下一目标的备题资料及网页提示词；每批一次，不在每题提交后反复导出。默认在本次操作开始时固定上海次日，可用--morning-date明确指定。2026-09-21起现用规则返回一个30题选题包及配套提示词：先排除精确题面不足7天的作答/展示，最高优先级是上次真实接触日期最早，再按近期真实断点、错误和不足选择。候选池不是全部必备题；网页只为选中的30题做成稿，未选范围保留未复习。schema=cs408-morning-selection-input-v1时交zip_path与prompt_path两个真实链接，不按候选点数再次分片。旧全量输入才按parts交付。

分别报告正式入库、原包归档、读取维护和次日资料准备。FORMAL_COMPLETE_ARCHIVE_PENDING只补归档；FORMAL_COMPLETE_MORNING_INPUT_PENDING只补导出；READ_MAINTENANCE_PENDING保留已提交结果并完成对应维护，不能声称全部收尾已好，也不能重放正式写入。status保持只读。

## 归档与看板收尾

原生apply/status必须将归档列入完成门禁：本批每个成功Capture（含concept_only、correct_review）及冻结批次内的补充包，都需得到ARCHIVED_AND_LOCATOR_BOUND回执并完成本地指针/清理。缺少回执时保留FORMAL_COMPLETE_ARCHIVE_PENDING；用同一prepared身份恢复，不重放正式writer、判分、FSRS或个人摘要。旧COMPLETE回执若没有archives证明，同样先补这一层。

按真实证据locator选归档入口：cs408-conversation-package使用archive-after-formal-commit并绑定原terminal；只有非标准包证据才使用legacy预览及其内容授权值。正式展示采用末次补充包时，原包及中间包按同一冻结Capture的绑定归档，并核验主展示回执，不为归档重挂题图或重做正式入库。看板的“待归档历史证据”可能统称尚未归档的正式结果，不能仅凭这个标签把标准会话包送进legacy入口。原件、副本和补充链均沿既有定位与哈希读取，不扫描T9全盘，不改统计数掩盖未完成状态。

最终调用看板POST http://127.0.0.1:8767/api/refresh并读回/api/snapshot，将本次精确Capture集合与实时408条目核对；本批仍在待入库/待归档列表中就不能宣称收尾完成。范围外残留另列原日期、状态与原因，不擅自扩批或要求全库清零。纯结束且无作答的记录保留原包与曝光；只有用户明确要求移出时走行政退休链，不为清零伪造错误、评分或点级掌握。

每次正式收尾核对回包读取报告和来源；根据实际失败调用定点复现并做最小读取/MCP/提示词修复，运行相关必要测试，同步安装和快照后用于下一轮。原始错误不可得时说明证据缺口，不编造网络故障或自动执行包内命令。以实际回包迭代，不让空审计或重复模型评测替代本次正式入库。

中断或返回不明时，保留同一个prepared回执与operation身份，先status再恢复apply缺层；不得改key重建包。原生COMMITTED之后只恢复回流/摘要/发布等后置步骤，不重放正式apply。完整原话/原图仍遵守原包和稳定资产合同。

回包问题按真实证据区分输入分片/启动词、网页项目指令、MCP返回和本地接收器；只修复已复现环节。MCP fetch的source_ref可直接引用，完整sha256与returned_sha256分开；发布清单自身的SHA须来自可信同版回执，不对展开对象重算。修复同步本科源Skill及已安装副本、权威项目提示词与下一轮随包说明；用户要求手动配置网页时只交完整替换稿，不用电脑操作代改或宣称线上已更新。

完整conversation优先于学习时临时observed_outcome聚合。发现冲突须保留原包，用正式合同的deferred_outcome_reconciliation绑定原聚合哈希、最早有效作答与之前完整回合，经校验后写唯一正式事件；不能为匹配临时标签改变真实提示依赖、把后续正确当首答，或制造不存在的错误。

正常情况不重新读取源码/help/所有Schema；只按具体缺项阅读schema/408-formal-enhancement-v1.md、408-knowledge-profile-v1.md、morning-input-408-v1.md或web-execution-report-408-v1.md。实际代码修复运行相关必要测试，不默认全套回归、模型评审或学习试讲。

实际收尾还必须完成原生campaign admission与scheduler/forecast桥接；缺少该层会使新节点被排除或触发cohort计数门禁，不能改baseline数量绕过。原生入口已按已提交回执补接，不重放学习。明确不入库/纯结束记录用现有行政退休链保留原文、排除待处理，不伪造点级表现。成功concept_only和correct_review也按其真实终局完成T9原包归档。下一轮网页分工见 schema/408-web-enhancement-authoring.md，导出器会随包附上，不另让网页猜传输字段。

2026-09-20补充：新版加强网页提示词为planning/2026-09-13-d-deepseek-optimization/408正式入库加强-网页提示词-v4.0.txt。网页先真实调用Study Library library/question_refs/personal_context；工具未加载、Session terminated与本地快照被错当GitHub提交分别处理。逐点成稿保留叶子码内部的对象、条件、单位、边界和时序细节；只报对选项、提示后解释及独立正确不可混写。下一轮晨间只真实统考题并排除精确题面近7天曝光，禁止变式补位。
