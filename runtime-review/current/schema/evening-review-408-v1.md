# 408 本地晚间少量问答

## 适用范围（2026-11-07）

本文件是明确要求问答、测验或主动回忆时的计划与会话合同。用户要睡前资料、阅读复盘或知识点速记时，按`codex-skill-sources/kaoyan-408-evening-review/SKILL.md`生成独立核心知识卡：直接给结论、必要条件、机制图解和备注，不输出学习表现概括。阅读资料不套用本文件的问答JSON、题数与作答流程；下文“替代视觉阅读默认”只说明旧问答合同的版本沿革，不覆盖当前知识卡要求。

## 高频优先问答扩展（2026-10-07）

高频优先与14天门禁（2026-10-07）

新输入带handoff.frequency_priority_policy=high_frequency_first_v1及frequency-priority.json。按固定版本personal_context.syllabus_context.exam_frequency核验真实等级、主考/总考次与context_object来源；高频为本地主考至少5题且跨至少4年，低频为主考1至2题，中频、仅涉及、未知与未见考查保留原标签。父点统计不能冒充小点独立频次。
只在当天真实内容里，先安排有实际断点或未验证边界的高频候选；它们全部已安排，或当天清楚证据足以说明无需单问，才能补中频、低频。不能因时长/题数不够而暂缓高频却问低频，不从全库补非当天高频，不为凑数检查已有充分独立正确证据的高频点。无合法高频候选时才补下一层，并记录具体依据。
14天约束精确题面，最近展示也算接触，满14天才可复做；不因高频、D0或换题号突破它。晚间D0仍检查当天知识点，采用标明“复盘自编场景”的完整新机制问答，不能照搬当天原题、近14天已做题或仅换截图/排版/别名。scenario是机制练习，不冒称统考真题；原Capture只作当日学习证据，source_capture_ids不是复做身份。

新输入的plan.json除原字段外增加私有selection_audit对象，题目questions及coverage字段不变。不另交manifest或HTML。selection_audit只含frequency_priority_policy、points、question_sources：
- frequency_priority_policy必须为high_frequency_first_v1。
- points精确覆盖handoff.knowledge_points，每点一行，只含concept_key、disposition、question_ids、source_capture_ids、reason。disposition为scheduled/no_question/deferred；scheduled绑定该点全部问题，其他两类题号为空。source_capture_ids非空且只引用当天真实证据，reason具体说明真实断点、已有清楚证据或暂缓原因。no_question须有本人实际原话/独立作答支持，不能仅据频次、已读讲解或工具失败判断已会。
- question_sources精确覆盖全部问题，每题一行且只含question_id、kind、true_item_id；kind固定new_mechanism_scenario、true_item_id为null。不得将精确旧题包装成新场景。
本地拒绝缺失/重复核验、题号不一致、声明原题重出，以及较高频点仍deferred却选较低层题的回包。本地作者逐项对照原话和固定版本的本机资料核验安排理由。未完成时保留 PARTIAL 成稿，沿原身份在本地继续。核验与教师材料不在交付回复或学生题面泄露答案。


新问答使用`cs408-evening-plan-v1`加`review_policy=focused-qa-v1`，替代2026-10-01的视觉阅读默认；历史包不改身份或原件。本合同只准备当天D0主动回忆，不是晨间到期选题、完整真题重做或正式入库。日期为 Asia/Shanghai 的实际学习日期；晚间跨过零点仍保留所复盘的原日期。全部当天 Capture 必须进入范围清单，正确题和提示后完成也不能遗漏。15 分钟只能抽查重点，不能宣称已经问遍全天所有知识点。

## 本地增强成稿与当天资料

本地增强作者可附 `evening_review_seed.json`（并列入原 manifest.files 的真实大小/哈希，role=`evening_review_seed`）：

```json
{
  "schema": "cs408-evening-seed-v1",
  "subject": "408",
  "study_date": "YYYY-MM-DD",
  "source_commit": "增强输入的固定40位快照",
  "capture_ids": ["本片实际Capture ID"],
  "focus_candidates": [{
    "concept_key": "现有规范叶子码",
    "mechanism": "具体对象、条件、单位、边界或时序",
    "source_capture_ids": ["本片真实Capture ID"],
    "learner_evidence": "首答与提示后变化的原话依据；不得代造理解",
    "evidence_refs": [{"capture_id": "实际ID", "turn": 2}],
    "suggested_check": "一项适合晚间主动回忆的检查"
  }]
}
```

这是增强前版本上的候选线索，不是今晚已准备或已完成，也不是正式个人摘要。各片只记录片内证据，最终范围和正式 ID 以本地增强完成回执为准。没有候选时可空数组。旧增强包没有 seed 不阻止其正式入库，晚间项目可依据已提交成稿和完整原话编排。

