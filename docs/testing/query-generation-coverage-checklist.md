# 查询 SQL 生成覆盖清单

本清单定义 `select-fuzz` 的查询生成范围。它以 MySQL 8.0.41、只读单语句
query expression 为边界；correctness 生产路径默认由版本化 `.grammar.yy` 文法驱动，
语义绑定器接收表名、列名、列类型，以及经过安全过滤的可见索引名和分区名。覆盖必须
同时具备可达文法、真实作用域绑定、安全校验、自动化测试，并在可用时取得精确
MySQL 8.0.41 见证。仅存在 catalog 行或固定模板不算覆盖。

状态：`[x]` 已实现并有本地测试，`[~]` 已部分实现或仍需扩大精确 MySQL 见证，
`[ ]` 尚未闭环，`[-]` 明确排除。

## 2026-07-16 当前任务总览

### 已完成并取得本地证据

- [x] P0：correctness 与一分钟 socket soak 均只接入 `GrammarQueryGenerator`；旧 typed-AST
  renderer、batch planner 及其运行时 fallback 已物理删除。
- [x] P0：`excluded_families` 已贯穿文法选择、列/表/集合签名、`*`、`TABLE`、USING 和
  NATURAL JOIN；默认 correctness scope 不会隐式带入 JSON、SPATIAL、FULLTEXT。
- [x] P0：optimizer hint fallback 改为带真实别名的 `NO_ICP(alias)`；带 hint 的候选若
  `EXPLAIN` 返回 hint warning 会在三节点执行前拒绝。
- [x] P0：105/105 个 production 从真实 root 加 semantic edge 静态可达；alternative ID
  改为与源码行号无关的 `grammar_alt:v1:<production>:<hash>`，并记录相邻 set operator
  的有序 pair tag。
- [x] P1：安全可见 index、partition 元数据进入绑定器；显式 `PARTITION` 与
  `USE/FORCE/IGNORE INDEX`（default/JOIN/ORDER BY/GROUP BY）按 MySQL 表因子顺序生成。
- [x] P1：多 SELECT modifier、多列表达式/位置 GROUP BY、更长 ORDER BY、多列 USING、
  四表 join、相关 LEFT/RIGHT LATERAL 均已接入；RIGHT LATERAL 不会走 STRAIGHT_JOIN。
- [x] P1：derived table/CTE 可使用完整 query expression 并显式固定输出列；pair recursive
  CTE、四操作数 set chain，以及 7×7=49 个 set operator 有序 pair 均有定向测试。
- [x] P1：`OVER()`、多列表达式窗口、命名窗口继承/修改、numeric/temporal bounded
  RANGE、安全 ROWS/RANGE frame 已接入；位置敏感函数强制确定性总序。
- [x] P1：CAST/CONVERT 安全类型配对、全部简单/复合 INTERVAL 单位、确定性
  GROUP_CONCAT/JSON_ARRAYAGG/JSON_OBJECTAGG 已接入；NCHAR 与受 sql_mode 影响的 REAL
  不进入 correctness 无 warning lane。
- [x] 函数注册表的 335 个 signature/NULL lane 已在 normal、boundary、special 三个
  profile 下逐个在三套 MySQL 8.0.41 上执行，共 1005 条 SQL；唯一 warning 为
  `encoding_sha2_2_null_1` 的 1583。
- [x] 三套 socket 均确认是 MySQL 8.0.41；60 个 P1 grammar 见证（11 个结构族 + 49 个
  set pair）全部通过 EXPLAIN、执行和三节点结果/警告一致性检查。
- [x] 聚焦单元/服务测试为 120 passed；最新一分钟运行完成 20 轮、1965 条成功比较、
  0 finding，命中 698 个稳定 alternative ID 和随机 47/49 个 set pair。
- [x] 一分钟运行仅未命中默认 scope 明确排除的 `fulltext_query`、`json_scalar_function`、
  `spatial_scalar_function`；partition 在本次随机 schema 中未命中，但定向三节点见证已通过。
- [x] 三节点一致运行时错误的事件和 worker 记录现在直接保存 `query_sql` 与
  `observed_error_identities`，后续归因不再依赖 SQL 顺序反推。

最新一分钟证据目录：
`artifacts/latest-p0-p1-one-minute-20260716/`。该轮共有 47 个准入/运行时拒绝和 2 个
resource-limit；已复放的运行时错误类别为 1038（sort memory）、1690（unsigned
overflow）、3513（binary bit operand length）和 3854（binary-to-utf8mb4 conversion），
三节点错误身份一致，因此未形成差分 finding。

### 本轮新增闭环

