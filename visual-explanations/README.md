# Codex 交互式讲解（Visual Explanations）

同一套渲染器把公开场景数据转换为可嵌入 Codex 的 HTML 片段。它支持报文时序、并排状态对照、队列等流程、数学区域切片和几何条件对照。示例数据可以脱离原学习系统使用。

| 上游项目 | 在本项目中的作用 | 实现 |
| --- | --- | --- |
| [Chrome Modern Web Guidance](https://github.com/GoogleChrome/modern-web-guidance-src) | 按容器宽度重排，优先原生控件和键盘操作 | [样式与模板](fragment.html)、[数学图样式](math/math.css) |
| [Vercel UI SDK](https://github.com/vercel-labs/ui) | 时间轴拖动、步点吸附、对象选择与焦点恢复 | [共用交互](view.js)、[数学图交互](math/view.js) |
| [A2UI](https://github.com/a2ui-project/a2ui) | 通过 A2UI 1.0 与 Lit 挂载组件，把场景数据和当前交互状态分开管理 | [A2UI 适配](a2ui.js)、[场景校验与打包](render.py) |

A2UI 加载失败时，流程、时序及数学图可走同一套图形的原生渲染。简单切片默认使用原生滑条。Chrome 指南用于布局和可访问性设计，运行依赖是锁定的 UI SDK、A2UI 和 Lit。细节见 [设计说明](visual-guide.md)。

## 构建与使用

使用 Python 3.11+ 和 Node.js 20+。从这个目录安装锁定的依赖并生成三个 JS bundle：

```sh
npm ci
node build.mjs
node math/build.mjs
```

构建后的图解无需 npm、服务端或 API 凭据。从已公开的 JSON 生成片段：

```sh
mkdir -p output
out="$(cd output && pwd -P)"
python3 render.py --input examples/gbn-sr.json --inline-root "$out" --output "$out/gbn-sr.html"
python3 math/render_math.py --input examples/region.json --question demo-region --focus slice \
  --view direct --allow-geometry --inline-root "$out" --output "$out/region.html"
```

上面的 `output` 用于本地生成。放进 Codex 聊天时，把输出目录换为当前会话允许的可视化目录，使用渲染回执中的 `visualize_reference`。也可以把片段嵌入自有宿主的沙盒，由宿主提供基础样式及可选的状态恢复接口。

## 公开示例

- [GBN／SR](examples/gbn-sr.json) 同一时间轴上的发送、丢失及接收状态。
- [队列流程](examples/flow.json) 点选对象可查看当前状态的说明。
- [三角区域](examples/slice.json) 和 [梯形区域](examples/region.json) 用滑条检查切片端点。后者可跳到端点所属边界改变的位置。
- [线代条件对照](examples/compare.json) 在相同尺度下比较两条约束，选中对象时同时更新文字。
- [仅题设坐标](examples/given.json) 是受保护模式示例。用数学入口的 `--view protected` 生成。
- [Cache 带参追问](examples/cache-followup.html) 是独立的可嵌入示例。拖动 32 B／64 B 只更新图中的组索引和块内偏移，点击「按当前配置追问」才通过 Codex 的 `sendFollowUpMessage` 提交公开参数，开启新的对话回合。宿主缺少该方法时，图仍可操作并在点击时提示追问不可用。

场景作者应先剔除未获准展示的信息，再把场景交给渲染器，因为片段内的 JSON 本身就对阅读者可见。数学入口默认拒绝派生几何。这个目录的示例只用公开构造数据。图中状态恢复不写入学习记录。

原创源文件沿用仓库根目录的 MIT 许可。第三方依赖保留各自的许可，见 [licenses](licenses/)。
