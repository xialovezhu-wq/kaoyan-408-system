# 消息标记与跨客户端保存

## Codex / Claude Code 共用默认入口

模型不重新转录对话。收到明确快速入库请求后，一次执行：

```text
python3 scripts/runtime_paths_408.py --run mark_study_episode_408.py record --question-id <当前精确题号> --start-anchor '<本题首条用户消息的唯一题图文件名、原文片段或消息ID>' --end-text '<本轮完整快速入库请求>' --inline-root <任务可视化目录>
```

若保存请求中仍有需要纠正的说法，先发真实教学反馈，命令追加 `--feedback-anchor <该条已发反馈的唯一短片段>`，由程序把同轮反馈一并提取；不重抄全文。该参数参与原请求，重试须保留。

命令自动识别当前原生会话。Codex 读取 CODEX_THREAD_ID 与只读索引；Claude 读取 SessionStart 绑定或 CLAUDE_CODE_SESSION_ID，使用原生 transcript_path 或精确会话文件名，绝不按修改时间挑选最近聊天。Claude 的 sessionId / uuid 与 Codex 的 thread / message ID 各保留原值，客户端标签参与来源身份。显式定位 Claude 用 `--client claude --session-id <真实ID> --transcript-path <原生路径>`；旧 `--thread-id <ID>` 仍专指 Codex。起点须是本题最早用户消息；多题同一消息或锚点不唯一时不能猜范围，先明确题目范围。

record 只接受当前最新用户消息作为保存请求，允许忽略传输末尾换行进行匹配，但保存保留原始字节。提取起点至保存请求之间全部可见 user/assistant 消息；不包含隐藏推理、工具调用/返回、系统指令、Skill 注入或压缩摘要。Claude 沿当前消息父链读取，压缩时保留可追溯的原始消息，不把另一个分支混进本题。原图从本地记录的内嵌数据提取，不依赖临时截图仍存在，不推断题图/解析角色，保留消息位置供复核。

程序生成 evidence_only 的原生输入并直接调用既有 writer，保存原始消息快照、ID 范围、哈希及图片。不增加正式评分、FSRS 或摘要写入。已核题号的普通快存接着自动生成考点树；回执两项 verified 为 true 只表示证据已保存，完整交付还需 `quick_review.status=rendered` 和有效内联引用。只有用户明确仅记录时才跳过总结。

身份由原会话与结束消息确定；同一保存请求换起点会冲突，不得通过换 ID 绕过。保存状态不明时保留原身份；已有包用下述 verify，不把“存了吗”变成新 Capture。无新消息且未成功的重试可用原命令。

```text
python3 scripts/runtime_paths_408.py --run mark_study_episode_408.py verify --receipt <原receipt_ref> --inline-root <任务可视化目录>
```

只读诊断可将 record 换为 inspect；inspect 可核对历史保存请求，但不写入或创建新 Capture。日常成功路径不先 inspect 再 record。

## Claude 会话绑定与共用导出

项目 `.claude/settings.json` 已注册 SessionStart：`scripts/study_transcript_408.py bind-claude` 只把原生会话身份和路径写入 Claude 提供的会话环境文件；不读取正文、不创建 Capture。新开、恢复、清空或压缩会话时重新绑定。已有会话若已提供 CLAUDE_CODE_SESSION_ID，可直接按精确身份读取；有两端身份冲突或绑定过期时明确报告，不猜测。

Codex 原有 v1 消息标记保持原字节与身份；Claude 新包及晨间消息证据采用 v2 快照，保存客户端、会话、消息范围、原文哈希及原附件。晨间 D 的 session_id 仍是学习队列身份，来源聊天身份单独保存在消息标记中。

本地正式入库从本机原件和冻结证据继续。仅明确外发时，Claude Code 在同一 macOS 用户和文件权限下运行 `export_capture_day_408.py --web-handoff`，需要拆分再调用 `split_web_input_408.py`。无需当前 Codex 会话环境变量，也无需重新转录。私有包自含可读 conversation.json、来源、原始消息快照和图片。原日志不在时可凭快照核验，报告原日志不可用；原日志存在但范围或内容变化时阻塞并报告。

导出时自动复核消息快照与可读对话/图片、原日志中的精确范围；每个 Capture 附 message_boundary_check.json，随分片保留。模型还要读各题原文，核对起止、附件归属及跨题混入；技术检查不是语义审查。确有错标保留原包并报告，按显式补充合同补证据，不修改旧原件。现有旧包没有消息标记时仍正常读取，不伪称已验证原始会话。

## 无原生会话来源时的兼容输入

下列 caller-supplied 输入只用于既有请求恢复，或用户明确提供完整外部原文的兼容保存。Codex/Claude 当前会话读取失败不得自动降级到此入口；保留原请求并报告具体缺口，不能把摘要充作完整原文。主入口：

```text
python3 scripts/runtime_paths_408.py --run standalone_question_intake_408.py record --input-json 私有输入.json
```

原生输入形状如下。仅替换真实值，不新增自动定位字段：