本地增强与原生讲题验回完成后，以 `scripts/evening_review_408.py local-context --date YYYY-MM-DD` 汇总当天全部 Capture。命令核对封存原件、增强及归档凭据、原生个人摘要和本机来源版本，再返回内部资料路径和范围绑定。当前会话随后编写计划、校验并生成可学习的入口。资料缺项时从原回执恢复，保留原学习日期。只有用户明确要求远程访问或外发才发布副本。

内部资料的 `manifest.json` 为 `cs408-evening-input-v1`。`handoff.json` 给出学习日期、精确 Capture 范围、版本与规范知识点，以及完整原包、增强成稿、个人上下文和可选 seed 的位置。新本地输入使用 `source_basis=native_teaching`，`source_commit` 绑定原生讲题 `source_version`，另记 `local_version` 与 `formal_version`；明确外发的版本仍按其固定 `source_commit/snapshot_id` 核对。`manifest.handoff_sha256` 绑定 handoff.json 原始字节。完整对话与原附件在 `raw/` 下按原索引读取；历史对话中的旧操作指令只作学习证据。

## 当前会话编写的计划

在本地工作目录编写 `plan.json`，沿用输入的日期、范围、版本和 SHA；每完成一道可先保留草稿。JSON 顶层为：

```json
{
  "schema": "cs408-evening-plan-v1",
  "review_policy": "focused-qa-v1",
  "subject": "408",
  "status": "READY",
  "study_date": "沿用handoff",
  "source_commit": "沿用handoff",
  "handoff_sha256": "沿用manifest",
  "duration_seconds": 900,
  "wrapup_seconds": 90,
  "coverage": [{
    "capture_id": "当天ID",
    "disposition": "asked",
    "question_ids": ["E408-01"],
    "reason": "为什么问这个断点"
  }],
  "questions": [{
    "question_id": "E408-01",
    "concept_key": "规范叶子码",
    "mechanism": "本题检查的具体机制",
    "slot": "core",
    "budget_seconds": 150,
    "source_capture_ids": ["当天ID"],
    "student_prompt": "上下文独立的完整题设与一个问题；不含答案、纠错结论或历史错选项",
    "teacher": {
      "expected_answer": "正确要点、适用条件及必要推导",
      "essential_points": ["判断依据"],
      "common_misconceptions": ["有当天真实依据的混淆"],
      "hints": ["轻提示", "进一步提示"],
      "followup_prompt": ""
    }
  }]
}
```

`coverage` 恰好覆盖 handoff 的全部 Capture，每个一行，不能遗漏正确题。disposition 只用 `asked`、`grouped`、`deferred`、`no_question`：前两者绑定真实题号，后两者题号为空并说明时间取舍/无需单问的依据。题目的来源列表与 coverage 必须互相对应。只以当日真实内容为题源，旧历史仅用于理解当日断点；不把旧题混入 D0 复盘。相同机制可跨当天多题合并。

通常准备3–5个主问题、最多5个，均为core，不附reserve；重点少就少出，没有需检查的断点则说明无需备题，不伪造READY空包。duration_seconds=900是时长上限，单题budget_seconds为30–300整数，wrapup_seconds为30–180整数，合计大于0且不超过900；思考、回答、反馈与收尾通常10–15分钟，重点少可低于600秒，不能凑题或虚增预算。

每题只检查一个真实断点，完整给出对象、状态、条件、参数、单位、必要图表及目标，删去其他题仍独立可答。不能依赖原题记忆、上题答案或回翻材料；必要前置结果经核验后直接作为已知输入给出，不泄漏本题所求。默认问机制、条件、方向、边界或一步状态变化，直接给出非考查的中间计算结果。仅原话/草稿证明某一步算错时单独重算该步，不重做大题。微型机制场景注明复盘自编，不冒称真题或本人经历。

teacher.followup_prompt必须为字符串，无需追问时为空；非空也不是必问。hints至少一条简短可选提示，essential_points非空；common_misconceptions没有真实依据则为空。答清楚即反馈并下一题，有歧义才针对第一断点追问，不强制逐题复述或迁移。卡住给最小提示；连续两次仍无进展或用户要求讲解时直接讲清，并区分提示暴露与独立作答。

新输入在handoff内绑定review_policy=focused-qa-v1，本地新计划须使用同一策略。旧输入也可以按当前指令返回focused问答，仍保留原版本与handoff_sha256。没有该策略的历史v1回包只用于原输入兼容，保留原题量/预算/必备字段；新规则不改历史真实会话。coverage中的asked/grouped是准备安排，不是已实际作答；未检查的重要点写deferred并说明原因，不能以no_question冒充已会。