- [x] frame grammar 已将全部当前合法 numeric/temporal frame-bound 组合拆成 25 条定向
  SQL，在三套 MySQL 8.0.41 上逐条 EXPLAIN、执行并比较；GROUPS/IGNORE NULLS/FROM LAST/
  EXCLUDE 继续由生成阶段 fail-closed 排除。
- [x] optimizer hint 已形成正向/负向矩阵：11 条正向 SQL 在三节点执行通过，4 类无真实
  alias/index/derived 条件在生成阶段拒绝，hint warning 不进入 valid lane。
- [x] 函数注册表已在 normal、boundary、special 三个值域 profile 下逐条实际生成并在三套
  MySQL 8.0.41 上执行 335 个 witness，共 1005 条 SQL；唯一 warning 为 registry 明确登记的
  `encoding_sha2_2_null_1` / 1583。

### 待完成

- [x] 已以约 3 分钟短批次替代 30 分钟长跑：2 轮、200 条查询、80.6 秒完成、0 个未
  归因 finding；固定命令和 artifact 见实现验收计划 12.1。
- [x] NOT EXISTS/NOT IN 已补齐空/单/多行、outer/inner/both nullable 和嵌套矩阵，12 条
  SQL 在三节点通过，证据位于 `artifacts/latest-grammar-matrix-20260716/`。
- [x] 本轮中断长跑、旧 smoke、旧生成器 SQL 和历史诊断产物均已移入
  `artifacts/archive/query-generation-acceptance-20260716/legacy/`；根目录只保留当前最终
  evidence 和必要的 P0/P1 witness。
- [x] P0/P1 修改已完成测试，按主题拆分 commit 并合并回 `main`；临时 codex 分支已删除。

## 2026-07-16 文法生成迁移快照

- correctness 生产路径使用 `catalog/mysql-8.0.41-select.grammar.yy`；重复 alternative
  直接形成权重，修改文法文件即可调整结构和概率。
- 关系先绑定、表达式后展开；每层 query block 维护独立 symbol table，普通 derived
  table 隔离外层，LATERAL/相关子查询显式继承外层可见列，CTE/derived 输出列注册后
  才能被父层引用。
- 列类型是软约束：默认 80% 选同类型族、20% 优先跨类型族，可通过配置调整；查询生成器
  可见经过过滤的普通索引名和分区名，但仍不依赖 index part、主键/唯一性证明或物理 plan。
- 每条候选先在 baseline 执行普通 `EXPLAIN`，10 秒内成功且返回非空计划才发往三节点；
  EXPLAIN 失败不记录 SQL/plan。`queries_per_round` 只统计三节点执行成功且比较通过的 SQL。
- 旧类型化 AST、coverage-debt 和 negative lane 暂留给 performance、验证工具及迁移回归，
  不再是 correctness 生产查询入口，稳定后删除。

## 2026-07-14 精确快照

- 23/23 个官方来源、96 个 locator 已验证；catalog 的 64 个 variant 全部
  generator-supported 且 evidence-ready。
- 默认生产范围调度 51 个 target；明确排除 13 个 JSON/FULLTEXT/SPATIAL target。
- 19 个 target 细分为 525 个持久 leaf-debt 单元，其余 32 个按 target 级记账。
- 确定性函数注册表包含 133 个 signature、202 个 NULL 参数位置，共 335 个函数见证；
  `function_deterministic_scalar` 另有 49 个谓词/NULL 运算叶。
- free-random 从 6 类扩展到 13 类安全组合形态，并保留受总序证明约束的 top-N 路径。

因此当前结论是：生成器已经覆盖一个丰富、闭合、安全且可审计的 MySQL 8.0.41
只读查询子集，但不等于覆盖 MySQL 的全部查询语法与全部内建函数。

## 明确排除

- [x] JSON 函数、`JSON_TABLE` 和 JSON 表达式已进入文法；默认 correctness scope 排除 JSON，
  JSON 多值索引名也不作为生成器输入。
- [x] `MATCH ... AGAINST` 与确定性空间函数已进入文法；默认 correctness scope 排除
  FULLTEXT/SPATIAL，启用时仍由 baseline EXPLAIN 做最终准入。
- [x] 新文法绑定真实 partition 名及经过过滤的可见普通 index 名，不伪造名字；显式
  partition 与 table-level index hint 已有定向 MySQL 8.0.41 见证。
- [-] DDL/DML、锁定读、外部文件、存储函数/UDF、用户变量及多语句。
- [-] 随机、当前时间、连接/会话状态、等待/锁等非确定性函数。

## 类型、值与数据形态

