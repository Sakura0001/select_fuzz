# MySQL 8.0.41 SELECT 查询能力与项目覆盖矩阵（历史快照）

> 审计日期：2026-07-17；目标：MySQL Community Server 8.0.41；项目：select-fuzz。此报告记录当时的 8.0.41 覆盖，不描述当前 canonical grammar。

## 结论

本报告记录一个覆盖面较宽、可绑定真实 schema、默认只读且强调确定性的 MySQL 8.0.41 SELECT 子集，但不是完整语法/函数实现。下列 grammar 计数、SHA 和覆盖结论均对应当时的 8.0.41 快照；当前生产 grammar 已切换为 MySQL 8.0.22，且明确不生成 INTERSECT、EXCEPT、旧 SELECT modifier 或固定 `utf8mb4_0900_ai_ci` 谓词。

| 指标 | 历史值 |
| --- | ---: |
| 当前 SELECT 文法 production | 105 |
| 当前 SELECT 文法 alternative | 1026 |
| 文法 SHA-256 | `e81f1e030db444f615a7bd705f68a3d4d777dcd7c47e29a15d5415bebc89a1a7` |
| 官方来源记录 | 23 |
| catalog feature / variant | 19 / 64 |
| 默认调度 target / 显式排除 target | 51 / 13 |
| 确定性函数 signature / 唯一函数名 | 133 / 124 |
| 基础与定向 NULL witness | 335 |
| normal/boundary/special 三 profile witness | 1005 |
| 官方函数/运算符名称或语法项 | 496 |
| 已实现 / 默认可达 | 223 / 204 |

状态口径：

- **实现**：2026-07-17 的 `GrammarQueryGenerator` 能从版本化文法或确定性函数注册表生成该结构，并经过作用域绑定与只读安全校验。
- **默认**：当时在 `DEFAULT_QUERY_SCOPE` 下能够进入 correctness 生产轮次。JSON、FULLTEXT、SPATIAL 即使已有生成器也统一记为默认 ❌。
- 函数表的 ✅ 是**函数名级至少一个安全签名**，不是所有 overload、所有参数组合或所有 SQL mode 的完全覆盖。
- ❌ 同时包含真实缺口和有意排除；有意排除原因在文末集中说明。

## 官方基线与本地证据

