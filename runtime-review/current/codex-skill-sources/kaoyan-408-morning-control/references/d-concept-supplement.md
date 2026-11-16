# D固定学习入口：v13

用于ordered_native_D与真实D独立display；campaign v2使用自己的learning-turn。旧v11/v12仍兼容；首次采用v13只重新加载助手，保留当前input_context、题序、保存请求和模型，不重启学习。Codex/Claude使用同一Python入口，程序自动绑定当前真实会话；学习session_id不是聊天编号。

## 一次加载、调用和缓存

首次按当前阶段读取question-worker、首次终态前读取wrong-intake，之后复用。用下列完整模板；助手源码只进store，不输出给模型，不拆成“读助手→下一次执行”或“调用→另一次存状态”。INPUT与OPERATION换成实际对象：

```javascript
// @exec: {"max_output_tokens": 24000}
const key = "cs408.trusted-one-call-helper.v13";
let source = load(key);
if (!source) {
  const r = await tools.exec_command({cmd:'cat "${CODEX_HOME:-$HOME/.codex}/skills/kaoyan-408-daily-study-loop/scripts/one-call-turn.js"',max_output_tokens:10000});
  if (r.exit_code !== 0) throw new Error("one_call_helper_unavailable");
  store(key, source = r.output);
}
const run = new Function("tools","text","image",source)({
  exec_command:tools.exec_command, write_stdin:tools.write_stdin
},text,image);
const out = await run({operation:OPERATION,input:INPUT,
  teaching_cache:load("cs408.d.current_teaching") || null});
const next = out.result.next_input_context || out.result.input_context;
if (next) store("cs408.d.current",next);
store("cs408.d.last_result",out.result);
if (out.teaching_cache) store("cs408.d.current_teaching",out.teaching_cache);
```

只读context和D题图交付不用view_image。v13在一次调用内完成完整传输：适用大小一次读取；超限或完整输出被截断时，最多四路并行读取2000字符块。按同一UTF-8快照的字节数、SHA256、字符数和JSON完整性核对，不截取摘要、不把半份结果交给讲解。只有已有明确礼花授权时，才向助手传可用的celebrate回调；可选UI失败不阻塞保存或重试。

## 启动与恢复

新ordered交接用 OPERATION="d-supplement-start"，INPUT为：

```javascript
{
  handoff:"<用户给出的绝对handoff路径>",
  handoff_sha256:"<用户给出的SHA256>",
  native_import_sha256:"<用户给出的导入回执SHA256>",
  session_id:"<交接中的D-ORDERED身份>"
}
```

程序内部核验原文件全部字节与共同顺序，只返回紧凑结果，不把整包输出给模型。首次只展示第一题；已有标准ordered曝光则沿本session已保存结果只读恢复当前题。不调用campaign status/start/prepare，不重新选题，不扫日志或手造目录。日期由程序取实际上海日期。既有非ordered D入口仍使用其真实handoff的display合同；已知input_context一律优先只读context。

恢复当前题用 d-supplement-context，input为已保存的当前input_context，view="protected"；不重放上一题、display或新曝光。pending_save/recover_delivery保留返回的learning_request_file；hold不擅自开下一题。首次或恢复结果返回后直接展示来源与全部presentation_markdown，等作答，不预解题目。

## 答后读取必要个性化摘要

d-supplement-context 的 input 用当前 `input_context` 和实际断点的 `teaching_mechanisms`。提示按 `after_attempt`，完整讲解获准后用 `direct`。已有 KSP 用精确身份，未知时传当前具体机制词。首轮返回本题哈希绑定的 `grading_reference`、`prepared_context` 和有界 `teaching_context`。以当前作答定位第一断点，历史只提供对应条件、提示依赖和检查动作。确有关键省略时同范围补读。按预置图解的精确题及阶段回执交付图解。

返回的input_context携带同题已选择的teaching_mechanisms、known_version和known_material_version。同题同断点直接续用；出现新断点时用本轮所需数组替换旧范围，不机械累加；明确回到父点时传[]。新题不继承旧机制范围或版本。后续仍核验最新个人版本和当前文件哈希；unchanged复用完整缓存，同时保留本次missing/ambiguous诊断，不把未命中称为精准命中。v13只在题目身份、视图、版本与实际持有材料匹配时发送known字段；缓存缺失自动完整读取。若上下文已经丢失旧正文，先store("cs408.d.current_teaching",null)，不能因为JS仍保存对象就声称本轮已读。变化和缺件读取返回内容或按诊断处理，不另读答案文件或重复resolve。

