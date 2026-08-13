# sql_fuzz

`sql_fuzz` 是面向 MySQL 8.0.22 的 SQL 模糊测试工具。

项目默认使用中文文档、中文配置说明和中文界面文案。SQL、MySQL、PolarDB、函数名、错误码和数据类型保留官方英文写法。

## 当前能力

- 读取基表 SQL 目录，并在每个任务启动时按文件名顺序全部执行。
- 基于已知表和列元数据生成随机 SELECT SQL。
- 支持一任务配置主节点和可选备节点；基表初始化与 DML 固定走主节点，随机查询固定走备节点。
- 支持任务级跳板机配置复用。
- 跳板机支持 SSH 账号密码登录，也保留私钥路径作为可选登录方式。私钥请使用 RSA、ECDSA 或 Ed25519；Paramiko 5 不再支持 DSA/DSS 私钥。
- 支持配置备节点查询线程数，每个查询 worker 使用一个独占备节点连接；默认 16 个查询线程。
- 可选启用逐表 CRUD 压力：74 张永久基表各使用一个独占主节点连接，持续随机执行 INSERT、UPDATE、DELETE。
- 支持从前端暂停、恢复和停止单个任务。
- 持续执行查询和可选 DML，不校验查询结果正确性，也不等待主备复制完成。
- 记录日期、任务、节点、执行状态和 SQL。
- 任务接口和前端任务卡片会展示主写/备读目标、版本化种子、查询与 INSERT/UPDATE/DELETE 统计，以及主备重连状态。
- 主库连接或表暂未就绪时，初始化在后台按 0.1 秒起步、5 秒封顶持续重试，创建任务接口不会被连接阻塞；确定性的配置、DDL 或基表结构错误才会保留失败任务并展示失败环节和原因。
- 后台会记录每个 worker 的状态和当前 SQL；worker 执行 SQL 超过阈值时会关闭该 worker 连接并标记为“疑似卡住”，同时写入 SQL 日志、失败 SQL 文件和任务级告警，下一轮执行前会重连并重建该 worker 的临时表会话。
- 每次执行随机查询前，后台都会对当前 worker 会话执行 `SET SESSION max_execution_time = 5000`，将单条 SELECT 最大执行时间限制为 5 秒。
- 旧查询任务的普通错误和 lost connection 会把失败 SQL 额外写入 `logs/failed_sql/日期/任务.sql`；角色化任务的普通非约束错误也会记录，lost connection 则只写带 worker 角色和目标的去重事件。失败 SQL 文件只包含原始 SQL，具体数据库错误信息写在对应 JSONL 的 `error_message` 字段。
- 旧查询任务的 lost connection 按同一节点 10 分钟窗口去重；角色化任务改用 worker 级独立重连统计。
- 角色化主写备读任务中，每个 worker 独立无限重连并保留待重试 SQL，不因单连接故障暂停其他 worker。
- 提供 FastAPI 接口和中文前端大屏。

## 本地测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## 后端启动

```bash
.venv/bin/uvicorn select_fuzz.api.app:app --host 127.0.0.1 --port 8000
```

默认 API 会使用当前目录下的 `logs/` 保存指标和事件索引。

## 前端启动

```bash
cd web
npm install
npm run dev -- --port 5173
```

前端会读取 `/api/tasks` 获取真实任务。若本地后端未启动，页面只显示“后端未连接”和空任务状态，不会展示内置示例任务。

## 任务级基表模式

新建任务时，“扩展基表列（每表 200～500 列）”开关默认关闭，关闭时每张内置基表使用 42 个核心列。开启后会显示生成器版本和复现种子；首个版本为 `v1`。种子使用 `0`～`18446744073709551615` 范围内的规范无符号 64 位十进制字符串，留空时由后端生成。

模式、最终版本和种子在任务创建后不可修改。任务卡片会始终展示完整基表模式；扩展模式可直接复制 `v1:种子` 复现标识。即使任务在“准备基表”或“连接实例”阶段失败，卡片也会保留这些信息。

## 主写备读与逐表 CRUD

新建任务时，`host:port` 始终表示主节点；可通过 `replica_host` 指定备节点，`replica_port` 留空时继承主节点 `port`。未填写 `replica_host` 时，查询连接兼容性回退到主节点地址。系统不会检查主备地址是否相同或节点是否为 `read_only`，不会等待复制完成，也不会采集、展示复制延迟；请在已授权的测试集群中填写正确拓扑。

“逐表 CRUD”对应请求字段 `enable_crud`，默认关闭。关闭时保留原有查询任务方式；开启后只为 74 张永久表创建 DML worker，排除 session 级临时表 `t2`～`t6`。每张永久表固定一个 DML worker 和一个独占主节点连接；`thread_count` 表示备节点查询 worker 数，每个查询 worker 使用一个独占备节点连接，默认值为 16、允许范围为 1～128。因此开启后的连接规模是 74 个主 DML 连接加 N 个备查询连接，另有主节点初始化连接的生命周期复用。

