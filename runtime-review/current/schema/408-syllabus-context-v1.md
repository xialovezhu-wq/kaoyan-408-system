# 2027 408 考纲与讲题上下文

## 权威来源与身份

`support/syllabus-2027/source.txt` 是用户提供文字版的逐字副本；`catalog.json` 保留原始T9路径、文字/PDF SHA256、行号、科目、章、节、原文条目和附列细项。扫描原件20页已按科目核对。`S27-*` 仅用于原文层级定位，不改已有629个规范代码，也不把849个KSP个人机制点冒充官方列项。

`bindings-{DS,CO,OS,CN}.json` 对每个规范点与已存在KSP分别保存原文锚点、范围判断、具体教学边界、审查依据；`reviewed_formal_ids` 与当次冻结全集核对。`explicit`为原文明确对应，`within_scope`为条目内必要机制，`prerequisite_only`只支撑当前题，`out_of_scope`不自动拓展，`needs_review`不冒称已校准。范围判断不是掌握度。

## 全库统计

`exam-frequency.json` 绑定已审核 `question-index.csv` 的SHA256，使用唯一 `EXAM408-YYYY-QNN` 去重。当前范围由exam-corpus-acceptance.json绑定：2009—2026，每年47题，共846题；2026仅采用用户指定新精解版，旧provisional版本不混入。保留各题主点和所有合法命中点，包括跨科命中。一个点分别统计主考题数、涉及题数、年份和精确题号；一个大纲子树按题目集合并集计数，不累加子点数量。

2026-10-27统一计数口径 `choice_or_subquestion_primary_parent_dedup_v1`：选择题采用本题主考点；综合题采用已审核独立小问的主考点，不再额外计入父题的原主标签。每个考点仍按唯一父题去重，同一父题有多个同点小问只算一次；同一综合父题可分别主考不同规范点。主考集合必须包含于实际assessed集合，兼考为assessed减主考，总考次不因此增加。完整综合父题及其独立小问覆盖缺失或未审核时拒绝生成，不猜测主点、不回退旧口径。

本地频次等级不变：主考至少5题且跨至少4年为高频，主考1—2题为低频，其余有主考记录为中频；仅兼考或未标注主考不判低频。阈值不是官方权重、不用于推断个人错误或未来概率。0命中只代表当前已有标签未标注，不证明从未考过。2026-10-01第二轮逐题读取全部799题（680选择、119综合）的题干和解析，必要时核对原图；综合题按小问提取，四科exam-review文件为原始语义标注源。assessed/prerequisite/context三类集合分离，仅实际考查参与计次，选项干扰和场景背景不加考次。原题、原解析和原始标注保留，不把发现的解析错误写成考点事实。

父题原 `primary_point/main_knowledge` 保留作来源与讲解入口，不改成多值。计数使用派生 `frequency_primary_units/frequency_primary_points`，同版讲解、双向索引、晨间/晚间频次优先级都从该统计读取。大纲主考锚点按实际单元确定：唯一已审直接锚点可用；多个锚点依据主点绑定筛选，只有有来源绑定的精确单元审查可以覆盖，保存于 `frequency-primary-anchor-review.json`；仅向祖先聚合，不把兼考锚点或兄弟条目升为主考。该文件是派生统计的消歧依据，不修改四科原始review、subquestions或bindings。

小点未独立逐题标注，频次 `frequency_scope=parent_point_not_subpoint`、`subpoint_count=null`，讲题次数和年份必须明确归父考点。原正式卡年份冻结/冲突、probable映射、教材题不充当统计真题身份。

## 生成与读取

`scripts/syllabus_context_408.py build --repo <repo> --baseline <冻结baseline.json>` 精确核验规范点、KSP、正式卡全集后生成 `context.json` 和 `考纲索引.md`。每个大纲原文节点都有独立入口；没有精确规范点映射的叶子保留 `syllabus_only_entry`，不能假报已由宽父点覆盖，也不制造个人错误摘要。

