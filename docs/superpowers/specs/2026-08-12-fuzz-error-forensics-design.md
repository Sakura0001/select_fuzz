# Fuzz 错误取证与错误风暴聚合设计

## 背景

内网 fuzz 日志显示：读计数在运行约 116 秒后停止，写入继续推进，错误数随后以约每秒 500 次
增长；应用将 72 个 reader 标记为 `reader_executing`，但备节点 PROCESSLIST 最终为 44 个
`Sleep`、0 个 `Query`，最近错误只有 `InternalError:errno=-1`。现有日志能够确认客户端快速失败，
却没有保留异常消息、异常链、cursor 清理错误、连接本地状态、MySQL 可见性和 watchdog 动作结果，无法继续区分
`Unread result found`、连接已关闭、协议状态损坏或其他 connector 内部错误。

本功能的目标是：下一次发生同类问题时，仅依靠终端输出和 `events.jsonl`，就能还原错误首次发生时
的 Python、connector、连接和 watchdog 现场，并能从大量重复错误中看出根因分布和演化趋势。

## 范围与约束

- 只增强 fuzz 模式诊断，不改变生成 SQL、主备分流、线程数、超时语义或重连策略。
- 不因诊断异常而中止或延迟负载；诊断始终是有界、尽力而为的旁路。
- 新增结构化事件和字段；已有事件类型及已有字段和值的形状保持兼容。错误事件的逐次写入改为
  有界采样，错误总数应读取 counters 或新增 summary，事件基数变化是本功能明确接受的行为。
- 新错误指纹首次出现时保存完整现场；相同指纹后续聚合，避免错误风暴产生数 GB 日志。
- 用户环境允许记录异常原文和完整 SQL；终端显示仍做长度截断，结构化样本保留完整 SQL。
- 所有内存集合、异常链、traceback、字符串和代表样本都有明确上限。

## 方案

采用“结构化取证 + 稳定错误指纹 + 周期聚合”方案。

### 1. 执行层错误现场

`StreamingQueryExecutor` 在失败路径构造结构化错误现场，并随 `FuzzExecutionResult` 返回。现场包含：

- `failure_stage`：`connection_open`、`connection_id`、`watchdog_arm`、`execute`、`fetch`、
  `cursor_close` 或 `watchdog_cancel`；
- 主异常的类名、完整模块名、`str(error)`、`repr(error)`、有界 `args`、errno 和 SQLSTATE；
- 最多 8 层 `__cause__` / `__context__` 异常链，并阻止循环引用；
- 最多 16 KiB 的 traceback；
- execute、fetch、cursor close 和总执行耗时；
- cursor close 发生的独立异常，不能再被静默吞掉或覆盖主异常；
- 查询前取得的 connection ID；
- timeout、manual stop 和 watchdog 动作诊断；
- 对 timeout、connector 内部异常、客户端错误或 cursor close 异常记录 watchdog 与连接 ID；
  服务层复用后台周期 PROCESSLIST 样本判断对应 connection ID 是否仍被 MySQL 看见，同时记录
  样本年龄、采集错误或尚未完成首次采样的原因。工作线程不执行额外控制查询，也不在工作会话上
  调用可能无限阻塞的 `session.is_alive()`，因此错误风暴不会放大控制连接负载。

普通服务端 SQL 错误仍可复用原路径，不为每个预期语法错误额外执行连接探测。

### 2. Watchdog 动作诊断

`KillHandle` 在锁保护下维护并暴露只读诊断快照：

- deadline 是否触发以及动作类型；
- `KILL QUERY` 是否开始、完成、成功及异常类型和原文；
- fallback abort 是否尝试、是否成功及异常类型和原文；
- 必要时 `KILL CONNECTION` 是否尝试及结果；
- 动作线程是否完成。

快照只增加观察能力，不改变 watchdog 的现有时序、等待和终止行为。

### 3. 错误指纹

错误指纹使用稳定字段计算 SHA-256，并在日志中显示前 12 位。输入包括：

- failure stage；
- 异常模块、类型、errno、SQLSTATE；
- 规范化异常消息；
- cursor close 异常身份；
- timeout、watchdog kill、abort 和连接存活结果。

指纹不包含 SQL、数据库名、worker、connection ID、时间或普通数值，避免同一根因被拆成大量指纹。
规范化只替换消息中的连接 ID、地址、耗时和大整数，不删除能区分根因的文本。

### 4. 有界聚合

新增线程安全的错误聚合器，最多保留 64 个活跃指纹。每个指纹保存：

- 首次和最后出现的 monotonic 时间；
- 累计次数和上个诊断周期后的增量；
- 影响的 worker、数据库和端点数量，各集合最多保留 64 个成员，另记录截断数；
- 首次完整现场、首次完整 SQL和最近 3 个有界代表样本；
- timeout、连接丢失、连接在 MySQL 不可见的累计次数。