- [MySQL 8.0.41 `sql_yacc.yy`](https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy)
- [MySQL 8.0.41 `parse_tree_nodes.h`](https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/parse_tree_nodes.h)
- [MySQL 8.0.41 `item_create.cc`](https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/item_create.cc)
- [MySQL 8.0 SELECT 语法](https://dev.mysql.com/doc/refman/8.0/en/select.html)
- [MySQL 8.0 JOIN / table factor 语法](https://dev.mysql.com/doc/refman/8.0/en/join.html)
- [MySQL 8.0 内建函数与运算符总表](https://dev.mysql.com/doc/refman/8.0/en/built-in-function-reference.html)
- [MySQL Server 函数版本矩阵](https://dev.mysql.com/doc/mysqld-version-reference/en/built-in-functions.html)
- [MySQL 8.0 优化器提示总表](https://dev.mysql.com/doc/refman/8.0/en/optimizer-hints.html)
- 历史版本化文法：`git show 93ee593:catalog/mysql-8.0.41-select.grammar.yy`
- 本地安全函数注册表：[`src/select_fuzz/generation/function_registry.py`](../../src/select_fuzz/generation/function_registry.py)
- 本地默认排除范围：[`src/select_fuzz/generation/query_scope.py`](../../src/select_fuzz/generation/query_scope.py)
- 本地官方来源锁：[`catalog/mysql-8.0.41-query-shapes.yaml`](../../catalog/mysql-8.0.41-query-shapes.yaml)

官方 8.0 手册是滚动页面，因此本历史报告以精确 `mysql-8.0.41` 源码标签为语法基线，并用版本矩阵核对 8.0 系列可用性。函数表中同一官方行的多个 `code` 名称/别名被拆开逐项检查；赋值 `=` 与比较 `=`、二元 `-` 与一元 `-` 保留为不同语义项。

## 当前实现（MySQL 8.0.22）

当前 canonical grammar 为 [`catalog/mysql-8.0.22-select.grammar.yy`](../../catalog/mysql-8.0.22-select.grammar.yy)。它与本历史矩阵的 8.0.41 覆盖结论分开维护。

## 查询表达式、SELECT 主体与子句

| 类别 | MySQL 8.0.41 项 | 实现 | 默认 | 证据或边界 |
| --- | --- | :---: | :---: | --- |
| 查询主干 | `SELECT query specification` | ✅ | ✅ | 普通表查询与无 FROM 标量查询 |
| 查询主干 | `parenthesized query expression` | ✅ | ✅ | 单层、多层、分支局部 ORDER BY/LIMIT |
| 查询主干 | `TABLE query primary` | ✅ | ✅ | 真实表与集合运算分支 |
| 查询主干 | `VALUES ROW(...) query primary` | ✅ | ✅ | 单行、多行与集合运算分支 |
| 集合运算 | `UNION / UNION DISTINCT` | ✅ | ✅ | 二元、链式、混合优先级 |
| 集合运算 | `UNION ALL` | ✅ | ✅ | 含递归 CTE member |
| 集合运算 | `INTERSECT / INTERSECT DISTINCT` | ✅ | ✅ | 8.0.31+ |
| 集合运算 | `INTERSECT ALL` | ✅ | ✅ | 定向类型与 pair 见证 |
| 集合运算 | `EXCEPT / EXCEPT DISTINCT` | ✅ | ✅ | 8.0.31+ |
| 集合运算 | `EXCEPT ALL` | ✅ | ✅ | 定向类型与 pair 见证 |
| 集合运算 | `set precedence and parentheses` | ✅ | ✅ | 7×7 有序 operator pair |
| 集合运算 | `branch-local ORDER BY + LIMIT` | ✅ | ✅ | Top-N operand |
| CTE | `WITH one nonrecursive CTE` | ✅ | ✅ | 显式或推导列名 |
| CTE | `WITH multiple independent CTEs` | ✅ | ✅ | 同一 WITH 列表 |
| CTE | `dependent CTE` | ✅ | ✅ | 后项引用前项 |
| CTE | `CTE reuse` | ✅ | ✅ | 同一 CTE 多次引用 |
| CTE | `WITH RECURSIVE ... UNION ALL` | ✅ | ✅ | 有界终止 |
| CTE | `WITH RECURSIVE ... UNION DISTINCT` | ✅ | ✅ | 有界终止 |
| CTE | `recursive pair state (n,total)` | ✅ | ✅ | 双列递归状态 |
| CTE | `recursive member ORDER BY` | ❌ | ❌ | MySQL 8.0.41 返回 1235/42000，不进入 valid lane |
| 投影 | `select_expr` | ✅ | ✅ | 列、表达式、函数、子查询 |
| 投影 | `*` | ✅ | ✅ | 展开实际物理列见证 |
| 投影 | `table.*` | ✅ | ✅ | qualified star |
| 投影 | `expr AS alias` | ✅ | ✅ | 显式 AS |
| 投影 | `expr alias` | ✅ | ✅ | 隐式别名 |
| SELECT modifier | `ALL` | ✅ | ✅ | 与 DISTINCT 互斥 |
| SELECT modifier | `DISTINCT` | ✅ | ✅ | 普通与聚合 |
| SELECT modifier | `DISTINCTROW` | ✅ | ✅ | 别名形态 |
| SELECT modifier | `HIGH_PRIORITY` | ✅ | ✅ | 合法顺序栈 |
| SELECT modifier | `STRAIGHT_JOIN` | ✅ | ✅ | SELECT modifier 与 join operator 均支持 |
| SELECT modifier | `SQL_SMALL_RESULT` | ✅ | ✅ | 合法顺序栈 |
| SELECT modifier | `SQL_BIG_RESULT` | ✅ | ✅ | 合法顺序栈 |
| SELECT modifier | `SQL_BUFFER_RESULT` | ✅ | ✅ | 合法顺序栈 |
| SELECT modifier | `SQL_NO_CACHE` | ✅ | ✅ | 安全 lane |
| SELECT modifier | `SQL_CALC_FOUND_ROWS` | ✅ | ✅ | 安全 lane；FOUND_ROWS() 本身未覆盖 |
| 子句 | `FROM` | ✅ | ✅ | 真实 schema 绑定 |
| 子句 | `WHERE` | ✅ | ✅ | 布尔谓词树 |
| 子句 | `GROUP BY column` | ✅ | ✅ | 单列与多列 |
| 子句 | `GROUP BY expression` | ✅ | ✅ | 受控表达式 |
| 子句 | `GROUP BY position` | ✅ | ✅ | 位置 1 |
| 子句 | `WITH ROLLUP` | ✅ | ✅ | GROUP BY rollup |
| 子句 | `HAVING` | ✅ | ✅ | 分组列与聚合表达式 |
| 子句 | `WINDOW name AS (...)` | ✅ | ✅ | 命名窗口、继承与修改 |
| 子句 | `ORDER BY column/expression/position` | ✅ | ✅ | 最多 5 项，ASC/DESC |
| 子句 | `ORDER BY ... WITH ROLLUP` | ❌ | ❌ | 当前文法没有 ORDER BY rollup |
| 子句 | `LIMIT row_count` | ✅ | ✅ | 含 LIMIT 0 |
| 子句 | `LIMIT offset,row_count` | ✅ | ✅ | 逗号形式 |
| 子句 | `LIMIT row_count OFFSET offset` | ✅ | ✅ | 关键字形式 |
| 副作用 | `INTO OUTFILE` | ❌ | ❌ | 外部文件副作用，安全门禁止 |
| 副作用 | `INTO DUMPFILE` | ❌ | ❌ | 外部文件副作用，安全门禁止 |
| 副作用 | `INTO user/local variables` | ❌ | ❌ | 变量与多语句不在产品边界 |
| 锁定读 | `FOR UPDATE` | ❌ | ❌ | 只读差分边界明确排除 |
| 锁定读 | `FOR SHARE` | ❌ | ❌ | 只读差分边界明确排除 |
| 锁定读 | `NOWAIT / SKIP LOCKED` | ❌ | ❌ | 依赖锁状态、非确定性 |
| 锁定读 | `LOCK IN SHARE MODE` | ❌ | ❌ | 只读差分边界明确排除 |

## 表因子、JOIN 与索引提示

| 类别 | MySQL 8.0.41 项 | 实现 | 默认 | 证据或边界 |
| --- | --- | :---: | :---: | --- |
| table factor | `tbl_name` | ✅ | ✅ | 真实表名 |
| table factor | `tbl_name PARTITION (...)` | ✅ | ✅ | 1–2 个真实分区 |
| table factor | `AS alias` | ✅ | ✅ | 真实作用域注册 |
| table factor | `implicit alias` | ✅ | ✅ | 无 AS |
| table factor | `derived table` | ✅ | ✅ | 隔离外层作用域 |
| table factor | `derived table column list` | ✅ | ✅ | 显式稳定输出列 |
| table factor | `LATERAL derived table` | ✅ | ✅ | 相关 LEFT/RIGHT 方向约束 |
| table factor | `(table_references)` | ✅ | ✅ | 受控嵌套 join tree，最多四表形态 |
| table factor | `{ OJ table_reference }` | ❌ | ❌ | ODBC escape 未生成 |
| table factor | `JSON_TABLE()` | ✅ | ❌ | 四类 column form 已实现，默认 JSON 排除 |
| table factor | `other table functions` | ❌ | ❌ | MySQL 8.0.41 内建 table function 仅重点覆盖 JSON_TABLE |
| table factor | `DUAL` | ❌ | ❌ | 无 FROM 标量已覆盖，但不显式生成 DUAL |
| table factor | `view reference` | ❌ | ❌ | schema 生成器不创建 VIEW |
| table factor | `CTE reference` | ✅ | ✅ | WITH 定义后注册 |
| JOIN | `comma join` | ✅ | ✅ | 笛卡尔积 |
| JOIN | `JOIN` | ✅ | ✅ | 有条件与无条件 |
| JOIN | `INNER JOIN` | ✅ | ✅ | ON/USING/无条件 |
| JOIN | `CROSS JOIN` | ✅ | ✅ | ON/无条件 |
| JOIN | `STRAIGHT_JOIN` | ✅ | ✅ | ON/无条件 |
| JOIN | `LEFT JOIN` | ✅ | ✅ | 含 OUTER |
| JOIN | `RIGHT JOIN` | ✅ | ✅ | 含 OUTER |
| JOIN | `NATURAL JOIN` | ✅ | ✅ | INNER/LEFT/RIGHT 及 OUTER |
| JOIN | `ON search_condition` | ✅ | ✅ | 普通与相关谓词 |
| JOIN | `USING (one column)` | ✅ | ✅ | 真实公共列 |
| JOIN | `USING (multiple columns)` | ✅ | ✅ | 真实公共列列表 |
| index hint | `USE INDEX` | ✅ | ✅ | default/JOIN/ORDER BY/GROUP BY |
| index hint | `FORCE INDEX` | ✅ | ✅ | default/JOIN/ORDER BY/GROUP BY |
| index hint | `IGNORE INDEX` | ✅ | ✅ | default/JOIN/ORDER BY/GROUP BY |
| index hint | `USE KEY` | ❌ | ❌ | KEY 同义词未生成 |
| index hint | `FORCE KEY` | ❌ | ❌ | KEY 同义词未生成 |
| index hint | `IGNORE KEY` | ❌ | ❌ | KEY 同义词未生成 |
| index hint | `multiple index names` | ✅ | ✅ | 单个 hint 内最多两个真实索引 |
| index hint | `multiple index_hint list entries` | ❌ | ❌ | 每个 table factor 当前只生成一个 hint |
| index hint | `USE INDEX () empty list` | ❌ | ❌ | 未生成空列表语义 |

## 表达式、运算符、谓词与子查询因子

| 类别 | MySQL 8.0.41 项 | 实现 | 默认 | 证据或边界 |
| --- | --- | :---: | :---: | --- |
| 原子 | `column reference` | ✅ | ✅ | 同层与合法相关外层列 |
| 原子 | `numeric literal` | ✅ | ✅ | 普通值与边界值 |
| 原子 | `text literal` | ✅ | ✅ | 空、控制字符、多字节、尾空格 |
| 原子 | `binary / bit literal` | ✅ | ✅ | X'' 与 b'' |
| 原子 | `DATE / TIME / DATETIME / TIMESTAMP literal` | ✅ | ✅ | 显式输入 |
| 原子 | `NULL` | ✅ | ✅ | 定向真值矩阵 |
| 原子 | `TRUE / FALSE` | ✅ | ✅ | 布尔原子 |
| 原子 | `JSON literal` | ✅ | ❌ | 默认 JSON 排除 |
| 原子 | `user variable @x` | ❌ | ❌ | 会话状态依赖 |
| 原子 | `system variable @@x` | ❌ | ❌ | 配置状态依赖 |
| 原子 | `parameter marker ?` | ❌ | ❌ | 生成器输出直接可执行 SQL，不生成预处理参数 |
| 复合 | `ROW(expr,...)` | ✅ | ✅ | 二列 row constructor |
| 复合 | `(expr,expr) row constructor` | ✅ | ✅ | 隐式 ROW |
| 复合 | `simple CASE` | ✅ | ✅ | CASE value WHEN |
| 复合 | `searched CASE` | ✅ | ✅ | 单/双 WHEN |
| 复合 | `CAST()` | ✅ | ✅ | 数值、字符、二进制、时间安全类型；JSON/SPATIAL 默认排除 |
| 复合 | `CONVERT()` | ✅ | ✅ | 类型转换与 USING utf8mb4 |
| 复合 | `BINARY expr` | ✅ | ✅ | 一元二进制转换 |
| 复合 | `COLLATE` | ✅ | ✅ | 固定 utf8mb4_0900_ai_ci |
| 复合 | `character set introducer` | ❌ | ❌ | 未覆盖 _utf8mb4 等 introducer |
| 时间 | `expr +/- INTERVAL` | ✅ | ✅ | 全部简单与复合 interval unit |
| 时间 | `DATE_ADD / DATE_SUB` | ✅ | ✅ | 显式时间输入 |
| 时间 | `TIMESTAMPADD / TIMESTAMPDIFF` | ✅ | ✅ | 多个单位 |
| 算术 | `+ - * / DIV % MOD` | ✅ | ✅ | 含 NULL 定向输入 |
| 位运算 | `& \| ^ << >> ~` | ✅ | ✅ | 数值与二进制输入 |
| 一元 | `+expr / -expr` | ✅ | ✅ | 一元正负 |
| 逻辑 | `AND / &&` | ✅ | ✅ | 三值逻辑 |
| 逻辑 | `OR / \|\|` | ✅ | ✅ | 三值逻辑；不切换 PIPES_AS_CONCAT |
| 逻辑 | `XOR` | ✅ | ✅ | 三值逻辑 |
| 逻辑 | `NOT / !` | ✅ | ✅ | 谓词否定 |
| 比较 | `= <> != < <= > >=` | ✅ | ✅ | 标量与 row |
| 比较 | `<=>` | ✅ | ✅ | NULL-safe equality |
| 比较 | `IS NULL / IS NOT NULL` | ✅ | ✅ | 定向 NULL |
| 比较 | `IS TRUE/FALSE/UNKNOWN` | ✅ | ✅ | 含 NOT 形态 |
| 比较 | `BETWEEN / NOT BETWEEN` | ✅ | ✅ | 定向 NULL |
| 成员 | `IN-list / NOT IN-list` | ✅ | ✅ | 含 NULL 元素 |
| 成员 | `IN-subquery / NOT IN-subquery` | ✅ | ✅ | 空/单/多行与 nullable matrix |
| 成员 | `EXISTS / NOT EXISTS` | ✅ | ✅ | 相关与非相关 |
| 量化 | `comparison ANY / SOME / ALL` | ✅ | ✅ | 标量与合法 row 形态 |
| 模式 | `LIKE / NOT LIKE` | ✅ | ✅ | 含 ESCAPE |
| 模式 | `REGEXP / RLIKE` | ✅ | ✅ | 含否定 |
| 模式 | `REGEXP_LIKE()` | ✅ | ✅ | 固定安全 pattern |
| 模式 | `SOUNDS LIKE` | ✅ | ✅ | 文本列 |
| JSON | `-> / ->>` | ✅ | ❌ | 默认 JSON 排除 |
| JSON | `MEMBER OF()` | ✅ | ❌ | 默认 JSON 排除 |
| JSON | `JSON_OVERLAPS()` | ✅ | ❌ | 默认 JSON 排除 |
| FULLTEXT | `MATCH ... AGAINST` | ✅ | ❌ | 默认 FULLTEXT 排除 |
| SPATIAL | `ST_IsValid predicate` | ✅ | ❌ | 默认 SPATIAL 排除 |
| 调用解析 | `schema-qualified stored function` | ❌ | ❌ | 闭合 allowlist 禁止 |
| 调用解析 | `loadable function / UDF` | ❌ | ❌ | 插件状态与副作用不可控 |

## 聚合、窗口函数与 frame

| 类别 | MySQL 8.0.41 项 | 实现 | 默认 | 证据或边界 |
| --- | --- | :---: | :---: | --- |
| 聚合 | `COUNT(*) / COUNT(expr)` | ✅ | ✅ | ALL/DISTINCT/NULL |
| 聚合 | `SUM()` | ✅ | ✅ | ALL/DISTINCT/NULL |
| 聚合 | `AVG()` | ✅ | ✅ | ALL/DISTINCT/NULL |
| 聚合 | `MIN() / MAX()` | ✅ | ✅ | ALL/DISTINCT/NULL |
| 聚合 | `BIT_AND() / BIT_OR() / BIT_XOR()` | ✅ | ✅ | 数值 |
| 聚合 | `STDDEV_POP() / STDDEV_SAMP()` | ✅ | ✅ | 统计聚合 |
| 聚合 | `VAR_POP() / VAR_SAMP()` | ✅ | ✅ | 统计聚合 |
| 聚合 | `GROUP_CONCAT()` | ✅ | ✅ | 同表达式 DISTINCT+ORDER BY，限制输出 |
| 聚合 | `GROUPING()` | ✅ | ✅ | WITH ROLLUP |
| 聚合 | `JSON_ARRAYAGG()` | ✅ | ❌ | 顺序无关常量，默认 JSON 排除 |
| 聚合 | `JSON_OBJECTAGG()` | ✅ | ❌ | 同 key/value binding，默认 JSON 排除 |
| 聚合场景 | `global aggregate` | ✅ | ✅ | 无 GROUP BY |
| 聚合场景 | `grouped aggregate` | ✅ | ✅ | 单/多列 GROUP BY |
| 聚合场景 | `all-NULL aggregate` | ✅ | ✅ | 定向数据场景 |
| 窗口 | `OVER()` | ✅ | ✅ | peer-safe ranking 与聚合 |
| 窗口 | `inline window specification` | ✅ | ✅ | PARTITION/ORDER/frame |
| 窗口 | `named WINDOW` | ✅ | ✅ | 继承与修改 |
| 窗口 | `ROW_NUMBER()` | ✅ | ✅ | 强制总序 |
| 窗口 | `RANK() / DENSE_RANK()` | ✅ | ✅ | 支持 peer |
| 窗口 | `CUME_DIST() / PERCENT_RANK()` | ✅ | ✅ | ranking |
| 窗口 | `NTILE()` | ✅ | ✅ | 正整数桶 |
| 窗口 | `LAG() / LEAD()` | ✅ | ✅ | 1/2/3 参数安全形态 |
| 窗口 | `FIRST_VALUE() / LAST_VALUE()` | ✅ | ✅ | 确定性排序 |
| 窗口 | `NTH_VALUE() FROM FIRST` | ✅ | ✅ | 正整数位置 |
| 窗口 | `NTH_VALUE() FROM LAST` | ❌ | ❌ | MySQL 8.0.41 返回不支持 |
| 窗口 | `RESPECT NULLS` | ❌ | ❌ | 默认语义未显式拼写 |
| 窗口 | `IGNORE NULLS` | ❌ | ❌ | MySQL 8.0.41 返回不支持 |
| frame | `ROWS` | ✅ | ✅ | 合法边界组合 |
| frame | `RANGE` | ✅ | ✅ | 无界、numeric bounded、temporal bounded |
| frame | `GROUPS` | ❌ | ❌ | MySQL 8.0.41 返回不支持 |
| frame | `UNBOUNDED PRECEDING/FOLLOWING` | ✅ | ✅ | 合法位置 |
| frame | `CURRENT ROW` | ✅ | ✅ | start/end |
| frame | `expr PRECEDING/FOLLOWING` | ✅ | ✅ | numeric 与 INTERVAL |
| frame | `EXCLUDE` | ❌ | ❌ | MySQL 8.0.41 返回不支持 |

## 优化器提示

| 类别 | MySQL 8.0.41 项 | 实现 | 默认 | 证据或边界 |
| --- | --- | :---: | :---: | --- |
| optimizer hint | `BKA` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_BKA` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `BNL` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_BNL` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `DERIVED_CONDITION_PUSHDOWN` | ✅ | ✅ | 真实 alias/index 绑定，EXPLAIN warning 拒绝 |
| optimizer hint | `NO_DERIVED_CONDITION_PUSHDOWN` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `GROUP_INDEX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_GROUP_INDEX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `HASH_JOIN` | ❌ | ❌ | 8.0.41 可解析但 8.0.19+ 已无效果；未生成 |
| optimizer hint | `NO_HASH_JOIN` | ❌ | ❌ | 8.0.41 可解析但 8.0.19+ 已无效果；未生成 |
| optimizer hint | `INDEX` | ✅ | ✅ | 真实 alias/index 绑定，EXPLAIN warning 拒绝 |
| optimizer hint | `NO_INDEX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `INDEX_MERGE` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_INDEX_MERGE` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `JOIN_FIXED_ORDER` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `JOIN_INDEX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_JOIN_INDEX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `JOIN_ORDER` | ✅ | ✅ | 真实 alias/index 绑定，EXPLAIN warning 拒绝 |
| optimizer hint | `JOIN_PREFIX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `JOIN_SUFFIX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `MAX_EXECUTION_TIME` | ❌ | ❌ | 改变执行资源或会话状态，未生成 |
| optimizer hint | `MERGE` | ✅ | ✅ | 真实 alias/index 绑定，EXPLAIN warning 拒绝 |
| optimizer hint | `NO_MERGE` | ✅ | ✅ | 真实 alias/index 绑定，EXPLAIN warning 拒绝 |
| optimizer hint | `MRR` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_MRR` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_ICP` | ✅ | ✅ | 真实 alias/index 绑定，EXPLAIN warning 拒绝 |
| optimizer hint | `NO_RANGE_OPTIMIZATION` | ✅ | ✅ | 真实 alias/index 绑定，EXPLAIN warning 拒绝 |
| optimizer hint | `ORDER_INDEX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_ORDER_INDEX` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `QB_NAME` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `RESOURCE_GROUP` | ❌ | ❌ | 改变执行资源或会话状态，未生成 |
| optimizer hint | `SEMIJOIN` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_SEMIJOIN` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `SKIP_SCAN` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `NO_SKIP_SCAN` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| optimizer hint | `SET_VAR` | ❌ | ❌ | 改变执行资源或会话状态，未生成 |
| optimizer hint | `SUBQUERY` | ❌ | ❌ | 当前优化器 hint allowlist 未登记 |
| hint 组合 | `multiple hints in one /*+ ... */` | ❌ | ❌ | 当前每次只渲染一个 optimizer hint |
| hint 作用域 | `@query_block_name` | ❌ | ❌ | QB_NAME 与跨 query-block 引用未覆盖 |
| hint 校验 | `EXPLAIN + SHOW WARNINGS contract` | ✅ | ✅ | warning 候选在三节点执行前拒绝 |

## Schema、数据与索引因子

| 类别 | MySQL 8.0.41 项 | 实现 | 默认 | 证据或边界 |
| --- | --- | :---: | :---: | --- |
| 整数类型 | `TINYINT/SMALLINT/MEDIUMINT/INT/BIGINT signed` | ✅ | ✅ | min/min+1/-1/0/1/max-1/max |
| 整数类型 | `TINYINT/SMALLINT/MEDIUMINT/INT/BIGINT unsigned` | ✅ | ✅ | 0/1/max-1/max |
| 数值类型 | `BIT(1..64)` | ✅ | ✅ | 0/1/max-1/max |
| 数值类型 | `DECIMAL(p,s)` | ✅ | ✅ | p/s 边界与缩放端点 |
| 数值类型 | `FLOAT/DOUBLE` | ✅ | ✅ | 有限边界、正负零；NaN/Inf 禁止 |
| 文本类型 | `CHAR/VARCHAR` | ✅ | ✅ | 长度 0/1/上限与字符边界 |
| 文本类型 | `TINYTEXT/TEXT/MEDIUMTEXT/LONGTEXT` | ✅ | ✅ | LOB 写入预算限制 |
| 二进制类型 | `BINARY/VARBINARY` | ✅ | ✅ | 长度 0/1/上限 |
| 二进制类型 | `TINYBLOB/BLOB/MEDIUMBLOB/LONGBLOB` | ✅ | ✅ | LOB 写入预算限制 |
| 时间类型 | `DATE/TIME/DATETIME/TIMESTAMP/YEAR` | ✅ | ✅ | fsp 0/6 与上下界 |
| 枚举类型 | `ENUM/SET` | ✅ | ✅ | 成员与组合值 |
| JSON 类型 | `JSON` | ✅ | ❌ | 专用 profile 已实现，默认排除 |
| 空间类型 | `GEOMETRY/POINT/LINESTRING/POLYGON/MULTI geometry types/GEOMETRYCOLLECTION` | ✅ | ❌ | 专用 profile 已实现，默认排除 |
| NULL | `nullable columns` | ✅ | ✅ | 全 NULL、混合 NULL、nullable unique 多 NULL |
| 字符集 | `utf8mb4 + utf8mb4_0900_ai_ci` | ✅ | ✅ | 固定可比配置 |
| 字符集 | `多字符集/多 collation 矩阵` | ❌ | ❌ | 当前不是 schema 生成维度 |
| 表场景 | `empty table` | ✅ | ✅ | 生产轮次可达 |
| 表场景 | `single row` | ✅ | ✅ | 生产轮次可达 |
| 表场景 | `multi-row` | ✅ | ✅ | 生产轮次可达 |
| 分布 | `duplicate low-cardinality` | ✅ | ✅ | 定向生成 |
| 分布 | `hot-key skew` | ✅ | ✅ | 定向生成 |
| 分布 | `all-NULL aggregate input` | ✅ | ✅ | 定向生成 |
| schema | `ordinary InnoDB` | ✅ | ✅ | 默认 profile |
| schema | `temporary InnoDB table` | ✅ | ✅ | 专用 profile |
| schema | `foreign-key graph` | ✅ | ✅ | 兼容类型与动作校验 |
| partition | `HASH` | ✅ | ✅ | 真实 partition 名绑定 |
| partition | `KEY` | ✅ | ✅ | 真实 partition 名绑定 |
| partition | `RANGE` | ✅ | ✅ | 边界 bucket |
| partition | `LIST` | ✅ | ✅ | bucket routing |
| partition | `RANGE COLUMNS` | ✅ | ✅ | 列分区 |
| partition | `LIST COLUMNS` | ✅ | ✅ | 能力允许时 |
| index | `BTREE primary/secondary` | ✅ | ✅ | 普通 InnoDB |
| index | `UNIQUE` | ✅ | ✅ | nullable 与非 nullable |
| index | `composite index` | ✅ | ✅ | 真实列列表 |
| index | `column prefix index` | ✅ | ✅ | 文本/二进制合法前缀 |
| index | `descending key part` | ✅ | ✅ | 8.0.41 |
| index | `functional index` | ✅ | ✅ | LOWER/CAST 受控表达式 |
| index | `FULLTEXT index` | ✅ | ❌ | 专用 profile 已实现，默认排除 |
| index | `SPATIAL index` | ✅ | ❌ | 固定 SRID，默认排除 |
| index | `JSON multivalue index` | ✅ | ❌ | CAST JSON array，默认排除 |
| index | `invisible index` | ❌ | ❌ | 未作为 schema 因子 |
| schema object | `VIEW` | ❌ | ❌ | 不创建 VIEW |
| schema object | `generated column` | ❌ | ❌ | functional index 不等于 generated column |
| schema object | `stored routine / UDF` | ❌ | ❌ | 明确排除 |

## MySQL 8.0.41 内建函数与运算符逐项矩阵

官方表共 465 行；拆开同一行内的别名后得到 496 个名称/语法项。下表逐项给出实现与默认状态。

函数名级 ✅ 只说明当前至少有一个受控参数签名；例如 `TRIM()`、`CONVERT()`、`TIMESTAMP()` 仍只覆盖注册表声明的安全形态。JSON/SPATIAL/FULLTEXT 的实现项默认均为 ❌。

### 位函数与位运算符

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/bit-functions.html)；项目实现 7/7，默认可达 7/7。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `&` | ✅ | ✅ | 至少一个安全签名可达 |
| `>>` | ✅ | ✅ | 至少一个安全签名可达 |
| `<<` | ✅ | ✅ | 至少一个安全签名可达 |
| `^` | ✅ | ✅ | 至少一个安全签名可达 |
| `BIT_COUNT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `\|` | ✅ | ✅ | 至少一个安全签名可达 |
| `~` | ✅ | ✅ | 至少一个安全签名可达 |

### 比较函数与谓词

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/comparison-operators.html)；项目实现 21/23，默认可达 21/23。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `>` | ✅ | ✅ | 至少一个安全签名可达 |
| `>=` | ✅ | ✅ | 至少一个安全签名可达 |
| `<` | ✅ | ✅ | 至少一个安全签名可达 |
| `<>` | ✅ | ✅ | 至少一个安全签名可达 |
| `!=` | ✅ | ✅ | 至少一个安全签名可达 |
| `<=` | ✅ | ✅ | 至少一个安全签名可达 |
| `<=>` | ✅ | ✅ | 至少一个安全签名可达 |
| `=（比较）` | ❌ | ❌ | 未进入当前生成 allowlist |
| `BETWEEN ... AND ...` | ✅ | ✅ | 至少一个安全签名可达 |
| `COALESCE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `EXISTS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `GREATEST()` | ✅ | ✅ | 至少一个安全签名可达 |
| `IN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `INTERVAL()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `IS` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS NOT` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS NOT NULL` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS NULL` | ✅ | ✅ | 至少一个安全签名可达 |
| `ISNULL()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LEAST()` | ✅ | ✅ | 至少一个安全签名可达 |
| `NOT BETWEEN ... AND ...` | ✅ | ✅ | 至少一个安全签名可达 |
| `NOT EXISTS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `NOT IN()` | ✅ | ✅ | 至少一个安全签名可达 |

### 算术运算符

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/arithmetic-functions.html)；项目实现 8/8，默认可达 8/8。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `%` | ✅ | ✅ | 至少一个安全签名可达 |
| `MOD` | ✅ | ✅ | 至少一个安全签名可达 |
| `*` | ✅ | ✅ | 至少一个安全签名可达 |
| `+` | ✅ | ✅ | 至少一个安全签名可达 |
| `-（二元）` | ✅ | ✅ | 至少一个安全签名可达 |
| `-（一元）` | ✅ | ✅ | 至少一个安全签名可达 |
| `/` | ✅ | ✅ | 至少一个安全签名可达 |
| `DIV` | ✅ | ✅ | 至少一个安全签名可达 |

### JSON 搜索函数与运算符

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/json-search-functions.html)；项目实现 6/10，默认可达 0/10。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `->` | ✅ | ❌ | 已实现但默认 family 排除 |
| `->>` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_CONTAINS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_CONTAINS_PATH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_EXTRACT()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_KEYS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_OVERLAPS()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_SEARCH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_VALUE()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `MEMBER OF()` | ✅ | ❌ | 已实现但默认 family 排除 |

### 赋值运算符

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/assignment-operators.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `:=` | ❌ | ❌ | 未进入当前生成 allowlist |
| `=（赋值）` | ❌ | ❌ | 未进入当前生成 allowlist |

### 数学函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/mathematical-functions.html)；项目实现 30/31，默认可达 30/31。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ABS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `ACOS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `ASIN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `ATAN()（官方重复/别名项 1）` | ✅ | ✅ | 至少一个安全签名可达 |
| `ATAN2()` | ✅ | ✅ | 至少一个安全签名可达 |
| `ATAN()（官方重复/别名项 2）` | ✅ | ✅ | 至少一个安全签名可达 |
| `CEIL()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CEILING()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CONV()` | ✅ | ✅ | 至少一个安全签名可达 |
| `COS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `COT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CRC32()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DEGREES()` | ✅ | ✅ | 至少一个安全签名可达 |
| `EXP()` | ✅ | ✅ | 至少一个安全签名可达 |
| `FLOOR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LOG()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LOG10()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LOG2()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MOD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `PI()` | ✅ | ✅ | 至少一个安全签名可达 |
| `POW()` | ✅ | ✅ | 至少一个安全签名可达 |
| `POWER()` | ✅ | ✅ | 至少一个安全签名可达 |
| `RADIANS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `RAND()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ROUND()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SIGN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SIN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SQRT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TAN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TRUNCATE()` | ✅ | ✅ | 至少一个安全签名可达 |

### 日期与时间函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/date-and-time-functions.html)；项目实现 34/65，默认可达 34/65。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ADDDATE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ADDTIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CONVERT_TZ()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURDATE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_DATE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_DATE` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_TIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_TIME` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_TIMESTAMP()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_TIMESTAMP` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURTIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `DATE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DATE_ADD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DATE_FORMAT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `DATE_SUB()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DATEDIFF()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DAY()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DAYNAME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `DAYOFMONTH()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DAYOFWEEK()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DAYOFYEAR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `EXTRACT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `FROM_DAYS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `FROM_UNIXTIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `GET_FORMAT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `HOUR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LAST_DAY` | ✅ | ✅ | 至少一个安全签名可达 |
| `LOCALTIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `LOCALTIME` | ❌ | ❌ | 未进入当前生成 allowlist |
| `LOCALTIMESTAMP` | ❌ | ❌ | 未进入当前生成 allowlist |
| `LOCALTIMESTAMP()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MAKEDATE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MAKETIME()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MICROSECOND()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MINUTE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MONTH()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MONTHNAME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `NOW()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `PERIOD_ADD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `PERIOD_DIFF()` | ✅ | ✅ | 至少一个安全签名可达 |
| `QUARTER()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SEC_TO_TIME()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SECOND()` | ✅ | ✅ | 至少一个安全签名可达 |
| `STR_TO_DATE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SUBDATE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SUBTIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SYSDATE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `TIME()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TIME_FORMAT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `TIME_TO_SEC()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TIMEDIFF()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TIMESTAMP()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TIMESTAMPADD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TIMESTAMPDIFF()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TO_DAYS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TO_SECONDS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `UNIX_TIMESTAMP()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UTC_DATE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UTC_TIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UTC_TIMESTAMP()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `WEEK()` | ✅ | ✅ | 至少一个安全签名可达 |
| `WEEKDAY()` | ✅ | ✅ | 至少一个安全签名可达 |
| `WEEKOFYEAR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `YEAR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `YEARWEEK()` | ✅ | ✅ | 至少一个安全签名可达 |

### 加密、哈希与压缩函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/encryption-functions.html)；项目实现 5/13，默认可达 5/13。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `AES_DECRYPT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `AES_ENCRYPT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `COMPRESS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MD5()` | ✅ | ✅ | 至少一个安全签名可达 |
| `RANDOM_BYTES()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SHA1()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SHA()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SHA2()` | ✅ | ✅ | 至少一个安全签名可达 |
| `STATEMENT_DIGEST()` | ✅ | ✅ | 至少一个安全签名可达 |
| `STATEMENT_DIGEST_TEXT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `UNCOMPRESS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UNCOMPRESSED_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `VALIDATE_PASSWORD_STRENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 逻辑运算符

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/logical-operators.html)；项目实现 7/7，默认可达 7/7。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `AND` | ✅ | ✅ | 至少一个安全签名可达 |
| `&&` | ✅ | ✅ | 至少一个安全签名可达 |
| `NOT` | ✅ | ✅ | 至少一个安全签名可达 |
| `!` | ✅ | ✅ | 至少一个安全签名可达 |
| `OR` | ✅ | ✅ | 至少一个安全签名可达 |
| `\|\|` | ✅ | ✅ | 至少一个安全签名可达 |
| `XOR` | ✅ | ✅ | 至少一个安全签名可达 |

### 其他函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/miscellaneous-functions.html)；项目实现 9/19，默认可达 9/19。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ANY_VALUE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `BIN_TO_UUID()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `DEFAULT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `GROUPING()` | ✅ | ✅ | 至少一个安全签名可达 |
| `INET_ATON()` | ✅ | ✅ | 至少一个安全签名可达 |
| `INET_NTOA()` | ✅ | ✅ | 至少一个安全签名可达 |
| `INET6_ATON()` | ✅ | ✅ | 至少一个安全签名可达 |
| `INET6_NTOA()` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS_IPV4()` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS_IPV4_COMPAT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS_IPV4_MAPPED()` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS_IPV6()` | ✅ | ✅ | 至少一个安全签名可达 |
| `IS_UUID()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `NAME_CONST()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SLEEP()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UUID()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UUID_SHORT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UUID_TO_BIN()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `VALUES()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 字符串与二进制函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/string-functions.html)；项目实现 44/50，默认可达 44/50。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ASCII()` | ✅ | ✅ | 至少一个安全签名可达 |
| `BIN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `BIT_LENGTH()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CHAR()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CHAR_LENGTH()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CHARACTER_LENGTH()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CONCAT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CONCAT_WS()` | ✅ | ✅ | 至少一个安全签名可达 |
| `ELT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `EXPORT_SET()` | ✅ | ✅ | 至少一个安全签名可达 |
| `FIELD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `FIND_IN_SET()` | ✅ | ✅ | 至少一个安全签名可达 |
| `FORMAT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `FROM_BASE64()` | ✅ | ✅ | 至少一个安全签名可达 |
| `HEX()` | ✅ | ✅ | 至少一个安全签名可达 |
| `INSERT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INSTR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LCASE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LEFT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LENGTH()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LOAD_FILE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `LOCATE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LOWER()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LPAD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LTRIM()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MAKE_SET()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MID()` | ✅ | ✅ | 至少一个安全签名可达 |
| `OCT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `OCTET_LENGTH()` | ✅ | ✅ | 至少一个安全签名可达 |
| `ORD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `POSITION()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `QUOTE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `REPEAT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `REPLACE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `REVERSE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `RIGHT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `RPAD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `RTRIM()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SOUNDEX()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SOUNDS LIKE` | ✅ | ✅ | 至少一个安全签名可达 |
| `SPACE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SUBSTR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SUBSTRING()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SUBSTRING_INDEX()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TO_BASE64()` | ✅ | ✅ | 至少一个安全签名可达 |
| `TRIM()` | ✅ | ✅ | 至少一个安全签名可达 |
| `UCASE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `UNHEX()` | ✅ | ✅ | 至少一个安全签名可达 |
| `UPPER()` | ✅ | ✅ | 至少一个安全签名可达 |
| `WEIGHT_STRING()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 异步复制故障转移函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/replication-functions-async-failover.html)；项目实现 0/5，默认可达 0/5。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `asynchronous_connection_failover_add_managed()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `asynchronous_connection_failover_add_source()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `asynchronous_connection_failover_delete_managed()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `asynchronous_connection_failover_delete_source()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `asynchronous_connection_failover_reset()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 聚合函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/aggregate-functions.html)；项目实现 16/19，默认可达 14/19。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `AVG()` | ✅ | ✅ | 至少一个安全签名可达 |
| `BIT_AND()` | ✅ | ✅ | 至少一个安全签名可达 |
| `BIT_OR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `BIT_XOR()` | ✅ | ✅ | 至少一个安全签名可达 |
| `COUNT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `COUNT(DISTINCT)` | ✅ | ✅ | 至少一个安全签名可达 |
| `GROUP_CONCAT()` | ✅ | ✅ | 至少一个安全签名可达 |
| `JSON_ARRAYAGG()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_OBJECTAGG()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `MAX()` | ✅ | ✅ | 至少一个安全签名可达 |
| `MIN()` | ✅ | ✅ | 至少一个安全签名可达 |
| `STD()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `STDDEV()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `STDDEV_POP()` | ✅ | ✅ | 至少一个安全签名可达 |
| `STDDEV_SAMP()` | ✅ | ✅ | 至少一个安全签名可达 |
| `SUM()` | ✅ | ✅ | 至少一个安全签名可达 |
| `VAR_POP()` | ✅ | ✅ | 至少一个安全签名可达 |
| `VAR_SAMP()` | ✅ | ✅ | 至少一个安全签名可达 |
| `VARIANCE()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 信息函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/information-functions.html)；项目实现 0/19，默认可达 0/19。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `BENCHMARK()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CHARSET()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `COERCIBILITY()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `COLLATION()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CONNECTION_ID()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_ROLE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_USER()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CURRENT_USER` | ❌ | ❌ | 未进入当前生成 allowlist |
| `DATABASE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `FOUND_ROWS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ICU_VERSION()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `LAST_INSERT_ID()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ROLES_GRAPHML()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ROW_COUNT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SCHEMA()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SESSION_USER()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `SYSTEM_USER()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `USER()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `VERSION()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 类型转换

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/cast-functions.html)；项目实现 3/3，默认可达 3/3。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `BINARY` | ✅ | ✅ | MySQL 标记 deprecated；至少一个安全签名可达 |
| `CAST()` | ✅ | ✅ | 至少一个安全签名可达 |
| `CONVERT()` | ✅ | ✅ | 至少一个安全签名可达 |

