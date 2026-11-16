# 新v2固定学习入口

仅用于已发布且绑定saving-contract-v2的campaign。模型负责判断和教学；入口负责原话、首答/草稿路由、游标、终态、Capture与导航。D独立补充、普通standalone和旧会话沿各自入口。

实际讲解复用返回摘要内的 `syllabus_context`，按2027大纲路径和 `teaching_boundary` 限定范围。答后首次优先使用question_exam_introduction的预生成计数句，说明考点、统计范围、总考次、主考/兼考次数和频次等级，然后直接讲当前题；禁止只报年份不报次数；不播报考纲覆盖清单。同题追问不重复，protected不暴露方法提示。主考/涉及分别表述，小点 `parent_point_not_subpoint` 的次数和年份只能归父考点；D替换题按当前true_item_id绑定开场。范围外不自动展开，必要前提只补本题所需，未核验范围不冒称通过。其余细则复用 question-worker 的“2027 考纲与真题开场”。

## 正常调用

当前题身份已知时一次exec_command直接提交JSON，设置yield_time_ms=30000，不另写生成器、临时文件或输出过滤器：

```text
python3 scripts/runtime_paths_408.py --run learning_turn_408.py --input-json - <<'CS408_LEARNING_JSON'
{"schema":"cs408-learning-turn-v1","request_id":"本轮稳定唯一ID","display_receipt_sha256":"当前真实SHA","intent":"discuss","response":"B","reasoning_result":"diverged","prompt_dependency":"none","message_refs":[{"id":"previous_assistant","kind":"utterance"},{"id":"latest_user","kind":"answer","expected_text":"本条用户完整原文，保留末尾换行"}]}
CS408_LEARNING_JSON
```

占位值替换为真实当前值。新回应使用一个request_id；重试原ID/原字节。只含新原话；程序读取原草稿，模型不填cursor/run_id/item_id/terminal_state，不重抄已保存全文。完整读取返回，不只打印选项或丢掉摘要/下一题SHA。

| 字段 | 用法 |
|---|---|
| intent | discuss讨论；complete已核实完成；pause暂歇不封存；end结束未解并hold；skip明确跳过继续；correct-late已封原题追加更正 |
| response | 当前真实最终选项A/B/C/D/BLANK/UNCERTAIN；尚未作答或纯求助为null |
| reasoning_result | sound/partial/diverged/not_observed，按真实推理，不为保存成功改标签 |
| prompt_dependency | none/minimal/progressive/full/not_observed，按整题实际提示；已记录提示由程序保留 |
| new_turns | 只含未保存的原始消息；role=user/assistant；kind=utterance/answer/reasoning/hint/correction/restatement等；observed_at未知为null |
| message_refs | 与new_turns二选一，按当前线程消息读取原文，见下节 |
| attachments | 新增用户图/解析的path/role/mime_type/label；已绑定题图由原生writer收集 |
| missing_fields | 真实附件缺口的字符串数组 |
| teaching_concepts | 确有必要的规范代码；默认由题目绑定解析，不猜编号 |
| teaching_mechanisms | 当前具体断点的精确关键词数组；与父规范代码同次传入，优先取命中小点，未命中不换词补查 |
| need_context | 可主动请求摘要；讲解/纠偏/无最终选项的概念求助已自动读取，不填写或false都不会关闭必要读取 |
| recognized_original | 仅用户明确认出原题才true，默认false |
| response_seconds | 实际作答秒数，否则null |

complete不是强制封正确：选项不对、理由不足或提示依赖未知时保存事实并继续教学。真正完成时程序派生correct/prompted_correct，不提供screened_correct切换参数。end/skip保留已有首答与提示，封存为unresolved，不补造正确。

## 原始消息引用

Codex 和 Claude Code 正常学习统一用 message_refs。程序自动取得原生会话编号，解析选择器为真实消息编号并封存原文快照；客户端、会话和消息编号一起用于去重，不把 Claude 的 ID 填进 Codex 索引。new_turns 只保留给既有恢复或明确外部原文兼容。

```json
{"message_refs":[{"id":"真实消息ID","kind":"answer"}]}
```

普通连续答题也可用previous_assistant和latest_user：前者读取本次用户消息之前最后一条实际助手消息；后者必须带expected_text原话，防止执行期间新消息到达后错绑。程序保留原文本、按消息ID去重并冻结本轮请求；重试不重新指向最新消息。

```json
{"message_refs":[{"id":"previous_assistant","kind":"correction"},{"id":"latest_user","kind":"reasoning","expected_text":"当前用户消息完整原文\n"}]}
```

