# `test` 数据库单次初始化 SQL 导出设计

## 目标

基于仓库当前已经生成的 `sql_base_tables/` 内容，导出一份可由 MySQL 客户端直接执行的完整 SQL 文件，用于复现任务启动流程的前五步。导出过程不重新随机生成基表或种子数据。

## 输出文件

- 路径：`artifacts/select_fuzz_init_test_once.sql`
- 目标数据库：`test`
- 编码：UTF-8
- 执行方式：支持通过 MySQL 客户端的标准输入一次性执行

## 内容和顺序

输出文件严格按以下顺序拼接：

1. 文件头风险说明，明确脚本会删除并重建 `test` 数据库。
2. 执行 `SELECT CONNECTION_ID()`，对应连接建立后的会话信息查询。
3. 执行 `DROP DATABASE IF EXISTS test`、`CREATE DATABASE test`、`USE test`。
4. 按自然文件名顺序原样写入 `t0.sql` 至 `t78.sql`，创建 79 张基表。
5. 原样写入 `zz_seed_fk_data.sql`：
   - 创建并填充 `_select_fuzz_seed_numbers`；
   - 从 `t78` 到 `t0` 清理已有数据；
   - 从 `t0` 到 `t78` 插入本次固定种子数据。
6. 对 `t0` 至 `t78` 各执行一条 `SELECT COUNT(*)`，并使用表名作为结果别名，便于人工核对每张表的初始化行数。

## 不包含的内容

- 不包含持续模糊测试阶段生成的随机查询。
- 不包含后台循环、超时看门狗、暂停、恢复或停止逻辑。
- 不包含多 worker 连接中重复创建临时表的会话初始化。
- 不新增通用导出命令或修改现有运行逻辑。

## 生成方式

使用仓库现有 SQL 拆分和自然排序规则读取 `sql_base_tables/`，通过一个短期生成步骤拼接固定前后段与已有 80 个 SQL 文件。生成结果作为独立工件保存，不修改源 SQL。

## 验证标准

生成后执行静态验证，至少确认：

- 数据库操作顺序为 `DROP DATABASE`、`CREATE DATABASE`、`USE`。
- 包含 79 条基表 `CREATE TABLE` 或 `CREATE TEMPORARY TABLE`。
- 包含种子脚本中的 79 条 `DELETE FROM`。
- 包含 80 条种子相关 `INSERT INTO`：1 条辅助数字表插入和 79 条基表插入。
- 文件末尾包含 79 条 `SELECT COUNT(*)` 校验语句。
- `t0.sql` 至 `t78.sql` 以及 `zz_seed_fk_data.sql` 的内容顺序与运行时自然排序一致。
- 文件不包含持续随机查询样例。

## 回滚与风险

执行该文件会删除目标实例中的整个 `test` 数据库及其已有数据。生成文件本身是新增工件，可直接删除回滚；本次工作不修改现有程序、基表源文件或配置。
