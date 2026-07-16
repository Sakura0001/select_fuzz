# 查询 SQL 生成实现与实际运行验收计划

## 1. 计划目的

本文件将 `docs/testing/query-generation-coverage-checklist.md` 中的未闭环事项，以及本轮工作中涉及的 SQL 生成逻辑，拆分为可以逐项实现、逐项运行、逐项验收的细粒度任务。

本计划的核心要求是：

1. 不能只通过静态代码检查、AST 断言、字符串断言或单元测试判断 SQL 生成功能已经完成。
2. 每个涉及 SQL 生成逻辑的任务，都必须实际生成 SQL。
3. 生成出的 SQL 必须实际经过 MySQL 8.0.41 baseline `EXPLAIN`。
4. 通过 EXPLAIN 后，必须在 baseline、custom_off、custom_on 三个节点上实际执行。
5. 必须同时检查生成成功的 SQL 和生成失败/执行失败的 SQL。
6. 成功 SQL 必须证明新功能确实出现在 SQL 中，并且三节点结果、列元数据和 warning 一致。
7. 失败 SQL 必须能够通过 SQL、feature tag、seed、errno、SQLSTATE、warning 或 resource-limit 进行归因。
8. 当前功能未完成验收前，不得进入下一个 SQL 生成逻辑的开发。
9. 每个已通过任务都必须保留可 replay 的 SQL 和运行 artifact。
10. checklist 只有在代码、测试、实际 SQL、三节点运行和证据都齐全后才能标记为 `[x]`。

## 2. 当前检查结论

本轮按用户新增的“每次验证约 3 分钟，并且程序在该批次内实际触发并完成任务”约束，
将原定长跑改为可重复的短批次。当前重点事项均已完成实际闭环：

1. NOT EXISTS/NOT IN 的空、单行、多行、outer nullable、inner nullable、双方 nullable 和嵌套矩阵已闭环。
2. 当前 grammar 可达的全部合法 frame-bound 组合已逐项做成三节点见证；MySQL 8.0.41 不支持的语法继续 fail-closed。
3. optimizer hint 已形成正向/负向生成矩阵，并完成正向三节点执行和负向生成拒绝。
4. 函数注册表 335 个 witness 已在 normal、boundary、special 三个值域 profile 下通过，
   共 1005 条当前 grammar SQL；warning contract 已登记并精确验证。
5. 最新 grammar 已完成两轮、200 条查询、约 3 分钟上限内的三节点随机批次，0 个未归因 finding。
6. 本轮新生成的定向 artifact 已保留，之前中断的长跑诊断产物移入明确归档目录。
7. 当前 P0/P1 修改、测试和文档已按主题提交并合并回 `main`；临时 codex 分支已删除。

当前已有的本地验证结果：

- 全量 pytest：`1779 passed, 20 skipped`。
- Ruff：通过。
- 三节点定向 acceptance：anti 12 条、frame 25 条、hint 11 条、函数 1005 条，全部通过。
- 最新 grammar 短批次：`elapsed_seconds=80.605776`、2 轮、200 条查询、0 finding、5 个资源上限事件、1 个生成拒绝。
- 三节点版本均为 MySQL `8.0.41`；短批次完整运行，满足“每次约 3 分钟且实际完成任务”的验证约束。

上述结果不能替代本计划要求的所有三节点实际运行；未在当前工作树重新执行的历史 evidence 只能作为参考，不能直接标记新功能完成。

## 3. 统一单项验收协议

每个编号任务必须按以下顺序执行：

1. 确认对应 grammar production、semantic hook、绑定逻辑和 feature tag。
2. 增加或确认固定 seed 的定向生成测试。
3. 实际调用生成器，保存生成出的完整 SQL。
4. 检查 SQL 中包含目标语法，并且不包含被排除语法。
5. 执行 baseline `EXPLAIN`。
6. EXPLAIN 失败、返回空计划或出现 hint warning 时，不得把 SQL 发往三节点正式执行。
7. 对通过 EXPLAIN 的 SQL，在三个 MySQL 8.0.41 节点上执行。
8. 收集三节点结果、列元数据、warning、耗时、errno、SQLSTATE 和 feature tag。
9. 成功 SQL 必须满足三节点结果和 warning 一致。
10. 对预期失败 SQL，必须匹配预期 errno/SQLSTATE/warning；对 valid lane，任何非资源限制错误都必须阻塞当前任务。
11. 保存 SQL、seed、grammar hash、schema/data 场景、节点版本和运行结果。
12. 执行 replay，证明 artifact 可复现。
13. 更新任务状态和 checklist。
14. 只有当前任务全部通过，才能开始下一个任务。

## 4. 阶段 0：测试基础设施

### 4.1 三节点环境准入

要求：确认 baseline、custom_off、custom_on 三套 socket/endpoint 均为 MySQL 8.0.41。