### 内部函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/internal-functions.html)；项目实现 0/28，默认可达 0/28。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `CAN_ACCESS_COLUMN()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CAN_ACCESS_DATABASE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CAN_ACCESS_TABLE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CAN_ACCESS_USER()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `CAN_ACCESS_VIEW()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `GET_DD_COLUMN_PRIVILEGES()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `GET_DD_CREATE_OPTIONS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `GET_DD_INDEX_SUB_PART_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_AUTO_INCREMENT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_AVG_ROW_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_CHECK_TIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_CHECKSUM()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_DATA_FREE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_DATA_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_DD_CHAR_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_GET_COMMENT_OR_ERROR()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_GET_ENABLED_ROLE_JSON()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_GET_HOSTNAME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_GET_USERNAME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_GET_VIEW_WARNING_OR_ERROR()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_INDEX_COLUMN_CARDINALITY()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_INDEX_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_IS_ENABLED_ROLE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_IS_MANDATORY_ROLE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_KEYS_DISABLED()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_MAX_DATA_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_TABLE_ROWS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `INTERNAL_UPDATE_TIME()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 流程控制函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/flow-control-functions.html)；项目实现 4/4，默认可达 4/4。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `CASE` | ✅ | ✅ | 至少一个安全签名可达 |
| `IF()` | ✅ | ✅ | 至少一个安全签名可达 |
| `IFNULL()` | ✅ | ✅ | 至少一个安全签名可达 |
| `NULLIF()` | ✅ | ✅ | 至少一个安全签名可达 |