DML worker 只随机执行 INSERT、UPDATE、DELETE，不执行 SELECT。表的估算行数在 10～200 之间时三种操作等权随机；估算行数小于等于 10 时优先 INSERT，大于等于 200 时优先 DELETE。每条 DML 请求 1～10 行，SQL 返回后立即生成下一次操作。UPDATE 会避开 PRIMARY、UNIQUE、FOREIGN KEY、分区键和 generated 列；备节点查询禁用锁定读，并且不访问临时表。

主、备 worker 分别维护自己的连接和重连状态。断连或表暂未就绪时，只有对应 worker 按 0.1、0.2、0.4 秒递增、5 秒封顶的退避持续重连；尝试次数可以无限增长而退避仍固定封顶。任务保持运行，其他 worker 不受影响。重连成功后重试同一条 SQL，不做幂等性或主备一致性判断。常见唯一键、死锁、外键和 CHECK 约束冲突（MySQL 错误码 1062、1213、1451、1452、3819）只累计 DML 失败数，不写逐条 SQL 日志或失败 SQL 文件，随后继续生成新操作。

每次新建任务时，查询 `query_seed`、启用 CRUD 时的 `crud_seed`，以及开启扩列时的 `base_table_seed` 都会分别随机生成；对应生成器版本当前均为 `v1`。任务卡片会展示适用的 `v1:种子`，暂停、恢复和重连都不会更换种子。需要复跑时，可以显式提交原版本与种子；相同基表版本和结构下，相同生成器版本与根种子会稳定派生出相同 worker 随机序列，相同基表 `v1:种子` 也会生成相同的扩列表。种子均为 `0`～`18446744073709551615` 范围内的规范无符号 64 位十进制字符串。

下面是开启主写备读及逐表 CRUD 的任务请求示例；密码必须通过实际运行环境安全传入，不要把真实凭据提交到仓库：

```json
{
  "node_name": "polardb-test-cluster",
  "host": "172.18.4.12",
  "port": 3306,
  "replica_host": "172.18.4.13",
  "username": "fuzz",
  "password": "<数据库密码>",
  "database": "test",
  "thread_count": 16,
  "enable_crud": true,
  "expand_base_table_columns": false
}
```

`replica_port` 在上例中省略，因此使用主节点端口 3306。若需要精确复跑，可额外填写 `query_generator_version`、`query_seed`、`crud_generator_version`、`crud_seed`；开启扩列时再填写 `base_table_generator_version` 和 `base_table_seed`。自定义 `base_sql_dir` 不支持逐表 CRUD，带 `enable_crud: true` 的请求会在建立数据库连接前被拒绝。

## 基表 SQL 目录

项目默认使用内置 `sql_base_tables/`。核心模式会在连接数据库前从配置目录一次性加载并校验不可变内存包；扩展模式则由版本化生成器在内存生成并校验。启动、附加 worker、lost connection 恢复和 worker 重连全部复用同一份内存 SQL，不会在任务运行期间重读目录或重新随机生成。

核心模式仍支持配置自定义 `base_sql_dir`：系统会在连接前按文件名顺序一次性加载并校验其 `.sql` 文件。扩展算法只适用于项目内置的 79 张表，不会对自定义 DDL 自动加列；自定义目录下开启扩展模式时，任务会在“准备基表”阶段失败，且不会启动跳板机、连接数据库或执行 SQL。

基表包校验通过后，启动阶段会执行 `DROP DATABASE IF EXISTS test`、`CREATE DATABASE test`、`USE test`，随后创建基表和插入种子数据，并对每张解析到的表执行 `SELECT COUNT(*)` 校验，发现 0 行会直接失败。

`sql_base_tables/` 包含 79 张基表：2 张普通表、5 张临时表、8 张一级分区表和 64 张二级分区表，不包含向量类型、向量索引或向量函数。默认二级分区表面向内网扩展 MySQL 内核，覆盖 `RANGE`、`RANGE COLUMNS`、`LIST`、`LIST COLUMNS`、`HASH`、`LINEAR HASH`、`KEY`、`LINEAR KEY` 的 8 x 8 组合；`RANGE/LIST` 子分区使用显式 `SUBPARTITION ... VALUES LESS THAN/IN (...)` 定义。

