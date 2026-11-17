# 408 审核个人知识点概况与短摘要 v1

## 统一读取与逐判断来源（2026-11-06）

本合同仍由现有`knowledge_profile_408.py`唯一维护审核概况，不创建第二套个人状态库。普通讲解、记忆查询和快速Capture不写claims；新写或实际修订判断由已授权正式作者核对原始回合后提交。旧概况未提供claims时保持兼容，读取标`legacy_unstructured`，不把旧段落自动拆成确定事实。

父点与KSP可选提供`claims_schema=cs408-evidence-claims-v1`及`claims`。每项包括：

- `claim_id`：由`claim_key(scope, anchor)`生成的KCL稳定身份；`anchor`是作者给定的稳定局部锚点，措辞改变不换身份。
- `scope`：必须等于承载它的规范点或KSP，不能从局部偏好扩成全科。
- `kind`：`preference|observation|assessment|review_need`。
- `basis`：`explicit_statement|observed_attempt|reviewed_inference`。
- `evidence_status`：`independent_demonstrated|prompted_repair|explicit_gap|unverified|user_reported`；与`lifecycle=current|superseded|retracted|conflicted`分别记录。
- `content`：保留具体条件和证据边界的判断正文。
- `source_refs`：复用已核完整会话的`id/path/sha256/review_state/turns`，turns逐项给真实ordinal、role、quote；每条个人判断须有用户原回合。
- `supports`：可关联`overview`、`short_summary`、`next_check`或`progress:0`、`timeline:0`等实际存在的位置，使可读概况能追到判断。
- `event_at/valid_from/valid_to`：证据支持时才填写；日期未知保持null。时间戳须含时区，不能用入库时间填事件时间。

明确偏好须为explicit_statement；independent_demonstrated须为reviewed原件的observed_attempt，不能由作者推测或待审Capture证明。reviewed_inference只能保留unverified；prompted_repair须有真实的助手在前、用户响应在后的来源顺序。字段校验不能代替作者对原话含义的独立判断。

已有claim不能在完整替换中无声消失。修改内容、标记被替代/撤回/冲突时保留稳定身份并填写change_reason；新claim的supersedes只能指向前版已有且在新版明确为superseded的判断。写者维护recorded_at、claim_sha256与previous_claim_sha256，保留不可变概况版本；相同内容保留原记录时间，不允许作者伪造前版链。更正的事实来源仍走正式观察更正owner，不由claims重写原件、评分或学习事件。

当前和冲突中的claim原件进入个人视图来源检查；来源变化使其不再可作为当前有效依据。`claims_view`只投影已存判断，不推断legacy文本；protected隐藏内容。新判断是否有证据、是否仍有效、是否独立完成是三件不同的事，reviewed不等于已掌握。