- [x] 整数：全部 signed/unsigned 类型的 `min/min+1/-1/0/1/max-1/max`。
- [x] `BIT(1..64)`：`0/1/max-1/max`。
- [x] `DECIMAL(p,s)`：缩放后的正负端点、相邻端点、零和一。
- [x] `FLOAT/DOUBLE`：正负零、正负一、最小正规值、最大有限值；禁止 NaN/Inf。
- [x] 字符/二进制：空、单单位、声明/预算最大容量、NUL、引号、反斜线、控制字符、
  多字节字符和尾随空格。
- [x] 时间：DATE/TIME/DATETIME/TIMESTAMP/YEAR；fsp 0/6；DATETIME 和 TIMESTAMP
  上限不越界且覆盖 `.499999`。
- [x] ENUM/SET 成员和组合值。
- [x] Python seeded random：同 seed 字节稳定，不同 seed 对窄值域仍可变化；不使用 SQL `RAND()`。
- [x] NULL：全 NULL、固定混合 NULL、nullable unique 多 NULL、连接键 NULL、聚合全 NULL。
- [x] 数据规模：空表、单行、多行、重复低基数、高热点偏斜均可从生产轮次到达。
- [x] schema 声明边界与数据值边界定向联动，并记录运行时场景 tag；`*`、qualified
  `*` 和 `TABLE` 会展开为实际物理列见证。

## SELECT 与 query expression

- [x] 无 FROM 标量、普通表投影、表达式投影、别名、`DISTINCT`。
- [x] WHERE、多列表达式/位置 GROUP BY、HAVING、WINDOW、更长最终 ORDER BY、ASC/DESC。
- [x] 合法 SELECT modifier 有序栈；ALL/DISTINCT 和 SQL_NO_CACHE/SQL_CALC_FOUND_ROWS
  分别走互斥安全 lane。
- [x] LIMIT、OFFSET、LIMIT 0、无符号 BIGINT 边界与确定性总序证明。
- [x] 单层/多层 parenthesized query expression、分支局部 ORDER BY + LIMIT。
- [x] `SELECT`、`TABLE`、`VALUES` query primary。
- [x] `*`/qualified `*`、ORDER BY alias/expression、SELECT modifier 的叶级 tag。

## 表引用与 JOIN

- [x] INNER/CROSS/STRAIGHT/LEFT/RIGHT/NATURAL LEFT/NATURAL RIGHT 的 ON/无条件形态。
- [x] 逗号连接、多列 USING、NATURAL INNER、四表嵌套 join tree。
- [x] 相关 LEFT/RIGHT LATERAL；RIGHT LATERAL 先绑定右表再反向渲染，相关引用可见且
  lateral join type 不含 STRAIGHT_JOIN。
- [x] `USE/FORCE/IGNORE INDEX` × default/JOIN/ORDER BY/GROUP BY 的 12 个表级 hint 叶。
- [x] 显式 partition、临时表、外键图和普通 InnoDB 场景。

## 表达式、运算符与谓词

- [x] `= <> < <= > >=`、`+ - * %`、AND/OR/NOT、LIKE、IS NULL/IS NOT NULL。
- [x] `<=>`、`/`、`DIV`、位运算、移位、XOR、一元正负。
- [x] BETWEEN/NOT BETWEEN、IN-list/NOT IN-list、LIKE/NOT LIKE ESCAPE、
  REGEXP/NOT REGEXP 和正反真值谓词。
- [x] simple/searched CASE、CAST、row constructor/row comparison。
- [x] NULL 作为比较、算术、位、逻辑、LIKE/REGEXP、BETWEEN、IN 的左/右/双方输入，
  使用 23 个精确定向真值矩阵而非随机碰撞。

## 子查询、derived table 与 CTE

- [x] scalar/row/table 子查询、EXISTS、IN、ANY、ALL、相关与非相关形态。
- [x] NOT EXISTS/NOT IN、空/单/多行、nullable outer/inner/both、嵌套子查询；12 条当前
  grammar matrix SQL 已在三节点执行并保存 artifact。
- [x] 普通/显式列 derived table、相关 LATERAL derived table；完整 set query expression
  作为 body 时强制显式稳定输出列。
- [x] 单个非递归 CTE、带终止条件的 recursive UNION ALL CTE。
- [x] 多个独立/依赖 CTE、CTE 复用、显式列与 recursive UNION ALL/DISTINCT，以及
  `(n,total)` pair recursive CTE。
- [-] recursive query-expression LIMIT 仅在不带 ORDER BY 时可用；MySQL 8.0.41 对递归
  UNION 上的 ORDER BY 返回 `1235/42000`，因此无法满足本项目的确定性 LIMIT 总序契约。

## 集合运算