验收：三套连接执行 `SELECT VERSION()` 均返回 `8.0.41`；任一节点版本不符则停止所有三节点 SQL 验证。

### 4.2 测试数据库生命周期

要求：每个测试独立创建数据库，测试结束后自动删除。

验收：测试中断、异常、超时后数据库仍能清理；不得复用上一次测试的表和数据。

### 4.3 数据场景固定化

要求：建立统一的空表、单行、多行、重复值、混合 NULL、全 NULL 和热点数据场景。

验收：每个场景有稳定 seed 和行数；三节点初始化后的表结构和数据摘要一致。

### 4.4 SQL 特征识别器

要求：为每个 feature 建立基于实际 SQL 的检查器，不能只依赖 feature tag。

验收：检查器可以从 SQL 中识别 `NOT EXISTS`、`RANGE BETWEEN`、`JOIN_ORDER`、`PARTITION` 等目标功能；tag 与 SQL 不一致时测试失败。

### 4.5 成功 SQL 记录器

要求：保存成功 SQL、结果摘要、列元数据、warning 和执行耗时。

验收：每条成功 SQL 都能独立 replay；replay 后三节点结果一致。

### 4.6 失败 SQL 记录器

要求：保存失败 SQL、三节点 observed error identity 和失败分类。

验收：失败 SQL 不依赖执行顺序反推；日志中直接包含 SQL 和三节点错误身份。

### 4.7 EXPLAIN 准入测试

要求：所有带 optimizer hint 的 SQL 必须先经过 EXPLAIN。

验收：EXPLAIN 失败、返回空计划或 hint warning 时，不进入三节点正式执行，也不计入成功覆盖。

### 4.8 单 feature 测试入口

要求：提供按 feature、variant、seed 运行单项验证的命令行入口。

验收：能够执行 `generate -> inspect SQL -> EXPLAIN -> execute triad -> compare -> save artifact`；任一步失败都返回非零退出码。

## 5. 阶段 1：grammar 主路径和安全边界

### 5.1 最新 grammar 主路径

要求：correctness 生产路径只能使用 `catalog/mysql-8.0.41-select.grammar.yy`。

验收：运行时记录 grammar hash；生成 SQL 的 coverage tag 必须包含 `grammar:*` 或 `grammar_alt:*`，不得回退到旧 `QueryGenerator`。

### 5.2 alternative 稳定 ID

要求：alternative ID 不依赖源码行号。

验收：在 grammar 中插入无语义变化的 alternative 后，原有 alternative ID 不发生不必要变化；测试覆盖 `grammar_alt:v1:*`。

### 5.3 生产排除族

要求：默认 correctness scope 不生成 JSON、FULLTEXT、SPATIAL 目标。

验收：随机和定向运行中，`json_scalar_function`、`fulltext_query`、`spatial_scalar_function` 不进入默认 scope；显式启用时必须经过 EXPLAIN 准入。

### 5.4 只读校验

要求：所有 grammar 生成 SQL 必须为单条只读语句。

验收：DDL、DML、用户变量、锁定读、多语句、外部文件、存储函数/UDF 全部被拒绝。

### 5.5 真实表绑定

要求：生成器只能使用当前 schema 中存在的表、列和别名。

验收：每个 SQL 执行前检查表名和列名均来自当前 manifest；伪造对象名时生成阶段失败。

### 5.6 真实索引和分区绑定

要求：索引名和分区名只能来自安全过滤后的元数据。

验收：SQL 中的 index/partition 名称均能在 manifest 或 `SHOW CREATE TABLE` 中找到；隐藏、FULLTEXT、SPATIAL、multivalue index 不得生成。

### 5.7 作用域隔离

要求：普通 derived table、CTE、LATERAL 和相关子查询分别遵守作用域规则。

验收：普通 derived table 不能引用外层列；LATERAL 和 correlated subquery 只能引用允许的外层列；非法引用必须在生成阶段拒绝。

## 6. 阶段 2：表、JOIN、modifier、索引和分区

### 6.1 普通表投影

要求：覆盖普通表、别名、表达式投影、`*`、qualified `*` 和 `TABLE`。

验收：每种形态至少生成一条实际 SQL，在三节点成功执行且结果列元数据一致。

### 6.2 SELECT modifier 栈

要求：覆盖 `ALL`、`DISTINCT`、`HIGH_PRIORITY`、`STRAIGHT_JOIN`、`SQL_SMALL_RESULT`、`SQL_BUFFER_RESULT`、`SQL_NO_CACHE`、`SQL_CALC_FOUND_ROWS` 等合法组合。

验收：每个 modifier 至少单独执行一次；互斥组合必须被拒绝或拆分到安全 lane；warning 三节点一致。

### 6.3 INNER/CROSS/STRAIGHT JOIN

