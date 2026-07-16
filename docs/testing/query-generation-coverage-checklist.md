# 查询 SQL 生成覆盖清单

本清单定义 `select-fuzz` 的查询生成范围。它以 MySQL 8.0.41、只读单语句
query expression 为边界；correctness 生产路径默认由版本化 `.grammar.yy` 文法驱动，
语义绑定器只接收表名、列名和列类型。覆盖必须同时具备可达文法、真实作用域绑定、
安全校验、自动化测试，并在可用时取得精确 MySQL 8.0.41 见证。仅存在 catalog 行或
固定模板不算覆盖。

状态：`[x]` 已实现并有本地测试，`[~]` 本轮正在补齐/等待精确 MySQL 见证，
`[ ]` 尚未闭环，`[-]` 明确排除。

## 2026-07-16 文法生成迁移快照

- correctness 生产路径使用 `catalog/mysql-8.0.41-select.grammar.yy`；重复 alternative
  直接形成权重，修改文法文件即可调整结构和概率。
- 关系先绑定、表达式后展开；每层 query block 维护独立 symbol table，普通 derived
  table 隔离外层，LATERAL/相关子查询显式继承外层可见列，CTE/derived 输出列注册后
  才能被父层引用。
- 列类型是软约束：默认 80% 选同类型族、20% 优先跨类型族，可通过配置调整；索引、
  主键、唯一键不进入查询生成器输入。
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

- [x] JSON 函数、`JSON_TABLE` 和 JSON 表达式已进入文法；JSON 多值索引名不作为生成器输入。
- [x] `MATCH ... AGAINST` 与确定性空间函数已进入文法；候选是否匹配实际全文/空间索引由
  baseline EXPLAIN 准入，不读取索引元数据。
- [~] 显式 partition 名、index-level/table-level index hint 需要分区或索引名，仍仅由迁移期
  旧生成器精确覆盖；最小 schema snapshot 的新文法生成器不伪造这些名字。
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
- [x] WHERE、GROUP BY、HAVING、WINDOW、最终 ORDER BY、ASC/DESC。
- [x] LIMIT、OFFSET、LIMIT 0、无符号 BIGINT 边界与确定性总序证明。
- [x] 单层/多层 parenthesized query expression、分支局部 ORDER BY + LIMIT。
- [x] `SELECT`、`TABLE`、`VALUES` query primary。
- [x] `*`/qualified `*`、ORDER BY alias/expression、SELECT modifier 的叶级 tag。

## 表引用与 JOIN

- [x] INNER/CROSS/STRAIGHT/LEFT/RIGHT/NATURAL LEFT/NATURAL RIGHT 的 ON/无条件形态。
- [x] 逗号连接、USING、NATURAL INNER、三表嵌套 join tree。
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
- [~] NOT EXISTS/NOT IN、空/单/多行、nullable outer/inner/both、嵌套子查询。
- [x] 普通/显式列 derived table、相关 LATERAL derived table。
- [x] 单个非递归 CTE、带终止条件的 recursive UNION ALL CTE。
- [x] 多个独立/依赖 CTE、CTE 复用、显式列与 recursive UNION ALL/DISTINCT。
- [-] recursive query-expression LIMIT 仅在不带 ORDER BY 时可用；MySQL 8.0.41 对递归
  UNION 上的 ORDER BY 返回 `1235/42000`，因此无法满足本项目的确定性 LIMIT 总序契约。

## 集合运算

- [x] UNION DISTINCT/ALL、INTERSECT、EXCEPT、等操作链、分支局部 Top-N。
- [x] SELECT/TABLE/VALUES 分支和括号改变结合顺序。
- [x] INTERSECT ALL、EXCEPT ALL，以及 numeric/text/binary/temporal 四类跨类型集合叶。
- [~] 混合集合优先级已覆盖 UNION→INTERSECT、EXCEPT→INTERSECT、UNION→EXCEPT 及
  括号反转，但尚未枚举全部有序运算符对。
- [x] 负向列数不一致使用精确 `1222/21000` 契约。

## 聚合与窗口

- [x] COUNT/MIN/MAX、global/grouped aggregate、HAVING、WITH ROLLUP。
- [x] SUM/AVG、DISTINCT aggregate、bit/statistical aggregate、GROUPING 和全 NULL 聚合。
- [x] inline/named window、ROW_NUMBER、SUM window、确定性唯一排序、ROWS frame。
- [x] ranking/navigation/value window、ROWS/RANGE frame、UNBOUNDED/CURRENT 边界。
- [~] 尚未枚举全部合法 frame-bound 组合；`GROUPS`、`IGNORE NULLS`、
  `NTH_VALUE ... FROM LAST` 和 window `EXCLUDE` 在 MySQL 8.0.41 均为 `1235/42000`，
  因而 fail-closed 排除而非放入 valid lane。

## 确定性函数

- [x] 闭合 allowlist、合法 arity、禁止 schema-qualified stored/UDF 调用。
- [x] ABS/COALESCE/CONCAT/LOWER/OCTET_LENGTH/COUNT/MIN/MAX。
- [x] 数学、字符串/二进制、显式输入时间、控制流、编码/哈希/IP 等 133-signature
  确定性安全注册表；这不是 MySQL 全部 built-in 的清单。
- [~] 每个 signature 均有参数 recipe 和全部声明 NULL 位置的三节点见证；完整值域及每个
  signature 的 warning/error 契约尚未穷举。

## 索引、提示与回归种子

- [x] BTREE prefix/descending/functional index 查询形态。
- [x] join-order、index-level、derived pushdown optimizer hints。
- [x] 表级 index hint 的 12 个 action/scope 叶。
- [~] optimizer hint 已覆盖 JOIN_ORDER、INDEX、DERIVED_CONDITION_PUSHDOWN 和 NO_RANGE
  回归种子，但不是完整正反矩阵。
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
- [x] NULL、函数注册表、数据场景、错误契约及 91 个扩展语义变体均取得三套精确
  MySQL 8.0.41 本地见证。
- [ ] 30 分钟随机差分运行后，逐个归因全部错误 fingerprint，并将可修项回灌生成器。
