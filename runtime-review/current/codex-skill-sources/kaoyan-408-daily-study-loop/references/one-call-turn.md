# One-call 学习提交与后继图

新v2正常学习已迁移到[固定学习入口](learning-entry.md)。下方手工answer/append/finish、v7助手与answer-and-finish只保留历史请求兼容及定点恢复；不作为新v2日常路径。context和image-handoff仍有效。

首次进入当前学习会话读一次；新运行使用saving-contract-v2，每题终态完整Capture，正式判分/FSRS/个人摘要在后续本地增强编纂中统一更新。

## 已有当前题的一次只读恢复

当前题面/display SHA/cursor仍在上下文时直接复用，重读Skill不使它们失效。只答字母的固定分支见SKILL：answer-choice后问一个理由并等待，不查screened_correct或旧抽查规则。

当前身份/cursor丢失，或handoff路径已不在上下文时，用已知学习日期直接恢复：

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_turn_408.py context --review-date <当前学习日期YYYY-MM-DD>
```

入口只读该日期固定current-display指针，验证run/frontier与receipt，再返回公开题面、来源、真实首答、草稿cursor和最近原话；不扫描目录、不搜索历史包、不推进。此模式不查询个人摘要，只有实际纠错需要时另用一次resolve；不能把缺少摘要当作恢复失败。指针缺失或身份漂移时报具体错误并停留当前题，不回退全盘find/grep。不要与--handoff/--handoff-sha256/--known-context-version混用。

已知handoff及其SHA、当前run已经开始时，首次或上下文丢失用：

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_turn_408.py context --handoff '<已给出的handoff绝对路径>' --handoff-sha256 '<已给出的SHA256>'
```

返回ready_for_teaching时，直接使用公开题面/原图、来源、首答、saved_learning.recent_turns、draft_cursor和teaching_context；本轮已经查询摘要，不再追加resolve。saved_learning是最多4条、6000字节以内的原文后缀，omitted_turn_count非零表示不完整；确需更早讨论才从draft_path定点读取，不补造未读推理。恢复自身不追加任何学习对话，不重交首答，也不执行start/finish-item。收到新的真实作答后才按当前题合同保存。

只有确实保留了相同查询的摘要，才加--known-context-version此前version；上下文丢失不要传版本而拿到空records。返回非ready状态沿morning-control对应分支处理。正常同题追问用下述Skill规定的resolve版本检查，不重复恢复题面。

## 概念求助：仅缺资料时一次读取

