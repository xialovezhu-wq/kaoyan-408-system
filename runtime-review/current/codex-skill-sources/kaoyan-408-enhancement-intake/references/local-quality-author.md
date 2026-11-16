# 本地概况、考纲与可视化质量修订

学习证据正式入库按质量合同（`<repo>/schema/408-formal-quality-v1.md`）验回双 API 和原生写者。下列资料整理命令维护已审核的资料与概况，待审 Capture 沿正式入口办理。

本地 Codex 是默认作者。直接用本机已接纳的原表、各科完整概况、原图、教师路线和逐题讨论，不要求网页会话、分包、云端MCP或模型API。只有用户指定网页处理才沿旧分片回包合同。已在本地或网页处理的批次先查其原状态，复用成功稿，依当前基线处理其余目标。

## 定范围与查证据

在408仓库运行下列入口，它们共用正式资料与原生写者，正式写入仅由 apply 或相应正式入库事务执行。

- `python3 scripts/runtime_paths_408.py --run local_quality_408.py audit [--subject DS|CO|OS|CN]` 给出当前已编制范围、有真题但纲目绑定待核、缺专属KSP路线/范围、没有匹配已考单元等具体目录。它不把这些目录判为个人弱点。
- `python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py resolve --concept <父点或KSP> --view direct --personal-scope full --materials core` 读取当前有效完整个人依据和已核来源；精确题增加 `--current-question-id <本问题号>`。核对personal_read_receipt与当前语义状态；不把“存在摘要”当作读过全文或该点已掌握。历史、比较、来源和关系使用同入口memory history/compare/trace/relations，按固定版本和实际时间核对，旧wiki仅为补充。需要视觉配方时同范围补读materials visual。只读查询中的 `local_syllabus_lessons` 提供此次可用的本地考法身份。
- `python3 scripts/runtime_paths_408.py --run local_quality_408.py context --id <规范点/KSP/正式ID/真题或Pxx/原纲ID> --view direct` 取得该精确目标的本地教师材料。父点默认每种题型给一个完整原例，exam_focuses_by_form另完整列出同题型下的实际设问、条件和首步；他点真实考法另列，绝不计入本点。加 `--all-forms` 取全部已标注实际单元，带 `--current-question <精确真题>` 排除本题族。独立小问只取自己的条件与题图。本地作者可加 `--personal-evidence` 在同次 direct 下读取原生最新个人概况及证据对象指针。小点考频若继承父点必须写出父点名。答前使用 `--view protected`；after_attempt只给可用身份，展开教师资料须先满足当前提示权限。
- 阶段间本机题源、原图、教案与3D指针按 exact identity 查证。题图实际核看图块、字段、方向和初态，缺图如实标受限。教师静态审核、学生实际显示和用户独立应用分别记录。生产目录在T9时按项目规则先check-output后登记，热库用于查询和原生写者，不把热运行依赖移到T9。

## 在本地写成稿

按一处具体断点逐项写当前题原理、起手、条件、状态、边界，接回原题的检查。再把本知识点的2027原纲各分面排清楚，并列已经考过的题型、各自题给什么/求什么/第一步差别。允许同点差异、一个明确上下级或已审关联，须有真实 assessment_unit 和纲内范围双方依据；零命中或未单独核定写明边界。点击对象和3D只解释步骤所需的局部真实机制。提示级与当前练习年度限制优先；会泄漏答案的成稿仅留教师私有侧。完整覆盖是教师准备与用户明确要求的分段梳理，日常一题不要机械讲完一章。

旧完整概况与老师已经审核的逐步解释可按证据复用，明确当次新写内容与复用部分。个人字段沿真实首答、独立/提示后表现与待验证边界；客观纲目、题型和本次作者的讲法不能生成新学情。只修订真实需要改变的部分。同日原题学情由独立正式入库事务优先；共享对象待其回执出来后重读，按基线／本次增量／当前状态三方合并，每次只恢复冲突或缺失项。

## 提交已审核修改及读回

个人字段与客观教师路线各用一个严格限定目标的本地JSON稿，不能把自然语言待办当成正式记录。共同字段为：
- schema 为 `cs408-local-quality-draft-v1`；layer 为 `profile`、`route` 或 `scope`；point 是父规范点，精确小点另填 subpoint_id。
- expected_previous_version 来自本机当前不可变对象SHA，新建独立路线填 null；review_reason 记录本次人工核源的具体结论。
- source_refs 每条给可重开的真实绝对 path、完整字节 sha256、页码、原句或对象字段 locator。原题、原纲和学情分开。
- profile 稿交 changes，只允许本点/小点的 overview（仅父点）、short_summary、next_check、teaching_focus、teaching_plan、claims_schema、claims 中本轮实际改动。提供claims时须传该目标完整claims列表及claims_schema，不能只交局部补丁；修改旧claim内容或有效状态须保留稳定ID并给change_reason，所有claim source_refs均须核原件。旧格式不自动生造claims。程序保留已审核原时间线、证据与问题引用，调用 knowledge_profile_408.apply_updates；真题学习新事件及新建个人概况走正式入库的原生审核。
- 替代仍有效的实答判断时，核实新证据与适用条件。待核推断可记为 `conflicted`，沿原出处保留已有判断。
- scope 稿只处理已存在正式KSP、尚缺主表独立范围的补充。scope 对象包含 parent/title、已核原纲 syllabus_ids、scope_status、teaching_boundary 和 rationale。本地受控写者锁内核来源及上一版本；以后主表正式核定相同KSP时以主表为准。
- route 稿交完整 cs408-derived-teaching-route-v1 对象，route.source_refs 等于稿件来源，ordered_blocks 与 visual_insertions.step_id 对应；沿 teaching_research_408.apply_incremental 提交。涉及改动精确题模板和原图方案必须同时走原教案/binding写者，不用单条独立路线代写原图或3D。

`python3 scripts/runtime_paths_408.py --run local_quality_408.py check --reviewed <本地成稿.json>` 验原字节、范围、旧版本和字段。经授权后 `python3 scripts/runtime_paths_408.py --run local_quality_408.py apply --reviewed <同一份.json>` 正式提交这一层；同一稿重试沿原生幂等记录。多个互不重复的父点概况可重复 `--reviewed` 一次只提交一个原生事务，分开处理教师路线。版本冲突或证据不可重开时保留稿件只补该目标。

首次创建本机课程或用户明确更改年度隔离时运行 `python3 scripts/runtime_paths_408.py --run local_quality_408.py build --exclude-year <当前需隔离的年>`；有多个年份时重复参数。明确学习日可改用 `--study-date YYYY-MM-DD` 读取该日原生年度计划；未指定日期时不自动推进。查到 pending_local_rebuild 时只恢复本地课程物件，保留原生学习。未变的对象按内容寻址复用。常规入库与质量维护走同批正式收尾：先调用既有 `exam_tutoring_context_408.py refresh-current --refresh-date <上海实际日期>` 。此入口先刷原生纲目与题表，随后只在已登记课程来源变化时沿原保护集合自动补编和验回。再走对应发布/读回合同；不可把对象编制成功报告成个性化热视图、云发布或学生显示成功。按具体修改点、将要讲解的题号及KSP抽查 direct 与 protected；保留精确输出/回执。正式入库已完成其中的刷新/发布时只补质量层缺项，不重新apply学情或分数。
