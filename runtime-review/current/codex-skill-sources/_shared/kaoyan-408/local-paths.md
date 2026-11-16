# 408 本机路径与解释器

本页只负责定位已有文件，不启动学习、Capture、正式写入或发布。工作目录使用本次环境给出的 408 仓库。不要把示例里的占位符当成路径执行。

## 统一定位

路径尚未知时，首次使用已安装的只读定位助手：

```sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/_shared/kaoyan-408/scripts/resolve_408_paths.py"
```

已在项目目录时，可直接运行 `python3 scripts/runtime_paths_408.py`。两者只报告实际路径、存在性和可用解释器，选一个即可。仓库默认跟随脚本所在的检出目录；详情库、Study-Pro-Bridge、Study-Storage-Map 和电子书默认取仓库同级目录。私有状态跟随 `CODEX_HOME`，未设置时取当前用户的 `.codex`。正常布局无需用户配置。

安装在技能目录中的助手从当前工作目录及自身位置向上识别仓库，再检查当前用户的常规 Documents 位置。确实改了目录布局时，可由当前任务传入已核验绝对位置或设置现有覆盖项：

| 文档占位符 | 默认定位 | 可选环境覆盖 |
| --- | --- | --- |
| `<repo>` | 当前检出目录 | `KAOYAN_408_PROJECT_ROOT`；兼容 `KAOYAN_408_REPO` / `CS408_REPO` |
| `<details>` | 仓库同级 `kaoyan-408-details` | `KAOYAN_408_DETAILS_ROOT` |
| `<codex-home>` | 当前用户 `.codex` | `CODEX_HOME` |
| `<package-root>` | Codex 私有根下 `kaoyan-408-conversation-packages` | `KAOYAN_408_CONVERSATION_PACKAGE_ROOT` |
| `<bridge>` | 仓库同级 `Study-Pro-Bridge` | `STUDY_BRIDGE_ROOT`；兼容 `STUDY_PRO_BRIDGE_ROOT` |
| `<storage-map>` | 仓库同级 `Study-Storage-Map` | `STUDY_STORAGE_MAP_ROOT` |
| `<sources>` | 仓库同级 `408考研电子书资料` | `KAOYAN_408_SOURCE_ROOT` |

位置覆盖必须是绝对路径。找不到依赖时报告该路径与缺项，不创建空库，不按相似文件名猜原件。

## 执行入口

本技能的仓库命令在已定位的 `<repo>` 工作目录运行。用标准库引导入口执行实际脚本：

```sh
python3 scripts/runtime_paths_408.py --run mark_study_episode_408.py record ...
```

引导程序检查实际可导入的 `fsrs`、`PIL`，在当前解释器、PATH 中已有 Python 和同级 Bridge 的已有环境间选择，不绑定 Intel、Apple Silicon、Homebrew 或旧电脑地址。`CS408_PYTHON` 可显式指定现有解释器；显式选择缺依赖时直接报告，不能偷偷切换。需要额外依赖的专用资料工具仍按自身合同核验。

已安装的 one-call / shell 助手自动经过同一定位与解释器检查。读取源码和工具 JSON 的纯标准库步骤可直接用 `python3`。已返回可用解释器时，同一会话可复用其绝对路径；环境变更或真实运行失败后才重查。

既有聊天若缓存了迁移前的 one-call 助手，首次恢复时只重载新版助手，保留原请求、游标和幂等键；已经成功的保存不重放。

T9 的固定挂载身份、UUID、哨兵、先校验后写入与登记合同继续由原存储入口执行。历史回执、冻结请求、来源正文和旧会话中的绝对路径是原始记录，不批量替换；恢复时沿原身份与文件级定位表处理，不重放已成功写入。