要求：覆盖 INNER JOIN、CROSS JOIN、STRAIGHT_JOIN、逗号连接、带 ON 和无条件形态。

验收：每种 JOIN 生成 SQL 均实际执行；`STRAIGHT_JOIN` 不出现在不允许的 LATERAL 组合中。

### 6.4 LEFT/RIGHT JOIN

要求：覆盖 LEFT、RIGHT、OUTER JOIN 及 ON 条件。

验收：有匹配行、无匹配行和 NULL 补齐行三种数据场景均实际运行；三节点结果一致。

### 6.5 NATURAL JOIN

要求：覆盖 NATURAL INNER、NATURAL LEFT、NATURAL RIGHT。

验收：参与表存在共同列时生成 SQL；无共同列时不能伪造 USING 列；三节点成功。

### 6.6 USING 单列

要求：覆盖单列 USING JOIN。

验收：USING 列必须同时存在于两侧表；SQL 成功执行，结果列去重行为一致。

### 6.7 USING 多列

要求：覆盖多列 USING JOIN。

验收：至少生成 2 列和 3 列 USING；列顺序稳定；三节点结果一致。

### 6.8 三表和四表 JOIN tree

要求：覆盖嵌套三表和四表 JOIN tree。

验收：每种 JOIN tree 至少生成 5 条不同 seed 的 SQL；所有 SQL 均通过 EXPLAIN 并成功执行。

### 6.9 LEFT/RIGHT LATERAL

要求：覆盖相关 LEFT LATERAL 和 RIGHT LATERAL。

验收：相关外层列确实出现在子查询中；RIGHT LATERAL 不生成 STRAIGHT_JOIN；三节点执行成功；不允许的外层引用生成阶段失败。

### 6.10 表级 USE/FORCE/IGNORE INDEX

要求：覆盖 3 个 action × 4 个 scope：default、JOIN、ORDER BY、GROUP BY，共 12 个组合。

验收：12 个组合逐个生成 SQL，并检查 action、scope、真实 index 名和三节点 EXPLAIN/执行结果。

### 6.11 optimizer index hint 真实索引回退

要求：隐藏 secondary index 时回退到可见 PRIMARY 或其他可见 BTREE。

验收：SQL 不包含隐藏 index；能生成有效 fallback；只有隐藏索引时明确返回 `TargetNotReachable`。

### 6.12 显式 PARTITION

要求：覆盖单分区和多分区选择。

验收：每个 partition 名真实存在；分区不存在时不得生成；至少验证 1 个单分区和 1 个多分区 SQL。

## 7. 阶段 3：derived table、CTE、子查询和集合运算

### 7.1 普通 derived table

要求：覆盖普通 derived table 和显式输出列。

验收：父查询只能引用 derived 输出列；输出列名固定；SQL 三节点成功。

### 7.2 derived table 隐式列名

要求：覆盖允许的隐式输出列场景。

验收：生成 SQL 后实际检查 `cursor.description`，确认父层引用的列名真实存在。

### 7.3 derived query expression

要求：derived body 可以是完整 query expression，包括 set operation。

验收：至少执行 SELECT、TABLE、VALUES 与集合运算混合的 derived body；父层列绑定正确。

### 7.4 非递归单 CTE

要求：覆盖单个非递归 CTE。

验收：CTE 定义、引用、输出列全部实际执行；不能出现未定义 CTE。

### 7.5 多 CTE 独立关系

要求：覆盖多个互不依赖 CTE。

验收：每个 CTE 都被实际引用；三节点结果一致。

### 7.6 多 CTE 依赖关系

要求：覆盖 CTE A 被 CTE B 引用的依赖链。

验收：定义顺序正确；父层只能看到已定义 CTE；SQL 成功执行。

### 7.7 CTE 复用

要求：同一个 CTE 被多个位置引用。

验收：至少生成一次两次引用和一次三次引用；结果和 warning 三节点一致。

### 7.8 recursive UNION ALL CTE

要求：覆盖带终止条件的递归 CTE。

验收：递归终止；结果行数符合预期；无超时、无限递归或三节点差异。

### 7.9 recursive UNION DISTINCT

要求：覆盖递归 UNION DISTINCT。

验收：去重行为在三节点一致；递归结果不包含不预期重复行。

### 7.10 pair recursive CTE

要求：覆盖 `(n,total)` 双列递归 CTE。

验收：两列类型、递归引用、终止条件均正确；实际结果满足预先定义的数值关系。

### 7.11 scalar subquery

要求：覆盖返回单列单值的 scalar subquery。

验收：子查询最多返回一行；多行场景必须被限制或进入预期错误 lane。

### 7.12 row subquery

要求：覆盖 row constructor 与 row subquery 比较。

验收：左右列数相同；列类型可执行；不等长 row 必须被拒绝。

### 7.13 EXISTS/NOT EXISTS 基础