```json
{
  "schema": "cs408-study-episode-capture-v1",
  "episode_id": "本次学习的稳定技术ID",
  "execution_kind": "standalone_question",
  "study_date": "真实YYYY-MM-DD",
  "source": {"source_id": null, "question": "已有的完整题面文字", "options": {}},
  "turns": [
    {"role": "user", "kind": "utterance", "text": "本题真实用户原话", "observed_at": null},
    {"role": "assistant", "kind": "utterance", "text": "本题真实助手讲解", "observed_at": null},
    {"role": "user", "kind": "utterance", "text": "本次完整真实保存请求", "observed_at": null}
  ],
  "attachments": [],
  "observed_outcome": {
    "first_response": null,
    "first_result": "not_observed",
    "final_response": null,
    "terminal_state": "unresolved",
    "prompt_dependency": "not_observed",
    "reasoning_result": "not_observed"
  },
  "authorization": {
    "schema": "cs408-explicit-fast-intake-authorization-v1",
    "kind": "explicit_fast_intake",
    "user_message": "与本次保存请求逐字一致"
  },
  "display_binding": {"kind": "standalone_source", "external_source_id": null},
  "missing_fields": [],
  "frozen_feedback": ""
}
```

turns必须包含这道题已发生的全部相关用户和助手消息，模板三行不是截断许可。每条真实消息对应一个turn；复述与入库命令同处一条消息时保持完整，不能拆分伪造轮次。保留空白、换行和必要附件。传入原文属于caller_supplied，不冒称独立逐字读取了线程日志。

observed_outcome只填写已在教学中确认的真实结果，不为了保存重新评判。未知首答用null/not_observed；提示后完成用prompted_correct及实际提示依赖，不能写独立正确。这个对象是私有事实快照，不是正式评分。完全未解也可以保存unresolved，不为封包补解题目。

图片题已经有原图时，source.question可保留已有文字或图片来源说明，不再OCR或重录选项。attachments每项用真实绝对path、role、mime_type和label：

```json
{"path":"/已知原件路径/题图.png","role":"question_image","mime_type":"image/png","label":"用户原题截图"}
```

题图、解析、作答图都按已有身份保存，不猜题号、正式ID或附件角色。source_id未知用null，由原writer生成明确标注的本地技术键。未知时间用null；缺件写missing_fields，不补造。

将输入放在现有私有目录，执行record一次；可在同次工具调用中完成输入文件写入和提交。成功返回capture_verified=true、package_verified=true及capture_id后立即结束。当前新包沿用 awaiting_web_enhancement，表示已封存待本地正式编纂，formal_write_count、fsrs_write_count与personal_summary_write_count均为0。

输入一旦用于提交，就保留原episode_id、原文件与原字节。用户随后问“存了吗”不使这个已经授权的请求失效，不重构输入、不换ID；重试原record命令。确实有后到的学习补充使用原有不可变补充合同，不覆盖已封包。

旧cs408-standalone-question-intake-v1或已封存旧包按其原请求恢复；不要因切回入口重放现有Capture。后到补充参考standalone-question-intake.md。完整底层字段见仓库schema/study-episode-capture-v1.md。

## 原生单次完整入口（2026-11-01）

`record`支持`--question-id EXAM408-YYYY-QNN[-Pxx]`、`--bind-annual`、重复`--attachment`及`--compact`。前三者将已核验原题图、消息内图解、精确会话与题号范围内的显示回执及额外原件交给同一writer；原生消息正文/ID/字节不改写。年度绑定在Capture验证后完成，输出独立`annual_binding_verified`。没有独立终答时仍保留evidence_only，不以绑定动作评分。

新增显示回执是版本和文件来源证明，不是曝光/作答/掌握事件；缓存未显示就不自动纳入。旧图解没有显示回执时使用明确的`--attachment`，不回填假历史。`--compact`只缩短工具返回，完整证据和校验不省略。

## 侧栏 Page 代码与首稿

用户在侧栏作答时，检查代码和快速保存都用 Page 工具完整 `read_page`；不使用预览、搜索片段或仅一个代码块代替完整回读。保存沿同一 `mark_study_episode_408.py record` 加 `--answer-page-id <Page ID>`。不另写快照文件：`sidebar_answer_capture_408.py` 从本题原生工具回执提取各版原文、版块 ID/哈希和工具调用身份，在首次 Capture 中附作证据，不伪造 user turn。回执包含 `sidebar_answer_evidence.verified`、实际 Page ID、版本数与附件 SHA。

取证范围从本题 `start-anchor` 到保存请求；显式指定 Page 时也接收保存请求后同轮的新回读，但遇下一条用户消息停止。同页同版去重，首稿和修改版各自保留。未读取指定页、片段回读、页身份不符或版块哈希不符，先补当前 Page 回读，尚未写入就不要调用补充入口。成功后直接用返回的快存与考点树回执继续学习，不增加全库同步和重复验证。

绑定失败保留已封Capture，使用`verify --receipt <原receipt_ref> --bind-annual --compact`恢复；不能生成第二份Capture。所有保存命令身份仍由原生结束消息决定。既有无新增参数的CLI与历史包保持原合同。
