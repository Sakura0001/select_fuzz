# 查询生成范围与可选 PQ 准入

生成范围依据：桌面 `pq/粘贴的 markdown (1)。md(9)` 的第 2、7、9 节及文末限制列表。
文档中版本范围有差异的能力采用保守交集，例如排除 DISTINCT 聚合。

## 执行契约

correctness、performance、fuzz 和 replay 均支持没有 PQ 功能的普通 MySQL。
主入口使用执行器的默认串行兼容路径，不要求并行计划或 PQ worker。
correctness 仍用 baseline EXPLAIN 检查候选是否合法；performance 对同一次正式
EXPLAIN ANALYZE 的实际执行树计时，串行计划正常参与比较。fuzz reader 直接执行 SELECT，
不增加 PQ EXPLAIN 探测。自动生成器保留现有保守语法、类型范围和候选预算。

独立 `select_fuzz.pq` 以及显式设置 `require_pq=True` 的底层调用才要求 PQ 准入：
每次 PQ 工作负载执行前，在同一会话取得新的 EXPLAIN 并行计划；没有证据的候选被排除。
初始化、DML、元数据探测和串行对照不属于该 PQ 工作负载。

SQL 结构、强制开关和低成本阈值都不能保证服务端一定分配 PQ worker。资源不足、优化器
判断及运行时回退仍可能阻止 PQ。可选 PQ 路径中，普通 SELECT 的证据级别是“计划确认且
未报告回退”；EXPLAIN ANALYZE 还会检查实际执行树。当前不声称逐条拥有 worker 遥测。

## 删除与保留

| 范围 | 当前行为 |
| --- | --- |
| 主 grammar | 保留表扫描、范围扫描、基础聚合、GROUP BY/HAVING、INNER JOIN、派生表、非递归 CTE、UNION、正数且带 ORDER BY 的 LIMIT。所有 SELECT 分支需要 FROM。 |
| 删除的查询路径 | 窗口函数、ROLLUP、递归 CTE、LATERAL、TABLE/VALUES、无基表 SELECT、INTERSECT/EXCEPT、锁定读、标量/条件子查询、无序/零 LIMIT。 |
| 函数 | 基础聚合 COUNT/SUM/AVG/MIN/MAX，及文档白名单中的数学、日期、控制函数和 STRCMP。移除非白名单字符串函数、JSON/空间/UDF、BIT_*、GROUP_CONCAT、方差/标准差、DISTINCT 聚合等构造。 |
| 负载与性能构造 | 删除 CRC32/BIT_XOR、窗口、SHA2/CONCAT/REPEAT 排序及标量子查询负载；改用支持的数值计算、聚合、派生聚合及有界连接。 |
| schema | 生产正确性生成普通 InnoDB 表或外键图；移除临时表、TEXT/BLOB/JSON/空间列及表达式索引路径。fuzz 保留 56 个受支持字段变体和 BTREE 索引。 |
| 显式 schema | 自定义 grammar 校验已知表/列、引擎、生成列和分区；分区扫描必须明确单个分区。binary 字段属于支持的 STRING/VAR_STRING；BINARY 函数与 CAST AS BINARY 仍被排除。 |
| 独立 PQ 生成器 | 11 类：scan、aggregate、decimal_stress、join、join_multi、self_join、nested_agg、subquery_in、union、derived、distinct。删除 subquery_scalar；首轮和重试使用同一条含扫描计算的构造路径。 |
| 独立 PQ 的条件能力 | 保留受约束的正向 IN 半连接候选和 LEFT JOIN；是否真正形成可并行计划必须由运行时准入确认。主 grammar 使用更窄的 INNER JOIN/派生表路径。 |
| validation | 已移除构造的能力标记为 `pq_excluded`/GAP，避免反复搜索已不存在的路径。历史文档与离线 schema 模型不代表生产仍会生成这些能力。 |

## 参数、计数和有界退出

所有数据库运行参数均由用户预先配置。测试、重连、setup 和生成的重放脚本不再额外
下发 SET 来覆盖 PQ/DOP、成本阈值、回退开关、时区、SQL 模式、会话字符集或服务端超时。
主工具的 `session_variables_by_role` 配置保留兼容和记录，不在运行时下发。
驱动连接握手、客户端连接超时、watchdog、事务控制与诊断用用户变量仍用于执行和停止测试。