### 窗口函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/window-function-descriptions.html)；项目实现 11/11，默认可达 11/11。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `CUME_DIST()` | ✅ | ✅ | 至少一个安全签名可达 |
| `DENSE_RANK()` | ✅ | ✅ | 至少一个安全签名可达 |
| `FIRST_VALUE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LAG()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LAST_VALUE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `LEAD()` | ✅ | ✅ | 至少一个安全签名可达 |
| `NTH_VALUE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `NTILE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `PERCENT_RANK()` | ✅ | ✅ | 至少一个安全签名可达 |
| `RANK()` | ✅ | ✅ | 至少一个安全签名可达 |
| `ROW_NUMBER()` | ✅ | ✅ | 至少一个安全签名可达 |

### XML 函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/xml-functions.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ExtractValue()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `UpdateXML()` | ❌ | ❌ | 未进入当前生成 allowlist |

### Performance Schema 函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/performance-schema-functions.html)；项目实现 0/4，默认可达 0/4。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `FORMAT_BYTES()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `FORMAT_PICO_TIME()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `PS_CURRENT_THREAD_ID()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `PS_THREAD_ID()` | ❌ | ❌ | 未进入当前生成 allowlist |

### MySQL 空间构造函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-mysql-specific-functions.html)；项目实现 0/8，默认可达 0/8。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `GeomCollection()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `GeometryCollection()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `LineString()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MultiLineString()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MultiPoint()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MultiPolygon()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `Point()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `Polygon()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 锁函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/locking-functions.html)；项目实现 0/5，默认可达 0/5。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `GET_LOCK()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `IS_FREE_LOCK()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `IS_USED_LOCK()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `RELEASE_ALL_LOCKS()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `RELEASE_LOCK()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 组复制成员动作函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/group-replication-functions-for-member-actions.html)；项目实现 0/3，默认可达 0/3。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `group_replication_disable_member_action()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `group_replication_enable_member_action()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `group_replication_reset_member_actions()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 组复制通信协议函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/group-replication-functions-for-communication-protocol.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `group_replication_get_communication_protocol()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `group_replication_set_communication_protocol()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 组复制共识函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/group-replication-functions-for-maximum-consensus.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `group_replication_get_write_concurrency()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `group_replication_set_write_concurrency()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 组复制主节点函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/group-replication-functions-for-new-primary.html)；项目实现 0/1，默认可达 0/1。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `group_replication_set_as_primary()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 组复制模式函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/group-replication-functions-for-mode.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `group_replication_switch_to_multi_primary_mode()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `group_replication_switch_to_single_primary_mode()` | ❌ | ❌ | 未进入当前生成 allowlist |

