# sql_fuzz 工具完整设计

日期：2026-06-04

## 目标

本项目构建一个中文 Python 工程 `sql_fuzz`，面向 MySQL 8.0.41 语法和 PolarDB MySQL 兼容向量扩展。工具持续生成并执行 SELECT 类 SQL，不关心查询结果是否正确，只关注 SQL 生成覆盖、持续执行、lost connection 监控、多节点任务管理和前端大屏展示。

核心目标如下：

- 基于已知基表和列元数据生成查询，避免脱离 schema 随机拼接 SQL。
- 先登记完整 SELECT 算子覆盖矩阵，再按矩阵逐项实现随机生成。
- 查询表达式、算子、函数、CTE、子查询和集合操作采用递归随机生成。
- 一个任务绑定一个 MySQL 节点；多个节点通过多个任务运行。
- 任务支持直连和跳板机连接；某任务配置一次跳板机后，该任务后续连接全部复用。
- 每张表初始化 10 行数据，后续持续执行 SELECT SQL。
- lost connection 只在大屏显示，不要求 CLI 额外报警。
- 同一节点 10 分钟内只记录第一次 lost connection 事件，大屏次数也按该去重口径统计。
- lost connection 后任务不退出，每 1 分钟检测数据库状态，恢复后继续执行查询 SQL，不重建表、不重新插入数据。

## 项目语言要求

项目全部使用中文。文档、注释、命令说明、配置说明、错误提示、前端界面文案和测试说明默认使用中文。代码关键标识符可按工程惯例使用英文。SQL、MySQL、PolarDB、函数名、错误码、数据类型和第三方 API 名称保留官方写法。

## 工程目录

```text
AGENTS.md
README.md
pyproject.toml
configs/
  示例节点配置.yaml
  示例跳板机配置.yaml
  示例运行参数.yaml
sql_base_tables/
  001_basic_account.sql
  ...
logs/
docs/superpowers/specs/
select_fuzz/
  __init__.py
  metadata/
  sqlgen/
  runner/
  monitor/
  api/
web/
tests/
```

`sql_base_tables/` 已作为现有基表目录存在，后续实现应直接复用并补齐它，而不是另建平行基表目录。`logs/` 保存运行期日志，不提交到仓库。

## 基表与元数据设计

### 基表目录

`sql_base_tables/` 存放基表 DDL。每个文件使用稳定编号前缀，便于按顺序加载和定位问题。当前目录已有普通表、数值表、字符串表、时间表、二进制表、枚举集合表、JSON 表、生成列表、默认表达式表、CHECK 约束表、索引表、全文表、空间表、排序规则表、不可见列表、组合键表、分区表、函数索引表和表选项表。

后续需要补齐：

- PolarDB MySQL 向量表，包含 `VECTOR(N)` 列。
- 向量函数样例列和可查询数据。
- 向量索引表，覆盖 HNSW/FAISS 相关索引配置。
- 二级分区表，覆盖分区和子分区。
- 外键父子表和多表外键图。

### 表类型与限制

基表分为以下类别：

- 全类型普通表：覆盖 MySQL 常用数据类型。
- 分区表：覆盖 RANGE、LIST、HASH、KEY 以及二级分区组合。
- 索引表：覆盖主键、唯一索引、普通索引、联合索引、前缀索引、函数索引、全文索引、空间索引和向量索引。
- 外键图表：通过父子表和交叉引用关系覆盖 JOIN 路径。
- 向量表：覆盖 `VECTOR(N)`、向量距离计算和向量索引。

MySQL 8.0 InnoDB 分区表不能包含外键引用，也不能被外键引用。因此分区表和外键图表需要分开建模，查询生成时可以同时选择这些表参与 JOIN，但 DDL 不强行让分区表参与外键约束。

PolarDB 向量列遵守向量扩展限制：向量列不作为主键、外键、唯一键或分区键。

### 元数据模型

实现时将 DDL 解析为内部元数据：

- `表元数据`：表名、表类别、存储引擎、字符集、分区信息、索引列表、外键列表。
- `列元数据`：列名、SQL 类型、类型族、是否可空、默认值、是否生成列、是否不可见、是否适合参与谓词。
- `索引元数据`：索引名、索引类型、列集合、是否唯一、是否全文、是否空间、是否向量索引。
- `外键元数据`：父表、子表、列映射、JOIN 方向。
- `分区元数据`：分区类型、分区键、子分区类型、子分区键、分区名集合。
- `向量元数据`：维度、距离度量、索引算法、是否可用于当前环境向量距离函数。

