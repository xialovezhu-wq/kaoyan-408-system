# 第 7 节共享集成

408 负责整合。当前增量共享变更请求 `NONE`，结论 `KEEP_EXISTING`。

| 组件 | 核对的 GitHub main | 本轮兼容依据 |
| --- | --- | --- |
| `xialovezhu-wq/study-pro-bridge` | `809bf7351abe1282bb1187b82998f48908485d41` | 延续本机 `study_mcp.py`、`study_read_scope.py` 和 `study_english_context.py` 的已接入权限。发布目标仍按原 manifest。 |
| `xialovezhu-wq/local-study-read-mcp` | `5da8f9af60b286fe0e6bb69dcc8f7f40da2832fa` | 本科修复在原生 `resolve` 投影，现有公共读取契约与权限继续适用。 |
| `xialovezhu-wq/study-intake-v2-backend` | `3c14a3d2f1fbfe0b02ca24bf7a796b55819a21dc` | 从精确版本克隆的隔离根运行 `verify_projection_rebuilds.py --subject cs408`。合成夹具返回 `verified_isolated_repair_live_change_required`，正式写入及模型调用均为 0。配套 verifier 四项回归通过。 |

上一轮 `ENG-BRIDGE-ANSWER-SCOPE-01` 已在 Mac 共享层处理。本次读回 `study_mcp.py` 为 `2c3c90f8615ed8b619eaf408be23e52b1c0f6feaa1b7840c87f393e016f1770a`，`study_read_scope.py` 为 `bd92f871e7735d551c61346f28efaa380c22ec60b3a7b08b55186a72ba0ae971`，`study_english_context.py` 为 `b2147c1ae689dfef23b3c74b67eb5035b0a13622a6183da5e48d0e20686135b7`。三个文件仍为已核验的版本。默认练习权限继续适用，兄弟题严格沿已登记的授权范围处理。数学上一轮为 NONE。至本轮审查封版，两个消费者的本机报告目录没有本批新增共享变更请求。

新版投影不增加公共字段、共享入口或读 MCP 能力。这次同步的 `_shared/kaoyan-408` 资源仅归 408。消费者采用、生产绑定切换和真实共享发布均无待应用差异。隔离验证与此前本机消费端回执保留分立。两科专属文件和正式数据在本轮的写入数均为 0。

本轮使用隔离的 Bridge 代码副本运行 `test_study_mcp_english_context.py`，10 项通过。覆盖文章绑定解锁、兄弟题隔离、历史快照、原件文本／图片／ZIP 与 stdio 可用性。数学采用其已有兼容回执，本轮无共享候选要求重新切换消费端。