要求：覆盖 correlated 和 uncorrelated EXISTS/NOT EXISTS。

验收：外层有匹配、无匹配两种场景分别验证；SQL 中确实包含目标操作符；结果三节点一致。

### 7.14 NOT EXISTS 空结果

要求：NOT EXISTS 子查询固定返回空集合。

验收：生成 SQL 包含空集条件，例如 `WHERE (1 = 0)`；实际结果符合 NOT EXISTS 真值。

### 7.15 NOT EXISTS 单行结果

要求：NOT EXISTS 子查询返回单行。

验收：子查询确定返回一行；外层结果符合预期；三节点一致。

### 7.16 NOT EXISTS 多行结果

要求：NOT EXISTS 子查询返回多行。

验收：子查询至少返回两行；NOT EXISTS 结果符合预期；不得误将多行当作单行。

### 7.17 NOT IN 空结果

要求：NOT IN 子查询返回空集合。

验收：SQL 包含 `NOT IN (SELECT...)`；外层结果等价于真值预期。

### 7.18 NOT IN 单行结果

要求：NOT IN 子查询返回单行且 outer value 非 NULL。

验收：分别验证命中和不命中；结果与手工 oracle 一致。

### 7.19 NOT IN 多行结果

要求：NOT IN 子查询返回多行。

验收：至少覆盖一个命中值和一个不命中值；三节点结果一致。

### 7.20 NOT IN outer nullable

要求：NOT IN 左侧表达式可为 NULL。

验收：NULL 左值的结果必须符合 UNKNOWN/不返回语义；三节点结果一致。

### 7.21 NOT IN inner nullable

要求：NOT IN 子查询结果包含 NULL。

验收：子查询实际返回 NULL；不命中但 inner 含 NULL 的结果符合 MySQL 三值逻辑。

### 7.22 NOT IN outer/inner both nullable

要求：outer 和 inner 同时允许 NULL。

验收：覆盖 outer NULL、inner NULL、双方非 NULL、命中和不命中至少 6 种组合。

### 7.23 嵌套 NOT EXISTS/NOT IN

要求：覆盖 NOT EXISTS 内嵌 NOT IN、NOT IN 内嵌 NOT EXISTS。

验收：至少两层嵌套 SQL 成功执行；外层引用和内层引用均正确；结果可用手工 oracle 校验。

### 7.24 ANY/ALL 与 anti-subquery 混合

要求：覆盖 NOT EXISTS/NOT IN 与 ANY/ALL 同时出现在条件中的 SQL。

验收：所有子查询列数、类型、基数合法；三节点结果一致。

### 7.25 UNION/UNION ALL/UNION DISTINCT

要求：覆盖三种 UNION 形态。

验收：每个 operator 至少执行 3 条 SQL；去重和保留重复行为正确。

### 7.26 INTERSECT/INTERSECT ALL

要求：覆盖 INTERSECT 两种形态。

验收：至少覆盖 numeric、text、binary、temporal 四种类型；三节点结果一致。

### 7.27 EXCEPT/EXCEPT ALL

要求：覆盖 EXCEPT 两种形态。

验收：至少覆盖 numeric、text、binary、temporal 四种类型；三节点结果一致。

### 7.28 集合运算优先级

要求：覆盖混合 set operator 的默认优先级。

验收：SQL 包含至少两个不同 operator；括号解析结果与预期 AST/手工 oracle 一致。

### 7.29 集合运算括号反转

要求：覆盖括号改变结合顺序。

验收：同一组 operand 生成带括号和不带括号两类 SQL；两类结果符合预期。

### 7.30 49 个有序 set pair

要求：7×7 的 set operator 有序 pair 全部定向生成。

验收：49 个 pair 每个都有实际 SQL、EXPLAIN、三节点执行、结果/warning 比较和 replay artifact。

## 8. 阶段 4：窗口函数和 frame

### 8.1 `OVER()` 空窗口

要求：覆盖不带 PARTITION、ORDER、frame 的 `OVER()`。

验收：至少执行 RANK、ROW_NUMBER、COUNT 等函数；三节点结果一致。

### 8.2 单列 PARTITION BY

要求：覆盖单列窗口分区。

验收：数据包含至少两个 partition；结果排序和分区行为符合预期。

### 8.3 多列 PARTITION BY

要求：覆盖多列表达式 PARTITION BY。

验收：至少 2 列和 3 列组合；列顺序稳定；SQL 成功执行。

### 8.4 多列 ORDER BY

要求：覆盖窗口多列表达式 ORDER BY。

验收：包含 ASC、DESC 混合方向；增加唯一 tie-breaker；三节点结果一致。

### 8.5 ROWS UNBOUNDED PRECEDING

要求：覆盖 `ROWS UNBOUNDED PRECEDING`。

验收：实际 SQL 包含该边界；窗口累计结果符合手工计算。

