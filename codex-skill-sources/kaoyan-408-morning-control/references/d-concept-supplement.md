# D独立补充：固定学习与快速保存

仅用于真实D独立display；campaign v2仍走learning-turn。沿用已绑定handoff、session与当前receipt，不改题序或模型。

## 压缩后与首次接管

读取question-worker和本参考一次，随后复用；wrong-intake仅首次终态保存时加载，已加载则复用。当前题身份丢失才通过只读context恢复，不能重新display或提交上一题。context返回input_context、当前位置、当前题图和有界个人摘要；保留input_context，后继使用next_input_context替换。已封当前题拒绝冒充未答题。

真正functions.exec可用时使用v11助手的d-supplement-context，input是已知当前身份；它返回当前题图链接，不读取或处理图片，不产生曝光。不要查源码/help或另写适配程序。

## 一次调用助手

首次将固定助手加载到functions.exec的store，之后复用v11缓存；不能沿用旧v8/v9/v10缓存。INPUT与OPERATION替换为实际对象和操作名：

```javascript
const key = "cs408.trusted-one-call-helper.v11";
let source = load(key);
if (!source) {
  const r = await tools.exec_command({cmd:"cat /Users/your-user/.codex/skills/kaoyan-408-daily-study-loop/scripts/one-call-turn.js",max_output_tokens:10000});
  if (r.exit_code !== 0) throw new Error("one_call_helper_unavailable");
  store(key, source = r.output);
}
const run = new Function("tools","text","image",source)({
  exec_command:tools.exec_command, write_stdin:tools.write_stdin, view_image:tools.view_image,
  celebrate:typeof tools.mcp__codex_app__fire_confetti === "function"
    ? () => tools.mcp__codex_app__fire_confetti({}) : undefined
},text,image);
const out = await run({operation:OPERATION,input:INPUT});
const next = out.result.next_input_context || out.result.input_context;
if (next) store("cs408.d.current",next);
store("cs408.d.last_result",out.result);
```

终态用OPERATION="d-supplement-turn"，INPUT形状如下：

```javascript
{
  ...load("cs408.d.current"),
  expected_user_text:"本轮用户完整原话，保留空白和末尾换行",
  observed_outcome:{
    first_response:"真实首答", first_result:"correct|incorrect|blank|uncertain|not_observed",
    final_response:"真实终答", terminal_state:"correct|prompted_correct",
    prompt_dependency:"none|minimal|progressive|full|not_observed",
    reasoning_result:"sound"
  },
  attachments:[], missing_fields:[]
}
```

按真实证据填写outcome，不把模板选项字符串直接提交。没有首答用null和not_observed；提示后完成与独立正确分开。理由仍有缺口不封存，继续question-worker纠偏。明确结束本题加intent="end"和terminal_state="unresolved"；明确跳过继续用intent="skip"和unresolved；仅暂停不封终态。

只提交当前原话和判断。程序按当前题图的真实展示消息锚点，读取至本轮用户消息的全部实际user/assistant原文，保留换行与时间，首次读取后冻结。它自动填时间并从handoff与plan双重绑定的原顺序取后继；模型不重抄旧对话、不指定next ID、不查date。expected_user_text防止新消息到达后错绑。源消息含用户图片仍提供全部原始attachments。真实消息锚点、图片原件或线程来源缺失时报告具体缺口，不生成替代原文。

答案和推理已核实后先给简短实质反馈，再同轮保存；不能把拟稿当已发反馈，也不提前声称保存成功。消息入口会把用户之后已实际发出的反馈一并封存。保存、等待和回执校验在同次functions.exec内完成，v11仅返回题图链接。正常返回后直接用one_call_delivery.presentation_markdown或next_display.presentation_markdown展示，不调用view_image、裁图、放大、OCR或修图，不搜索解析或求解下一题。完整消息证明和回执保留在learning_request_file、result_file及out.result中。v11仅精简text输出：成功时不重复输出附件清单、完整消息引用和已显示题图，保留个人摘要、当前题必要材料、验证结果、恢复路径及后继身份；失败保留完整诊断。不要额外text(out.result)或再次打印完整回执。