独立 PQ harness 使用预配置的 PQ 与串行端点；不再在同一端点上切换 DOP，也不自动做
DOP sweep。CLI 的串行端点需显式提供；任何端点配置不符合计划要求时，记录拒绝原因，
不会尝试修正参数。普通 SELECT 的证据仍由 EXPLAIN 和紧接着的回退警告检查提供。

可选 PQ 准入中的传统计划需要 Extra 中正 worker 数的 `Parallel execute`；仅有 `<gather1>` 表别名
不足以通过。TREE 检查实际 Gather/Parallel scan/lookup 操作符，过滤引号内的假标记。

- correctness 每轮候选预算仍为 `max(100, queries_per_round × 12)`；候选持续被语法、
  baseline EXPLAIN 或一致运行错误排除时，达到预算后结束该轮。
- performance 在三节点同步后计时，串行 ANALYZE 正常参与耗时比较。
- fuzz 的成功读取、SQL 错误、超时及连接故障按实际执行结果计数，继续受运行时长、
  watchdog、连接数上限和停止信号约束。
- 底层可选 PQ 路径限制计划为 10,000 行、约 200 万字符，EXPLAIN 受查询 deadline
  约束；拒绝时中止等待中的对照，执行后检查回退警告和 ANALYZE 实际树。
- 原有 `pq_rejected` 识别和日志字段保留，兼容显式启用 PQ 的底层调用及注入执行器；
  主入口不会因普通串行计划产生该分类。准入超时保留 TIMEOUT/3024 和拒绝证据，
  不会被误判为数据库语义差异，连接清理失败也不会覆盖原始拒绝原因。
- 独立 PQ harness 继续使用每个查询槽的最大生成次数、每语句超时、运行时长及结果预算。

## 验证与产物

本地离线验证命令（不连接测试数据库）：

```sh
.venv/bin/python -m pytest tests -q \
  -m 'not mysql and not mysql_performance and not online and not soak' --timeout=30
.venv/bin/python -m ruff check src/select_fuzz tests
.venv/bin/python -m mypy src/select_fuzz --show-error-codes
```

此前不改参修正日志在 `artifacts/pq-preconfigured-20260908/`，覆盖当时的全量回归和静态检查。
该阶段离线回归为 **1994 passed、17 failed、15 deselected**，失败集合与修改前的独立
PQ oracle 完全相同。初次生成裁剪的 `artifacts/pq-only-20260908/` 保留 15,000 条候选的
静态验证，覆盖全部 56 个产生式和支持的类型别名。Ruff 与 `git diff --check` 通过；
完整 strict mypy 仍有 23 项既有问题，没有新增诊断（修正前为 27 项）。
上述两项完整检查仍以非零状态退出，不能报告为全绿。既有 oracle 失败未在本次修复。
该阶段没有连接真实数据库运行。

普通 MySQL 兼容回归见 `tests/integration/test_standard_mysql_modes.py` 和 fuzz reader
测试，覆盖生产入口、串行结果、实际 ANALYZE 计数以及不增加会话 SET。
真实集群验收应小规模运行三种主模式并确认查询完成数、错误和超时；不要求 PQ 命中。
只有独立 PQ 验收才需要检查准入和回退证据。该路径所有候选被排除表示没有合格 PQ
样本，不能称为 PQ 测试通过；PQ 性能计时和实际树必须来自同一次正式 ANALYZE。

## 回滚

本轮普通 MySQL 兼容修改之前的文件保存在 `artifacts/local-mysql-20260908/baseline/`。
此前不改参修正之前的文件保存在 `artifacts/pq-preconfigured-20260908/baseline/`；该阶段
文件清单与前后 SHA-256 见同目录上一级的 `changes.json`。回滚时只恢复清单中 changed 文件的对应 baseline 副本，
只删除清单中 added 的本次新文件。先核对当前文件仍与清单的修改后 SHA-256 相同，避免
覆盖后续编辑；不要执行 `git reset --hard` 或 `git clean`，仓库原本包含大量未跟踪工作。

初次 PQ 构造裁剪的更早快照仍在 `artifacts/pq-only-20260908/baseline/`。若要回滚两个
阶段，应从最新阶段开始按各阶段清单恢复，避免混用快照。