仓库提交的静态目录和离线生成器默认使用核心模式：每张表恰好 42 列，不包含 `extra_tN_NNN` 扩展列。79 份核心列类型参数、索引顺序和每表 10 到 100 行的种子数量均已冻结；例如 `char_col` 和 `varchar_col` 仍覆盖 `1` 到 `255` 的长度边界，相关索引前缀和核心种子值会同步适配。分区表使用 `tenant_id` 1 到 8、二级分区表使用 `subpart_id` 1 到 8 保证路由覆盖，并尽量把可安全唯一化的索引生成为 `UNIQUE KEY`；二级分区表会保守处理唯一索引，避免违反唯一键必须包含全部分区列和子分区列的限制。由于临时表是 session 级对象，多线程任务会在每个 worker 连接中单独创建临时表并插入临时表种子数据。lost connection 恢复后也只重建临时表并重新插入临时表数据，不重建永久表。

离线重生并校验默认核心目录：

```bash
.venv/bin/python tools/generate_sql_base_tables.py --output-dir sql_base_tables
.venv/bin/python tools/validate_sql_base_tables.py --sql-dir sql_base_tables
```

需要覆盖宽表场景时，必须显式选择扩展模式、生成器版本和复现种子。`v1` 使用规范 uint64 十进制种子；不接受符号、空白、Unicode 数字或前导零。同一个 `v1 + seed` 固定生成 80 个逻辑 SQL 文件，其中 79 张表各有 200 到 500 列，`t0` 和 `t1` 分别固定为 200、500 列；扩展列数量、类型参数和值表达式都由带用途隔离的 SHA-256 派生。运行时包入口只在内存返回 SQL，不写完整的任务级基表初始化 SQL；运行中的 fuzz 查询、失败查询和 lost connection 事件仍按日志规则记录。下面的离线 CLI 才会写入指定目录：

```bash
.venv/bin/python tools/generate_sql_base_tables.py \
  --output-dir /tmp/select_fuzz_expanded_12345 \
  --expand-columns \
  --generator-version v1 \
  --seed 12345
.venv/bin/python tools/validate_sql_base_tables.py \
  --sql-dir /tmp/select_fuzz_expanded_12345 \
  --expanded-columns \
  --generator-version v1 \
  --seed 12345
```

`v1` 的文件顺序、UTF-8/LF 渲染和长度前缀 bundle 序列化由固定摘要金标保护。项目升级后复现旧任务时，仍使用任务卡片中的完整 `v1:种子`；以后若需要改变扩展算法或 SQL 格式，应登记新的生成器版本，不能修改既有 `v1` 输出。

可以生成不含二级分区的本地 MySQL 兼容目录，用于普通 MySQL 建表和插入验证：

```bash
.venv/bin/python tools/generate_sql_base_tables.py --output-dir /tmp/select_fuzz_mysql_compatible --without-subpartition
.venv/bin/python tools/validate_sql_base_tables.py --sql-dir /tmp/select_fuzz_mysql_compatible --without-subpartition
```

`--without-subpartition` 是离线兼容变体，不属于标准 `v1 + seed` bundle 或其金标身份。

查询生成器按 MySQL 8.0.22 兼容范围生成查询表达式。集合运算只生成 `UNION` 和 `UNION ALL`，不生成 MySQL 8.0.31 才支持的 `INTERSECT`、`INTERSECT ALL`、`EXCEPT`、`EXCEPT ALL`，也不生成 MySQL 不支持的 `MINUS`。`SQL_CACHE` 已在 MySQL 8.0 移除，`SQL_NO_CACHE` 在 MySQL 8.0 中已废弃且无实际效果，因此也不会生成。

当前随机生成会覆盖以下 MySQL 8.0.22 支持的扩展点：