### 8.6 ROWS CURRENT ROW

要求：覆盖 `ROWS CURRENT ROW`。

验收：实际 SQL 成功执行；结果只包含当前行对应 frame。

### 8.7 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW

要求：覆盖累计窗口 frame。

验收：至少验证 3 行和 5 行数据；结果符合累计计算。

### 8.8 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING

要求：覆盖全分区 frame。

验收：每行结果均与整分区聚合一致。

### 8.9 ROWS bounded preceding/current

要求：覆盖 `_uint PRECEDING` 到 CURRENT ROW。

验收：至少验证边界值 0、1、2；不得生成逆序 frame。

### 8.10 ROWS bounded preceding/following

要求：覆盖 preceding 到 following。

验收：至少执行 `2 PRECEDING AND 1 FOLLOWING`；结果与手工 frame 计算一致。

### 8.11 ROWS CURRENT ROW/following

要求：覆盖 CURRENT ROW 到 bounded FOLLOWING。

验收：实际结果符合当前行向后的窗口范围。

### 8.12 ROWS preceding/preceding

要求：覆盖 `UNBOUNDED PRECEDING AND 1 PRECEDING`、`2 PRECEDING AND 1 PRECEDING`。

验收：窗口合法；无 MySQL 语法错误；边界行为三节点一致。

### 8.13 ROWS following/following

要求：覆盖 `1 FOLLOWING AND 2 FOLLOWING` 和 `1 FOLLOWING AND UNBOUNDED FOLLOWING`。

验收：实际生成并执行；尾部行的空 frame 行为一致。

### 8.14 ROWS CURRENT/CURRENT

要求：覆盖 `ROWS BETWEEN CURRENT ROW AND CURRENT ROW`。

验收：每行窗口结果只依赖当前行；三节点结果一致。

### 8.15 numeric RANGE

要求：覆盖 4 个 numeric RANGE 组合。

验收：窗口 ORDER BY 使用数值列；每个组合至少一条三节点见证。

### 8.16 temporal RANGE

要求：覆盖 4 个 temporal RANGE 组合。

验收：窗口 ORDER BY 使用 DATE/DATETIME/TIMESTAMP；INTERVAL 单位和值合法；三节点一致。

### 8.17 frame-bound 合法性检查

要求：所有 generated frame bound 必须符合 MySQL 8.0.41 合法顺序。

验收：不存在 lower bound 大于 upper bound 的 valid SQL；非法组合必须在生成阶段拒绝。

### 8.18 不支持窗口语法 fail-closed

要求：GROUPS、IGNORE NULLS、FROM LAST、EXCLUDE 不进入 valid lane。

验收：尝试生成这些语法时明确拒绝；不得出现“生成成功但执行失败”的 valid SQL。

### 8.19 命名窗口继承

要求：覆盖命名窗口继承、追加 ORDER BY、追加 frame。

验收：命名窗口定义顺序正确；引用窗口真实存在；三节点执行成功。

### 8.20 ranking/navigation/value 函数

要求：覆盖 RANK、DENSE_RANK、ROW_NUMBER、LAG、LEAD、FIRST_VALUE、LAST_VALUE、NTH_VALUE、NTILE、PERCENT_RANK、CUME_DIST。

验收：每个函数至少有一条实际 SQL 在三节点执行；结果和 warning 一致。

## 9. 阶段 5：CAST、CONVERT、INTERVAL 和聚合

### 9.1 CAST 数值类型

要求：覆盖 SIGNED、UNSIGNED、DECIMAL、FLOAT、DOUBLE。

验收：每种 CAST 至少一条边界值 SQL；成功结果类型和 warning 三节点一致。

### 9.2 CAST 字符类型

要求：覆盖 CHAR、字符集和多字节文本。

验收：实际 SQL 包含目标类型和字符集；结果字符集/长度符合预期。

### 9.3 CAST 二进制类型

要求：覆盖 BINARY 和 binary literal。

验收：SQL 可执行；不会引入未登记的字符集转换错误。

### 9.4 CAST temporal 类型

要求：覆盖 DATE、TIME(6)、DATETIME(6)、YEAR、TIMESTAMP 边界。

验收：覆盖 `.499999`、最小值、最大值；三节点结果一致。

### 9.5 CONVERT

要求：覆盖 `CONVERT(..., type)` 和 `CONVERT(... USING utf8mb4)`。

验收：实际 SQL 可执行；Warning 仅允许预先登记的精确 warning。

### 9.6 INTERVAL 简单单位

要求：覆盖 MICROSECOND、SECOND、MINUTE、HOUR、DAY、WEEK、MONTH、QUARTER、YEAR。

验收：每个单位至少生成并执行一条 SQL；日期边界不越界。

### 9.7 INTERVAL 复合单位

