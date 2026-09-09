# select_fuzz.pq — PQ 差分测试

独立提供 fast、compare、performance 三种模式。生成器按桌面 PQ 文档收敛，
每次正式执行仍检查同一会话的新 EXPLAIN：PQ 端必须有并行计划，串行对照端必须没有。
没有合格计划、结果不完整或报告运行时回退的候选会被排除。

详细范围、验证与回滚见 [PQ 查询契约](../../../docs/testing/pq-query-contract.md)。

## 生成范围

保留 11 类：scan、aggregate、decimal_stress、join、join_multi、self_join、
nested_agg、subquery_in、union、derived、distinct。移除 subquery_scalar；首次尝试与
后续重试使用相同的 PQ 构造路径，不再先试纯串行候选。保留受约束的 IN 半连接和
LEFT JOIN 候选，是否被优化为 PQ 由 EXPLAIN 决定。生成 schema 的类型必须受支持。

`pq_friendly` 参数仅为兼容旧调用保留，其值不再切换到不同的生成路径。

## 运行

```bash
export SELECT_FUZZ_MYSQL_USER='<test user>'
export SELECT_FUZZ_MYSQL_PASSWORD='<set in shell only>'
export SELECT_FUZZ_SERIAL_MYSQL_USER='<serial test user>'
export SELECT_FUZZ_SERIAL_MYSQL_PASSWORD='<set in shell only>'

.venv/bin/python -m select_fuzz.pq.cli --mode fast \
  --host 127.0.0.1 --port 3306 --serial-host 127.0.0.1 --serial-port 3307 \
  --queries 20 --rows 20000 --tables 4 \
  --max-attempts 12 --timeout-seconds 30 --duration-seconds 300 \
  --seed 43 --artifacts artifacts/pq
```

所有模式在连接与装载前都要求显式 `--serial-host`，或 Python 配置中的
`PQConfig.serial_endpoint`。PQ 与串行端的运行参数需提前配置，逐条 EXPLAIN 仍验证
前者有并行计划、后者没有。串行地址也可通过 `SELECT_FUZZ_SERIAL_HOST` 提供，
账户与密码分别从上述环境变量读取；密码不要写入命令行或产物。

`--mode compare` 执行 PQ/串行差分，可配置独立 MySQL reference；reference 必须使用
区别于 PQ 和串行端的 host/port。
`--mode performance` 记录合格样本的重复测量和 speedup。运行创建独立命名的测试 schema
与产物子目录。同一 host/port 的 PQ、串行账户共享一次 fixture；不同 host/port 按相同
seed、表结构和行数分别装载确定性 fixture。使用同一服务的地址别名或代理时，需要保证
上述地址对应关系正确，避免把一个已创建的 schema 当作新的服务重复装载。
查询次数表示有界候选槽，不能保证最终合格样本数；CLI 没有合格样本时以状态码 3 退出。
使用 Ctrl+C 可停止，客户端保留连接、读写超时、累计结果传输截止时间及结果行数上限。
服务端执行超时由预配置决定，客户端中断不承诺服务端查询已停止。

连接初始化、重连和测量均不额外下发运行参数 SET，包括 DOP、PQ 阈值与开关、SQL mode、
时区、连接排序规则、`max_execution_time`。保留 driver 的 charset 握手及 autocommit /
事务行为。请预先保持两端影响 SQL 语义与 fixture 装载的设置一致；连接会通过只读
`SHOW SESSION VARIABLES` 记录继承值。装载后 `ANALYZE TABLE` 刷新统计信息。

`--dop` 只记录预期配置，不改变参数，也不覆盖 EXPLAIN 的 DOP。
性能产物的 `dop` / `planned_dop` 来自实际计划；无法确定或样本间变化时记为 0，
兼容字段 `requested_dop` 表示预期配置。`--dop-sweep` 只接受与 `--dop` 相同的单个值，
多 DOP 测量会在连接前被拒绝；需要在外部配置完成后分别运行。

## 证据与比较

有效标记包括正 worker 数的 `Parallel execute`、TREE 的 Gather 和 Parallel scan/lookup。
单独的 `<gatherN>` 表别名、SQL 字符串中的标记、`force parallel` 配置文字不能证明 PQ。
普通 SELECT 的证据是新计划加即时回退警告检查，不代表已采集每个 worker 的运行遥测。

FLOAT/DOUBLE 使用配置中的绝对与相对误差。DECIMAL 表达式列默认允许绝对误差 1e-9，
仅对生成器明确标记的表达式列生效，已接受的精度变化仍保留观测；可用
`--decimal-absolute 0 --decimal-relative 0` 要求精确。文档不规定这些工程容差。
无完全排序保证的结果使用 multiset 比较，并受匹配预算限制。

已有 oracle 回归中的 17 个失败在本次查询路径修改前已存在，涉及元数据变化与精度差异
证据，未在本次修复。历史缺陷报告和独立 probe 脚本保留为原始证据，不能作为当前所有
查询都会实际使用 PQ worker 的保证。