类型族至少包括：整数、浮点、DECIMAL、布尔、日期时间、字符串、二进制、枚举、集合、JSON、空间、向量。

## SELECT 算子覆盖矩阵

实现顺序是先完整登记覆盖项，再逐项实现生成器。覆盖矩阵作为运行时可查询的注册表存在，前端可展示已实现、未实现和近期命中的覆盖项。

### 查询结构

- `WITH`
- `WITH RECURSIVE`
- `SELECT ALL`
- `SELECT DISTINCT`
- `SELECT DISTINCTROW`
- `HIGH_PRIORITY`
- `STRAIGHT_JOIN`
- `SQL_SMALL_RESULT`
- `SQL_BIG_RESULT`
- `SQL_BUFFER_RESULT`
- `SQL_NO_CACHE`
- `SQL_CALC_FOUND_ROWS`
- select list
- `FROM`
- 显式 `PARTITION`
- `WHERE`
- `GROUP BY`
- `GROUP BY ... WITH ROLLUP`
- `HAVING`
- `WINDOW`
- `ORDER BY ASC`
- `ORDER BY DESC`
- `ORDER BY ... WITH ROLLUP`
- `LIMIT`
- `OFFSET`
- `FOR UPDATE`
- `FOR SHARE`
- `NOWAIT`
- `SKIP LOCKED`
- `LOCK IN SHARE MODE`
- `UNION`
- `UNION ALL`
- `INTERSECT`
- `INTERSECT ALL`
- `EXCEPT`
- `EXCEPT ALL`
- 子查询
- 相关子查询
- 派生表
- 括号查询表达式
- `TABLE` 查询表达式
- `VALUES` 查询表达式

### JOIN 结构

- `INNER JOIN`
- `LEFT JOIN`
- `RIGHT JOIN`
- `CROSS JOIN`
- `NATURAL JOIN`
- `STRAIGHT_JOIN`
- `JOIN ... ON`
- `JOIN ... USING`
- 多表 JOIN 链
- 外键路径 JOIN
- 非外键随机列 JOIN

### 表达式运算符

算术运算：

- `+`
- `-`
- 一元负号
- `*`
- `/`
- `DIV`
- `%`
- `MOD`

比较运算：

- `=`
- `<=>`
- `<>`
- `!=`
- `>`
- `>=`
- `<`
- `<=`
- `BETWEEN ... AND ...`
- `NOT BETWEEN ... AND ...`
- `IN`
- `NOT IN`
- `EXISTS`
- `NOT EXISTS`
- `IS NULL`
- `IS NOT NULL`
- `IS TRUE`
- `IS NOT TRUE`
- `IS FALSE`
- `IS NOT FALSE`
- `IS UNKNOWN`
- `IS NOT UNKNOWN`

逻辑运算：

- `AND`
- `&&`
- `OR`
- `||`
- `XOR`
- `NOT`
- `!`

位运算：

- `&`
- `|`
- `^`
- `~`
- `<<`
- `>>`
- `BIT_COUNT()`

字符串匹配与正则：

- `LIKE`
- `NOT LIKE`
- `LIKE ... ESCAPE`
- `REGEXP`
- `NOT REGEXP`
- `RLIKE`
- `NOT RLIKE`

JSON 运算：

- `column->path`
- `column->>path`
- `MEMBER OF()`

类型转换：

- `BINARY expr`
- `CAST()`
- `CONVERT()`
- `CAST(... AT TIME ZONE '+00:00' AS DATETIME)`

控制流：

- `CASE value WHEN ... THEN ... ELSE ... END`
- `CASE WHEN ... THEN ... ELSE ... END`
- `IF()`
- `IFNULL()`
- `NULLIF()`

### 函数池

函数池按类型族和返回类型注册，生成表达式时只从兼容输入类型中选取：