## 讲解、资料和展示

- 正确且推理充分：先给已核实的简短反馈，同轮终态一次调用；成功后给进度、来源和全部题图链接，等待作答，不重复确认。
- 字母正确但推理错误：定位第一断点，给必要依据和一个推进问题，等待，不保存成整题正确。
- 只答字母：只问一个关键理由；D非终态对话由终态入口从真实消息统一收集，不为保存提前结束。
- 局部纠偏不重新推演整题全部路径、不读保存源码、不规划后继；当前题和已有同版摘要够用就直接讲。
- 必要个人依据尚未取得时，仅调用一次d-supplement-context，input为当前input_context加view="after_attempt"及当前断点的teaching_mechanisms，必要时同次加known_version。同次返回有界teaching_context、当前题哈希绑定的grading_reference，以及prepared_context中的当前题solution和teaching必要正文；私有正文只在after_attempt/direct可用。先复用这些正文与条件，不再另开一轮读取solution.md或teaching.md，不读图。prepared_context不是最新个人摘要；个性化只按teaching_context的source_version/status判断。缺件、哈希不符或truncated时保留明确缺口，只有确实缺少当前判断所需内容才沿精确引用恢复。同版摘要复用；未命中、缺失或过期直接current-only讲解，不追加来源或全库检索。
- 正常讨论及终态提交前不读保存/消息提取源码、环境、SQLite或会话日志，不写临时预检脚本；消息提取与完整性校验由固定入口负责。capture_verified与package_verified已通过就接受返回结果，不再次手工对比保存原文。只有真实失败才按返回的具体恢复动作处理；必要教学反馈先给，失败不提前导航。
- 私有核对只用current_display.private_material_refs或prepared_material_refs/next_display.prepared_material_refs的精确path和SHA，不猜private/units目录。预处理材料是题目依据，最新个人状态以teaching_context的source_version/status为准。
- 默认来源加已准备的全部题图链接，不读图或转录选项；视觉缺陷由用户在网站核对，学习中不设图像修复兜底。用户明确要文字时才提供已有公开文本，不猜题。未作答不公开答案、不提前讲解下一题。

首段实质讲解10–20秒只是目标。模型请求等待不是本地脚本耗时；新真实回合才可验收速度，静态测试不证明端到端秒数。

末尾CR/LF差异仅在expected_user_text比较时容忍，入库仍逐字使用真实消息，不能strip原文、忽略内部换行或宽松匹配正文。返回check_current_message时，使用diagnostic里的当前message_id与长度核对本轮消息，保持未提交；不得搜索源码、全仓库或全部sessions排障。

## 恢复与能力降级

capture_verified与package_verified均true且next_action=present_next才交后继；hold不推进。recover_delivery表示本题已保存，仅恢复交接。原请求保持learning_request_file，不换身份、日期、原话或key：
`/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/d_supplement_learning_408.py --input-json <learning_request_file>`

晨间题图直接交聊天渲染，不检查图片显示结果，也不因裁切、模糊或显示失败启动恢复。用户自行在网站看原题。历史显式image-handoff仅恢复已有结果，不重提作答。只读恢复不放礼花；正式写入/FSRS/个人摘要均为0。

没有functions.exec时，本会话固定用同一短输入调用Python入口，exec_command设置yield_time_ms=30000；超过窗口沿原session等待，不重提保存。成功后直接展示返回的题图链接，不读图、不加载JS助手或重复探测组合工具。这条降级路径仍有一次模型往返，不能宣称已实现同次保存读图。可选礼花工具若已返回unsupported则本会话跳过，不重复尝试或查路由阻塞学习：
`/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/d_supplement_learning_408.py --input-json -`

旧d_concept_supplement_408.py的display/commit/turn保留历史请求恢复和真实消息源不可用时的完整原文模式；不自动退回手工链。旧concept-review-session-v1只走原recover，不补造Capture。顺延仍保留原plan，display用真实执行日review_date重查曝光；新入口沿用真实display日期。跨日失败只恢复导航，不重放成功保存。