teaching_context中的visual_template须同轮按question-worker辅助文字线性讲解：正文先把本轮获准的步骤与因果连续讲清，再依据当前小点与真实作答选取scene/steps的必要部分，核对checks并交付对应可点击图；不能只解释模板，也不能让用户点击寻找主线。父点模板不冒称KSP独立审定教案，effectiveness=unverified不改为学习效果已验证。未作答的protected视图继续保护当前题答案。

同次teaching_context还可返回当前真实true_item_id绑定的question_visual_template。精确-Pxx只交当前小问教案，与最新局部个人摘要一起使用，不回退为父题或兄弟问；前问必要结果与当前答案区分。当前题为变式或身份明确未知时不得调用原型正式题教案冒充当前题。已预处理的模板内容只在after_attempt/direct出现，protected不暴露正文或完整对象引用；当前提示层级仍约束可展示步骤。

D终态快存按wrong-intake收尾：保存核验成功后的同一条final先给回执与刚结束题的知识点、实际考试问法、必记结论或条件及下次第一动作，再展示后继。commentary已讲过也须在final保留这段短速记，不重复长讲义；明确只记录不讲时服从限定。验证成功才导航，后继或兄弟题不进入速记。

本轮仅为“理解了／下一题／结束”的导航确认，且当前题答案、理由和必要资料已经核实、没有新增教学判断时，直接d-supplement-turn，不在保存前重复context。若用户仍提出新答案、理由或疑问，照常先做本轮有界context核验。

缺失/未命中/semantic_stale保留真实状态，当前题按实际证据讲，不扩全库。protected不公开答案或暗示方法。私有材料缺件/哈希不符/截断，且确实影响当前判断时，才沿返回的精确path和SHA补缺，不猜private目录。

只给字母先问一个关键理由；已有真实理由直接核对，不重复索要。正确字母配错误推理继续纠偏。D非终态不提交discuss；本题完整对话由终态入口统一提取，不能为保存提前结束。每轮至多一个推进问题并等待。

交接绑定新版预制图解时，同次 context 还返回 `prepared_visual_context`：仅当前 request_id 与 true_item_id 的独立语义模型和教师说明，已核对索引/文件哈希与当前 source_version。`available` 表示当前题文件与版本绑定通过，可作为本题教师候选，与当前精确模板的exam_visual_binding/lesson_bridge、有效父点/KSP摘要及grading_reference一并核对；不能仅凭版本相同就替代题面与语义核实。原题有图保留有意义的布局、拓扑、编号、字段、标签、方向和初态，可清晰矢量重绘；retire_old_design的旧设计不再使用；`unchanged` 复用已读同题内容；`stale/unavailable` 保留具体原因，按最新精确题模板现场讲解，不换题或重放学习。`teaching_notes.role=current_question_corrections` 标记本题已核验的来源纠偏，须对照当前题面及grading_reference处理，版本相同本身不证明语义正确。完整预制模型始终是direct/L5教师材料；after_attempt不能直接把它放入前端，也不能将完整模型降级来绕过提示级。protected不返回教师内容，小问模型不得继承父题或兄弟问。