要求：覆盖 YEAR_MONTH、DAY_HOUR、DAY_MINUTE、DAY_SECOND、DAY_MICROSECOND、HOUR_MINUTE、HOUR_SECOND、HOUR_MICROSECOND、MINUTE_SECOND、MINUTE_MICROSECOND、SECOND_MICROSECOND。

验收：每个复合单位至少一条三节点成功 SQL。

### 9.8 全 NULL 聚合

要求：覆盖 COUNT、MIN、MAX、SUM、AVG、统计聚合和 bit 聚合的全 NULL 输入。

验收：每个聚合结果符合预期 NULL/0 语义；三节点一致。

### 9.9 DISTINCT 聚合

要求：覆盖 COUNT DISTINCT 及其他允许 DISTINCT 的聚合。

验收：数据包含重复值；实际结果证明 DISTINCT 生效。

### 9.10 GROUP_CONCAT

要求：GROUP_CONCAT 使用同表达式 DISTINCT 和确定性 ORDER BY，并限制输出大小。

验收：输出顺序稳定；三节点字符串结果完全一致。

### 9.11 JSON_ARRAYAGG/JSON_OBJECTAGG

要求：保留确定性安全的 JSON 聚合形态。

验收：JSON_ARRAYAGG 使用顺序无关常量；JSON_OBJECTAGG key/value 来源稳定；三节点结果一致。

## 10. 阶段 6：确定性函数注册表

### 10.1 函数 signature 数量一致

要求：函数注册表、grammar alternative、测试 witness 三者数量一致。

验收：133 个 signature、202 个 NULL 参数位置、335 个 witness 数量自动校验。

### 10.2 数学函数值域

要求：实际执行数学函数的正常值、边界值和 NULL 值。

验收：每个 signature 在 normal、boundary profile 各至少一条实际 SQL，并对每个声明 NULL
位置执行 NULL SQL；三节点结果和 warning contract 一致。

### 10.3 字符串/二进制函数值域

要求：覆盖空串、ASCII、多字节、NUL、引号、反斜线和 binary input。

验收：normal、boundary、special profile 均实际生成并执行；覆盖空串、ASCII、多字节、
NUL、引号、反斜线和 binary input；结果不出现未登记字符集 warning。

### 10.4 temporal 函数值域

要求：覆盖 DATE、TIME、DATETIME、TIMESTAMP 的正常值、边界值和 NULL。

验收：normal、boundary、special profile 的三节点结果和类型元数据一致。

### 10.5 控制流函数

要求：覆盖 COALESCE、IF、IFNULL、NULLIF、GREATEST、LEAST 等。

验收：每个声明 NULL 参数位置均有实际 SQL；normal、boundary、special 结果符合手工
oracle，三节点一致。

### 10.6 编码和 hash 函数

要求：覆盖 MD5、SHA1、SHA2、STATEMENT_DIGEST 等。

验收：三种 profile 的正常值无 warning；已知 `Warning 1583` 只在
`encoding_sha2_2_null_1` 出现，并由 registry contract 精确登记。

### 10.7 IP 函数

要求：覆盖 IPv4、IPv6 文本、数字和二进制输入。

验收：三种 profile 的合法 IPv4/IPv6 文本、数字和二进制输入均成功；NULL 输入结果符合
注册表声明；三节点一致。

### 10.8 函数错误契约

要求：登记每个 signature 可能产生的错误、warning 或禁止值域；无登记项的 profile 必须
是无 warning、无 error 的 valid lane。

验收：非预期 errno/SQLSTATE 阻塞当前函数；预期 warning/error 必须精确匹配；当前
registry 唯一 valid warning 为 SHA2 NULL 的 1583。

### 10.9 三节点 335 witness 全量重跑

要求：在当前工作树使用 canonical grammar 重新执行 335 个 witness 的 normal、boundary、
special 三个 profile。

验收：1005 条 SQL 均有执行记录、canonical grammar hash、SQL 和 production trace；warning、
结果、列元数据和错误身份三节点一致。

## 11. 阶段 7：optimizer hint 正向/负向矩阵

### 11.1 derived MERGE

要求：对真实 derived alias 生成 `MERGE(alias)`。

验收：SQL 包含真实 alias；EXPLAIN 无 hint warning；三节点执行成功。

### 11.2 derived NO_MERGE

要求：对真实 derived alias 生成 `NO_MERGE(alias)`。

验收：alias 存在；EXPLAIN 和执行成功；三节点结果一致。

### 11.3 DERIVED_CONDITION_PUSHDOWN

要求：对真实 derived alias 生成 `DERIVED_CONDITION_PUSHDOWN(alias)`。

验收：hint 语法有效；EXPLAIN 不产生 hint warning；SQL 成功执行。

### 11.4 JOIN_ORDER 双表

要求：对两个真实表 alias 生成 `JOIN_ORDER(a,b)`。

