# sql_fuzz

`sql_fuzz` 是面向 MySQL 8.0.22 的 SQL 模糊测试工具。

项目默认使用中文文档、中文配置说明和中文界面文案。SQL、MySQL、PolarDB、函数名、错误码和数据类型保留官方英文写法。

## 第一版能力

- 读取基表 SQL 目录，并在每个任务启动时按文件名顺序全部执行。
- 基于已知表和列元数据生成随机 SELECT SQL。
- 支持一任务绑定一个 MySQL 节点。
- 支持任务级跳板机配置复用。
- 跳板机支持 SSH 账号密码登录，也保留私钥路径作为可选登录方式。私钥请使用 RSA、ECDSA 或 Ed25519；Paramiko 5 不再支持 DSA/DSS 私钥。
- 支持为单个实例配置并发线程数，每个 worker 使用独立数据库连接执行查询。
- 支持从前端暂停、恢复和停止单个任务。
- 持续执行查询 SQL，不校验查询结果正确性。
- 记录日期、任务、节点、执行状态和 SQL。
- 任务接口和前端任务卡片会展示成功查询、失败查询、普通错误和 lost connection 事件统计。
- 启动、建库建表、种子数据校验等环节失败时，前端会保留失败任务并展示失败环节和错误原因。
- 后台会记录每个 worker 的状态和当前 SQL；worker 执行 SQL 超过阈值时会关闭该 worker 连接并标记为“疑似卡住”，同时写入 SQL 日志、失败 SQL 文件和任务级告警，下一轮执行前会重连并重建该 worker 的临时表会话。
- 每次执行随机查询前，后台都会对当前 worker 会话执行 `SET SESSION max_execution_time = 5000`，将单条 SELECT 最大执行时间限制为 5 秒。
- 普通错误和 lost connection 的失败 SQL 会额外写入 `logs/failed_sql/日期/任务.sql`，文件内容只包含原始 SQL 语句；具体数据库错误信息写在 `logs/日期/任务ID.sql.jsonl` 的 `error_message` 字段。
- lost connection 按同一节点 10 分钟窗口去重。
- lost connection 后每 1 分钟探测数据库状态，恢复后继续执行查询。
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

- 同一节点 10 分钟内只记录第一次 lost connection 事件。
- 大屏 lost connection 次数按去重后事件数展示。
- 发生 lost connection 后任务进入恢复检测状态。
- 恢复检测每 1 分钟执行一次。
- 数据库恢复后继续执行查询 SQL。

## 任务控制和异常展示

- 任务启动后会按“连接实例 → 准备基表 → 执行 SQL”的环节推进。任一环节失败时，任务状态变为“失败”，任务卡片停留在失败环节，并展示后端返回的错误原因。
- 任务卡片提供暂停、恢复和停止操作。暂停不会关闭数据库连接，恢复后继续按暂停前的状态执行。
- 多线程任务会展示每个 worker 的状态、后台线程存活状态、数据库连接状态、连接 ID、连接/关闭/内部重连次数、已成功 SQL 数和最近错误。前端任务列表每 1 秒刷新一次。后台看门狗默认在 worker 单条 SQL 执行超过 120 秒时关闭该 worker 连接，防止线程长时间卡住且无法定位。

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
