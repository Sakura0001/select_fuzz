# 保留 Crash SQL 并抑制 Lost Connection 误报设计

## 背景

MySQL 8.0.22 可被特定的 `EXISTS (TABLE ...)` 查询触发进程崩溃。当前生成器会在执行前拒绝这类候选，虽然能够保护测试实例，但也丢失了发现数据库 crash 的覆盖能力。

对比模式已经把 MySQL 客户端连接错误归类为 `INFRA_ERROR`，并在进入结果 oracle 之前拦截。需要显式保留并测试该边界，避免一侧节点崩溃后返回 lost connection、另一侧成功或返回普通数据库错误时，被错误写入 `findings/`。

## 目标

- 默认 MySQL 8.0.22 文法继续生成并执行 `EXISTS (TABLE ...)` 查询。
- 单侧 lost connection 不作为结果差异 finding。
- 不新增 `crashes/` 或其他专用 crash 产物目录。
- 保证触发执行的 SQL 和两侧异常信息能够从现有日志中恢复。
- 保持现有连接恢复和基础设施重试语义。

## 非目标

- 不根据 lost connection 自动断定数据库已经 crash；网络中断也可能产生相同客户端错误。
- 不探测 core dump、容器退出状态或服务端错误日志。
- 不改变性能模式的性能告警判定。
- 不把基础设施错误转换为正常通过结果。
- 不限制或删除其他 MySQL 8.0.22 查询文法。

## 设计

### SQL 生成

删除默认文法针对 `EXISTS (TABLE ...)` 的已知 crash 形态拦截。候选仍需通过现有只读安全校验，因此恢复 crash 覆盖不会放宽 DDL、DML 或多语句安全边界。

### 执行和分类

MySQL 客户端错误码 2000–2999 或 SQLSTATE `08xxx` 继续由执行器分类为 `ExecutionStatus.INFRA_ERROR`。两节点协调器返回任意一个 `INFRA_ERROR` 时，正确性轮次必须：

1. 在进入 `compare_two_nodes` 和查询错误契约分析前识别基础设施错误；
2. 写入本次尝试的完整节点诊断；
3. 发布 `infrastructure_pause`；
4. 不创建 `FindingRecord`，因此不写入 `findings/`；
5. 按现有退避和连接恢复逻辑重试同一 SQL。

该规则同时覆盖：

- lost connection 对成功；
- lost connection 对普通数据库错误；
- 两侧均 lost connection。

### 日志与可追溯性

在调用数据库执行器前，系统继续按以下顺序持久化：

1. `sql/worker-NNN.jsonl` 的 `query_attempt_started`，其中包含完整 `query_sql`、种子、数据库、轮次和 attempt ID；
2. `rounds/<database>.sql` 中的实际尝试 SQL；
3. 可选的 `sql/worker-NNN.sql` 全量线程 SQL 日志。

`query_attempt_started` 使用追加写和 `fsync`，所以服务端随后瞬间崩溃时，触发 SQL 仍可恢复。执行返回后，`query_attempt_finished` 保存每个节点的状态、errno、SQLSTATE、异常原文和 failure evidence；`events.jsonl` 保存含 SQL 的 `infrastructure_pause`。

内网正确性配置继续保持 `correctness.query_attempt_json_log: true`。`full_thread_sql_log` 可保持 `false`，不影响 JSONL 诊断和轮次 SQL。

## 测试

- 生成器回归测试验证默认 MySQL 8.0.22 文法不再拒绝固定种子生成的 `EXISTS (TABLE ...)`。
- 轮次引擎回归测试验证单侧 lost connection、另一侧成功时：记录基础设施尝试、不生成 finding、不进入 oracle 结果分类。
- 轮次引擎回归测试验证单侧 lost connection、另一侧普通数据库错误时同样不生成 finding。
- 验证失败尝试日志包含完整 SQL、节点错误及 failure evidence。
- 运行定向测试、全量 pytest、Ruff、mypy 和构建。

## 验收标准

- 先前触发 MySQL 8.0.22 crash 的生成种子重新产生可执行候选。
- 所有包含单侧 lost connection 的执行批次均不会增加 `summary.findings`，且 `findings/` 下无对应 manifest。
- 触发 SQL 至少存在于 `sql/worker-NNN.jsonl` 与 `rounds/<database>.sql`。
- 现有基础设施恢复、重试和停止行为保持不变。