`load_contexts` 只在冷构建加载，验证绑定与统计源哈希。`context_for` 对未来新增小点保留父范围且标待校准。派生个人摘要保留原short_summary/history，在独立 `syllabus_context` 提供考纲定位与范围；prepared父/KSP视图和query投影均包含，普通/D/晨间同入口获取。protected省略该层以保护本题解法。完整上下级与真题身份写入知识点页及索引；讲题视图保留短范围和年份。

全部358个原文节点也有独立 `syllabus_entries` 哈希路由，可用S27精确身份或唯一原文名称读取，既有规范点和KSP优先；多义标题明确返回候选。此层不加入 `points/point_records`，不改变正式知识点数量或调度分母；`syllabus_only` 明确无个人证据。缺独立规范点映射的条目保留客观讲解入口，频次无独立标注时不得将0宣称为从未考过。

新增源与构建模块纳入source_version和freshness检查。资料变化后必须重建并刷新、发布；普通学习不能现场全库重建。发布验收检查所有父点/KSP与真实关键词路由、protected和known_version复用，不把源码写好当实际生效。

## 教学呈现

答后首次优先使用question_exam_introduction中的预生成计数句，说明考点、统计期、总考次、主考/兼考次数及频次等级，再直接讲题；不能只列年份而漏总次数。无题级开场时使用syllabus_context.frequency_intro；同一题同一考点即使多个小问命中也只计一次，总数=主考+兼考，不是main_count+hit_count。内部依据原文上下级、范围边界决定必要解释，不播报“大纲里讲过/没讲到”，不展开无关目录。同题追问不重复开场；统计和范围不改变保存、评分、调度与答案保护合同。

## 全题双向连线与计数预处理

`syllabus_context_408.py preprocess`复核四科审查文件精确覆盖已接纳父题身份、全部code/S27合法、原始唯一主点在assessed内、parts并集与assessed一致、三角色不重叠，以及计数用独立小问完整覆盖，随后重建频次、全部父题/小问/正式题开场和双向索引。源文件与本地正式题数量变化时以实际全集为准，不截断综合题或新正式题。刷新派生讲题缓存和同版共享快照后再用于新选题；既有active/paused队列及其冻结交接保留原合同，不重排或重放学习。

`point-question-links.json`保存全部题→点、正式卡→点；`point-links/<point>.json`直接保存点→真题、主考题、前置/背景题及正式卡。规范真题及正式卡分别计数，正式卡的规范真题身份只取已声明来源，不从文件名年份猜。全部连线经正反集合一致性核验；知识点对应不等于用户错误，也不自动创建R01等题间强边。

原文大纲节点按逐题parts的S27直接锚点及祖先聚合，不把同一宽父点下的兄弟项都算命中。没有旧规范代码的小问仍可直接连S27，保留其真实考查次数。

`question-frequency.json`按canonical真题ID和formal ID预生成主考点开场及每个实际考点的计数句。冷构建物化为内容寻址对象及小哈希路由；D使用已验证true_item_id优先，被替换source_formal_id不抢身份，变式未知题面显式禁止回退原型。即使命中KSP-only，同次返回也保留顶层question_exam_introduction；protected不读/不返回开场，known-version绑定题目及计数对象，预算不足明确报告缺项。

按考点定位时用`exam_point_links_408.py --point <code或S27>`读取单点预处理文件；推荐正式错题时用`related_candidates_408.py --kp <考点>`读取预处理命中，再按现行最近真实接触日期筛14天。正式入库公共postcommit在主表变化后确定性刷新这些派生连线，再做原生讲题刷新和发布；不重写学习事实或判分。讲题热路径不遍历799题现场分析。

## 2026-10-03 独立小问

`exam-subquestions.json`按稳定子题ID保存本问实际考点和来源。每个小问只做当前目标，前问必要结果直接给定；完整题干留在详情库。小问拥有独立`question_exam_introduction`，频次按父题集合去重。2009—2026共846道父题；2026来源是用户指定新版，不能把第三方整理身份改称官方认证。