超过 64 个指纹时，保留先出现的 64 个指纹，并用 `other` 桶汇总剩余计数。聚合器只在内存中保存
有界状态；周期快照后清零周期增量，不清零累计值。

### 5. 结构化事件

新增两类事件：

#### `fuzz_error_sample`

仅在新指纹首次出现时写入，字段包括 run、generation、worker、endpoint、database、seed、SQL、
错误指纹、完整执行现场、连接现场、watchdog 现场和时间戳。traceback 只出现在该事件中。

#### `fuzz_error_summary`

随每次 `fuzz_stage_snapshot` 周期写入，包含本周期和累计错误数、错误速率、错误指纹总数、被聚合的
其他错误数，以及按本周期次数排序的前 8 个指纹摘要。

现有 `fuzz_operation_error` 保留原字段。每个新指纹首次出现时写一条完整兼容事件；同一指纹之后
最多每 30 秒写一条代表事件，并新增 `suppressed_repeats` 表示其间被聚合的次数。SQL及完整异常现场
只写入首次 `fuzz_error_sample`。这样现有解析器仍能读取该事件，但不能再通过事件行数统计错误总数；
准确总数来自 counters 和 `fuzz_error_summary`。

### 6. 中文终端判断

正常状态行增加有界错误摘要：最高错误指纹、周期次数和错误率。连续无读取时，按以下优先级判断：

1. worker 或 SQL 生成进程缺失；
2. 大量 reader 等待 SQL；
3. 错误率达到每秒 10 次且读取增量为 0：`客户端错误风暴`；
4. reader 长时间在 MySQL 执行或拉取；
5. 重连或连接数量异常；
6. 应用与 MySQL 状态矛盾；
7. 证据不足。

错误风暴警告至少显示：指纹、每秒次数、异常原文、failure stage、连接健康、watchdog/abort 结果、
影响 worker/数据库/端点数和一条截断 SQL。当应用处于快速 `execute`、MySQL 为 `Sleep` 且错误率
升高时，明确输出“查询未发送到 MySQL，客户端快速失败”。

### 7. 安全和性能边界

- 异常消息、repr 和单个参数各限制 4 KiB，异常链最多 8 层，traceback 最多 16 KiB；
- 终端字段和 SQL各限制 300 字符；首次结构化 SQL完整保存；
- 最多 64 个错误指纹、每个 3 个代表样本、状态行最多 3 个错误摘要；
- MySQL 可见性只读取后台诊断线程最近一次有界 PROCESSLIST 样本；精确 connection ID 集合只在
  进程内关联，不写入周期事件；
- 聚合和快照使用短临界区，不在锁内执行数据库调用、格式化 traceback 或写文件；
- 诊断构造、MySQL 可见性探测或输出失败时，记录有界的诊断自身错误，不影响 fuzz 主循环。

## 数据流

1. reader/writer 调用 `StreamingQueryExecutor.execute_session()`。
2. 失败时执行层分别捕获主异常、清理异常、watchdog 快照和 connector 本地状态。
3. 执行层返回包含结构化现场的 `FuzzExecutionResult`。
4. `FuzzModeService` 用最近一次 PROCESSLIST 样本补充 MySQL 可见性，计算指纹并提交错误聚合器。
5. 新指纹写 `fuzz_error_sample`；同一指纹最多每 30 秒写一条兼容的轻量
   `fuzz_operation_error`，其余只计数。
6. 诊断周期读取聚合快照，写 `fuzz_error_summary`，并让 reporter 结合读增量和 PROCESSLIST 判因。

## 测试策略

- 执行层测试覆盖 execute、fetch、cursor close 和 watchdog cancel 异常原文不丢失；
- 服务测试覆盖 PROCESSLIST 可见、不可见、未采样、陈旧和采集失败的连接证据；
- watchdog 测试覆盖 KILL QUERY、fallback abort、KILL CONNECTION 成功和失败快照；
- 指纹测试覆盖动态 ID/耗时归一化、不同根因区分和稳定性；
- 聚合测试覆盖首次样本、重复计数、周期增量、容量上限、other 桶和并发安全；
- 服务测试覆盖新增事件、旧字段兼容、重复错误不重复写完整 traceback/SQL；
- reporter 测试复现内网时间线：读取停止、错误高速增长、reader 快速 execute、备节点 Sleep，
  断言输出“客户端错误风暴”和“查询未发送到 MySQL”；
- 运行 fuzz 专项、全量 pytest、Ruff、Mypy、Python 构建和 CentOS 7 GitHub Action。

## 非目标

- 本次不自动关闭或重建 timeout/`InternalError=-1` 连接；
- 本次不修改 SQL 生成合法性或兼容性策略；
- 本次不改变 15 秒无读取阈值、5 秒诊断间隔和 30 秒重复警告间隔；
- 本次不对比或修复具体数据库内核行为。