验收：两个 alias 均存在且顺序可识别；三节点无 warning。

### 11.5 JOIN_ORDER 三表和四表

要求：对三表和四表 JOIN tree 生成完整 JOIN_ORDER。

验收：hint alias 数量和 FROM 中表 alias 数量一致；EXPLAIN 成功。

### 11.6 INDEX primary

要求：生成使用 PRIMARY 的 optimizer index hint。

验收：PRIMARY 真实存在；SQL 执行成功；不得误选隐藏索引。

### 11.7 INDEX secondary

要求：生成使用可见 secondary BTREE 的 optimizer index hint。

验收：secondary index 真实存在且可见；三节点 EXPLAIN 和执行一致。

### 11.8 NO_RANGE

要求：覆盖 NO_RANGE hint 的合法形态。

验收：hint 不产生 warning；SQL 成功执行；记录 plan 是否存在。

### 11.9 NO_ICP fallback

要求：单表场景生成带真实 alias 的 `NO_ICP(alias)` fallback。

验收：SQL 不再出现 `NO_ICP()`；三节点 EXPLAIN 无 hint warning。

### 11.10 hint 负向矩阵

要求：覆盖 alias 不存在、索引不存在、隐藏索引、hint 不适用等负向场景。

验收：负向 SQL 不进入 valid lane；失败原因可识别；不得把 hint warning 当作有效 SQL。

## 12. 阶段 8：随机差分和错误回灌

### 12.1 最新 grammar 3 分钟短批次

要求：使用当前工作树最新 grammar、固定 seed、三节点；每次运行最多约 3 分钟，
并限制为两个可观察 round，使程序在本批次内实际生成、执行并完成任务。

固定验收命令：

```bash
uv run python scripts/run_mysql8041_socket_soak.py \
  --sockets /tmp/sf8041-b.sock,/tmp/sf8041-o.sock,/tmp/sf8041-n.sock \
  --duration-seconds 150 --max-rounds 2 --queries-per-round 100 --workers 1 \
  --seed 20260716 \
  --artifact-root artifacts/latest-grammar-random-3m-final-20260716 \
  --run-id latest-grammar-random-3m-final-20260716 --full-thread-sql-log
```

验收：实际耗时不超过 180 秒；完成 2 个 round；artifact 记录 grammar hash、seed、
版本、配置、query count、round count、成功/拒绝/资源上限和 finding；至少有一条实际
生成并执行的 SQL；不得存在未归因 finding。该短批次替代原 30 分钟长跑，长跑不再作为
本轮交付门槛。

### 12.2 错误 fingerprint 聚合

要求：对所有 rejected、resource-limit、warning、error 进行分类。

验收：每个错误 fingerprint 都有 SQL、feature tag、seed、三节点 observed error、分类、是否可避免和是否需要回灌。

### 12.3 1690 overflow 归因

要求：定位 unsigned overflow SQL 生成路径。

验收：确认是合法预期错误还是可避免候选；可避免时增加约束，并用 replay 证明消失。

### 12.4 3513 binary bit operand 归因

要求：定位 binary bit operand length 错误。

验收：确认 operand 类型和长度来源；修复后旧 SQL 不再被 valid lane 生成。

### 12.5 3854 binary-to-utf8mb4 归因

要求：定位 binary 到 utf8mb4 转换错误。

验收：确认是允许 warning、预期错误还是可避免组合；修复后重新运行对应 SQL。

### 12.6 1038 sort memory 归因

要求：定位排序内存超限 SQL。

验收：区分真正 resource-limit 和可以通过限制投影、排序列、行数避免的候选。

### 12.7 历史 finding replay

要求：复放历史 `result_mismatch` finding。

验收：每个历史 finding 被归类为当前仍可复现、已修复、旧生成器产物、环境/资源问题或误报。

### 12.8 随机覆盖概率

要求：检查每个约 3 分钟短批次对 alternative、frame、hint、subquery、set pair 的命中
分布；低概率项必须通过定向矩阵补齐，不得为了命中率无限延长单次运行。

验收：记录未命中项；低概率项有定向 SQL 证据，或调整 grammar alternative 权重后由
下一批短跑复核。

## 13. 阶段 9：artifact、文档和交付

### 13.1 artifact 清理规则

要求：只保留当前工作树可复现的最终运行、定向 witness 和必要 replay；本轮中断的长跑
诊断不得混入最终证据目录。

验收：旧生成器 SQL、重复 smoke、失败诊断和无来源 artifact 被清理或移入
`artifacts/archive/query-generation-acceptance-20260716/`；最终证据目录能够独立定位。

### 13.2 artifact 目录规范

要求：目录名包含日期、grammar、运行时长或矩阵类型和 seed；`manifest.json`/事件日志
必须记录 grammar hash、节点版本和运行配置。

