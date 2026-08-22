# Correctness 双端会话与可靠性修复设计

## 背景

correctness 模式当前在 64 worker 高并发下暴露出一组相互放大的问题：普通查询每次重新建立连接；连接或 session 初始化失败会让健康端滞留在 barrier；setup 和 mutation 在部分连接获取失败时会为两端复制相同的内部错误；单端基础设施故障会被误判为 `setup_mismatch`；内部错误只保留异常类名；超大 setup SQL 在 finding manifest 中重复多次并触发 64 MiB 上限；没有稳定全排序的 `LIMIT/OFFSET` 会制造结果误报；connector 派生的 nullable/flags 差异会被当成值错误；fatal failure 后工作线程和数据库连接退出过慢。

本设计采用轮次级双端会话架构，修复这些根因，而不是单纯增加连接超时或接入一个无法保证 session 隔离的全局连接池。

## 目标

1. 每个 correctness worker 在一轮内稳定持有一对 `custom_off/custom_on` 会话，setup、EXPLAIN、SELECT 和 mutation 顺序复用。
2. 双端会话并发获取、分别记录真实结果；任何派生失败必须明确标记为 peer 未就绪，禁止复制另一个节点的异常。
3. SQL 只在两端均完成连接、session 初始化、连接身份读取和 watchdog 准备后才进入同步执行。
4. 对可安全重试的基础设施错误执行有界或可停止退避；对事务提交状态不明确的错误保留现场且不重放。
5. 默认生成足以定位连接、初始化、barrier、执行、watchdog、setup 和 mutation 根因的结构化日志。
6. finding 使用可扩展的 v2 格式保存任意体积 setup SQL，报告体积不能再触发全局 `run_failed`。
7. correctness 只比较具备确定性结果语义的查询；不确定的行限制查询不得进入结果 oracle。
8. 将结果值语义与协议/来源元数据差异分离，减少 PQ correctness 误报并保留诊断信息。
9. fatal failure 主动中止活动连接，使 worker 有界退出，并输出包含异常原文的中文根因。
10. 兼容读取和 replay 现有 v1 finding，不改变 fuzz 模式的读写分离语义。

## 非目标

- 不通过把默认 `connection_timeout` 从 10 秒改成 30 秒掩盖连接问题。
- 不引入跨节点共享的 mysql.connector 全局连接池。
- 不把公网社区 MySQL 与 TaurusDB 的性能差异解释为 PQ 性能结论；本设计只保证 correctness 值比较和基础设施诊断可信。
- 不删除失败轮次创建的数据库。
- 不在本次改动中扩大 MySQL 8.0.22 语法范围。

## 总体架构

### 1. 可拥有的单节点会话

`MySQLConnectorFactory` 增加显式的 owned-session API。一次打开返回可关闭的 lease；现有 context-manager API 继续由 lease 包装，以保持 replay、doctor 和其他调用方兼容。

会话建立过程分为以下阶段并分别计时：

1. `connect`：mysql.connector 建连和握手。
2. `role_session_init`：配置的角色级 `SET SESSION`。
3. `common_session_init`：统一执行 `SET SESSION time_zone = '+00:00'`。
4. `connection_identity`：读取并校验 connection ID。

公共初始化只在新会话建立时执行一次，不再在同一连接的每条 SQL 前重复执行。

所有 opened lease 注册到线程安全的活动会话注册表。正常关闭时注销；fatal failure 时 `abort_all()` 对注册表快照执行 socket abort，解除正在阻塞的 worker。

### 2. 双端会话获取

新增共享的 pair acquisition primitive，同时为 `custom_off` 和 `custom_on` 提交连接任务。返回值按角色保存：

- 成功的 session lease、connection ID 和阶段耗时；或
- 真实的失败阶段、异常证据和耗时。

如果只有一端失败，成功端会被正常关闭，结果标记为 `peer_not_ready`，但不会复制失败端的 errno、异常类型或异常消息。如果两端都失败，则各自保留各自证据。

setup 和 mutation 不再使用顺序 `ExitStack.enter_context()` 获取连接，从而消除“第一端失败后给两端制造同一个 65010/65012”的行为。