### GTID 函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gtid-functions.html)；项目实现 0/4，默认可达 0/4。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `GTID_SUBSET()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `GTID_SUBTRACT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `WAIT_FOR_EXECUTED_GTID_SET()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS()` | ❌ | ❌ | MySQL 标记 deprecated；未进入当前生成 allowlist |

### JSON 创建函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/json-creation-functions.html)；项目实现 2/3，默认可达 0/3。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `JSON_ARRAY()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_OBJECT()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_QUOTE()` | ❌ | ❌ | 未进入当前生成 allowlist |

### JSON 修改函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/json-modification-functions.html)；项目实现 1/10，默认可达 0/10。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `JSON_ARRAY_APPEND()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_ARRAY_INSERT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_INSERT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_MERGE()` | ❌ | ❌ | MySQL 标记 deprecated；未进入当前生成 allowlist |
| `JSON_MERGE_PATCH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_MERGE_PRESERVE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_REMOVE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_REPLACE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_SET()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_UNQUOTE()` | ✅ | ❌ | 已实现但默认 family 排除 |

### JSON 属性函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/json-attribute-functions.html)；项目实现 1/4，默认可达 0/4。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `JSON_DEPTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_LENGTH()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_TYPE()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_VALID()` | ❌ | ❌ | 未进入当前生成 allowlist |

