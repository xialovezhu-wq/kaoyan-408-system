# Codex 可视化讲解

普通聊天、错题、答后纠偏和机制说明共用。先确定用户当前卡点，再判断图能否比文字更直接地解释该关系、差异或状态。仅与主题相关、存在可画的对象或可用的控件，不构成出图理由；文字、原生公式或简短反例足够时直接讲，不为使用入口或展示新能力硬画图。确有收益时，用一张最小图对齐关键对象，直接标单位、边界、方向和当前状态。静态结构够用就用聊天内 Mermaid 或简单图；需要看见状态变化时再加控件。用户指出图无助时先回到实际卡点，不自动叠加更多图。

## 三个来源怎么用

- [Modern Web Guidance](https://github.com/GoogleChrome/modern-web-guidance) 提供布局和可访问性做法。按图自身的可用宽度重排，基本布局不依赖新 CSS，宽栏增强用容器查询。窄栏保留顺序、标签和关键状态，不把图整体缩小。用文字或形状补充颜色，减少动态效果时所有状态仍可读。开发模板按需补读 [布局](https://developer.chrome.com/docs/modern-web-guidance/guides/css/css-layout) 和 [动效](https://developer.chrome.com/docs/modern-web-guidance/guides/ui-behaviors/motion)。
- 共用模板用 Vercel UI SDK 的拖动、步长吸附、单选和焦点恢复。拖时间轴与点步号会切换同一个当前步骤；点击状态会显示相邻两步的变化及已核详情。键盘切步导致对象消失时，焦点回到可用的步进按钮。Enter、Space 走原生按钮，数学切片用原生滑条，英语选项沿 Visualize 标签行为。[Vercel UI SDK](https://github.com/vercel-labs/ui)
- 共用渲染器在 Visualize 里用官方 Lit 挂载步进、机制图和选中对象详情。组件只接收已核实、获准公开的数据。GBN／SR 和页表已由用户在桌面确认可操作，匹配的 flow 和 sequence 现在默认使用 A2UI。原生备用路径和 A2UI 共用图形与交互代码。 [A2UI 1.0](https://a2ui.org/specification/v1.0-a2ui/)

## 日常对话

能用 Mermaid 或简短表格表达的静态内容直接在聊天交付。需要同步切步的流程、两方案状态对比、可调的区间切片时，一次调用本机模板。读 Visualize 的输出合同，输出到当前会话明确给出的可视化目录，用回执的 visualize_reference 在同轮展示。命令中的「当前可视化目录」使用实际绝对路径，不复制其他会话目录。

存在可核实的连续步骤，且切步有助于看见对象变化时，直接使用 flow 或 sequence，同轮给出可拖动、可点步号的图。对照时让两侧共用同一组步骤，同一指标写相同且唯一的字段标签，使它们按行对齐；点状态值查看前一步和当前值。单侧没有公开值时留空缺。报文沿用 GBN／SR 原版格式：时间向下，保留发送方／接收方生命线、报文方向、丢失标记和已走过的历史轨迹；宽栏并排，窄栏重排。图不装入尚未获准的步骤、历史状态或对照项。

~~~sh
python3 render.py \
  --input - --inline-root "<当前可视化目录>" \
  --output "<当前可视化目录>/explanation.html"
~~~

输入是一个 JSON 对象。适用图型：

- flow：title、kind、initial（从 0 开始）、steps（步骤标题数组）、panels（1–2 项）。每个 panel 有 label、frames（和 steps 一一对应）。frame 的 fields 放 {id,label,value,active}，可加 detail（点击显示）和 link（指向下一字段的关系文字）。需要多步时至少有一个实际对象的值、关系或状态发生变化。
- sequence：和 flow 的步数结构相同；每个 panel 另有 lanes（两个角色），frame 另有 event（无消息时为 null；否则 {id,label,from,lost,detail?,arrives?}，from 为 0 或 1，表示发送角色；lost 为 true、false、null 分别表示丢失、确认到达、只公开发出）。已获准显示的接收事件另占一步时，arrives 是晚于发送的 0 起步号，走到该步才画到对侧。双方沿同一组步骤比较。
- slice：title、kind、control、xmin、xmax、ymin、ymax、initial、step、samples。samples 每行 [横坐标, 下界, 上界]，横坐标严格递增并覆盖定义区间。仅用于每条竖切片为单个区间的情形。折线边界列出真实转折点；曲线按题设密集采样并传 approximate: true 标注近似。非连通区间、开闭边界有关键区别或不能可靠绘出的曲线另用当前题的专图。公式和精确推导留在正文。

默认 auto 为 flow 和 sequence 选 A2UI，为 slice 选轻量原生图。A2UI 资源缺失、加入组件会超过片段大小限制，或客户端组件挂载失败时，使用同场景的原生渲染。需要复现时可显式指定 --engine native 或 --engine a2ui；回执 engine_selected 是生成时的选择，实际挂载以客户端证据为准。

数据模型无法忠实表达题意时，在现有 Visualize 路径编写小型专图，仍采用上述三个来源的布局、行为和数据分离原则。无需为使用 A2UI 改写成熟的学科图或把所有内容装进界面。缺少交互宿主时按当前规则降级。

## 错题与答后讲解

- 408 的既有原图、热点和 3D 继续走对应课程入口。只把已核准公开的局部场景转换为这里的 flow 或 sequence。
- 数学讲题根据当前困难选择正文、简图、区域扫描或条件对照。固定任一轴、已有临界位或需点中交点所属边界时，走 [数学入口](math/render_math.py) 的 region。基准与条件变化共用对象 ID 和尺度用 compare。简单旧 slice 输入继续兼容。公式、推导、矩阵和解法对照仍在聊天正文。
- 英语保留现有双语句图、单项选项标签与逐题解锁。选中从句时高亮关系与它修饰的实际成分。按已有选项空值和译文保护编译。普通句图不改走通用流程。
- 保护是写入 HTML 前的过滤。可视字段、ARIA、内嵌数据、待显示的步骤和恢复状态遵守同一公开边界。客户端保存的步号或选中节点仅用于展示，不是作答或掌握证据。

### 408 时序与对照

点已走过的报文回到发送步，上下键在历史报文间移动焦点，左右键和 Home／End 控制步号。图、步号和对象详情使用同一个本地 `/interaction`，选择仍可用时随步更新。窄栏逐个字段并列两侧值，保留方案名称；宽栏对齐表头，生命线独立保留。没有获准的到达状态不写进交互数据。参见 [UI SDK Timeline](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/apps/docs/content/docs/web-components/components/timeline.mdx)、[Modern Web Guidance 容器布局](https://github.com/GoogleChrome/modern-web-guidance-src/blob/562b5335197ad3d6bf157f5eee2722da733aebc0/guides/css/css-layout/guide.md) 和 [A2UI 1.0 数据绑定](https://a2ui.org/concepts/data-binding/)。

### 数学入口采用的上游能力

- [Modern Web Guidance 原生滑条与读数](https://github.com/GoogleChrome/modern-web-guidance-src/blob/562b5335197ad3d6bf157f5eee2722da733aebc0/guides/forms/slider/guide.md) 及[示例](https://github.com/GoogleChrome/modern-web-guidance-src/blob/562b5335197ad3d6bf157f5eee2722da733aebc0/guides/forms/slider/demo.html)：原生 range、标签、独立读数和 aria-valuetext；拖动不连续播报，确认时告知读数变化。[容器布局](https://github.com/GoogleChrome/modern-web-guidance-src/blob/562b5335197ad3d6bf157f5eee2722da733aebc0/guides/css/css-layout/guide.md)让基准和对照在窄栏上下堆叠。
- [UI SDK 数值定位实现](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/foundation/number/steps.ts) 与[提交示例](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/examples/components/slider/web/commit.ts)：用已装 beta.1 的 `scrubTo` 关键点和 `stepBy` / `stepFromKey`，允许某一点不在键盘步长网格上，按键可以离开吸附点。预览位置与提交恢复分开；依旧用原生输入而非另引入自定义整套滑条。线代条件使用原生互斥单选，图与文字共享所选对象。
- [A2UI 1.0 数据绑定](https://a2ui.org/concepts/data-binding/) 与 [Lit / DataModel 源码](https://github.com/a2ui-project/a2ui/blob/5f6d26c48a5c61bf845cf1ef2a02a47e31e8d9ba/typescript/core/web_core/src/state/data-model.ts)：目录注册布局、图和对象详情；图、输入和文字共用本地 `/interaction` 的 get/set/subscribe，静态获准数据在 `/scene`；不发 agent action。使用现有 0.15.0 安装包，原生备用路径共用同一图形逻辑。数学受保护入口用独立的 given schema，只生成无刻度的公开变量轴；派生几何和未许可状态不会进组件数据。

UI SDK 运行版本锁定为 0.1.0-beta.1。数学样式和 JS 按项目脚本构建；共享渲染器只接受本地 bundle 与 CSS，输入 JSON 不能指定代码或文件。片段的图形与控件在本地执行。

## 维护和实机验证

构建命令见 [README](README.md)，依赖版本见 [package-lock.json](package-lock.json)。构建后由 Python 使用片段模板和本地 bundle。

开发验证用公开样例，检查 736 / 320 像素、键盘、减少动态效果、状态恢复和保护字段缺失。分别记录离线预览、生成器选择和桌面挂载证据。新增图型先用公开示例核对实际交互再扩展默认范围。客户端显示故障时按当前图型切到原生路径。