### 3. 轮次级双端会话

`ComparisonCoordinator.prepare()` 总是先获取双端 session pair，再通过这对会话 lockstep 执行 setup。setup 成功后，`PreparedRound` 持有 pair lease 直到轮次结束。

一轮内的执行顺序为：

```text
并发建立双端会话
  -> lockstep setup
  -> baseline EXPLAIN（custom_off 会话）
  -> 双端 SELECT
  -> 每 10 条成功查询执行一次双端 mutation 事务
  -> 重复 EXPLAIN/SELECT/mutation
  -> 关闭双端会话
```

这样 64 worker 的常态数据连接上限约为 128 条；control connection 仍由既有全局 semaphore 限制为 8 条，并且只在 watchdog 触发时建立。不会再按每条已接纳查询产生 1 条 EXPLAIN 连接和 2 条执行连接。

### 4. 启动同步门

保留双端同时开始 SQL 的能力，但同步门只负责“已准备执行”的线程，不负责等待连接建立。

每端进入执行门前必须完成：

- session 存活检查；
- connection ID 校验；
- watchdog handle 创建。

任一端在进入门前失败，必须立即 abort 同步门。peer 端返回明确的 `peer_start_aborted` 派生状态，不再等待完整的 SQL timeout。同步门的等待耗时单独记录，不能与 SQL 执行耗时混合。

### 5. 断连恢复规则

#### 普通持久表

SELECT 是只读且可重放。查询前检查或执行期间出现基础设施错误时，关闭整对 session，重新连接现有数据库并重试同一查询。退避沿用 `0.25s -> 0.5s -> ... -> 30s`，并可由 stop event 立即中断。

#### 临时表

临时表属于 session。任一端 session 丢失后不得只重连或单端重建；当前轮标记为基础设施中止，关闭双端 session，由调度器开始一个新轮次。失败数据库保留。

#### Mutation

mutation 使用 `PreparedRound` 持有的同一对 session，因此临时表 mutation 也能看到正确表结构。

- `START TRANSACTION`、事务中 DML 或 `ROLLBACK` 阶段出现基础设施错误：关闭两端连接以触发服务端回滚。普通表最多重连并从 batch 开头重试 3 次；临时表终止当前轮。
- `COMMIT` 发出后出现连接/响应错误：状态可能已提交，判定为 `commit_ambiguous`，禁止重放；保留数据库并终止当前轮。
- 相同的确定性 SQL 错误仍按既有一致错误逻辑 rollback。
- 基础设施错误发布事件但不生成 PQ correctness finding。

### 6. Setup 分类

setup statement verdict 使用以下优先级：

1. 任一角色是 `infra_error`：整条 statement 为 `infrastructure_pause`。
2. 两端 success 且需要比较 affected rows：相同为 ready，不同为 setup mismatch。
3. 两端是相同规范化 SQL error：rejected generation。
4. 其他 success/error 或不同 SQL error 组合：setup mismatch。

`prepare_until_recovered()` 仅对第一类执行退避重试。普通表重试使用新的 retry database，避免复用半完成 schema；临时表关闭旧 pair 后重新建立 pair 并重新应用完整 setup。

## 异常证据与日志

### 结构化异常证据

将 fuzz 已有的安全、限长异常采集能力移动到共享模块，correctness、performance 和 fuzz 共同使用。证据至少包含：

- `stage`
- `exception_type`
- `message` 与安全的 `repr`
- connector `errno`、`sqlstate`、`msg`
- cause/context 异常链
- 限长 traceback frames
- role、database、connection ID（已取得时）
- stage elapsed time

不得记录密码、密码环境变量值或完整 credentials。错误消息使用中文阶段前缀并保留原始异常原文，例如：

```text
查询会话建立失败：ConnectionTimeoutError: <connector 原文>
```

如果 connector 提供 errno 但 sqlstate 或 msg 缺失，仍保留 errno，并用 `HY000` 和 `str(error)` 补齐；不能因为字段不完整而退化成只剩异常类名。