### JSON 工具函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/json-utility-functions.html)；项目实现 0/3，默认可达 0/3。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `JSON_PRETTY()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_STORAGE_FREE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `JSON_STORAGE_SIZE()` | ❌ | ❌ | 未进入当前生成 allowlist |

### JSON 校验函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/json-validation-functions.html)；项目实现 1/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `JSON_SCHEMA_VALID()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `JSON_SCHEMA_VALIDATION_REPORT()` | ❌ | ❌ | 未进入当前生成 allowlist |

### JSON 表函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/json-table-functions.html)；项目实现 1/1，默认可达 0/1。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `JSON_TABLE()` | ✅ | ❌ | 已实现但默认 family 排除 |

### 字符串比较

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/string-comparison-functions.html)；项目实现 3/3，默认可达 3/3。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `LIKE` | ✅ | ✅ | 至少一个安全签名可达 |
| `NOT LIKE` | ✅ | ✅ | 至少一个安全签名可达 |
| `STRCMP()` | ✅ | ✅ | 至少一个安全签名可达 |

### 复制同步函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/replication-functions-synchronization.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `MASTER_POS_WAIT()` | ❌ | ❌ | MySQL 标记 deprecated；未进入当前生成 allowlist |
| `SOURCE_POS_WAIT()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 全文检索

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/fulltext-search.html)；项目实现 1/1，默认可达 0/1。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `MATCH()` | ✅ | ❌ | 已实现但默认 family 排除 |

### MBR 空间关系函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/spatial-relation-functions-mbr.html)；项目实现 0/9，默认可达 0/9。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `MBRContains()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBRCoveredBy()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBRCovers()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBRDisjoint()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBREquals()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBRIntersects()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBROverlaps()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBRTouches()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `MBRWithin()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 正则表达式函数与运算符

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/regexp.html)；项目实现 4/7，默认可达 4/7。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `NOT REGEXP` | ✅ | ✅ | 至少一个安全签名可达 |
| `REGEXP` | ✅ | ✅ | 至少一个安全签名可达 |
| `REGEXP_INSTR()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `REGEXP_LIKE()` | ✅ | ✅ | 至少一个安全签名可达 |
| `REGEXP_REPLACE()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `REGEXP_SUBSTR()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `RLIKE` | ✅ | ✅ | 至少一个安全签名可达 |

