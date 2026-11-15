> BrainCo uv implementation (2026-11-13): see [current protocol and clock/data contract](BRAINCO_CLOCK_HANDOFF.md). The baseline below is extended by GUI, fixed schedule, native SQLite and software packet-clock markers. Use Python 3.12 and root uv .venv. Native units are unverified; EDF/BDF/EEGQC readiness is not claimed.

# 当前模拟输出格式（schema_version = 1）

正式交付遵循 [本地多模态采集数据存放规范](../data/README.md)。本文仅描述当前模拟程序输出，正式会话的多模态导出仍待实现。本文的 session.json 是模拟运行元数据，不等同于公司交接规范中的同名文件；schema_version 也不能用于判定两者兼容。

每个会话目录以 UTC 时间和随机 ID 命名，不复用已有目录。

| 文件 | 内容 |
| --- | --- |
| `config.json` | 校验后的有效参数及解析为绝对路径的输出目录 |
| `session.json` | 会话 ID、任务名、backend、Python 版本、UTC 起止时间、运行模式、状态、样本数及完成试次数 |
| `samples.csv` | UTF-8，首行为列名，后续每行一个多通道采样点 |
| `events.jsonl` | UTF-8，每行一个独立 JSON 事件 |
| `run.log` | 事件摘要及异常堆栈 |

`status` 为 `running`、`completed`、`interrupted` 或 `failed`。异常可能带 `error` / `cleanup_error`。`realtime=false` 表示跳过模拟等待的快速测试。

## samples.csv

- `trial`：试次编号，从 1 开始。
- `sample_index`：试次内采样序号，从 0 开始。
- `scheduled_time_s`：相对会话起点的计划采样时间（秒）。
- `elapsed_s`：写入前主机单调时钟测得的相对时间（秒）。
- `utc_time`：带时区的 ISO 8601 UTC 时间。
- `ch_1` … `ch_N`：通道值，默认模拟正弦信号，无物理单位。

计划时间与实际时间允许存在差异；快速模式下计划时间仍保持原来的模拟时间轴。这些字段不是硬件采样时间戳。

## events.jsonl

公共字段为 `event`、`utc_time`、`elapsed_s`。试次事件另含 `trial`，失败事件可含 `error`，结束事件包含 `status`。

正常事件序列：`session_start` → 每个试次的 `trial_start` / `trial_end` → `session_end`。异常会记录 `session_failed`，Ctrl+C 记录 `session_interrupted`；设备打开失败时不会产生 `session_start`。设备关闭失败会在日志和 `cleanup_error` 中记录，并将原先成功的会话改为失败。

事件逐条刷新，采样数据在试次结束及会话退出时刷新。崩溃或断电后的完整性仍需核查。

## 跨电脑续采增补（2026-11-15）

原始 EEG 与 events 格式不变。上传清单新增 `resume/verified-progress-v1.json`；扩展 10 次以上的会话另保存基础/实际排程与 `schedule-provenance.json`。字段含义、完成位置与有效 EEG 的区别见 [跨电脑续采与次数扩展](cross-pc-progress.md)。