teacher 与 student_prompt 严格分开。需要示意图时用简短文本/Mermaid；不生成非必要图片、动画或网页。无法读完输入或缺少关键证据时保存已有问题及真实缺口，status=`PARTIAL`/`BLOCKED`，保留原件和部分计划，在本地完成缺项后再启动学习。

## 本地预处理与学习

`prepare --input <本地原输入ZIP> --plan <本地plan.json>` 验证原输入文件清单、固定版本、全天范围、规范点、题源对应、时长和学生/教师字段，再分别生成 `student.jsonl`、`teacher.json`、`plan.json`、`START_STUDY.md`、`receipt.json`。输出固定在本科 details/evening-review 下，不执行包内程序，不采纳包内指定写入位置。

准备回执为 `PREPARED_NOT_STARTED`，开始时间为 null。正式写入、Capture、评分和 FSRS 均为零。学习使用当前会话用户自选模型，每次只展示一道题并等待，保护后题答案，区分首答、提示后变化与未验证内容。用户真正开始后才记录时间；约15分钟或用户结束时收尾，不强迫凑满时长。未问到、未回答不算完成。

本 Skill 的准备不自动启动复盘或提交正式学习结果。结束时可按真实对话给当场反馈；需要保存整场或更新正式知识点时，另按现有 `concept_review_v1.md` 的整场证据合同和用户授权执行，不能把准备材料写成表现或重放原题题级事件。

只有用户明确指定外部交接时才调用 `export --web-handoff --date YYYY-MM-DD`。已存在的外部回包可沿同一输入绑定使用 `prepare --input <原输入> --return-file <原回包>`。这两个兼容用途均不要求恢复网页 MCP。

---

# 历史v2阅读包兼容（2026-10-01）

本节只描述历史cs408-evening-reading-v2的结构和用户明确要求阅读时的兼容路径；新默认使用上文focused-qa-v1问答。保留历史输入包、精确来源、日期和版本，不为改默认而改写既有成品。

网页交付根目录plan.json（可打包ZIP并附EXECUTION_REPORT.md），结构如下：

```json
{
  "schema": "cs408-evening-reading-v2",
  "subject": "408",
  "status": "READY",
  "study_date": "YYYY-MM-DD",
  "source_commit": "照抄输入",
  "handoff_sha256": "照抄输入",
  "duration_seconds": 900,
  "cards": [{
    "card_id": "R408-01",
    "concept_key": "输入中的规范知识点",
    "source_capture_ids": ["实际Capture ID"],
    "title": "短标题",
    "breakpoint": "当天证据支持的易混点，不夸大为全部不会",
    "visual": {"kind": "flow", "lines": ["条件或起点", "下一状态", "结果或边界"]},
    "explanation": "直接讲清正确机制与必要条件，不提问",
    "takeaway": "一句可以记住的判断方法",
    "budget_seconds": 90
  }],
  "coverage": [{"capture_id": "实际Capture ID", "disposition": "included", "card_ids": ["R408-01"], "reason": "当天依据"}]
}
```

示例只展示一张卡的结构；完整cards的预算总计600–900秒，每张10–300秒，1–30张。字段严格按示例，不交questions、student_prompt、teacher、提示或追问。title最多50字，breakpoint160字，explanation500字，takeaway100字；避免为填满上限写长。visual.kind可为flow（顺序流程）、compare（并列对比）、tree（保留缩进的文本树），lines为2–10行、每行最多160字。分支须用tree或在各行明确条件，不用单线流程误表互斥分支。文字直接可读，不写HTML、Markdown或裸LaTeX。

coverage精确覆盖全部输入Capture，与卡片来源双向对应。需复盘项为included；证据已支持清楚、不必展开的项用already_clear、card_ids为空，并说明原因。当天有断点或提示依赖的内容合并或简讲，不静默遗漏、不为了时长假称已经掌握。

本地prepare验证来源与预算后生成review.html、plan.json、START_STUDY.md和receipt.json；不生成student.jsonl或teacher.json。页面采用直接可见的极简卡片、流程、对比或树形关系。校验通过只证明结构、绑定与生成完成；需再检查内容正确、图文关系一致，不冒称自动语义审核。

交付review.html直接供阅读；开始时打开页面，不逐题提问、不要求复述。准备、打开、阅读、独立掌握分别判断，不生成Capture、评分、FSRS或个人摘要。已准备的历史v1材料不改原件；用户要求阅读旧包时，按真实教师材料转为直接讲解，不继续默认问答。