### Polygon 属性函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-polygon-property-functions.html)；项目实现 0/6，默认可达 0/6。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_Area()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Centroid()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_ExteriorRing()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_InteriorRingN()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_NumInteriorRing()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_NumInteriorRings()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 空间格式转换函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-format-conversion-functions.html)；项目实现 2/5，默认可达 0/5。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_AsBinary()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `ST_AsWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_AsText()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `ST_AsWKT()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_SwapXY()` | ❌ | ❌ | 未进入当前生成 allowlist |

### GeoJSON 函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/spatial-geojson-functions.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_AsGeoJSON()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeomFromGeoJSON()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 空间运算函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/spatial-operator-functions.html)；项目实现 0/11，默认可达 0/11。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_Buffer()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Buffer_Strategy()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_ConvexHull()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Difference()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Intersection()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LineInterpolatePoint()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LineInterpolatePoints()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PointAtDistance()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_SymDifference()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Transform()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Union()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 空间聚合函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/spatial-aggregate-functions.html)；项目实现 0/1，默认可达 0/1。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_Collect()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 对象形状空间关系函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/spatial-relation-functions-object-shapes.html)；项目实现 0/11，默认可达 0/11。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_Contains()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Crosses()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Disjoint()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Distance()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Equals()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_FrechetDistance()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_HausdorffDistance()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Intersects()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Overlaps()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Touches()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Within()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 空间通用属性函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-general-property-functions.html)；项目实现 0/6，默认可达 0/6。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_Dimension()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Envelope()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeometryType()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_IsEmpty()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_IsSimple()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_SRID()` | ❌ | ❌ | 未进入当前生成 allowlist |