- 聚合函数：`AVG()`、`COUNT()`、`COUNT(DISTINCT)`、`SUM()`、`MIN()`、`MAX()`、`GROUP_CONCAT()`、`BIT_AND()`、`BIT_OR()`、`BIT_XOR()`、`JSON_ARRAYAGG()`、`JSON_OBJECTAGG()`、`STD()`、`STDDEV()`、`STDDEV_POP()`、`STDDEV_SAMP()`、`VAR_POP()`、`VAR_SAMP()`、`VARIANCE()`。
- 窗口函数：聚合函数的 `OVER` 形式，以及 `ROW_NUMBER()`、`RANK()`、`DENSE_RANK()`、`PERCENT_RANK()`、`CUME_DIST()`、`NTILE()`、`LAG()`、`LEAD()`、`FIRST_VALUE()`、`LAST_VALUE()`、`NTH_VALUE()`。
- 数学函数：绝对值、三角函数、对数、幂、随机数、舍入、取整等 MySQL 8.0 内置数学函数。
- 字符串函数：长度、拼接、截取、替换、大小写、查找、编码转换、格式化等。
- 日期时间函数：当前时间、时间提取、日期加减、格式化、时间戳转换等。
- JSON 函数：创建、搜索、修改、属性读取、`JSON_TABLE()`、schema 校验和工具函数。
- 空间函数：空间构造、空间关系、空间测量、GeoJSON 转换、空间聚合。
- 全文检索函数：`MATCH() AGAINST()` 自然语言和布尔模式。
- 加密压缩函数：哈希、AES、压缩和解压函数。
- 信息函数：连接、用户、数据库、版本、last insert id 等上下文函数。
- 向量函数：`VEC_FROMTEXT()`、`VEC_TOTEXT()`、`VEC_DISTANCE_COSINE(v1, v2)`、`VEC_DISTANCE_EUCLIDEAN(v1, v2)`；当前环境不生成 `DOT` 距离和带第三个 metric 参数的 `DISTANCE()`。

## SQL 生成器设计

SQL 生成器采用类型感知递归 AST。

### 核心对象

- `生成上下文`：当前任务、随机源、可见表、可见列、CTE、别名、覆盖目标。
- `表达式节点`：列引用、字面量、函数调用、二元运算、一元运算、谓词、CASE、子查询。
- `查询节点`：select list、from item、join tree、where、group by、having、window、order by、limit、locking clause。
- `覆盖注册表`：记录所有算子和函数的实现状态、命中次数和最近生成样例。
- `类型规则`：定义输入类型、返回类型、可隐式转换规则和失败回退策略。

### 生成流程

1. 随机选择一个或多个基表。
2. 根据表元数据构造可见列集合。
3. 随机决定是否生成 `WITH` 或 `WITH RECURSIVE`。
4. 随机生成主查询结构。
5. 在 select list、where、having、order by、join condition 等位置递归生成表达式。
6. 随机注入集合操作、子查询、派生表和窗口函数。
7. 渲染为 SQL 字符串。
8. 记录本条 SQL 命中的算子覆盖项。

### 随机深度策略

用户要求查询深度不控制，因此不使用固定最大深度限制覆盖。实现上保留生成器自保护：

- 单条 SQL 最大生成耗时。
- 单条 SQL 最大字符数。
- 单个 AST 节点生成失败后的局部回退。
- 递归生成时使用概率衰减，避免生成器无法返回。

这些保护只防止工具自身卡死，不用于限制 SQL 语法覆盖。

## 执行与任务模型

### 任务定义

一个任务绑定一个 MySQL 节点。任务字段包括：

- 任务 ID
- 节点名称
- 数据库地址和端口
- 用户名和认证配置
- 数据库名
- 是否使用跳板机
- 跳板机配置
- 当前阶段
- 启动时间
- SQL 执行总数
- 成功数
- 普通错误数
- lost connection 去重事件数
- 最近 SQL 日志位置

### 任务阶段

任务阶段固定为：

1. 连接实例。
2. 创建基表并插入数据，每张表 10 行。
3. 长时间执行查询 SQL。

每次任务启动随机 seed。工具不保存复现 SQL 包，但保存执行过的 SQL 日志。

### 跳板机模式

任务创建时可以选择跳板机配置。跳板机配置包含：

- 配置名
- SSH host
- SSH port
- SSH 用户
- SSH key 或认证方式
- 本地端口分配策略
- 目标数据库 host 和 port

任务启动后建立本地隧道。该任务后续所有数据库连接全部复用这个跳板机和隧道。任务停止后释放隧道。

### lost connection 恢复

执行查询时识别以下异常为 lost connection：

- `Lost connection to MySQL server during query`
- `MySQL server has gone away`
- 连接 EOF
- socket 断开
- 驱动层等价连接中断异常

发生 lost connection 后：

1. 当前任务进入恢复检测状态。
2. 大屏展示告警状态。
3. 写入去重后的 lost connection 事件。
4. 每 1 分钟检测数据库状态。
5. 数据库恢复后继续执行 SELECT SQL。
6. 不重新创建基表。
7. 不重新插入数据。

同一节点 10 分钟内只记录第一次 lost connection 事件。窗口内重复异常不写入事件列表，也不增加大屏 lost connection 次数。

## 日志与监控