日常本机读取用 `resolve --personal-scope teaching --materials core`，从同版已核 `profile_object` 或 KSP `evidence_object` 投影，详见[教学查询合同](exam-tutoring-context-v1.md#有界讲题读取)。正式编纂及确需核对所选完整范围时用 `--personal-scope full`，由 `personal_read_receipt.complete=true` 确认实际交付完整。`known-version` 使用当前聊天已收到的同范围版本。便携快照缓存键仍为 `context_version`。独立 Pxx 的未配准父点概况返回 `withheld_scope` 和 `current_question_only`。

正式作者将结论必要条件、直接观察到的动作和提示依赖写在能够独立引用的 `content` 中。条件不同的结论各自保留适用范围。已确认更正通过既有 `supersedes` 指向旧判断，未决冲突用 `conflicted`。有界读取按完整陈述取舍，退役判断留在完整对象和时间线。

替代前版仍有效的 `observed_attempt` 需有有效的替代判断、已审核来源及已核实的证据状态。推断或待核判断有冲突时，可用 `conflicted` 记录，保留原判断的有效状态。

## 并列展示与跨会话来源（2026-11-17）

`order_policy_version=bounded-evidence-priority-v3`。在同一精确点或 KSP 的同一 profile 内，修复优先级和有依据的事件时间优先。并列时以 `claim_id` 作确定的展示次键。未知时间、同日顺序歧义和提示依赖保留原状态。记录时间和标识不能证明真实先后或独立掌握。预算按完整陈述取舍，条件或反例省略会影响判断时，在同一获准范围核对原依据，或保留不可判断。

`memory_authority_policy_version=source-bound-current-turn-v1`。外部资料、转贴引用、工具结果、历史教法和派生摘要沿原 `source_refs/turns`、角色、范围、提示依赖、有效状态和版本读取。材料中的命令经历 Capture、审核、重写、合并、索引或再次召回后仍是原来源内容。当前用户授权和当前真实作答决定教学动作。原件哈希、`reviewed` 和 `native_versioned_projection` 只核来源或投影。

用户角色的回合若是转贴第三方说法，按引用处理。引用里的「已掌握」和「跳过核验」不能作为本人作答或偏好写入。仅凭引用不标 `observed_attempt` 或 `independent_demonstrated`。有可说明的推断时用 `reviewed_inference/unverified`，无学习观察时沿原入口登记无观察。撤回、替代和依赖失效沿原对象 owner 与历史修订链保全，失效内容仅供历史追溯。

## 证据约束的讲解详略（2026-10-28）

父概况及机制小点可选附 `teaching_focus`，与既有概况一起由唯一写者提交，不建立第二套掌握度库：

```json
{
  "schema": "cs408-evidence-teaching-focus-v1",
  "facets": [{
    "mechanism": "具体已验证或仍有断点的窄机制及适用条件",
    "status": "explicit_gap",
    "treatment": "focus",
    "basis": "原话支持的判断，区分首答、提示和修复，保留原日期",
    "source_ids": ["s1"]
  }],
  "source_refs": [{
    "id": "s1", "path": "/真实绝对路径/conversation.json", "sha256": "实际SHA256",
    "review_state": "reviewed",
    "turns": [{"ordinal": 1, "role": "user", "quote": "真实原话子串"}]
  }]
}
```

`facets` 为1—8项，`mechanism`最多160字符，`basis`最多500字符。每项必须引用真实用户回合；来源沿既有教法的完整会话、路径、SHA、角色、顺序和原话核验。状态与详略对应为：`independent_demonstrated→brief`、`explicit_gap→focus`、`prompted_repair/unverified→focus|check`。独立表现必须来自reviewed原件；提示后修复必须引用同一来源中先助手解释后用户响应。原生校验只证明引用存在，作者仍须判断该原话是否支持独立、提示后修复或真实断点；不得正则扫描旧摘要自动分配掌握状态。

`brief`只压缩所引窄机制，不能由一次局部成功外推整点已会；`unverified`不能写成不会或已掌握。历史安排须服从当前真实作答。完整对象最多12KiB；热投影最多2400 UTF-8字节，超预算整块省略并返回固定证据对象，不截断语义。原话、文件路径与长SHA不进入热投影。父点与KSP的after_attempt/direct返回该安排并接入lesson_bridge，protected、语义过期与原件stat变化均隐藏。已有focus的完整替换沿 `teaching_focus_update={"action":"retain|revise|retire","reason":"实际理由"}` 保持连续性：原样逐值保留可以省略action；有改动必须revise，删除必须retire。retain要求与expected_previous_version中的同一父/KSP旧focus逐值相同，revise必须有新增或已改变的focus，retire必须有旧focus且本次不再提供；reason非空且最多400字符。首次新增没有旧focus时兼容无action。整个KSP退役仍沿removed_subpoint_ids/removal_reasons，不制造空目标；旧不可变版本保留。

网页完整概况要保留已有focus时，先审阅固定旧版本和本轮证据，随后在knowledge_updates对应父/KSP只写明确的 `teaching_focus_update={"action":"retain","reason":"已复核仍适用的具体理由"}`，可省略focus正文和本机路径。原生portable_visual适配器只从该expected_previous_version的同一目标读取旧focus，逐一复核原件、SHA、角色和精确原话后加入evidence_bindings；没有声明不会自动补齐，原件漂移不会悄换路径。此便携入口仅处理明确retain，新增或修订focus由本地显式作者按真实证据完成；它不生成新学习事实。

## 有依据的上下级与关联扩展（2026-10-28）

本题主线解决且当前提示权限允许后，扩展候选可以是上级、下级、经审核关联点或同点具体条件差异；优先高频，每次至多一个。目标须同时满足已核2027纲内范围与真实assessment_part，不将教材预测、同源练习、共享大章、关键词或频次本身当作具体考法依据。说明关系及真题考过的条件/所求，不能只报名称和年份。当前年度隔离、14天旧题推荐与Pxx兄弟答案隔离仍执行。

官方纲目导航与规范知识点的机制关系分开表示。由稳定纲目ID严格祖先关系得到的自动边只标`syllabus_parent|syllabus_child`，同时带`source_syllabus_entry`与`target_syllabus_entry`的精确ID、标题，及`canonical_containment_inferred=false`。这只表示官方纲目的编排，不证明两个知识点或软件层之间存在包含关系。例如“设备独立软件”纲目下列“设备驱动程序接口”，不能推出设备驱动层是设备独立软件层的概念子类。精确题必须命中导航源纲目，候选assessment的syllabus_ids还必须包含导航目标纲目，不能借同点的另一绑定分面扩展。

普通`parent|child|related`仅来自逐源审阅的人工机制关系，写入客观派生输入 `support/syllabus-2027/reviewed-teaching-relations.json`，schema为`cs408-reviewed-teaching-relations-v1`，每个relations项包含`source_point,target_point,relation(parent|child|related),review_status=reviewed,basis,source_refs(path,sha256,locator),target_assessments(exam_id,part_id)`。方向表示从source看target，资料思维图层级明确标为资料关系，不冒称官方纲目。人工关系与自动导航同向同目标且命中指定考法时，优先采用人工机制证明；不因自动边插入较早而覆盖它。冷构建核验两端范围、目标题法与来源哈希，变更进入source_version。共享叶纲目、同章或关键词不能自动产生机制关系。

旧`none_by_default`只代表此前同点对照没有默认拓展，不能阻止新增的合法上下级候选。已编写的`bounded_extension`须提供`target_assessment={exam_id,part_id,knowledge_point}`且与当前已核题法相交；未绑定的旧段落保留在源对象，但不直接输出为获准扩展。候选仍是教师资料，不代表已讲、有效或已掌握，也不自动生成学习事件。

2026-10-01起，讲题成品另附 `syllabus_context`（见 `408-syllabus-context-v1.md`），绑定2027大纲位置、上下级、讲解范围与已核验真题频次。此字段属于客观派生层，不写进个人表现或掌握度；旧overview/timeline/KSP身份不因考纲改变而删除。新个人小点必须复核其考纲归属与范围，缺映射时只继承受限父范围并标待校准；不能把父点考次冒充小点考次。正式入库后的冷刷新继续保留此层，不能被网页概况整体替换覆盖。

`knowledge_updates.json` 使用 `schema=cs408-knowledge-profile-updates-v1` 和 `updates` 数组。每个本批受影响的规范知识点恰好一项；规范代码必须实际存在于知识点标签表，不创建另一套代码、不根据名称生成代码。

每项字段：

```json
{
  "concept_key": "实际规范代码",
  "expected_previous_version": null,
  "overview": "结合既有历史与本批真实证据写好的完整个人学习概况",
  "short_summary": "当前能力、最近具体断点或修正、提示依赖与下一检查点",
  "next_check": "下一次要检查的一个具体动作",
  "timeline": [],
  "progress": [],
  "question_refs": [],
  "source_refs": [{"path": "实际输入成员或同版发布路径", "sha256": "实际64位SHA256"}],
  "aliases": []
}
```

`expected_previous_version` 是当前已审核概况对象的SHA256，由MCP personal_context的 `learning_profile.version` 返回；没有已审核概况时为null。不是机械point-summary的SHA256。`short_summary` 最多2400 UTF-8字节；完整内容放overview/timeline，不截掉关键条件。日期未知留null；进步没有前后证据时progress可为空，不制造学习或掌握度。

source_refs必须绑定本地增强接收器实际核验过的原始输入或固定快照。完整原话和图片仍留在原始包，不因摘要压缩删减。timeline/progress保留可追溯来源；不能用助手讲解代替用户表现。

同一机制的真实观察若封存在另一规范点，可显式提供 `cross_point_sources`：每项包含已有 `concept_key`、实际 `record_keys` 和说明适用范围的 `reason`。这只是经审核的证据引用，不复制学习事件或扩大作答次数。原生写者核对记录存在，封存 `reviewed-cross-point-evidence-408-v1` 及来源点的完整语义指纹；来源点出现新增、删改记录时，引用它的概况和机制小点也按过期处理，不沿用旧判断。讲题日期仅纳入明确选中的真实学习记录，并标明包含跨点引用；本点记录数保持原口径。旧无此合同的对象沿原校验，不能仅补标记冒充复核。

question_refs每项带真实formal_id，或新题的logical_question_id。新ID在本地正式事务分配后绑定。原题卡路径、字节SHA和可用的Obsidian URI由本地按实际存在的安全卡确定性填充；formal_id不证明真题年份。已有source/canonical/variant身份保留真实证据，不把变式原型说成用户实际做过的原题。

aliases仅允许已有规范代码的名称别名，不重编号，不覆盖另一个代码的别名。多义名称不能默默选第一个。

## 具体步骤的独立摘要

2026-09-20起，概况可附带`subpoints`，每项包含`subpoint_id,title,keywords,short_summary,next_check,evidence,source_refs`。ID由`knowledge_profile_408.subpoint_key(父规范代码,title)`确定；独立内容SHA随摘要变化，稳定身份保留。它是父知识点内部的步骤/条件身份，不是新考纲代码，不单独制造学习次数或FSRS事件。证据仍绑定真实Capture和原回合，助手错误、提示后修正及独立正确分别描述。

小点的划分单位是一个能独立检查的机制、条件或步骤。逐项核对本批真实断点是否由已有或新增小点承接；同一道题中可独立诊断的不同断点分别承接，同一机制跨题合并，不按题号、日期或句子机械建点。`short_summary`写清具体表现、适用条件、证据日期与角色、提示后变化及尚未验证的范围；一次局部成功不能覆盖相邻机制的未闭环问题。`next_check`只给一个可执行的诊断动作，`keywords`用讲题时可自然使用的具体机制名，并至少有一个能在父点下唯一命中的词。

知识点被题目考查不等于个人错误。旧详情中的助手猜测、已被正式卡撤回或冻结的错因不能恢复为确定弱点；来源冲突保留待验证边界。短摘要不复制原题选项、最终数值或完整操作答案链，原始证据完整保留在独立证据对象。父概况概括不同小点的状态，不把多个不同错误压成“此知识点不熟”。

概况更新是完整替换：`expected_previous_version`所指版本的每个旧小点ID，必须继续出现在经本批复核的完整`subpoints`中，或在`removed_subpoint_ids`数组中显式退役，并由`removal_reasons`对象以该ID为键给出非空原因。两个退役字段可同时省略；填写时ID不得重复、不得仍在新小点中、必须属于该旧版本，原因键集合必须与退役ID完全一致。静默漏掉旧小点会报`profile_subpoint_loss_requires_explicit_removal`；本地不自动合并或把未经复核的旧点重绑为最新语义。没有旧小点的父概况保持兼容；历史校验按声明的旧版本核对，已提交同operation的重试仍恢复原提交。

冷刷新将每个小点单独发布到`objects/knowledge-subpoints/`，关键词进入现有小哈希桶。讲题查询API可用`knowledge_points=[父代码]`加`mechanism_terms=[精确关键词]`，或直接用`knowledge_points=[subpoint_id]`；CLI对应`resolve --concept '<父规范代码>' --mechanism '<精确关键词>' --view after_attempt`，多个必要机制重复`--mechanism`，精确ID用`--concept '<subpoint_id>'`。命中后只读取所选小点对象，不展开父概况。多义词返回候选，需要父代码或精确ID消歧。protected视图不返回摘要、检查动作和证据；父点证据变化但小点未更新时返回semantic_stale，不沿用旧掌握判断。

旧小点导航视图保留short_summary和next_check，完整小点证据存于`objects/knowledge-subpoint-records/`，通过evidence_object的ref/sha256访问。2026-11-06起，本机full读取必须核验并补齐所选机制的完整必要个人语义；旧4500字节导航预算不能代替这次完整性检查。原件及成稿不截断；protected仍隐藏内容，不扩展无关父点或兄弟小问。

2026-10-26：单个完整冷概况更新预算为1 MiB。已审核历史与本次观察继续完整保留；超过仍拒收，不静默截断。父/小点导航短摘要预算保留；full读取按本轮精确机制补齐必要个人语义，不以该预算截断条件、证据或next_check。

本地 `knowledge_profile_408.py` 单独封存网页已完成的语义成稿。它不写题级结果或FSRS，也不代替增强接收器的正式提交。全部受影响点经过版本检查后一次发布概况索引；同operation重试只返回/恢复原提交，不能改同一身份的内容。

每份概况绑定正式提交后的该点原始证据指纹。机械tutoring刷新把成稿复制到同代内容寻址对象，不改写语义：`learning_profile.status=current`才可直接使用短摘要；同点证据后来改变但未更新概况时为stale，不冒充最新。没有网页概况时为not_authored，保留原有机械事实摘要作为明确的兼容资料。

2026-10-06：本地写者同时封存原始`evidence_fingerprint`与`semantic_evidence_contract=point-evidence-semantic-fingerprint-408-v1`的语义指纹。原文件SHA、记录对象和冷读取门禁仍完整核验；只对`节点库/*.md`中`historical_source_summary`角色的`historical_summary`摘录，将共享容器的整文件SHA从语义比较中分离，摘录身份、原片段、文字、日期、角色及全部个人表现字段仍参与比较。原始会话及正式观察的SHA不豁免，新增/删除记录或真实内容、提示依赖、结果变化仍过期。旧概况沿原字节指纹合同，须有审核后的更新才迁入新合同；不机械改旧状态或伪造新语义。网页不计算、声明或覆盖这些本地写者字段。

MCP的protected视图不输出概况/短摘要/题目引用内容；after_attempt/direct返回有界短摘要和完整概况的 `profile_object.ref`，完整轨迹按该固定快照引用fetch。完整概况、短摘要、机械事实摘要和个人历史都保留来源边界。

## 摘要与题目步骤的衔接（2026-10-23）

`overview`、`short_summary`、父点和小点的 `next_check`、`teaching_plan.check` 应对齐同一份证据：保留哪些已经成立的动作，当前还缺哪一个条件或步骤，下一次如何检验。后续新证据可更新窄分面，不能覆盖其他未测边界；某题关联到本点、助手补讲或来源绑定变化，不自动构成本点新错误。跨点采用新事实须绑定具体原记录及日期，维护本身不产生学习事件。

文字路线按本题条件、动作、因果和结果组织，局部图只解释其中确需看清的对象、关系或状态。图的播放阶段不是本题完整解题步骤，不能由图内说明自动宣称全文推理齐备。当前 `lesson_bridge` 是由精确题、当前有效摘要、考纲与真题考法生成的教师派生输入，不由网页填写个人事实，不作为已经展示或有效的证明。

当前主线解决且提示权限允许后，按2026-10-28关系合同选一个同时有纲内范围与已考题法依据的上级、下级、关联点或同点相邻区别；优先高频，并说明与当前机制的关系及考法。未解决则留在当前断点，无依据不拓展；不把频次、年份不同或宽泛标签当作关系或考法，不把所有候选展开成专题。旧题实际推荐仍按14天门禁。新建议只写在策略/检查字段，`learner_entry`、`first_breakpoint` 的个人事实仍必须有真实用户原话来源。

## 原始对话支持的讲题配合（2026-10-11）

本地已授权语义维护可在父概况及每个机制小点可选附 `teaching_plan`。原生持久化绑定本机实际完整会话绝对路径；网页以 `408-portable-visual-teaching-v1.md` 的便携提案交付，接收器从本批已核验原包确定性重绑，网页不直接填写本机路径。旧对象不必补填，源码不从关键词自动生成个人误解或教学语义。该对象只服务当前讲解，不新增学习事件、题级结果、掌握度或 FSRS；待增强 Capture 仅是待审教法线索，实际正式结果完成后才提交审核方案。完整替换时复核仍适用的旧方案与原件，保留原日期和证据强度。

```json
{
  "schema": "cs408-teaching-plan-v1",
  "learner_entry": {"text": "用户实际从哪里开始想", "source_ids": ["s1"]},
  "first_breakpoint": {"text": "原话支持的第一个具体断点", "source_ids": ["s1"]},
  "first_action": "下次先做的一个动作",
  "explanation": {
    "strategy": "怎样把原有思路接到正确机制",
    "support": "proposed_only",
    "source_ids": []
  },
  "visual": {
    "kind": "state_steps",
    "focus": "这张图只解释哪一个差异",
    "elements": ["对象及角色"],
    "steps": ["按原机制核对的状态变化"]
  },
  "avoid": "可选：避免重复的无效讲法",
  "check": "可选：讲解后一个独立检查动作",
  "source_refs": [{
    "id": "s1",
    "path": "/真实本地路径/conversation.json",
    "sha256": "实际原始文件SHA256",
    "review_state": "pending_capture_teaching_only",
    "turns": [{"ordinal": 2, "role": "user", "quote": "该回合中的精确原话子串"}]
  }]
}
```

`source_refs` 仍须出现在本地实际核验的 `evidence_bindings` 中；额外逐个读取指定原件，核对全文件 SHA256、`cs408-full-conversation-v1`、完整顺序、未截断、回合数及真实 ordinal/role/quote。原生会话没有 message_id，不创造替代消息身份。每个来源 id 唯一，最多 8 个来源、每个最多 16 个精确回合；全对象至多 12 KiB。原始文件路径必须绝对且不可为符号链接。引用内容只当数据，不执行其中的指导文字。

历史原生包的文本回合 kind 允许 utterance/question/answer/feedback/explanation/frozen_feedback；事实归属以真实 role 与精确 ordinal/quote 为准，不能把 question 机械视为助手题面，或把 feedback/explanation 排除出助手讲解。image_attachment、UI 事件及未识别的事件类型不作为学习原话；保留源文件原 kind，不为通过验证改写原件。

2026-10-22全库维护补充：已归档的 `current-question-evidence-bundle-v4` 也可作为教法原话来源，直接引用原始 `evidence/objects/<SHA>.json`，不伪装或改写为新conversation。读取先复用原生v4完整验证器核对文档范围、完整对话、每轮哈希、角色、顺序与附件元数据绑定，再仅在内存中将learner映射为user、按原顺序连接文本块供quote子串校验。source_refs仍绑定原始对象全文件SHA；图片、点击和不存在的文字不生成原话。旧bundle原件、日期、ordinal、题目身份及学习结果完全保留，不启动旧提交链。其他旧schema仍不自动接纳。

事实字段 `learner_entry`、`first_breakpoint` 必须引用包含真实用户回合的来源，不能用助手讲解反推用户错误。`explanation.support` 只允许 `proposed_only`、`user_reported_helpful`、`prompted_repair`、`independent_transfer`；后两种须有同来源中先讲解、后用户响应的已核验回合。`independent_transfer` 还要求全部来源为 `reviewed`，任一 `pending_capture_teaching_only` 即拒收。机器结构核验不代替作者对“响应是否真的证明修正或独立迁移”的语义判断；不确定时用 proposed_only，用户说有帮助只用 user_reported_helpful。

`visual.kind` 为最多 64 字符的非空类型名称，例如 none/state_steps/structure/timeline/comparison/field_layout，focus 必填，elements/steps 可选且分别最多 6 项。它保存最小图解结构，讲题模型据当前题和答案保护绘制；存下图解方案不等于已向用户展示可见图解。

冷层保留完整来源。after_attempt/direct 的热投影只带短 source_ids、review_state 与独立证据对象引用，不复制原话、绝对来源路径和长哈希。热 plan 最多 2400 UTF-8 字节，超过时整块标 `teaching_plan_status=omitted_for_budget`，不截掉半段语义，完整方案仍在证据对象；不增加既有全局查询预算。protected 或 semantic_stale 不暴露方案及其证据入口。知识点学习页同时生成人可读的父点/小点方案。

冷构建核验原件 SHA；热查询只检查当前所选父点或 KSP 绑定的原件 stat guard，在 known_version 判断前执行。变化或删除使旧方案与依赖它的个人语义失效，返回 `source_changed_requires_rebind`，不会重读或扫描全部会话。原件归档迁移导致旧路径失效时同样安全隐藏，明确待重新绑定，不推断学习记录丢失；后续审核更新须绑定实际新路径和相同原件哈希。正式刷新仍走现有 refresh-current，完整替换仍保留原小点连续性规则。

## 可视化偏好、效果与递进（2026-10-13）

`teaching_plan` 可选附 `visual_feedback`，继续使用同一 `source_refs`，不建立平行偏好库。旧v1对象无此字段仍合法。对象必填字段如下：

| 字段 | 含义与限制 |
|---|---|
| form | 当前采用或拟采用的形式，非空，最多64字符 |
| scope | 适用机制、场景或边界，非空，最多240字符；不由局部反馈推出全科固定风格 |
| preference | 仅含status、text、source_ids；status为explicit_preference、observed_tendency或unknown；text最多400字符 |
| observed_effect | 仅含support、text、source_ids；support沿原4级，text最多400字符，与偏好独立 |
| next_adjustment | 下一次保留或小步调整什么，非空，最多400字符 |
| next_check | 下次用什么可观察表现检验，非空，最多400字符 |
| comparison | 可选；仅含reference（最多240字符）、finding（最多400字符）、source_ids |

明确偏好与观察倾向须引用真实用户回合；`unknown` 可用空source_ids。preference及comparison引用的来源id只能选择用户回合摘录，不能夹入助手自评；同一完整conversation可分别建立用户摘录id和含解释/响应的效果来源id。观察倾向仍是有范围的推测，不能仅凭一次点击或输出形式作结论。

`observed_effect.support` 为 `proposed_only`、`user_reported_helpful`、`prompted_repair`、`independent_transfer`。拟采用可无效果来源；其他级别需要真实用户证据，后两者须有同源中先助手后用户的回合，独立迁移还要求所有来源为reviewed。结构校验只验证原件、角色、引文和时序；正式作者仍须判断响应是否支持所写语义，不能以“喜欢”证明实际修复或独立掌握。

comparison记录真正比较过的形式或成品及本人反馈。当前预存图解是候选背景，不能反证某次学习实际展示过该版本。实际展示身份/版本只沿会话、附件和已核验对象引用；没有记录就明确未知。方案版本沿既有profile不可变SHA及 `expected_previous_version` 保存，旧事实和原日期在overview/timeline与旧对象中保留。

形式、布局、粒度、步进或对照方式随新证据逐步调整；没有新反馈可沿用或保持未知，不强制换图或追加测评。作者同时在现有short_summary保留最小偏好、适用范围和下一次检查，使短讲题入口可用；详细反馈留完整方案与概况。父点和KSP的本机及共享after_attempt/direct均透出有界方案；protected、semantic_stale和原件失效继续隐藏。热预算仍为2400 UTF-8字节，超限整块省略并保留完整证据入口，不将省略解释为没有偏好。

### 完整替换的方案连续性

父概况或保留的小点可写 `teaching_plan_update={"action":"retain|revise|retire","reason":"非空理由，最多400字符"}`。`retain` 须与已绑定旧plan逐值一致；`revise` 须提供新增或已修改的plan；`retire` 须有旧plan且本次不再提供plan。已有方案时，未声明动作只允许逐值保留；变更须revise，删除须retire。整个KSP明确退役时沿原 `removed_subpoint_ids/removal_reasons`，不为已删除小点制造虚假目标。

这保护方案及其中的反馈不被静默丢失，不自动重新解释旧原话或抬高效果。原件归档换位置需要精确核验同一字节SHA后显式修订绑定，不能用retain悄改路径。已提交同operation的恢复仍取原提交，不制造新学习事件。