### 默认日志

correctness 默认启用 worker query-attempt JSONL。每次 attempt 至少写入：

- query/case/round/worker/attempt ID
- database 和 SQL
- 两端 stage、status、connection ID、elapsed time
- 原始错误证据
- barrier wait、SQL execute、row fetch 和 watchdog 诊断
- retry 原因和下一次退避

`events.jsonl` 的 `infrastructure_pause` 同时携带两端的精简证据，不能只记录 database 和 SQL。

每 5 秒发布一次 correctness 运行诊断快照，汇总 worker 当前阶段、阶段停留时间、连接数、最近基础设施错误、重试次数、完成查询数和活动数据库。详细 SQL 和 traceback 留在 worker JSONL，快照保持有界。

## Finding v2 格式

### 文件布局

```text
findings/case_x/
  manifest.json
  setup.sql.jsonl.gz
  execution.sql.jsonl.gz      # 非空时存在
  case.sql
  case.diff
  custom_off.result.json.gz
  custom_on.result.json.gz
```

`setup.sql.jsonl.gz` 和 `execution.sql.jsonl.gz` 每行保存一个严格 JSON 字符串，按原顺序流式压缩。manifest 不再内嵌 SQL 数组，而保存：

- 相对路径
- statement count
- 未压缩字节数
- 压缩字节数
- SHA-256

`schema_version` 升级为 2。reader/replay 同时支持现有 v1 内嵌数组和 v2 外部压缩文件。

setup mismatch 的 `first_difference` 只保存失败 statement ordinal、摘要、限长预览和双端结果，不再复制此前每条 setup SQL。完整 SQL 通过 setup ref 和 ordinal 定位。

writer 继续使用临时目录、fsync 和原子 rename。SQL 体积不再受 64 MiB manifest 限制；manifest 本身仍执行安全上限。若 artifact 发布失败，先写入有界的 `artifact_write_failed` 事件和紧急诊断文件，再以包含完整根因的 fatal error 停止，不能只显示 `ValueError` 类名。

## 确定性查询准入

correctness oracle 不能判断一个没有全序的 `LIMIT/OFFSET` 子集是否正确，因此生成阶段增加 determinism contract。

第一版采用保守规则：

- `LIMIT 0` 允许。
- 没有 FROM 的标量常量查询允许。
- 专用生成器能够证明 ORDER BY 覆盖全部投影值，或包含生成器已知唯一键作为最终 tie-breaker 时允许。
- 其他含任意层级非零 `LIMIT/OFFSET` 的 grammar candidate 在 EXPLAIN 前拒绝，记录 `nondeterministic_row_limit`，且不计入 coverage debt。

fuzz 和 performance 模式仍可生成这些 SQL；限制只作用于 correctness 值 oracle。这样不会用“忽略所有 LIMIT mismatch”的方式漏掉已经具备稳定全序的真实错误。

## 元数据与值语义

oracle 将列信息拆成两层：

### 值语义元数据

用于选择 canonicalization 和判断真实结果兼容性，包括：

- type code/type family
- unsigned
- binary/text 解释
- 文本 character set
- 会改变值解释的 decimals/scale

值语义元数据不同仍是 finding。

### 协议/来源元数据

包括：

- column name
- nullable flag
- display length
- key、auto increment、group、origin 等 advisory flags

当值语义和结果行一致、只有协议元数据不同时，oracle 返回 match，并附带 `metadata_advisory`。服务写入有界诊断事件和统计，但不增加 finding、不终止轮次。原始列元数据仍完整保存在真正 finding 的 result artifact 中。

## Fatal failure 与停止

`CorrectnessRunService` 捕获 worker fatal error 后按顺序执行：

1. 设置 stop event。
2. 发布带完整 exception evidence 的中文 `run_failed`。
3. 调用 round engine 的 `abort_active()`，进而 abort factory 注册的全部活动 session。
4. cancel 尚未开始的 futures。
5. 等待已开始 worker 释放资源并记录 shutdown elapsed。

