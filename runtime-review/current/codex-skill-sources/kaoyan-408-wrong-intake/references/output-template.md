# Visible intake output

Follow workspace chat formatting. Use the template matching the actual terminal
state; never merge fast capture and formal completion wording.

## Fast fact capture

工具后的 final 按回执的 `display_order` 交付。完整条件、证据状态、只记录或局部提示的例外统一见 SKILL.md。以下是主线完成且获准专题复习的顺序。

```text
上一题已快速保存（实际 Capture ID）。

〈一到两句说明这道题怎样带出同专题。〉

〈同轮内联可点击考点树。首屏显示考频，点击只展开本节点的考场记忆和知识点记忆。〉

〈下一题题序、真实来源和准备好的题图〉
```

## 按本轮作答取材和出图

验回后用本题原话确认复盘重点。优先本轮已经验证的思路或第一个真实卡点，再用同版题面和本题备课材料核定对象、字段、值和单位。清楚区分题设值、已核验且已经给本问的前置值、当前获准推导。复盘调整只影响这张图的焦点。

正常快存由原生标记入口默认生成并返回考点树。年度会话加 `--bind-annual`；普通单题省略。默认命令只需题号、原生消息边界和本任务的 `--inline-root`。已有明确焦点时，下列可选 `--recap-json` 始终绑定当前精确题号，年度保护下的知识点回退由程序完成。

```text
python3 scripts/runtime_paths_408.py --run mark_study_episode_408.py record --question-id <当前精确题号> --start-anchor '<首条原话>' --end-text '<完整快存请求>' --recap-json - --recap-output <任务可视化目录>/recap.html --inline-root <任务可视化目录> --compact
```

从标准输入传下述现有 `cs408-recap-case-v1`。`recall` 用已确认的本轮语义编写，主公式留在聊天正文。题干与上一轮已核信息充分时，不为快存重读完整题解。已有原题参数需要精确字段图时用 `inputs`；例如 `partition.scheme=instruction-reg-base-offset` 与 `widths=[8,4,4,16]` 生成操作码、源寄存器、基址寄存器、补码偏移量四段示意，`anchor` 应绑定本题已核原纲。

只有 `quick_review.status=rendered` 且 `inline_path_preflight_passed=true` 才交付返回的引用。普通逐题不再单独读回HTML或启动浏览器；渲染器更改在开发阶段一次验收。返回考频以选中规范点的同版 `frequency_scope=canonical_point` 为准，章节与其他节点保留各自范围，不在出图后人工替换。

已经保存的题或本次仅验证耗时，用 `verify --receipt <原receipt_ref> --inline-root <任务可视化目录>` 代替 `record`，默认补全树；有显式焦点再附同样的总结参数。验证原文、图片、年度绑定和总结，禁止为计时创建第二个Capture。只有用户明确仅记录时用 `--no-recap`。脚本时延和整轮回复时延分别报告。

现有 `render-topic` 命令加 `--case-context -` 时，通过标准输入传一个 `cs408-recap-case-v1` JSON 对象。下例仅指定第二条报文的确认号。实际 `identity` 必须用刚结束的精确题号或 Pxx。

```json
{"schema":"cs408-recap-case-v1","identity":"<本问ID>","point":"CN05-12","anchor":"S27-CN-05-03-02","highlight":"ack","step":2}
```

`ack` 表示确认号字段，`flag-ack` 表示 ACK 控制位。`step` 从 1 起算。按实际断点选已核 `point` 或 `anchor`。题设已绑有数字时复用绑定，选择本次焦点即可。其他已核题设参数可用 `inputs` 传一个对应对象，并传 `source` 为 `question` 或 `prepared`。握手输入用 `tcp` 的 `handshake`，给 `roles`、`isn`、`base` 和逐报文允许的 `show`，每项只列 `seq` 或 `ack`。实际给出的报文序列用 `observed` 与 `events`，每条指定 `from`、`label` 和已获准的数值字段。虚实地址用 `address_widths` 的 `va`、`pa`、`offset`，Cache 或指令字段用 `partition` 的 `scheme` 和 `widths`。未确认的字段沿用已核机制示意及题设条件。

完整题或已核变式可给选中节点传 `recall`，用一句 `summary` 和各一句 `action`、`rule` 对应本轮真实卡点与考场动作。摘要、起手、规则分别最多 30、75、100 字。独立 Pxx 用本问备课及获准的字段或报文焦点细化图内提示，聊天简述本轮卡点。题设没有公开该报文字段时只聚焦步骤，不替它编数值或高亮。

位级布局先合成整个报文，再按实际位宽画各字段。标尺、固定首部、选项、数据和尾部各自有归属。对照本题原图的版式与进制。画 TCP 建连或抓包时，箭头旁标本问实际报文编号及获准 SEQ、ACK。未解、提示层、兄弟小问和后继的答案在出图前排除。JSON 只装已核题设参数和展示焦点，完整对话仍按原保存入口封存。

## Explicit formal request

已封存待增强的 Capture 交 kaoyan-408-enhancement-intake 本地编纂；其他明确截止日编纂沿 kaoyan-408-daily-intake-curation 的原生入口。正式结果以对应写者的回执为准。

当前题尚未封存且用户明确正式入库时，先按主入口用真实正式请求和 `--intent formal` 封证，再继续本地正式提交。仅有采证回执时，可以报告证据已就绪，不能报告正式入库完成。
