# 408 日终原始Capture总包

`export_capture_day_408.py --date YYYY-MM-DD` 仅读取指定上海日期、固定截止时刻的已封存Capture与原始包，输出外部details下的 `408_learning_<日期>_<集合版本>.zip`。没有正式作答事件或新快照也可导出；不判分、不正式入库、不刷新个人语义。

根manifest使用 `schema=cs408-raw-capture-day-v1`。`captures` 包含每个Capture的真实capture_id、capture_payload_sha256、episode_id、execution_kind、evidence_locator、evidence_sha256，按capture_id排序；`capture_set_sha256`由这份排序ID数组的紧凑JSON（无结尾换行）计算，网页加强包直接沿用，不从题目数量猜集合。

每个Capture的原始payload、完整不可变conversation包、全部已保存附件及补充版本原样进入 `captures/`。有补充链时，原生supplement_history最后一项是正式增强的主引用，父包也保留；不靠文件名、mtime或猜测轮数选择最新。

baseline_snapshot_id只作背景，可以为null。GPT-6 Pro增强时另行library固定最新实际正式快照；总包不是已正式确认的历史。正确、提示后完成、未解决、原题和独立补充均保留原始类别。

历史Capture缺episode/kind时，使用既有Capture ID作兼容键并在capture_index.legacy_identity_notes明示，不伪造过去的学习event_id。缺少可验证原始包时返回BLOCKED和精确缺项，不能导出一个删掉缺件题却声称全量的成品。已经归档的包只按精确locator走原生T9验证，不扫描归档盘。

导出输入是“已经快速保存的内容”；尚未封存的当前题必须在结束学习时由其原事务明确完成或以unresolved封存，暂停中的题不伪称已完成。重复相同集合复用已有原始总包；集合或补充证据变化产生新版本，不覆盖旧ZIP。