数据库 socket 被主动关闭后，setup/query/mutation 不应继续等待 310 秒 read timeout。正常 duration 或 Ctrl+C 停止仍遵守现有安全退出语义；不会删除数据库。

## 配置与兼容性

新增 correctness 配置：

```yaml
correctness:
  query_attempt_json_log: true
  mutation_infrastructure_retry_attempts: 3
  diagnostics_interval_seconds: 5
```

连接超时继续默认 10 秒，并在后续需要时作为独立配置暴露；本次不把它提高到 30 秒。旧配置不需要新增字段即可运行。

现有 v1 finding 可读取和 replay；新写入统一使用 v2。performance 使用共享 v2 artifact writer 时同步获得大 SQL 外置能力，但本次不改变 performance 的测量会话与计时语义。

## 测试设计

### 单元和故障注入

1. 双端并发 acquire：单端失败时另一端不复制错误；成功 lease 被关闭。
2. connector errno/sqlstate/msg 不完整时仍保留异常原文。
3. 一端 setup success、一端 infra error 被分类为 infrastructure pause 并重试。
4. 一端执行前失败会立即 abort peer gate，等待时间显著小于 query timeout。
5. 一轮 2000 条查询复用同一对 connection ID。
6. 普通表 SELECT 断连后双端重连并重试；临时表断连终止轮次。
7. mutation 在 COMMIT 前断连可安全回滚重试；COMMIT ambiguous 不重试且保留数据库。
8. 65xxx 派生错误明确标记 peer 状态，不再显示为两个真实节点同时失败。
9. 大于 64 MiB 的 setup SQL 能写入 v2 finding、读取并 replay，manifest 保持有界。
10. v1 finding 继续读取和 replay。
11. 非确定性 LIMIT/OFFSET 被拒绝；有唯一 tie-breaker 的 LIMIT 被接纳。
12. nullable/flags 差异产生 metadata advisory；type/value 差异仍产生 finding。
13. fatal worker error 调用 abort_active，pending futures 取消，run_failed 包含原文。
14. correctness 默认创建 worker query-attempt JSONL 和 5 秒诊断事件。

所有生产行为修改遵循测试先行：每项先写可复现失败测试并确认 RED，再写最小实现确认 GREEN。

### 本地 MySQL 8.0.22 验收

1. 启动两个隔离的 `mysql:8.0.22` 容器，分别作为 custom_off/custom_on。
2. 运行 doctor，确认两端可写、版本和会话初始化正常。
3. 使用 8 worker 运行至少 5 分钟 correctness smoke，要求：
   - 无 `run_failed`；
   - 无 65xxx 假 finding；
   - 相同引擎不产生结果值 finding；
   - query attempt 和诊断日志完整；
   - 每 worker 的 connection ID 在健康轮次内稳定；
   - 常态数据连接不超过 worker pair 上限加 control reserve。
4. 在另一次运行中停止 custom_on、等待诊断捕获后重启，要求单端真实错误可见、peer 标记准确、普通表轮次能够恢复或安全终止。
5. 运行超大 artifact 专项测试，确认不会重现 `artifact JSON exceeds the 67108864-byte safety limit`。
6. 检查退出后的 `PROCESSLIST`、容器连接数和客户端进程，确保没有遗留活动连接或长时间 Sleep。

### 完整验证

- 受影响模块定向 pytest。
- 完整 pytest。
- lint/type check（按项目现有命令）。
- Python 包和 CentOS 交付包构建。
- `git diff --check` 和工作树检查。

## 验收标准

- 所有上述自动化和本地 MySQL 测试通过。
- 任一 65xxx 事件都能从日志确定真实 role、阶段和原始异常；不得再从相同包装消息推断两端都失败。
- 单端基础设施故障不产生 setup/result correctness finding。
- 非确定性 row-limit 和协议元数据差异不再产生结果值误报。
- 任意受配置允许的 setup SQL 体积不会因 manifest 64 MiB 上限导致全局失败。
- fatal failure 能主动中止活动连接并输出可直接分析的中文完整根因。
- 旧 finding replay、fuzz 读写分离语义及 performance 计时语义没有回归。