### 空间便捷函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/spatial-convenience-functions.html)；项目实现 1/5，默认可达 0/5。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_Distance_Sphere()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_IsValid()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `ST_MakeEnvelope()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Simplify()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Validate()` | ❌ | ❌ | 未进入当前生成 allowlist |

### LineString 属性函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-linestring-property-functions.html)；项目实现 0/6，默认可达 0/6。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_EndPoint()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_IsClosed()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Length()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_NumPoints()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PointN()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_StartPoint()` | ❌ | ❌ | 未进入当前生成 allowlist |

### Geohash 函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/spatial-geohash-functions.html)；项目实现 0/4，默认可达 0/4。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_GeoHash()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LatFromGeoHash()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LongFromGeoHash()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PointFromGeoHash()` | ❌ | ❌ | 未进入当前生成 allowlist |

### WKT 空间构造函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-wkt-functions.html)；项目实现 1/16，默认可达 0/16。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_GeomCollFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeometryCollectionFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeomCollFromTxt()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeomFromText()` | ✅ | ❌ | 已实现但默认 family 排除 |
| `ST_GeometryFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LineFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LineStringFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MLineFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MultiLineStringFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MPointFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MultiPointFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MPolyFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MultiPolygonFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PointFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PolyFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PolygonFromText()` | ❌ | ❌ | 未进入当前生成 allowlist |

### WKB 空间构造函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-wkb-functions.html)；项目实现 0/15，默认可达 0/15。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_GeomCollFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeometryCollectionFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeomFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_GeometryFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LineFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_LineStringFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MLineFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MultiLineStringFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MPointFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MultiPointFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MPolyFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_MultiPolygonFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PointFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PolyFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_PolygonFromWKB()` | ❌ | ❌ | 未进入当前生成 allowlist |

### GeometryCollection 属性函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-geometrycollection-property-functions.html)；项目实现 0/2，默认可达 0/2。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_GeometryN()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_NumGeometries()` | ❌ | ❌ | 未进入当前生成 allowlist |

### Point 属性函数

[官方分类页](https://dev.mysql.com/doc/refman/8.0/en/gis-point-property-functions.html)；项目实现 0/4，默认可达 0/4。

| 官方名称/语法 | 实现 | 默认 | 说明 |
| --- | :---: | :---: | --- |
| `ST_Latitude()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Longitude()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_X()` | ❌ | ❌ | 未进入当前生成 allowlist |
| `ST_Y()` | ❌ | ❌ | 未进入当前生成 allowlist |

## 缺口归因与建议优先级

### P0：报告与覆盖记账一致性

- 当前文法实测为 105 production / 1026 alternative，旧覆盖清单仍写 96/96；应让文档数字由文法解析器自动生成，避免迁移后漂移。
- 建议把本报告中的函数名级矩阵转成机器可读 snapshot，并在 CI 中对官方表/本地 allowlist 做差分。

### P1：适合继续扩大的确定性能力

- 日期时间：优先补显式输入且无当前时间依赖的 `ADDTIME()`、`SUBTIME()`、`EXTRACT()`、`DATE_FORMAT()`、`TIME_FORMAT()`、`FROM_UNIXTIME(expr)` 等安全签名。
- 正则：补 `REGEXP_INSTR()`、`REGEXP_REPLACE()`、`REGEXP_SUBSTR()` 的固定 ICU pattern/flags。
- 字符串：补 `CHAR()`、`INSERT()`、`POSITION()`、`WEIGHT_STRING()` 等可固定 charset/collation 的签名。
- 聚合别名：补 `STD()`、`STDDEV()`、`VARIANCE()` 等同义函数，减少名称级 ❌，同时保持现有数值 oracle。
- 查询结构：补 `KEY` index-hint 同义词、多个 index_hint list、显式 `DUAL`、`ORDER BY ... WITH ROLLUP` 的定向正例。
- optimizer hint：优先补 `NO_INDEX`、`JOIN_INDEX`、`GROUP_INDEX`、`ORDER_INDEX` 及反向形态，继续要求真实 alias/index 与 EXPLAIN warning=0。

### P2：需要单独产品决策的能力

- JSON/SPATIAL/FULLTEXT 已有部分实现但默认整体排除；应以独立 profile/独立 oracle 扩展，不能直接混入当前默认 correctness。
- VIEW、更多 table function、多字符集/collation、generated column 会扩大 setup 与 schema 语义，建议独立里程碑。

### 永久或长期排除

- `RAND()`、当前时间、UUID、连接/用户/版本/会话状态、锁等待、复制状态等非确定或环境状态函数。
- 文件读写、加解密密钥/插件依赖、密码校验、内部字典函数、组复制/GTID 管理函数。
- stored function、loadable function/UDF、用户变量、赋值运算符、多语句、DDL/DML、锁定读。
- `GROUPS`、window `EXCLUDE`、`IGNORE NULLS`、`NTH_VALUE ... FROM LAST`：MySQL 8.0.41 本身不支持，不应作为 valid-lane 缺口。

## 复核命令

```bash
uv run pytest -q tests/generation/test_query_grammar.py \
  tests/generation/test_query_grammar_p1.py \
  tests/generation/test_function_registry.py \
  tests/generation/test_query_scope.py \
  tests/catalog/test_official_catalog.py

uv run python scripts/verify_catalog_sources.py \
  catalog/mysql-8.0.41-query-shapes.yaml
```

复核结果（2026-07-17）：

- ✅ 定向生成与 catalog 测试：111 passed（9.14s）。
- ✅ 来源锁单元测试：16 passed、1 skipped（1.24s）。
- ✅ 在线来源锁：23 个官方来源、96 个定位点的 canonicalized SHA-256 与 locator 全部匹配。`dev.mysql.com` 首次经 Python `urlopen` 下载时发生 TLS 超时，改用带重试的 `curl` 作为 `verify_catalog_source_lock()` 的下载回调后通过；只替换下载传输层，哈希、规范化和定位点校验仍使用项目原实现。
- ✅ 报告矩阵完整性：496 个函数/运算符条目均含实现与默认状态；统计复算为实现 223、默认 204；所有 730 个覆盖矩阵数据行均含 ✅/❌。
- ✅ Markdown 完整性：无行尾空白，4 个本地证据链接均可解析到现有文件。

在线来源校验只下载并哈希官方内容，不执行网页中的 SQL。
