# Fuzz 实时诊断输出设计

## 背景

内网长时间运行 fuzz 后，MySQL `SHOW PROCESSLIST` 中可能出现大量 `Sleep`，但仅凭
服务端状态无法判断客户端是在等待 SQL 生成、执行查询、拉取结果、重连，还是工作线程已经
退出。项目当前每 5 秒把阶段聚合写入 `events.jsonl`，终端只在运行结束时输出汇总 JSON，
不足以在现场快速定位停滞位置。

## 目标

- fuzz 模式默认每 5 秒向 `stderr` 输出一行中文状态，用户无需读取事件文件即可判断问题。
- 联合展示程序线程阶段、SQL 生成流水线和主备 MySQL 会话状态。
- 连续无读取进展时自动给出中文原因判断，并展示最有用的线程、错误和 SQL 证据。
- 保持最终 `stdout` 汇总 JSON、已有事件类型、已有结构化字段和值完全兼容。
- 所有诊断均为有界、尽力而为；诊断失败不能中断或改变 fuzz 负载。

## 非目标

- 本功能不根据诊断结果自动调小线程、增加生成进程或重启工作线程。
- 本功能不改变读写路由、SQL 生成语义、预取深度、查询超时或换代逻辑。
- 本功能不把 MySQL `Sleep` 本身当作错误；必须结合程序阶段判断。
- 本功能不保存完整结果集，也不无限保留每次操作的历史。

## 方案

### 1. 程序阶段和最长停留时间

扩展现有 `FuzzStageTelemetry`，在工作线程真正切换阶段时记录单调时钟时间。同一线程重复
写入同一阶段不会重置计时。快照继续保留已有 `stages` 和 `durations`，并新增：

- `stage_details`：每个阶段的线程数、最长停留纳秒数和最老的 3 个线程；
- `worker_groups`：`reader_primary`、`reader_replica`、`writer_primary` 各自的阶段分布。

内存上限与工作线程数成正比，不记录逐操作历史。

### 2. SQL 生成流水线状态

为 `InlineQueryPipeline` 和 `ProcessQueryPipeline` 增加统一的只读 `snapshot()`：

- 配置的生成进程数和当前存活数；
- 已注册数据库数；
- 待处理请求数、涉及 reader 数；
- 最老请求等待时间和单 reader 最大待处理数。

提交请求时记录有界的单调时钟时间，结果消费或 reader 取消时删除。每个 reader 仍最多保留
3 个待生成请求，不修改现有预取语义。

### 3. 工作连接和 MySQL PROCESSLIST 联合采样

工作线程在长连接建立后登记 `connection_id`、worker、主备端点和数据库，在连接关闭前注销。
独立的尽力而为采样线程每 5 秒使用控制连接，按已登记的连接 ID 查询
`information_schema.PROCESSLIST`，因此不会把其他压测程序或诊断连接混入统计。

每个端点记录：

- 程序登记连接数、MySQL 可见连接数和缺失连接数；
- `Sleep`、`Query` 等命令数量；
- 最长 Sleep、最长 Query；
- 时间最长的 3 个连接，包括 worker、状态和截断到 300 字符的 SQL。

采样使用现有控制连接上限。权限不足、连接失败或查询超时只记录
`diagnostics_error_type` 和原始错误文本，不影响压测线程。程序侧状态输出不等待本轮数据库
采样，使用最近一次结果并显示采样年龄。

### 4. 运行阶段、计数器和最近错误

有界运行时诊断状态记录：

- 当前 generation 及 `materializing`、`prewarming`、`running`、`stopping`、`failed`、
  `finished` 阶段；
- 当前代耗时和距离下一次刷新时间；
- 当前连接按 `primary_writer`、`primary_reader`、`replica_reader` 的数量；
- 最近 3 个操作错误或重连错误，SQL 最多保留 300 字符；
- reads、writes、errors、timeouts、connection_losses、reconnects 累计值和 5 秒增量。

最近错误使用固定长度队列，不随运行时长增长。

### 5. 中文输出和自动判断

生产入口注入一个只写 `stderr` 且立即 flush 的输出函数。正常状态每 5 秒输出一行，至少包括：

- 运行时间、generation、阶段、数据库就绪数；
- 预期/活动线程和按角色连接数；
- 读写累计、区间增量和每秒吞吐；
- 错误、超时、断连和重连增量；
- 线程阶段、最长停留；
- SQL 生成进程、待处理请求、最老等待；
- 主备 PROCESSLIST 的 Sleep/Query、最长时间和采样状态；
- 一条中文 `判断=` 结论。

连续 15 秒无读取进展时输出额外的 `[fuzz警告]`。同一原因最多每 30 秒重复一次；原因改变时
立即输出。判断优先级为：

1. 正在建库、预热或换代；
2. 工作线程缺失；
3. SQL 生成进程死亡；
4. 大量 reader 等待生成 SQL；
5. reader 长时间处于 MySQL 执行；
6. reader 长时间拉取/解析结果；
7. 连接重试或主备连接数量异常；
8. 程序显示执行而 MySQL 显示 Sleep 的状态矛盾；
9. 正常推进或证据不足。

警告追加最老的 3 个线程、最近 3 个错误以及截断 SQL。判断是启发式结论，输出中明确使用
“初步原因”，不会改变运行结果。

无读取计时在进入运行阶段或 generation 变化时重置。只有 PROCESSLIST 采样新鲜、无采样错误且
主备登记连接均完整可见时，才允许判断“程序显示执行而 MySQL 显示 Sleep”；SQL 生成等待、执行、
拉取、重连和连接数量异常的证据优先。

## 配置和兼容性

新增 `fuzz.diagnostics_interval_seconds`，默认 `5`，范围 `(0, 60]`。旧配置不需要修改即可
默认开启。配置示例和 README 说明终端诊断写入 `stderr`。

`fuzz_stage_snapshot` 继续保留原字段和值，只新增 `counters`、`pipeline`、`runtime`、
`connections`、`processlist`、`stage_details` 和 `worker_groups`。终端中文不进入结构化枚举值。
最终 `stdout` 仍只有运行汇总 JSON。

完整连接登记只保留在内存中供 PROCESSLIST 精确过滤。周期事件的 `connections.registered` 最多
保存 3 条有界代表项，并用 `total`、`groups` 和 `truncated` 保留规模信息，避免高连接长稳测试产生日志膨胀。

## 验证

- 单元测试覆盖阶段年龄不被同阶段刷新、分组统计及有界最老线程。
- 单元测试覆盖两种生成流水线的待处理数、最老等待、取消清理和进程存活数。
- 单元测试覆盖按登记 ID 查询 PROCESSLIST、主备分类、采样错误降级和 SQL 截断。
- 单元测试覆盖中文状态格式、无进展分类、告警节流以及 stdout 不受影响。
- 服务测试覆盖连接登记/注销、运行阶段变化、快照新增字段和诊断线程退出。
- 运行 fuzz 专项测试、全量 pytest、Ruff、Mypy 和 Python 构建。