新本地交接的 `prepared_visual_context.interactive_2d` 指向已经生成的 HTML 成品。带 `source_basis=native_due_sources_v1` 的常规 D 在首次展示时用本机真题身份、接触证据和已装配题图复核；旧交接沿原来源绑定。出题给来的 `input_context` 保留原 handoff SHA，常规 `after_attempt` 可先读本题备好状态；请求本轮允许的提示用 `hint_level=1–4`，获准完整讲解用 `direct/5`。按 question-worker 的[成品交付入口](../../kaoyan-408-question-worker/references/interactive-delivery.md#备题交接里的预制图解)验 SHA 并原样使用。旧的语义模型索引继续沿上一段合同，只有确实无对应成品或本题源发生变化时才按当前范围补图。

## 终态一次保存并导航

答案和理由核实后先发实际简短反馈，不说已保存；反馈随后进入同一真实消息包。OPERATION="d-supplement-turn"，INPUT：

```javascript
{
  ...load("cs408.d.current"),
  expected_user_text:"本轮用户完整原话，保留内部空白和换行",
  observed_outcome:{
    first_result:"correct",
    terminal_state:"correct",
    prompt_dependency:"none", reasoning_result:"sound"
  },
  attachments:[], missing_fields:[]
}
```

判断按真实证据填写，不套模板。省略first_response/final_response时，程序从本题已核验原始消息范围复制首、末条用户原文，避免模型重复抄写；首条用户消息只是操作询问而非首答等特殊情况，可显式给出真实首答。无首答用first_response=null与first_result=not_observed。旧的完整输入继续兼容，不改已冻结请求。提示后完成用prompted_correct，prompt_dependency只接受minimal、progressive、full（独立用none；未观察到用not_observed），助手补充不填成用户终答。明确结束加intent="end"和terminal_state="unresolved"并hold；跳过用skip/unresolved；暂停不封终态。

reasoning_result只接受sound、partial、diverged、not_observed，不自造partially_verified等值。complete要求sound与真实完成；未核实完但用户明确进入下一题，用skip/unresolved并保留partial等真实依据，不把理由改成sound以通过。非法输入在冻结请求前拒绝，按返回合法值更正；已有learning_request_file必须沿原请求恢复，不编辑已冻结请求或清除幂等记录。

程序从真实题图展示消息锚点提取至当前用户回复及其后已发反馈，保留全部原文、时间、消息ID与原图附件；expected_user_text防新消息错绑。模型不重抄旧对话、不填后继ID或时间、不读日志。只手填额外已持有且未内嵌原件，缺件保留missing_fields；不造替代原文。比较可容忍末尾CR/LF差异，封包仍逐字保留真实消息。

只有capture_verified与package_verified均true且next_action=present_next才展示后继。成功工具输出只保留必要验证与续接字段；先在final重呈已核实的上一题核心短速记，再原样附上one_call_delivery.presentation_markdown并等待。presentation_markdown中的题序、来源、全部题图及适用的原题formal_id不重组；不为速记重新分析上一题、预解后继或准备长收尾。hold不展示后继。无需额外text(out.result)或再查回执；完整回执、计时、消息证明和恢复路径保留在out.result及result_file。

## 耗时与失败

timing_ms仅为当前调用的诊断：personal_context_ms测摘要读取，question_material_ms测当前题依据；transcript_extract_ms测真实消息/原附件提取；capture_ms含normalize_and_attachments_ms、package_seal_verify_ms、capture_commit_ms、receipt_write_ms、joint_verification_ms；navigation_ms含display_ms。total_ms为函数内到结果封装前的耗时，不含进程启动、最终诊断文件写入/JSON输出，不能再叠加子项。JS的tool_roundtrip_ms测工具往返，模型返回时间需看聊天时间轴，不能拿脚本计时冒充。计时不参与原始事实、幂等键或评分，普通学习不逐项播报。

真实保存失败只按返回的learning_request_file恢复原请求：
`python3 scripts/runtime_paths_408.py --run d_supplement_learning_408.py --input-json <原learning_request_file>`
Capture与完整包验证通过就接受，不手工重复核对原文。消息竞态按diagnostic核对本轮正文，保持未提交；不扫描源码、全部会话或账本。保存成功但导航缺层不重放保存。

没有functions.exec时，直接将同样JSON交当前shell的一次Python入口：
`python3 scripts/runtime_paths_408.py --run d_supplement_learning_408.py --operation <start|context|turn> --input-json -`
exec_command使用yield_time_ms=30000，超窗沿原session等待，不重提请求。学习题图均直接交聊天渲染，不view_image、裁图、放大、OCR或修图；按同版已核本地题源报告显示缺口并保留原始题号。新流程正式判分、FSRS、个人摘要写入均为0。

文字主线与辅助图解沿question-worker合同同轮交付；正文按本问逻辑连续解释，交互沿references/interactive-delivery.md及当前Visualize协议。依据同次interaction配方和有效个人摘要确定辅助区域、关系与允许步骤，主线不藏在点击里。after_attempt不自动授权公开答案，受保护内容在生成前排除，不放进隐藏面板或前端数据。包内旧教案不能覆盖当前交互要求；不重备、不重排、不重展示、不写作答来升级交付。

固定入口在开始/恢复或已验证终态的回应中附带当前本地后续去向。这是代理内部导航；普通教学与快存回执无需播报这段规则。只在用户提出对应请求时执行，日终正式编纂走 kaoyan-408-enhancement-intake，当日晚间问答走 kaoyan-408-evening-review。旧回执状态 awaiting_web_enhancement 表示快速证据已保存、正式事务尚待处理，沿现行本地作者流程续办。