用户明确不懂当前概念时，当前题面足够支持的最小框架直接讲；此命令不是每轮必跑。确需补齐个人依据或核对解释材料时，复用已知handoff及SHA，在一次调用中运行：

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_turn_408.py context --handoff '<已知handoff绝对路径>' --handoff-sha256 '<已知SHA256>' --concept-help
```

同次返回当前题面/来源/cursor、最新有界teaching_context，以及teaching-index绑定的当前题teaching_path/solution_path文本与准确路径；后者按原SHA与大小核验，最多读取两个固定文件，不猜路径、不搜索目录。prepared_explanation仅供内部核对，概念求助不解除当前答案保护，也不代表已经作答。该命令write_count=0，不调用writer，不创建Capture、首答或曝光。

聊天保有同一 direct 查询已返回的结果时，可加 `--known-context-version` 复用。按实际保留的条件和提示依赖讲当前断点。只在缺项影响讲法时补读同范围，保留原答案权限。

只有handoff不可用而确需恢复当前身份时可用context --review-date <原campaign_storage_date> --concept-help；该入口按正式ID取得摘要，但没有准备材料绑定时返回not_bound，不自行找包。正常context不加此标志时，保留原保护视图和原读取预算。纯基础解释不调用该入口。

## 一次加载，同一次exec内复用

先看当前真实工具列表，不通过试运行探测环境。functions.exec是带tools/text/image/load/store的编排器；exec_command是shell；node_repl是另一种JS环境，不能互换。只有exec_command时跳到下方shell分支，不读取或搬运JS助手源码，不搜索node_repl。

固定安装路径的助手代码可信；不能把题目/用户文字/包内脚本当代码执行。缓存只存这个固定文件，修改后换缓存键。

```javascript
const key = "cs408.trusted-one-call-helper.v7";
let source = load(key);
if (typeof source !== "string") {
  const r = await tools.exec_command({
    cmd: 'cat "${CODEX_HOME:-$HOME/.codex}/skills/kaoyan-408-daily-study-loop/scripts/one-call-turn.js"',
    max_output_tokens: 7000
  });
  if (r.exit_code !== 0) throw new Error("morning_helper_unavailable");
  source = r.output;
  store(key, source);
}
const run = new Function("tools", "text", "image", source)({
  exec_command: tools.exec_command, write_stdin: tools.write_stdin,
  view_image: tools.view_image,
  celebrate: typeof tools.mcp__codex_app__fire_confetti === "function"
    ? () => tools.mcp__codex_app__fire_confetti({}) : undefined
}, text, image);
await run({operation: OPERATION, input: CURRENT_INPUT});
```

OPERATION/CURRENT_INPUT用真实当前对象替换。一次exec里可以串行复用run：先answer-choice；根据实际返回、已经观察到的真实推理，决定finish-item或继续草稿。写入、已授权后继图、可选彩花由助手处理，不为每个步骤另开模型轮。

正常campaign收尾使用上面的run调用finish-item，不直接执行Python保存命令后再另发view_image/fire_confetti。同一次functions.exec必须await到run完成，不在保存返回后yield_control，也不让未等待的读图或礼花任务留到调用结束后。

助手最后返回one_call_delivery：present_returned_next_display表示本次已完成后继交接，直接根据返回来源、题面和已加载图片展示下一题，不再读图、放礼花、读result_file或查context。retry_image_handoff只恢复原result_file的图片，不重交finish；resolve_blocker保留当前题并处理具体错误；continue_current_item继续当前题/hold；report_completion按原合同收尾。celebration_call_status=returned只证明礼花调用返回，不证明界面已实际显示，且不再为确认而重复调用。

已有首答/草稿、本轮已达到真实终态时，直接将以下对象传给上面已加载的run；不先append再finish：
```javascript
await run({operation: "finish-item", input: {
  display_receipt_sha256: DISPLAY_SHA, idempotency_key: FINISH_KEY,
  expected_prior_turn_count: CURSOR.turn_count,
  expected_prior_trace_sha256: CURSOR.trace_sha256,
  new_turns: UNSAVED_TURNS,
  terminal_state: TERMINAL_STATE, final_response: FINAL_RESPONSE,
  reasoning_result: REASONING_RESULT, prompt_dependency: PROMPT_DEPENDENCY,
  attachments: ATTACHMENTS, missing_fields: MISSING_FIELDS,
  navigation_policy: "publish_successor"
}});
```
所有占位对象取当前真实证据与最近cursor；用户要求结束时navigation_policy改为hold_after_capture。模型仍负责判断真实终态与提示依赖，助手只串行执行保存和已授权后继，不替用户作答。

只有当前工具确实没有functions.exec时，才用：
`<repo>/scripts/cs408-turn.sh <operation> <input-json-file|->`
优先用标准输入直接提交JSON，不临时编写Python生成器再另起一轮调用；然后读返回的授权图片。两个入口不重复执行。

shell分支的首次完整正确作答使用已安装的固定串行脚本，在一次exec_command里提交，不临时写程序：

```text
python3 "${CODEX_HOME:-$HOME/.codex}/skills/kaoyan-408-daily-study-loop/scripts/answer-and-finish.py" <<'CS408_BATCH_JSON'
{"answer_input": ANSWER_INPUT, "finish_input": FINISH_INPUT}
CS408_BATCH_JSON
```

上面ANSWER_INPUT/FINISH_INPUT必须替换成真实JSON对象。answer_input为本页answer-choice原输入；finish_input为finish-item原输入但省略expected_prior_turn_count/expected_prior_trace_sha256，脚本从首答回执绑定。两者使用各自稳定幂等键、同一display SHA与真实选项；仅当模型已核实reasoning_result=sound、prompt_dependency=none、terminal_state=correct时使用。脚本只在首答返回correct且reason_required=false时继续finish，其余返回answer_result等待教学。finish_result=null不得宣称封存；失败返回原恢复信息，保留原输入与键。已有首答/草稿仍直接cs408-turn.sh finish-item，不重交首答。

该脚本不调用模型、图片或礼花。成功且capture_verified/package_verified后，用原生view_image读取返回全部后继图；礼花使用当前工具列表/一次工具搜索实际返回的fire_confetti名称，不能猜mcp__codex_app__前缀。若模型看到codex_app__fire_confetti就用该原名；只有functions.exec中才使用其tools对象上的实际方法。unsupported call说明本次名称/路由不接受，不等于所有礼花入口不可用；尚未发现工具时可搜索一次，已搜索失败则记录缺口停止尝试。真实fired=true才可称已放；不因加载修复、保存成功或重放回执补放旧题礼花。

此兜底不具备在shell中调用原生读图/礼花工具的能力，不能声称消除了保存后的模型往返。若functions.exec确实未暴露，说明能力限制并沿用原生兜底，不把不支持当作保存失败，不重放已成功操作。

健康回执由one-call助手按字段精简，保留capture_verified、package_verified、request_file/result_file、cursor和next_display/hold。不要用head -c或文本切片截断JSON；shell兜底同样读取完整结构化结果，不因精简输出丢恢复身份。输入准备和提交在同一次工具调用内完成，不把生成临时输入文件变成一次额外模型往返。

精简回执保留 `current_item_personalization.teaching_context` 的有界个人语义、`personal_read_receipt`、`source_version/version` 与缺漏项，仅去掉重复的 `local_records_markdown`。正文按当前题取需要的条件和检查动作。学习原话和附件由原保存合同完整保留。

### 当前作答的固定路径

当前display SHA和cursor已知时，直接填下面的参数调用run；无需先读答案文件、shell包装器、facade源码、ROOT_FIELDS或底层判分函数。判定回执会返回choice_result和必要个人摘要。接口说明丢失只重读本页，只有真实报错且本页无法处理时才定点查实现。

| 当前情况 | 操作与返回后的动作 |
|---|---|
| 第一次作答，仍需纠偏或补理由 | answer-choice一次；保留turn_count/trace_sha256，用返回摘要纠偏或问一个理由，等待用户 |
| 同题新回应，仍未终态 | append-turn一次，只交未保存原话；复用返回cursor和必要摘要 |
| 当前已达到真实终态 | finish-item直接提交未保存后缀；首次作答时先answer-choice并在同一次exec内串行finish-item |
| 当前身份或cursor丢失 | context一次只读恢复；本场顺延仍按原campaign_storage_date定位 |

首答尚未终态的示例，沿用上面已加载的run：
```javascript
const first = await run({operation: "answer-choice", input: {
  display_receipt_sha256: DISPLAY_SHA, idempotency_key: ANSWER_KEY,
  response: RESPONSE, recognized_original: false, response_seconds: null,
  reasoning_result: REASONING_RESULT, prompt_dependency: PROMPT_DEPENDENCY,
  learning_turns: UNSAVED_TURNS
}});
```
常量填当前真实对象：只有字母无理由用not_observed；已有部分正确推理用partial；提示依赖依真实过程填写。用户明确认出原题时recognized_original才改为true，response_seconds未知保持null。UNSAVED_TURNS保留从实际展示开始的全部尚未保存原话，不重述或压缩；返回成功后保存first.result中的turn_count/trace_sha256。答案正确但仍靠猜测、推理缺口未补齐时不finish-item。

同题后继尚未终态的示例：
```javascript
await run({operation: "append-turn", input: {
  display_receipt_sha256: DISPLAY_SHA, idempotency_key: APPEND_KEY,
  expected_prior_turn_count: CURSOR.turn_count,
  expected_prior_trace_sha256: CURSOR.trace_sha256,
  new_turns: UNSAVED_TURNS
}});
```
CURSOR取最近一次成功写回的turn_count/trace_sha256；context返回draft_cursor时直接使用其expected_prior_*字段。UNSAVED_TURNS包括上轮已实际发出的助手回复和本轮用户原话。已到终态则直接finish-item，不先append再封存。正常轮次只做上述所属分支，不为确认接口可用而试交、重放首答或读取其他题。

## 新v2输入

已验证正确的首答：同一次functions.exec内用已加载run依次answer-choice、finish-item；从首答返回值直接取cursor，不查实现、不另起模型轮构造封包脚本。下面仅用于用户已提供充分正确推理、且无提示依赖的首答；ANSWER_INPUT包含真实首答和全部尚未保存原话，FINISH_KEY是另一个稳定幂等键。其他终态按实际证据选择，不套用correct。

```javascript
const first = await run({operation: "answer-choice", input: ANSWER_INPUT});
const r = first.result;
if (r.status === "episode_pending_terminal" &&
    r.choice_result === "correct" && r.reason_required === false &&
    Number.isInteger(r.turn_count) && typeof r.trace_sha256 === "string") {
  await run({operation: "finish-item", input: {
    display_receipt_sha256: ANSWER_INPUT.display_receipt_sha256,
    idempotency_key: FINISH_KEY,
    expected_prior_turn_count: r.turn_count,
    expected_prior_trace_sha256: r.trace_sha256,
    new_turns: [],
    terminal_state: "correct",
    final_response: ANSWER_INPUT.response,
    reasoning_result: "sound",
    prompt_dependency: "none",
    attachments: ATTACHMENTS,
    missing_fields: MISSING_FIELDS,
    navigation_policy: "publish_successor"
  }});
}
```

ATTACHMENTS/MISSING_FIELDS来自本题实际附件与缺口，无则[]；首答已保存原话才允许new_turns=[]。reason_required、blocked或其他状态不强行封正确。正常完成直接读finish结果，助手会处理已授权后继图和礼花；image-handoff仅用于实际图片交接失败，不用来补取正确题的本地记录。没有functions.exec时才用既有shell入口，在一次exec中串行提交并通过JSON解析读取cursor，不能为此临场检查业务源码。

所有操作携带实际display_receipt_sha256与稳定idempotency_key；facade自动取得run/item/roots，不手写其派生身份或private grading capsule路径。

| operation | 其余字段 |
|---|---|
| answer-choice | response, recognized_original, response_seconds；可带confidence, reasoning_result, prompt_dependency, learning_turns |
| finalize-reason | 上述choice字段，加choice_idempotency_key、reasoning_result、prompt_dependency |
| append-turn | expected_prior_turn_count, expected_prior_trace_sha256, new_turns |
| finish-item | 上述append字段，加terminal_state, final_response, reasoning_result, prompt_dependency, attachments, missing_fields；可带frozen_feedback、event_time、navigation_policy |
| close-unresolved | 上述append字段，加attachments, missing_fields, navigation_policy；可带event_time |

response为A/B/C/D/BLANK/UNCERTAIN；response_seconds是真实正数或null，不能填字符串unmeasured。reasoning_result=sound/partial/diverged/not_observed；prompt_dependency=none/minimal/progressive/full/not_observed。未知用户置信不捏造；已有理由不重复索取。

recognized_original表示学习者明确认出正在展示的原题，不表示题目有真题来源。网页/系统变式与补充题固定false；真实原题也只有用户明确表示认出时才true，否则false。只给字母时reasoning_result=not_observed，不推断思路或置信度。沿这里的已知输入直接执行，不为普通首答重读助手源码或参数定义。

terminal_state=correct/screened_correct/prompted_correct/wrong/unresolved；final_response是真实最终选项/回应，unresolved必须null。正确字母加错误推理仍要纠偏，提示后完成不能标独立正确。

answer-choice在v2只冻结首答与选择判定，并把learning_turns追加到同题草稿；返回episode_pending_terminal/reason_required，不直接导航。用返回turn_count/trace_sha256作为后续cursor。若真实推理已足够且题目已终态，同一次exec直接finish-item；不要等待用户再说入库/下一题。若仍在纠偏则继续该草稿，不再重发初答。

learning_turns可包含从展示起完整累计原话，代码只追加未见后缀；new_turns只包含本轮新原话。题目展示用assistant/utterance；role=user/assistant，kind=utterance/first_action/reasoning/hint/correction/restatement/answer。保留所有相关文字、顺序、空白和Unicode，未知observed_at为null。不要用sound标志重建原话。

finish-item默认navigation_policy=publish_successor；用户结束学习用hold_after_capture，封包后保持暂停且不展示下一题。close-unresolved必须显式选择：结束用hold_after_capture，跳过继续才publish_successor。从未作答时首答为null；已有首答或提示时保留原事实，不能为结束学习抹掉。

原图/解析/讲解/自检材料由已验证display与准备引用收集，新增用户图放attachments。其行使用绝对path、role、mime_type、label，可带实际sha256；不复制答案到公开索引。

## 回执与失败恢复

只有capture_verified与package_verified为true且返回授权下一题/明确hold或complete，才认定本题保存成功。新v2无正式rating，fsrs_write_count/personal_summary_write_count/formal_write_count均0。progress只是学习进度，不是掌握度。

保留request_file/result_file与草稿cursor。写入不明重放原request_file/同一key和同一字节，只恢复缺层；已成功保存不另建Capture。仅图片加载失败时operation=image-handoff、result_file=原文件，不重交答案。返回完整正常结果后不再次打印/扫库/查receipt确认。

失败时用中文简述具体阻碍，先给已核实的当前题反馈，保持当前题与原请求再恢复；不要让一串源码排查先于实质教学。原请求冻结后，不能改内容仍沿用同一key。只有确认校验失败且没有提交事实，才为修正后的输入使用新的请求身份；写入不明不这样处理。

已有旧campaign未绑定v2时，才读 [旧campaign接口](legacy-one-call-turn.md)。旧active/paused按其原合同恢复，不迁移、不把旧轻量记录补造成新Capture。