- 无 `FROM` 常量查询，例如 `SELECT 1`、`SELECT NULL`。
- 常量派生表、括号查询表达式、`TABLE` 查询表达式和独立 `VALUES` 查询表达式。
- 分区表显式 `PARTITION (p0)` 访问。
- `DISTINCTROW`、`HIGH_PRIORITY`、`SQL_SMALL_RESULT`、`SQL_BIG_RESULT`、`SQL_BUFFER_RESULT`、`SQL_CALC_FOUND_ROWS`。
- `NOT IN`、`NOT EXISTS`、`NOT BETWEEN`、`NOT LIKE`、`NOT REGEXP`、`RLIKE`、`LIKE ... ESCAPE` 和 `IS TRUE/FALSE/UNKNOWN`。
- `COUNT(DISTINCT)`、`BIT_AND()`、`BIT_OR()`、`BIT_XOR()`、带 `ORDER BY` 和 `SEPARATOR` 的 `GROUP_CONCAT()`。
- `LAG()`、`LEAD()`、`NTILE()`、`FIRST_VALUE()`、`LAST_VALUE()` 和 `ROWS BETWEEN ...` 窗口 frame。
- `JSON_TABLE()`、`JSON_CONTAINS()`、`JSON_KEYS()`、`JSON_LENGTH()`。
- `COLLATE`、`BINARY expr`、`ABS()`、`ROUND()`、`FLOOR()`、`CEILING()`、`CRC32()`、`TIMESTAMPDIFF()`、`DATE_FORMAT()`、`MONTH()`、`DAYOFWEEK()`。
- 表访问索引提示：`USE INDEX`、`FORCE INDEX`、`IGNORE INDEX`，并覆盖 `FOR JOIN`、`FOR ORDER BY`、`FOR GROUP BY` 作用域；索引名只取自当前表元数据。
- `SELECT /*+ ... */` 优化器提示：`JOIN_ORDER()`、`JOIN_FIXED_ORDER()`、`NO_MERGE()`、`SET_VAR()`、`JOIN_INDEX()`、`NO_INDEX()`，提示参数只引用当前查询块内存在的表别名和索引名。
- 行构造器谓词，例如 `(a, b) IN ((1, 2), (3, 4))` 和 `(a, b) = (...)`。
- `ANY`、`SOME`、`ALL` 单列子查询比较，以及引用外层别名的相关 `EXISTS` 子查询。
- `JOIN LATERAL` 派生表。
- 排序表达式扩展：`ORDER BY FIELD(...)`、`ORDER BY RAND()`、`ORDER BY 1`。
- 上下文函数：`USER()`、`CURRENT_USER()`、`DATABASE()`、`VERSION()`、`CONNECTION_ID()`。
- 十六进制和 bit 字面量，例如 `X'0F'`、`0xFF`、`b'1010'`。

SQL 生成策略全部由后台默认配置控制，前端不提供语法比例配置入口。默认按 MySQL 8.0.22 生成合法 SQL，同时保留小比例探索错误：`invalid_sql_ratio=0.03` 用于生成故意不合法或参数错误 SQL，`null_compare_ratio=0.08` 用于强化 `NULL` 比较覆盖，`risky_expr_ratio=0.08` 用于跨类型风险表达式。后台 SQL JSONL 日志会写入 `sql_validity`、`risk_tags` 和 `expected_error`，用于区分合法 SQL、风险 SQL 和故意不合法 SQL。

## lost connection 规则

- 未启用角色化主写备读时，保留原有任务级恢复规则：同一节点 10 分钟内只记录第一次 lost connection 事件，任务进入恢复检测状态并每 1 分钟探测一次，恢复后继续查询。
- 配置备节点或启用逐表 CRUD 后，改为 worker 级独立无限重连：查询连接只重连备节点，DML 连接只重连主节点，任务不进入全局恢复状态。
- worker 重连退避从 0.1 秒开始倍增并在 5 秒封顶；停止任务可以中断等待，重连后继续执行断连前保存的同一条 SQL。

## 任务控制和异常展示

- 任务启动后会按“连接实例 → 准备基表 → 执行 SQL”的环节推进。生产接口先登记任务并立即返回；主连接、跳板隧道、数据库或表暂未就绪时会在当前环节无限退避重试，`1049/1146` 会从 `DROP/CREATE DATABASE` 边界完整重做初始化。只有不可恢复的配置、DDL 或结构错误才进入“失败”并展示原因。
- 任务卡片提供暂停、恢复和停止操作。暂停会覆盖查询与 DML worker，但不会关闭数据库连接；恢复后继续按暂停前状态执行。停止会中断重连等待、关闭全部主备 worker 连接和相关跳板机隧道，但保留数据库现场，不自动 DROP 或清理测试库。
- 多线程任务会展示查询与 DML 汇总、主备重连状态；74 个 DML worker 明细默认折叠。展开后可查看每个 worker 的角色、目标、表名、生成器身份、后台线程存活状态、数据库连接状态、已成功 SQL 数和最近错误。前端任务列表每 1 秒刷新一次。后台看门狗默认在 worker 单条 SQL 执行超过 120 秒时关闭该 worker 连接，随后由该 worker 独立重连。

## Windows 运行方式

Windows 推荐使用 PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn select_fuzz.api.app:app --host 127.0.0.1 --port 8000
```

另开一个 PowerShell 启动前端：

```powershell
cd web
npm install
npm run dev -- --port 5173
```

浏览器打开 `http://localhost:5173/`。如果要通过跳板机连接内网实例，先在页面保存跳板机配置，再新建任务时选择该配置并设置并发线程数。

跳板机登录默认只使用页面或配置文件中显式填写的账号密码或私钥路径，不会扫描本机 SSH agent 和默认 `~/.ssh/id_*`。如果远程环境出现 `module 'paramiko' has no attribute 'DSSKey'`，优先确认是否还在使用 DSA/DSS 私钥，并将跳板机私钥替换为 RSA、ECDSA 或 Ed25519。