- [x] UNION DISTINCT/ALL、INTERSECT、EXCEPT、等操作链、分支局部 Top-N。
- [x] SELECT/TABLE/VALUES 分支和括号改变结合顺序。
- [x] INTERSECT ALL、EXCEPT ALL，以及 numeric/text/binary/temporal 四类跨类型集合叶。
- [x] 混合集合优先级、括号反转和 7×7=49 个 UNION/UNION ALL/UNION DISTINCT/
  INTERSECT/INTERSECT ALL/EXCEPT/EXCEPT ALL 有序运算符对均有定向三节点见证。
- [x] 负向列数不一致使用精确 `1222/21000` 契约。

## 聚合与窗口

- [x] COUNT/MIN/MAX、global/grouped aggregate、HAVING、WITH ROLLUP。
- [x] SUM/AVG、DISTINCT aggregate、bit/statistical aggregate、GROUPING 和全 NULL 聚合。
- [x] inline/named window、`OVER()`、ROW_NUMBER、SUM window、确定性唯一排序、ROWS frame。
- [x] ranking/navigation/value window、多表达式 PARTITION/ORDER、命名窗口继承/修改、
  numeric/temporal bounded RANGE、UNBOUNDED/CURRENT/有界边界。
- [x] GROUP_CONCAT 使用同表达式 DISTINCT + ORDER BY 并限制输出；JSON_ARRAYAGG 使用
  顺序无关常量，JSON_OBJECTAGG 使用同 key/value binding，避免重复 key last-wins 漂移。
- [x] 已枚举并执行全部当前合法 frame-bound 组合（numeric/temporal 共 25 条）；
  `GROUPS`、`IGNORE NULLS`、`NTH_VALUE ... FROM LAST` 和 window `EXCLUDE` 在 MySQL 8.0.41
  均为 `1235/42000`，因此保留 fail-closed 排除而不放入 valid lane。

## 确定性函数

- [x] 闭合 allowlist、合法 arity、禁止 schema-qualified stored/UDF 调用。
- [x] ABS/COALESCE/CONCAT/LOWER/OCTET_LENGTH/COUNT/MIN/MAX。
- [x] 数学、字符串/二进制、显式输入时间、控制流、编码/哈希/IP 等 133-signature
  确定性安全注册表；这不是 MySQL 全部 built-in 的清单。
- [x] 每个 signature 及全部声明 NULL 位置共有 335 个三节点见证；334 个无 warning，
  `encoding_sha2_2_null_1` 精确断言 Warning 1583。
- [x] 335 个基础/NULL witness 已扩展为 normal/boundary/special 三个 profile，共 1005 条
  当前 grammar SQL；每条均有三节点结果、元数据和 warning 证据，error/warning contract
  已由 registry 精确登记。

## 索引、提示与回归种子

- [x] BTREE prefix/descending/functional index 查询形态。
- [x] join-order、index-level、derived pushdown optimizer hints。
- [x] 表级 index hint 的 12 个 action/scope 叶。
- [x] optimizer hint 已覆盖 JOIN_ORDER、INDEX、DERIVED_CONDITION_PUSHDOWN、NO_RANGE
  及合法 NO_ICP fallback；正向 11 条三节点 SQL 与负向 4 类生成拒绝均通过，EXPLAIN hint
  warning 会拒绝。
- [x] 已登记的 MySQL 8.0.41 parser/optimizer 回归种子；物理 plan 命中与语法命中分开计数。

## 报错、归因与覆盖记账

- [x] correctness 文法生产路径不生成 negative SQL；迁移期旧生成器仍保留精确
  `errno + SQLSTATE` 契约供回归测试使用。
- [x] 文法候选三节点相同运行时错误跳过且不计成功；部分节点错误、错误身份不同或
  成功/错误混合仍记录 finding。
- [x] 迁移期预期负例成功或返回其他错误仍记 generator finding；差分 mismatch 优先。
- [x] timeout/内部结果上限记 resource limit；infra error 不进入语义 oracle。
- [x] 只有 valid + 三节点成功 + oracle match 才增加 target 和 leaf tag 覆盖。
- [x] generator finding 可 replay，expected-negative 事件保存 expected/observed error identity。
- [x] 动态文法的三节点一致运行时错误也保存 query SQL 和三份 observed error identity。
- [x] NULL、函数注册表、数据场景、错误契约及 91 个扩展语义变体均取得三套精确
  MySQL 8.0.41 本地见证。
- [x] 一分钟最新文法随机差分完成：20 轮、1965 条成功比较、0 finding；随机命中
  698 个 stable alternative 和 47/49 个 set pair。
- [x] 3 分钟随机差分短批次已完成：2 轮、200 条查询、80.6 秒、0 未归因 finding；长跑
  不再是本轮门槛，低概率功能由定向矩阵补齐。