验收：仅凭目录名和 metadata 即可定位运行来源；本轮最终 evidence 至少包括 anti、frame、
hint 三个定向矩阵、三个函数 profile 矩阵和一个 3 分钟随机批次。

### 13.3 checklist 状态更新

要求：每个条目只在对应证据完整后标记 `[x]`。

验收：有代码但无实际 SQL 执行保持 `[~]`；有实际执行但未三节点一致保持 `[~]`；有非预期 finding 不得标记 `[x]`；明确不支持且 fail-closed 的功能附 errno 证据。

### 13.4 测试证据索引

要求：建立 feature、variant、测试文件、artifact、SQL 数量和状态索引。

验收：任一 checklist 条目都可以反向找到测试和实际 SQL artifact。

### 13.5 按主题拆分 commit

要求：至少拆成以下主题：

- grammar/core generation；
- scope/index/partition；
- frame/window；
- hints；
- subquery/anti-join；
- function registry；
- runtime/artifact；
- tests and documentation。

验收：每个 commit 单独测试通过，commit message 能对应 checklist 条目。

### 13.6 合并前最终回归

要求：运行全量单元测试、静态检查、所有可用 MySQL 集成测试和最终 3 分钟随机差分短批次。

验收：

```text
pytest: 0 failed
ruff: 0 failed
三节点 valid SQL: 0 非预期错误
三节点 finding: 0 未归因 finding
required witness: 100% 通过
artifact: 可 replay
```

### 13.7 最终发布门槛

要求：只有所有 SQL 生成功能、三节点见证、随机差分归因、artifact 整理和代码交付条件全部完成后，才允许将本轮 checklist 标记为完成。

验收：

只有同时满足以下条件，才算本轮完成：

- NOT EXISTS/NOT IN 矩阵闭环；
- frame-bound 合法组合闭环；
- optimizer hint 正反矩阵闭环；
- 函数 335 witness 的 normal/boundary/special 三个 profile（1005 条 SQL）在当前工作树重新通过；
- 最新 grammar 三节点 3 分钟短批次完成，实际耗时不超过 180 秒；
- 所有 error fingerprint 已归因；
- 可避免错误已回灌；
- artifact 已清理；
- checklist 已更新；
- commit 已拆分并提交。

## 14. 推荐执行顺序

1. 完成阶段 0，先打通三节点实际测试入口。
2. 执行 5.1 至 5.7，确认新 grammar 主路径和安全边界没有问题。
3. 执行 6.1 至 6.12，完成表、JOIN、modifier、index、partition 验证。
4. 执行 7.1 至 7.30，优先完成 NOT EXISTS/NOT IN 矩阵，再完成 49 个 set pair。
5. 执行 8.1 至 8.20，逐项完成 frame 和窗口函数见证。
6. 执行 9.1 至 9.11，完成 CAST、INTERVAL 和聚合见证。
7. 执行 10.1 至 10.9，重新运行当前工作树的 335 个函数 witness。
8. 执行 11.1 至 11.10，完成 optimizer hint 正向/负向矩阵。
9. 执行 12.1 至 12.8，按 3 分钟短批次运行随机差分并归因全部错误。
10. 执行 13.1 至 13.7，清理 artifact、更新 checklist、拆分 commit 并提交。

## 15. 当前执行状态与实际证据

计划状态：全部核心待办已按“单功能生成 → SQL 特征检查 → EXPLAIN → 三节点执行 →
结果/警告比较 → artifact → 下一功能”顺序完成。

本轮实际证据：

| 功能 | 实际 SQL 数量 | 三节点结果 | 证据 |
|---|---:|---|---|
| NOT EXISTS/NOT IN 空/单/多/NULL/嵌套矩阵 | 12 | 通过 | `artifacts/latest-grammar-matrix-20260716/` |
| 全部合法 numeric/temporal frame bound | 25 | 通过 | `artifacts/latest-grammar-frame-matrix-20260716/` |
| optimizer hint 正向矩阵 | 11 | 通过 | `artifacts/latest-grammar-hint-matrix-20260716/` |
| optimizer hint 负向矩阵 | 4 类 | 生成阶段拒绝 | `tests/generation/test_query_grammar.py` |
| 函数 registry normal/boundary/special profile | 1005 | 通过 | `artifacts/latest-grammar-function-normal-20260716/`、`latest-grammar-function-boundary-20260716/`、`latest-grammar-function-special-20260716/` |
| 最新 grammar 3 分钟随机短批次 | 200 | 0 未归因 finding | `artifacts/latest-grammar-random-3m-final-20260716/` |

最终回归已实际执行：`ruff check src tests scripts` 通过；`uv run pytest -q` 为
`1779 passed, 20 skipped, 1 warning`。之前中断的长跑只作为诊断记录，不作为本轮 3 分钟
验收依据；其产物必须位于明确 archive 目录。