### SQL 日志

SQL 日志使用 JSONL，按日期和任务拆分，字段如下：

- 日期时间
- 任务 ID
- 节点名称
- 执行状态
- SQL 文本

执行状态包括：

- 成功
- 普通错误
- lost connection

### lost connection 事件日志

事件日志字段如下：

- 日期时间
- 任务 ID
- 节点名称
- 跳板机配置名
- 目标实例
- 触发 SQL
- 10 分钟去重窗口起始时间

### 指标存储

SQLite 保存指标和事件索引，供前端查询。JSONL 保存完整 SQL 文本，便于人工直接查看。前端显示的实时指标通过 API 和 WebSocket 或 SSE 推送。

## 后端 API

后端使用 FastAPI。主要接口如下：

- `GET /api/health`：健康检查。
- `GET /api/tasks`：任务列表。
- `POST /api/tasks`：创建并启动任务。
- `POST /api/tasks/{task_id}/stop`：停止任务。
- `GET /api/tasks/{task_id}`：任务详情。
- `GET /api/tasks/{task_id}/lost-connections`：任务 lost connection 事件。
- `GET /api/tasks/{task_id}/sql-logs`：任务 SQL 日志。
- `GET /api/metrics/summary`：全局指标。
- `GET /api/coverage`：算子覆盖矩阵。
- `GET /api/jump-hosts`：跳板机配置列表。
- `POST /api/jump-hosts`：保存跳板机配置。
- `GET /api/events/stream`：实时事件流。

## 前端设计

前端采用 Vite、React、Ant Design 和 ECharts。界面全部中文，保留 SQL、PolarDB、lost connection 等官方术语。

已确认的页面方向如下：

- 深色运维控制台风格。
- 左侧导航：运行监控、任务面板、SQL 日志、覆盖统计、系统设置。
- 顶部指标：运行任务、已执行 SQL、lost connection、集群速率。
- 中间任务卡片：展示节点、连接方式、三阶段任务流、状态和 lost connection 去重次数。
- 右侧操作面板：创建任务、选择跳板机、配置目标数据库并启动。
- 任务卡片支持点击展开。
- 展开区只展示跳板机信息和最近 lost connection 事件。
- lost connection 事件区域固定高度并支持滑动。

任务卡片阶段展示：

1. 连接实例。
2. 准备基表。
3. 执行 SQL。

## 测试策略

### 单元测试

- DDL 元数据解析。
- 类型族识别。
- 算子注册表完整性。
- 表达式类型匹配。
- SQL AST 渲染。
- CTE、JOIN、子查询、集合操作生成。
- 向量表达式生成。
- lost connection 10 分钟去重。
- 任务状态机。
- SQL 日志写入。

### 集成测试

- 使用可配置 MySQL/PolarDB 连接执行初始化和查询。
- 没有数据库时使用 mock 连接模拟成功、普通错误和 lost connection。
- 模拟 lost connection 后每 1 分钟恢复检测。
- 模拟跳板机隧道生命周期。

### 前端验证

- 运行监控页面截图检查。
- 任务卡片展开检查。
- lost connection 事件滚动区域检查。
- 中文文案检查。
- 核心布局在桌面宽度下不重叠。

## 实现顺序

1. 初始化 Python 工程、中文 README、配置样例和基础测试框架。
2. 建立 DDL 元数据解析和 `sql_base_tables/` 加载器。
3. 补齐向量表、二级分区表、外键图表的 DDL。
4. 建立 SELECT 算子覆盖矩阵。
5. 实现类型感知表达式生成器。
6. 实现 SELECT AST 生成和渲染。
7. 实现任务执行器和 SQL 日志。
8. 实现 lost connection 识别、10 分钟去重和 1 分钟恢复检测。
9. 实现跳板机连接模式。
10. 实现 FastAPI 接口和实时事件流。
11. 实现中文前端大屏。
12. 完成测试和端到端验证。

## 参考资料

- MySQL 8.0 SELECT 语法：https://dev.mysql.com/doc/refman/8.0/en/select.html
- MySQL 8.0 内置函数和运算符：https://dev.mysql.com/doc/refman/8.0/en/built-in-function-reference.html
- MySQL 8.0 WITH CTE：https://dev.mysql.com/doc/refman/8.0/en/with.html
- MySQL 8.0 分区限制：https://dev.mysql.com/doc/refman/8.0/en/partitioning-limitations-storage-engines.html
- PolarDB MySQL 向量检索：https://www.alibabacloud.com/help/en/polardb/polardb-for-mysql/vector-index-usage
