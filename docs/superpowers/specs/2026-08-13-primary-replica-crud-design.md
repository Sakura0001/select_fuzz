# 主写备读逐表 CRUD 设计

## 目标

在保留 79 张内置基表、每表 42 个核心列及版本化扩列种子的前提下，增加可选的主写备读压力模式：74 张永久表各运行一个独立 DML worker 和一个独占主节点连接；用户配置数量的查询 worker 各运行一个独占备节点连接。DML worker 只执行随机 INSERT、UPDATE、DELETE，查询 worker 只执行非锁定 SELECT。

## 已确认语义

- `host:port` 始终表示主节点；`replica_host` 表示备节点，`replica_port` 留空时继承主端口。
- 不检查主备地址是否相同，不检查 `@@read_only`，不等待复制完成，也不采集或展示复制延迟。
- 新能力由 `enable_crud` 开关控制，默认关闭；不开启时保留旧任务行为。
- 开启后只覆盖 74 张永久表，排除 session 级临时表 `t2` 到 `t6`。
- 74 个 DML worker 各持有一个主节点连接；`thread_count` 表示查询 worker 数量，各持有一个备节点连接。
- DML worker 在 10～200 行的软边界内随机等权选择 INSERT、UPDATE、DELETE；边界优先：估算行数小于等于 10 时 INSERT，大于等于 200 时 DELETE。单条语句最多影响 10 行。
- UPDATE 不修改 PRIMARY、UNIQUE、FOREIGN KEY、分区键或 generated 列；允许修改普通二级索引列。DELETE、UPDATE 随机作用于当前行。
- SQL 正常返回后立即开始下一次操作，不增加固定间隔。
- 单 worker 断连不会切换任务全局状态，也不会阻塞其他 worker。该 worker 保存待执行 SQL，按 0.1、0.2、0.4 秒递增并在 5 秒封顶，无限重连后重试同一 SQL；暂停或停止可中断退避。
- 已确认不要求提交一致性：断连后的原 SQL 始终重试。约束冲突不写逐条 SQL/失败文件，只累计失败并生成下一条 DML。
- 每个新任务的基表、查询和 CRUD 根种子均随机生成；显式填写旧 seed 时可以复现。重连、暂停和恢复不更换 seed。查询和 CRUD 生成器均使用版本 `v1`，worker 子种子通过 SHA-256 按角色与 worker 身份派生。
- 任务卡展示主写/备读目标、三组版本化 seed、CRUD 汇总和主备重连状态；不默认展开 74 个 DML worker，不展示复制延迟。
- 自定义基表目录不支持逐表 CRUD，开启时在建立数据库连接前拒绝。

## 架构

### 连接与路由

`RuntimeService` 构造两个角色化节点。主节点负责初始化数据库以及全部 DML；备节点只负责查询。若没有填写 `replica_host`，查询端点兼容性回退到主地址。跳板机场景为主、备分别建立隧道，因为一个 `JumpTunnel` 只能绑定一个远端目标。

初始化仍由主节点连接完成。开启 CRUD 后，该连接成为第一个永久表的 DML 连接，再创建 73 个主连接；查询侧按 `thread_count` 创建备连接。worker 对象在启动线程前完整登记，停止时逐个容错关闭。

### worker 状态机

worker 保留整数 `worker_id` 兼容现有 API，并增加稳定 `worker_key`、`worker_type`、`db_role`、`table_name`、生成器版本/seed、当前操作和独立重连状态。查询 worker 编号为 `0..N-1`，DML worker 编号接续其后；seed 不依赖这个可变编号，而按 `query:<序号>` 或 `dml:<表名>` 派生。

worker 每次 step 只完成一个动作。成功或普通 SQL 错误返回 `0`，后台循环立即进入下一步；断连返回当前退避秒数。待重试 SQL 只有在成功或普通错误后才清除。全局任务只有初始化不可恢复错误才失败；运行期单连接错误保持任务为“执行 SQL”。

### SQL 生成

新增 `DMLGenerator`，输出包含 SQL、操作类型和请求行数的值对象。INSERT 使用内置 v1 冻结的列和值表达式，并显式生成稳定批次值；UPDATE/DELETE 通过 MySQL 单表 `ORDER BY RAND(seed) LIMIT N` 选择随机现存行，每次最多 10 行。由于用户明确不要求断连幂等或一致性，原 SQL 重放可能再次命中不同记录，这是接受的压力语义。

查询生成器增加显式的 `allow_locking` 和 `allow_temporary_tables` 选项。备节点运行路径将两项关闭，确保不会生成 `FOR UPDATE`、`FOR SHARE`、`LOCK IN SHARE MODE` 或访问 session 临时表；默认值保持旧生成器测试覆盖。

### 监控与界面

SQL 日志补充 worker、角色、目标、表、操作和生成器身份。JSONL 按文件加锁以承受 74+N 并发写入。任务快照分别统计查询、INSERT、UPDATE、DELETE 的成功/失败以及主备重连数；高频计数保留内存，避免每条语句同步写 SQLite。

前端增加备节点地址/端口、逐表 CRUD 开关、查询及 CRUD 复现字段。`thread_count` 文案改为“备节点查询线程数”，默认 16。任务卡只显示 DML worker 汇总，详细列表放在默认折叠区域。

## 安全边界与清理

- 端口范围 1～65535，查询线程数 1～128；DML worker 固定最多 74 个。
- 数据库连接仍使用既有连接、读写和执行超时；断连退避封顶 5 秒，避免离线节点忙循环。
- pause、stop 和终态竞态必须覆盖初始化连接、74 个主连接、N 个备连接和两个隧道；迟到连接在发现终态后立即关闭。
- 停止任务保留数据库现场，不自动执行广泛清理或 DROP。

## 验收

- 旧请求未携带新字段时行为和响应兼容。
- 开启后恰好 74 个 DML worker/主连接和 N 个查询 worker/备连接；5 张临时表不出现在任何运行期 worker 的表池。
- 备查询从不生成锁定读；DML 从不发往备节点，SELECT 从不发往主节点（无备地址的兼容模式除外）。
- 同 `v1+seed` 生成相同的 per-worker 决策序列；新任务缺省 seed 互不相同；重连不重置生成器。
- 单节点或单 worker 反复断连时其他 worker 继续运行，待执行 SQL原文重试，stop 能中断退避并释放全部资源。
- 后端全量测试、前端静态行为测试和生产构建全部通过。