kind仍由模型按实际角色指定，不能把普通确认标成hint/correction。原生内嵌图片自动提取；各轮快照与附件留在原请求中，终态一起封存。只有额外外部原件才填 attachments。没有当前会话身份/原消息，或范围无法准确引用时，报告具体缺口；不扫描其他聊天、不自动退回模型重写。首次接管已有草稿时沿用真实保存边界，不重交旧消息。

verified_transcript表示从记录核对；caller_supplied_verbatim_not_independently_verified表示模型提供原文，不能冒称独立逐字核验。

## 教学反馈提前

当前答案与推理核实后，先在commentary发出简短实质反馈，再同轮调用保存。不提前声称已保存，不提前展示下一题。按实际顺序引用早发反馈，在 latest_user 后追加 `{"id":"current_feedback","kind":"utterance","expected_text":"本轮用户之后已实际发出的全部可见反馈原文"}`，程序会提取该段全部消息（包括 Claude 分块输出）并逐字核对。也可用真实消息 ID。不能只交 previous_assistant/latest_user 而遗漏本轮用户之后已发出的反馈。完整保存校验仍是下一题展示条件。核验成功后的final须按wrong-intake统一标准提炼上一题可迁移的知识点、二级结论、适用条件及下次第一动作，再附获准下一题。原题数值答案清单不能替代知识速记。有明确视觉收益时，以简短文字配本问可点击机制图，公式保留正文。commentary已讲过不能替代这段衔接，明确只记录不讲的限定除外。

## 返回后的动作

- ask_reason：保存首答后问一个关键理由，等待，不封存/庆祝/推进。
- teach_current：使用current_item_personalization.teaching_context直接纠偏；缺资料状态如实保留，不再查答案capsule。
- present_next：完整Capture通过，直接展示next_display的题图Markdown或next_image_paths的全部原图链接，不调用view_image、不裁图、放大或修图；保留后继display_receipt_sha256。
- hold：停止，不展示新题。
- continue_current_item：原题补充成功或旧回执重放，维持正在学习的题。
- recover_same_request：先给已核实反馈，按learning_request_file恢复，不换标签、原文或首答。
- recover_delivery：真实身份或交接缺层才按result_file恢复，不重做保存；仅视觉显示缺陷不启动修图或image-handoff。
- report_completion：按代表题/补充/未答carry-over真实范围收尾。

celebration_eligible提供本次可庆祝信号，实际礼花由已绑定原生工具执行一次。原生调用未成功不冒称已放。formal_write_count/fsrs_write_count/personal_summary_write_count仍为0。

## functions.exec适配

仅真正functions.exec可用时，首次加载固定助手并缓存v10，之后复用；不用node_repl模拟：

```javascript
const key = "cs408.trusted-one-call-helper.v10";
let source = load(key);
if (typeof source !== "string") {
  const r = await tools.exec_command({cmd:'cat "${CODEX_HOME:-$HOME/.codex}/skills/kaoyan-408-daily-study-loop/scripts/one-call-turn.js"',max_output_tokens:8000});
  if (r.exit_code !== 0) throw new Error("morning_helper_unavailable");
  source = r.output;
  store(key, source);
}
const run = new Function("tools", "text", "image", source)({
  exec_command: tools.exec_command, write_stdin: tools.write_stdin, view_image: tools.view_image,
  celebrate: typeof tools.mcp__codex_app__fire_confetti === "function"
    ? () => tools.mcp__codex_app__fire_confetti({}) : undefined
}, text, image);
await run({operation:"learning-turn",input:CURRENT_INPUT});
```

CURRENT_INPUT是同一份真实JSON。v10等待保存后直接返回题图链接，不读图；礼花方法名取当前实际工具。纯shell会话直接用首节命令并设置yield_time_ms=30000，保存后展示链接并执行已授权礼花，不加载JS助手。正常答题不读Codex Router。

## 恢复与后到更正

身份确实丢失才只读一次：
`python3 scripts/runtime_paths_408.py --run morning_campaign_turn_408.py context --review-date <原学习日期>`

同请求恢复：
`python3 scripts/runtime_paths_408.py --run learning_turn_408.py --input-json <返回的learning_request_file>`

封存后更正：正常入口intent=correct-late、display SHA为被更正原题的SHA，只交新增更正/附件。程序验证原题已Capture，追加不可变证据，绑定到日终本地正式处理的原题集合；不改原包/日期/原判分/当前导航。当前新题SHA仍保留，不用旧题SHA继续新题作答。
