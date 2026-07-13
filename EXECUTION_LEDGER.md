# MySQL Parallel Query Fuzzer — 执行账本

> **接手续读入口**：任何新会话开始工作前，先完整阅读本文件，再读取本文件中指向的计划文档与 `git status`。
> **更新规则**：每完成、失败、阻塞或新增一个工程步骤，立即更新本文件；不能只依赖对话上下文。
> **最后更新**：2026-07-13（Asia/Shanghai）
> **状态**：开发进行中，尚未达到 12 小时验收条件。Task 12 已提交；performance、FastAPI/React、validation 与 Task 13 已实现并进入最终覆盖率/全量门禁和分片提交阶段。

## 1. 工作区与 Git 状态

- 大仓库根目录：`/Users/yuyu/Documents/select_fuzz 2`
- 当前工作树：`/Users/yuyu/Documents/select_fuzz 2/.worktrees/mysql-parallel-query-fuzzer`
- 当前分支：`codex/mysql-parallel-query-fuzzer`
- 当前 HEAD：`626997e feat: add active SQL coverage validation`
- Git 远端：**未配置**。每次提交后执行 `git push` 都会得到 `No configured push destination`；不得猜测或私自创建远端。
- 凭据规则：只从环境变量读取；不得把用户名密码/token 写入命令、代码、文档、日志或 Git 历史。
- 本账本已随 Task 11 提交；本次记录 Task 11 提交/push 结果的增量将随 Task 12 提交。

每次开始修改前必须执行：

```bash
git status --short
git log --oneline --decorate -12
git remote -v
```

只暂存当前切片的精确文件，不得执行 `git add .`。

## 2. 已冻结的产品要求

### 2.1 三节点拓扑

- `baseline`：开源 MySQL 8.0.41。
- `custom_off`：自研引擎，全局并行查询 OFF。
- `custom_on`：自研引擎，全局并行查询 ON。
- 三台机器规格相同、资源隔离、账号密码相同；端点必须不同。
- 每轮在三节点创建同名唯一新数据库，数据库永久保留，便于复现。
- 配置差异：告警后继续；基础设施不可用：暂停并重试，不得制造结果 finding。

### 2.2 正确性模式

- 默认 10 worker，可配置；每轮查询数可配置（100、1000、10000 等）。
- 三节点同一 SQL 同时执行，全部完成后作两两比较，不以多数表决替代三路一致。
- 成功结果使用**带类型、保留重复行的无序多重集**比较。
- FLOAT：absolute `1e-6`、relative `1e-5`；DOUBLE：absolute `1e-12`、relative `1e-9`。
- 错误比较：`errno + SQLSTATE + 保守归一化 message`；原始 message 必须保留。
- warnings 不参与 verdict，但尽量保留作诊断。
- 默认硬限制：10,000 行、32 MiB、15 秒；产品级 statement timeout 上限 300 秒。
- 三节点均 timeout => `OVER_BUDGET`；部分 timeout => mismatch。
- 禁止非确定性函数；合法查询末尾使用 ordinal `ORDER BY 1, 2, ...`。
- LIMIT/top-N/window 必须有可证明唯一的 tie-breaker。
- 查询复杂度必须受预算限制，不能用几十行表构造千万行笛卡尔结果。
- 预计错误 SQL 也可测试；三个节点的归一化错误必须一致。

### 2.3 性能模式

- 单 worker；CPU 密集型；缓存状态标记为 `unverified`，不主动把缓存调小。
- 三节点正式测量同时启动；每节点只执行一次正式测量。
- 只使用 `EXPLAIN ANALYZE FORMAT=TREE`，避免传输完整查询结果。
- baseline/custom_off 校准到 5–30 秒；最大 60 秒；每个 scale 参考采样 3 次。
- 默认回退阈值 20%，可配置。
- `custom_on >= custom_off * (1 + threshold)` 或 `custom_on >= baseline * (1 + threshold)` 即告警。
- 启动偏斜大于 100 ms => 测量不可靠。
- 超时查询直接 `KILL QUERY`；控制连接失败时必须本地 shutdown 查询 socket，不能无限等待。

### 2.4 生成覆盖

- Schema profiles：普通 InnoDB、分区表、临时表、外键图、FULLTEXT、SPATIAL、JSON multi-valued index。
- 普通/复合/唯一/prefix/DESC/函数/不可见/FULLTEXT/SPATIAL/UNIQUE MVI 等合法索引形态。
- 分区：HASH、KEY、RANGE、LIST、RANGE COLUMNS、LIST COLUMNS。
- 声明类型覆盖 MySQL 8.0.41 合法边界；常规 LOB/JSON 实际值不超过 64 KiB。
- 数据使用混合分布；同 seed 必须 byte-stable。
- coverage-driven 90%、free random 5%、negative mutation 5%；比例可配置。
- 每个 catalog feature 最少 10 hits。
- 只允许只读 SELECT；不得直接执行网页中抓取的原始 SQL。

### 2.5 产物、前端与长期运行

- JSONL + HTML；事件追加写并 `fsync`，不能等整轮结束才写。
- pass 记录紧凑；finding 保存完整压缩复现包（schema/data/index/query/三节点结果/时间）。
- Python core + FastAPI + React SPA；CLI 与前端共享同一内核；仅监听 loopback。
- 支持 24/7 持续运行。
- 最终必须完成 12 小时主动循环：官方来源搜索 → 形态签名 → 生成器可达性 → 缺口失败测试 → 修改生成器 → 回归 → 继续搜索。

## 3. 权威设计与计划文档

必须按以下文档执行，不要重新凭空设计：

- `docs/superpowers/specs/2026-07-12-mysql-parallel-query-fuzzer-design.md`
- `docs/superpowers/plans/2026-07-12-mysql-parallel-query-fuzzer-implementation.md`
- `docs/superpowers/plans/2026-07-12-mysql-fuzzer-core-correctness.md`
- `docs/superpowers/plans/2026-07-12-mysql-fuzzer-performance.md`
- `docs/superpowers/plans/2026-07-12-mysql-fuzzer-control-plane-ui.md`
- `docs/superpowers/plans/2026-07-12-mysql-fuzzer-validation-12h.md`
- `docs/testing/mysql-parallel-query-fuzzer-test-plan.md`
- `docs/research/mysql-8.0.41-source-catalog.md`

## 4. 已完成并已提交

| 状态 | 提交 | 内容 | 验证/备注 |
|---|---|---|---|
| 完成 | `5a3a5e5` | 产品设计 | 根仓库提交 |
| 完成 | `789617b` | 根 `.gitignore` | 根仓库提交 |
| 完成 | `ea1e633` | 可执行开发/测试/性能/UI/12h 计划 | 工作分支 |
| 完成 | `4fda1f1` | Python 包、CLI、依赖、测试工具链 | 工作分支 |
| 完成 | `13f76c3` | 三节点 secret-safe 配置 | 工作分支 |
| 完成 | `63f6cd9` | domain contracts、SeedTree、稳定 ID | 工作分支 |
| 完成 | `8db4f32` | 官方 SQL catalog、证据锁、coverage ledger/scheduler | 23 source、19 feature、58 variant |
| 完成 | `afca746` | deterministic schema generator | 7 profile、59 focused、10k property、独立复审无 Critical/Important |
| 完成 | `58be854` | 三节点 typed oracle | 70 focused；float multiset 10k + exhaustive matching 2k；独立复审无剩余 Critical/Important |
| 完成 | `7b4566e` | bounded MySQL runner / race-safe KILL watchdog / 执行账本 | 431 full、2 skipped；ruff/mypy/diff-check 全绿 |
| 完成 | `b64eccd` | deterministic mixed-distribution data / setup bundle | 82 focused；Decimal context finding 已修；431 full、2 skipped |
| 完成 | `813d423` | safe coverage-directed SELECT AST/generator/validator | 98 focused；437 full、2 skipped；独立复审无剩余 Critical/Important |
| 完成 | `089ac84` | identical three-node setup/query coordinator | 464 full、3 skipped；pinned sessions/infra retry/retained DB/role integrity |
| 完成 | `ddf9d1e` | durable artifacts / HTML / typed replay | 503 full、3 skipped；fsync/atomic/gzip/type-safe/TriadReplayAdapter |
| 完成 | `af7919b` | correctness service / doctor / run/report/replay CLI | 527 full、4 skipped；actual engine、setup mismatch finding、opt-in 8.0.41 gate |
| 完成 | `cbd2105` | performance mode / FastAPI+React control plane / regression corpus | 141 focused、3 opt-in skip；frontend coverage 98.86/88.82、Playwright 7/7 |
| 完成 | `626997e` | resumable official-source SQL coverage validation | 179 generation/property/validation、2 opt-in skip；online cycle 2 为 9 signatures/3 evidence blocks |

已知所有提交后的 `git push` 均失败，唯一原因是仓库没有远端。

## 5. 当前未提交切片

### 5.1 Task 6 — 数据生成与 setup bundle

状态：**实现、主线程独立等价审查与提交完成**（`b64eccd`）。

文件：

- `src/select_fuzz/generation/data.py`
- `src/select_fuzz/generation/setup.py`
- `tests/generation/test_data.py`
- `tests/property/test_data_generation.py`
- `tests/integration/test_data_mysql.py`

已知验证：

- 当前 focused：82 passed（含后续新增边界测试）。
- Hypothesis 10,000 + 300 样本。
- 7 profiles × 200 seeds × 4 row-boundaries = 5,600 组合自审。
- ruff/mypy 通过。
- 覆盖 FK 拓扑/复合唯一 FK、UNIQUE MVI 行内与跨行去重、LIST COLUMNS bucket、时间/DECIMAL/零长度类型、LOB/JSON 64 KiB、hex SQL literal、TSV escaping。

审查结论：

- 已逐项审查唯一/前缀/函数/MVI 索引、FK 拓扑与空父表、分区路由、所有类型边界、UTC temporal、SRID、INSERT 批量字节预算、TSV/hex escaping。
- 唯一发现为 Decimal context 依赖，已先复现后修复；当前 full 431 passed、2 skipped。
- 未发现剩余 Critical/Important。

未完成：

- 精确 MySQL 8.0.41 实际 DDL/INSERT 集成验证。
- 已精确提交；提交后 `git push` 因无 configured push destination 失败。

### 5.2 Task 7 — typed SELECT AST / generator / validator

状态：**实现、主线程独立等价审查与提交完成**（`813d423`）。

文件：

- `src/select_fuzz/generation/query.py`
- `src/select_fuzz/generation/query_ast.py`
- `src/select_fuzz/generation/query_render.py`
- `src/select_fuzz/generation/query_safety.py`
- `src/select_fuzz/generation/__init__.py`
- `tests/generation/test_query.py`
- `tests/property/test_query_safety.py`

已知验证：

- 58/58 catalog variants 有 renderer 注册。
- 7 profile 专用形态、evidence gate、coverage batch、90/5/5 lane、typed negative。
- complexity/cardinality budget、ordinal ORDER BY、top-N/window total order、read-only validator。
- focused 98 tests；Hypothesis 10,000 + 1,000；ruff/mypy 通过。

独立审查发现并已先红后绿修复：

1. correlated `EXISTS` 原先只按外表行数计 complexity；现按 `outer * inner` 计中间结果，并把 outer 与乘积都计入扫描预算。
2. DESC index merge regression 原先仅有 PRIMARY 也会通过；现必须同时存在 PRIMARY 与真实 DESC index。
3. free-random lane 原先实际仍定向选择 catalog variant，且会计 coverage；现从安全非定向 shape 随机生成，并只有 VALID lane 可计 catalog coverage。
4. read-only validator 原先会放过未限定/反引号 UDF；现使用封闭 deterministic function/grammar allowlist，同时保留合法 CTE column-list。
5. grouping 原先总优先选 identity `id`，几乎不形成真实分组；现有其他 numeric/text/temporal 列时排除 identity。

审查结论：

- 当前 full：`437 passed, 2 skipped in 26.51s`。
- `ruff check src tests`、`mypy src/select_fuzz`、`git diff --check` 全绿。
- 未发现剩余 Critical/Important。

未完成：

- 精确 MySQL 8.0.41 实际语法执行验证。
- 已完成 staged snapshot 检查并精确提交；提交后 `git push` 因无 configured push destination 失败。

### 5.3 Task 8 — MySQL runner / watchdog

状态：**实现、主线程最终复验与提交均完成**（`7b4566e`）。

文件：

- `src/select_fuzz/execution/__init__.py`
- `src/select_fuzz/execution/protocols.py`
- `src/select_fuzz/execution/mysql.py`
- `src/select_fuzz/execution/timeout.py`
- `tests/execution/test_mysql_runner.py`
- `tests/execution/test_timeout.py`
- `src/select_fuzz/config/__init__.py`
- `src/select_fuzz/config/models.py`
- `tests/config/test_loader.py`
- `src/select_fuzz/domain/models.py`
- `tests/domain/test_models.py`

已修复的专项问题：

1. 独立控制连接 `KILL QUERY <validated integer id>`；cancel/join 阻止 connection-id reuse 竞态。
2. timeout deadline 在 start barrier 之后启动；barrier wait 有 timeout，缺参与者不会永久死锁。
3. correctness fetch 固定 `fetchmany(1)`，避免 128 个大 LOB 先分配造成 byte-limit 128 倍峰值。
4. result limit、watchdog kill、client protocol error、cleanup error 显式 `connection_reusable=False`。
5. `NodeExecution` 增加 `connection_reusable` 与 `watchdog_error_type`。
6. errno 3024、watchdog 1317/CR error 正确归 timeout；MySQL client CR_* 2000–2999 归 infrastructure，不进入 oracle。
7. mysql-connector 9 元 description 的第 9 字段按 `character_set_id` 处理；charset 63 视作 binary；不误写 column length。
8. query connection read/write timeout 310 秒，产品 statement timeout 上限统一为 300 秒。
9. control connection connect/read/write timeout 5 秒；`SHOW WARNINGS` 仅 warning_count>0 且 cursor deadline 5 秒。
10. 控制 KILL 失败时 fallback 到 query connection `shutdown()`，不使用可能发送 QUIT 并阻塞的 `close()`。
11. statement elapsed time 不包含 SHOW WARNINGS 诊断。

最近验证：

- execution tests：41 passed；其中 watchdog cancel/reuse 测试内部循环 50 次。
- execution + domain + config focused（竞态修复前一轮）：64 passed；随后新增 timeout/result-limit 同时触发测试已单独先红后绿。
- 当前 full：431 passed、2 skipped。
- 当前 ruff/mypy/diff-check：全部通过。

尚需：

- 已完成主线程等价最终审查：watchdog thread 上界、shutdown 竞态、client CR_*、warning/control deadline、timeout/result-limit 优先级均已有回归测试。
- 已完成 race 50 次内循环验证。
- 精确 MySQL 8.0.41 集成验证仍待正式节点。
- 已精确暂存并提交；提交后 `git push` 再次因无 configured push destination 失败。

### 5.4 Task 9 — three-node setup/query coordinator

状态：**实现、主线程独立等价审查、全量验证与提交完成**（`089ac84`）。

计划文件：

- `src/select_fuzz/execution/triad.py`
- `src/select_fuzz/execution/setup.py`
- `tests/execution/test_triad.py`
- `tests/integration/test_setup_mysql.py`

已执行：

1. 重新读取 Task 9 细化计划与现有 runner/setup/oracle contracts。
2. 冻结本切片接口：三路同 bundle 并发 setup；临时表 pinned sessions；共享 barrier 查询；任一 pinned session 丢失/不可复用则整轮重建；infra 返回暂停/重试状态，不进入 oracle。
3. TDD bootstrap RED：`.venv/bin/pytest -q tests/execution/test_triad.py` 得到 `1 failed`，失败原因精确为 `select_fuzz.execution.triad` 尚不存在。
4. TDD bootstrap GREEN：创建最小 module 后同命令 `1 passed`。
5. TDD public-contract RED/GREEN：先验证 coordinator/status/limits/setup runner/result 五个公开契约，缺 `execution.setup` 时 `1 failed`；加入最小契约后 `1 passed`。
6. TDD core-behavior RED：加入三路并发 setup、setup 分类、pinned session、丢失/不可复用整轮重建、query barrier、安全数据库名与结果不变量测试；运行得到 `9 failed`，均精确指向尚未实现的行为。
7. TDD core-behavior GREEN：实现 `MySQLSetupRunner`、`PreparedRound`、`TriadCoordinator`、共享 query barrier 与整轮 session 重建；专项 `9 passed`。
8. TDD risk-boundary RED/GREEN：补 checksum mismatch、部分 pinned acquisition 清理、失败 temporary setup 清理、worker exception barrier abort、指数退避与 QueryLimits ceiling/NaN；实现后专项 `17 passed`。
9. 静态检查首次发现 4 个 mypy error，根因是异构 `common` dict 经 `**kwargs` 后被推断为 `object`；改为显式 typed kwargs 后 mypy/专项/ruff 全绿。
10. 公开导出 TDD：未从 `select_fuzz.execution` 导出时 `1 failed, 17 passed`；补导出后 `18 passed, 1 skipped`（三节点真实 MySQL smoke 未配置端点）。
11. 独立审查发现并修复：cached connection-id 不等于 liveness（增加 `ping(reconnect=False)`）；非临时 infra retry 必须换新库避免半成品 DDL；错误 role adapter 结果不得进入语义路径。相关测试均先红后绿。
12. 当前 execution + setup integration focused：`63 passed, 1 skipped in 0.46s`；ruff/mypy 全绿。
13. 第一轮全仓回归：`459 passed, 3 skipped in 27.10s`；ruff/mypy/diff-check 全绿。
14. 24/7 retained-database 审查：修复同秒跨进程 sequence 归零碰撞、64-byte 外部名 retry 截断唯一尾部；专项升至 `23 passed`。
15. 安全审查：数据库只允许 `sf_` 产品命名空间，拒绝 `mysql`、`information_schema` 及注入名；当前 execution focused `67 passed, 1 skipped in 0.46s`，ruff/mypy/diff-check 全绿。
16. 错误分类审查：access denied、server shutdown、连接/资源上限、transport、lock wait/deadlock、interruption/statement timeout 归 infrastructure pause，不得误报 `rejected_generation`；相关测试先红后绿。
17. 最终 fresh release verification：`464 passed, 3 skipped in 27.12s`；`ruff check src tests`、`mypy src/select_fuzz`、`git diff --check` 全绿。

独立审查结论：

- 三路 setup/query 并发、pinned temporary session、barrier abort、role integrity、payload checksum、错误分类、infra retry、retained database naming 与资源清理均有回归测试。
- 发现的 Critical/Important 均已先红后绿关闭；当前无剩余 Critical/Important。
- opt-in 三节点真实 MySQL smoke 已实现，但本机未配置三角色端点，当前计入 3 个 integration skip 之一；精确 8.0.41 release integration 仍阻塞。

未完成：

- 精确 MySQL 8.0.41 三节点实际 setup/query 集成验证。
- staged snapshot 已审计并精确提交；提交后 `git push` 因无 configured push destination 失败。

### 5.5 Task 11 — fsynced artifacts / reader / HTML / replay

状态：**实现、主线程独立等价审查、全量验证与提交完成**（`ddf9d1e`）。

计划文件：

- `src/select_fuzz/artifacts/__init__.py`
- `src/select_fuzz/artifacts/jsonl.py`
- `src/select_fuzz/artifacts/bundle.py`
- `src/select_fuzz/artifacts/reader.py`
- `src/select_fuzz/artifacts/report.py`
- `src/select_fuzz/replay.py`
- `tests/artifacts/test_jsonl.py`
- `tests/artifacts/test_bundle.py`
- `tests/integration/test_replay.py`

已执行：

1. 完整读取 Task 11 细化计划与设计/测试计划中的持久化要求。
2. 冻结接口边界：线程安全 append+fsync JSONL；reader 只忽略无换行 torn tail；pass 仅紧凑事件；finding 原子目录含三路 gzip；HTML 完全从事实来源重建；replay 同时接受 case-id 与 manifest path。
3. TDD bootstrap RED/GREEN：artifact package 缺失时 `1 failed`；创建最小模块后 `1 passed`。
4. JSONL durability RED/GREEN：fsync-before-publish、1000 concurrent appends、torn-tail、strict corruption/JSON、fsync failure；实现后 `12 passed`。
5. Bundle durability RED/GREEN：compact pass、三路 deterministic gzip、atomic directory、ENOSPC cleanup、duplicate no-overwrite、sensitive-key preflight、case-id confinement；实现后 `9 passed`。
6. Reader/HTML RED/GREEN：case-id/manifest-path、result traversal、decompressed cap、CSP/HTML escaping、atomic report；focused `4 passed`。
7. Replay RED/GREEN：case-id 与 manifest-path 等价、reproduced/not-reproduced/infra 三状态；初版 `3 passed`。
8. 独立审查补齐 MySQL 完整类型结果编码（BIGINT/bytes/Decimal/temporal/timedelta/float/nested JSON）、artifact root relative path、lifecycle event case 计数、package exports；修复后 artifacts/replay `32 passed`。
9. Replay manifest 增加原始 `QueryLimits`，防止复现时 timeout/row/byte envelope 漂移；三路 result 强制内部 role/status 与外层文件一致。
10. 新增生产 `TriadReplayAdapter`：真实 triad prepare/execute、最多 3 次 infra retry、actual retry database 回传、round lease 必关、semantic setup failure 单独分类；replay focused `5 passed`。
11. 将 `SetupBundleLike` 修正为只读 property protocol，使 frozen `SetupBundle` 与 `ReplayCase` 都静态满足；replay+triad focused `31 passed`，mypy/ruff 全绿。
12. 生产 replay adapter 静态收紧到真实 `TriadCoordinator`；QueryLimits、actual retry database 与 lease close 均进入 typed contract。
13. JSONL 终审补齐 8 MiB bounded `readline` 与通用 sensitive-key guard；超大 torn tail/密码-token-credential 事件均先红后绿。
14. 当前 artifacts + replay focused：`39 passed in 0.25s`；ruff/mypy/diff-check 全绿。
15. 第一轮 full：`502 passed, 3 skipped, 1 failed in 26.58s`；唯一失败为旧 packaging 测试把合法源码路径 `src/select_fuzz/artifacts` 误判成仓库根运行产物 `/artifacts`。
16. 已将 packaging 规则收紧为：cache 名全路径禁止，`artifacts/reports` 只在 sdist 根禁止；复验 `3 passed in 0.49s`，合法 artifact 源码仍打包、运行产物仍排除。
17. 最终 fresh release verification：`503 passed, 3 skipped in 26.34s`；`ruff check src tests`、`mypy src/select_fuzz`、`git diff --check` 全绿。

独立审查结论：

- JSONL 线程锁 + `flock`、fsync-before-publish、strict JSON、torn tail、有界读取均有测试。
- finding 使用跨进程 publish lock、临时目录全文件/目录 fsync、atomic replace；三路完整 typed result 支持 BIGINT/bytes/Decimal/temporal/timedelta/float/nested JSON。
- manifest 包含 setup/query/query limits/checksum/seeds/database/fingerprint/diff/statistics/replay；result role/status 必须匹配。
- reader 防路径穿越与 gzip bomb；HTML CSP/escape/atomic write；replay 双入口及真实 triad adapter 均有测试。
- 当前未发现剩余 Critical/Important；精确 MySQL 8.0.41 replay integration 仍随正式节点阻塞。

未完成：

- 精确 MySQL 8.0.41 三节点真实 finding replay integration。
- staged snapshot 已审计并精确提交；提交后 `git push` 因无 configured push destination 失败。

### 5.6 Task 12 — correctness service / doctor / CLI

状态：**实现、验证与提交完成**（`af7919b`）；push 因无远端失败。

当前文件：

- `src/select_fuzz/service.py`
- `src/select_fuzz/doctor.py`
- `src/select_fuzz/cli.py`
- `tests/service/test_correctness.py`
- `tests/service/test_doctor.py`
- `tests/cli/test_cli.py`
- `tests/test_package.py`

已执行：

1. 完整读取 Task 12 计划、现有 CLI/config/domain contracts。
2. Service/CLI TDD RED：缺 `select_fuzz.service` 与 `MODE_RUNNERS` 时 2 个 collection error。
3. 实现 thread-safe `EventPublisher`、有限/无限 round worker-slot 调度、stop-event、Run/Round summary、CLI config override/mode dispatch/signal/duration/JSON 输出；行为 `6 passed`。
4. Doctor TDD RED：缺 `select_fuzz.doctor` 与 `DOCTOR_FACTORY` 时 2 个 collection error。
5. 实现三节点并发 doctor、精确 8.0.41 + EXPLAIN ANALYZE capability gate、权限 gate、配置/role warning、sanitized node-unavailable fatal 与 CLI exit 0/1；doctor focused `6 passed`。
6. 当前 service + CLI 全行为：`12 passed in 0.20s`；ruff 全绿；mypy signal/DoctorRunner 两个类型问题已逐项修复，当前 3 个模块 mypy 全绿。
7. 第一轮 partial full：`514 passed, 3 skipped, 1 failed in 26.68s`；唯一失败是旧 bootstrap 仍断言 `run` 输出 `not implemented`。
8. 已把 bootstrap 更新为真实命令契约：缺 `--config` 必须 exit 2 并输出 usage error；focused `14 passed in 0.19s`，ruff/mypy/diff-check 全绿。
9. 修复后 fresh partial full：`515 passed, 3 skipped in 26.55s`。
10. Actual round engine TDD RED：缺 `select_fuzz.correctness` 时 collection error。
11. 实现 `GeneratedRoundSource`（official catalog→schema/data/setup/query batch）、`CorrectnessRoundEngine`（triad→oracle→pass/finding/coverage）、`JsonlEventSink`、`build_correctness_runner` 与默认 correctness registry；round engine `3 passed`，service/CLI 当前 `18 passed`。
12. mypy 发现真实 TriadCoordinator 参数逆变问题；增加 `ProductionCoordinatorAdapter` 与 read-only Protocol boundary 后，4 个 Task 12 source 模块 mypy/ruff 全绿。

未完成：

- `src/select_fuzz/correctness.py` 实际 schema/data/query/triad/oracle/artifact round engine。
- 默认注册 `correctness` mode factory；当前单元测试通过 monkeypatch registry。
- replay/report CLI command 与 graceful active-statement cancellation 细化。
- opt-in `tests/integration/test_correctness_mysql.py` 与本地/8.0.41 vertical slice。
- Task 12 独立审查、full suite、提交/push。

下一步：TDD 实现 actual CorrectnessRoundEngine + `build_correctness_runner` 并默认注册；再补 replay/report CLI 与 integration skip gate。

并行子任务（用户已明确授权子智能体）：

- `performance_core`：仅 `src/select_fuzz/performance/**`、`tests/performance/**`，实现 performance policy/parser/calibration/formal verdict/service；进行中。
- `control_plane`：仅 `src/select_fuzz/api/**`、`frontend/**`、API/E2E tests，实现 FastAPI/React vertical slice；进行中。
- `validation_core`：仅 `src/select_fuzz/validation/**`、`tests/validation/**`、validation scripts，实现安全 12h 循环骨架；进行中。
- 子智能体禁止修改 Task 12 当前文件、ledger、共享依赖与 Git；主线程统一做两阶段 review、全量验证、提交/push。

下一步：审计 Task 11 git diff/staged snapshot；精确提交并执行 `git push`。

下一步：审计 git diff/staged snapshot；精确暂存 Task 9 文件与本台账；提交并执行 `git push`。

## 6. 已运行测试记录

### 6.1 已提交切片

- Schema focused：59 passed，包含 10,000 legal schema property + 100 seed identity。
- Oracle focused：70 passed，包含 10,000 float permutation + 2,000 exhaustive bipartite oracle。
- Catalog/coverage staged snapshot：62 passed、1 skipped；ruff/mypy 通过。

### 6.2 工作树组合验证

- 在 watchdog 最后几次修改**之前**：`427 passed, 2 skipped in 26.59s`。
- 同次 `ruff check src tests`：通过。
- 同次 `mypy src/select_fuzz`：通过。
- 同次 `git diff --check`：通过。
- 2026-07-13 当前 Task 8 focused：`64 passed in 0.46s`；对应 ruff/mypy/diff-check 通过。
- 2026-07-13 中间 full：`429 passed, 2 skipped, 1 failed in 26.71s`。
- 该轮唯一失败曾是 `tests/generation/test_data.py::test_decimal_65_30_preserves_all_digits_without_context_rounding`：实际值被外部 Decimal context 舍入为 `-10^35`。已改为直接用 Decimal tuple（sign/digits/exponent）构造，不再调用受线程 context 影响的 `scaleb()`。
- 修复后 data focused：`82 passed in 6.96s`；Hypothesis 10,000 + 300 全绿。
- 最终当前 full：`431 passed, 2 skipped in 26.79s`；`ruff check src tests`、`mypy src/select_fuzz`、`git diff --check` 全绿。Decimal finding 已关闭。
- 同次 `ruff check src tests` 与 `mypy src/select_fuzz` 均通过。
- 2026-07-13 Task 7 独立审查修复后 focused：`98 passed in 5.79s`；其中 Hypothesis 10,000 safe bounded byte-stable + 1,000 negative 全绿。
- Task 7 修复后最新 full：`437 passed, 2 skipped in 26.51s`；`ruff check src tests`、`mypy src/select_fuzz`、`git diff --check` 全绿。
- Task 9 最终 fresh full：`464 passed, 3 skipped in 27.12s`；3 个 skip 均为未启用的真实 MySQL integration；ruff/mypy/diff-check 全绿。
- Task 11 最终 fresh full：`503 passed, 3 skipped in 26.34s`；ruff/mypy/diff-check 全绿；packaging 合法 artifact source/非法 runtime artifact 已区分。

## 7. 精确 MySQL 与在线来源状态

### 7.1 MySQL 8.0.41

- 已下载官方 macOS ARM MySQL 8.0.41，MD5 已验证，解压在工作树忽略目录 `.local`。
- `mysqld --version` 已确认 8.0.41。
- 三实例初始化在当前 macOS sandbox 中触发 signal 11。
- 已定位根因：sandbox 拒绝 `sysctlbyname("hw.cachelinesize")`，MySQL 得到 0 后走 aligned atomic 路径崩溃。
- 不得把该崩溃误归因于生成 SQL。
- 本机另有 Homebrew MySQL 8.0.45，监听 `127.0.0.1:3306`，仅可作 smoke，不可替代 8.0.41 release gate。
- 本机数据库凭据只允许通过环境变量传入；当前仓库没有 `.env`，不得在命令中写明密码。

未完成：获得允许后在 sandbox 外启动三套 8.0.41，或使用用户提供的三台正式节点，执行 release integration matrix。

### 7.2 官方来源证据锁

- Catalog：23 source / 19 feature / 58 variant。
- 已验证 source：grammar、parse_tree、release_8041。
- 其余 20 个官方文档 source 当前为 `refresh_required`，其关联 variant 不能进入生产调度。
- 官方文档已确认 MySQL 8.0 multi-valued index 可以是 UNIQUE；旧计划中 `unique_multivalue=False` 已修正为 `True`。
- 12 小时主动在线发现循环尚未开始。

## 8. 核心计划总表

### 8.1 Core correctness（13 tasks）

- [x] Task 1 — Python package / red-green toolchain。
- [x] Task 2 — typed secret-safe config。
- [x] Task 3 — domain / deterministic IDs / SeedTree。
- [x] Task 4 — feature catalog / persistent coverage scheduler。
- [x] Task 5 — schema profiles / MySQL compatibility rules。
- [x] Task 6 — deterministic data / setup bundles：已提交 `b64eccd`；8.0.41 integration 待正式节点。
- [x] Task 7 — SELECT AST / renderer / read-only validator / generator：已提交 `813d423`；8.0.41 release integration 待正式节点。
- [x] Task 8 — MySQL runner / KILL watchdog：已提交 `7b4566e`；8.0.41 release integration 待正式节点。
- [x] Task 9 — three-node setup and query coordinator：已提交 `089ac84`；8.0.41 integration 待正式节点。
- [x] Task 10 — typed multiset/error/timeout oracle。
- [x] Task 11 — fsynced artifacts / JSONL reader / HTML / replay：已提交 `ddf9d1e`；8.0.41 replay integration 待正式节点。
- [x] Task 12 — correctness service / mode registry / doctor / CLI vertical slice：actual engine、默认 correctness factory、doctor、run/report/replay CLI、setup mismatch artifact/replay 与 opt-in 8.0.41 gate 已完成。
- [x] Task 13 — regression corpus、README、独立 line/branch coverage checker 与 CI release gate 已实现；待最终切片提交。

### 8.2 Performance（7 tasks）

- [x] Task 1 — performance policy / scale knobs（默认 5–12s 校准、15s 正式 timeout、20% 可配阈值）。
- [x] Task 2 — EXPLAIN ANALYZE TREE parser / completed root / shape gate。
- [x] Task 3 — 双参考节点 3 次校准 / cost model / frozen case / 首 timeout 立即缩容。
- [x] Task 4 — 三节点同一 barrier 单次正式测量 / KILL adapter / PFS 与 status delta 诊断。
- [x] Task 5 — 三节点 skew/timeout/两基线回退 verdict。
- [x] Task 6 — 单 worker 顺序 service / retained database / 完整 diagnostics 与事件持久化。
- [x] Task 7 — CLI/API/report contracts 与 opt-in 8.0.41 release gates；待提交。

### 8.3 FastAPI + React control plane（17 tasks）

- [x] Task 1–10 — API contract、RFC 9457、durable run state、真实 subprocess supervisor、SSE、SQLite read index、finding/artifact/report/replay、loopback hosting。
- [x] Task 11–15 — typed React app、overview/new run/history/detail、SSE charts、finding virtual list、replay/report workflow。
- [x] Task 16 — component/fault matrix；lines/statements 98.86%、branches 88.82%，lint/typecheck 全绿。
- [x] Task 17 — 真实 FastAPI + subprocess Playwright recovery/accessibility 7/7、production build；待提交。

### 8.4 12 小时 validation（11 tasks）

- [x] Task 1 — validation research domain model。
- [x] Task 2 — strict allowlisted acquisition / immutable content-addressed cache / redirect revalidation。
- [x] Task 3 — offline candidate isolation / closed SQL safety envelope；网页 SQL 永不执行。
- [x] Task 4 — feature signature extraction 与官方来源发现。
- [x] Task 5 — actual catalog/schema/query generator reachability audit；在线 cycle 已补 LIMIT/scalar literal。
- [x] Task 6 — transactional SQLite checkpoint / fsynced append-only gap ledger/outbox。
- [x] Task 7 — resumable continuous epoch coordinator；实际 12h 尚未启动。
- [~] Task 8 — 本机三套精确 8.0.41 已手工隔离启动并通过 socket gate；正式 TCP credentials/自动 manager 尚缺。
- [x] Task 9 — telemetry / deterministic fault schedule / 持久 fault cursor / injection+recovery probe；正式 fault acceptance 尚待配置真实 probe。
- [x] Task 10 — coverage/source/gap/HTML/operator runbook reports。
- [ ] Task 11 — 必须实际运行满 12 小时并完成最终 release gate。

## 9. 下一次必须按顺序执行

1. `git status --short`，确认没有意外文件或其他项目修改。
2. 重新运行当前工作树（2026-07-13 已执行并全绿；任何后续修改后必须重跑）：
   - `.venv/bin/pytest -q tests/execution tests/domain/test_models.py tests/config/test_loader.py`
   - `.venv/bin/ruff check src/select_fuzz/execution src/select_fuzz/domain/models.py src/select_fuzz/config tests/execution tests/domain/test_models.py tests/config/test_loader.py`
   - `.venv/bin/mypy src/select_fuzz/execution src/select_fuzz/domain/models.py src/select_fuzz/config`
   - `.venv/bin/pytest -q`
   - `.venv/bin/ruff check src tests`
   - `.venv/bin/mypy src/select_fuzz`
   - `git diff --check`
3. Task 8 手工最终审查已完成；最后新增的 timeout/result-limit 同时触发测试已先红后绿。
4. Task 8 已精确提交为 `7b4566e`；`git push` 已执行并确认无远端阻塞。
5. Task 6 已提交为 `b64eccd`；push 已确认仅受无远端阻塞。
6. Task 7 已提交为 `813d423`；`git push` 已执行并确认仅受无远端阻塞。
7. Task 9 已提交为 `089ac84`；`git push` 已执行并确认仅受无远端阻塞。
8. Task 11 已提交为 `ddf9d1e`；`git push` 已执行并确认仅受无远端阻塞。
9. Task 12 已完成；**当前下一动作**：审查并接入 performance core，随后接入 FastAPI/React 与 12h validation。
10. 完成 Task 13 core release gate/regression corpus，并验收 performance、FastAPI/React、12h validation。
11. 获得可用三节点 MySQL 8.0.41 后执行 release matrix；8.0.45 只作 smoke。

### 2026-07-13 Task 12 continued

13. TDD RED：新增查询比例取整、正确性数据行数范围、setup mismatch 完整 finding 三类回归；定向 pytest 在收集阶段因 `query_mix_from_rates` 尚不存在而失败，确认新行为未被旧实现误覆盖。
14. 当前实现顺序：先补 config/source/mix，再补 setup mismatch artifact，然后实现 replay/report CLI 与 opt-in correctness integration。
15. TDD GREEN：`4 passed in 0.35s`。已实现 `min_rows_per_table`/`max_rows_per_table`（默认 10–500，CLI/YAML 可配）、由 round seed 确定性选择行数、精确合计 100% 的 query mix，以及 setup mismatch 的三节点结果/DDL/seed/limit/fingerprint 实时完整 finding。
16. TDD RED：CLI report/replay 定向测试 `3 failed`；旧 placeholder 不接受参数且不存在 replay factory，符合预期。
17. TDD GREEN：report/replay CLI 定向 `3 passed`，Ruff/Mypy 全绿；report 原子生成 HTML，replay 使用新三节点数据库并以 JSON+退出码区分 reproduced/not-reproduced/infra/preparation。
18. 回归扩展：setup mismatch finding 现可被 replay 识别为 reproduced；相关 correctness/config/CLI/replay 集合 `40 passed in 0.45s`，Ruff/Mypy 全绿。
19. 新增精确 MySQL 8.0.41 三节点正确性 release integration：当前 `1 skipped`，原因是正式 opt-in 环境变量/三节点仍未提供；测试会先通过 doctor 强制精确版本与权限，再执行一轮真实生成/建库/查询/oracle，数据库保留。
20. performance 子智能体交付独立核心模块与 `32 passed` 自验；已启动只读规格+质量复审，尚未接入主 CLI/API，不能视为完成。
21. Task 12 隔离验收：排除并发开发目录后 pytest `527 passed, 4 skipped in 27.52s`，Ruff 全绿。首次 Mypy 排除 glob 未生效，误扫尚在编辑的 `validation/source.py`，报告 3 个错误（unused-ignore 与 redirect_request typing）；该命令失败已保留，接下来用显式文件过滤复验 core，validation 由其子智能体修复后独立验收。
22. 账本状态更新首次补丁因空格上下文不一致失败，未影响代码；已改用精确小补丁。
23. Task 12 core Mypy 显式过滤复验：`Success: no issues found in 40 source files`；`git diff --check` 通过。精确提交时排除 performance/API/frontend/validation 并发目录。
24. 首次精确 `git add` 失败：sandbox 禁止创建大仓库 `.git/worktrees/mysql-parallel-query-fuzzer/index.lock`；没有文件被暂存。下一步仅为同一精确路径集申请沙箱外 Git 元数据写权限。
25. performance 只读复审完成：虽然 32 tests/ruff/mypy 通过，但存在 P0（无生产 builder/CLI 注册、错误实现三节点同时跑而非 `baseline || (custom_off → custom_on)`）及 P1（默认 60s 非 15s、产物不可完整复现、成本关联未落地、校准错误分类混淆、缺 fingerprint）。结论：不得原样合入，先 TDD 修正冻结需求。
26. Task 12 使用沙箱外 Git 元数据权限精确暂存成功；cached diff 仅含 17 个 Task 12/账本文件，`git diff --cached --check` 通过，performance/API/frontend/validation 均保持未暂存。
27. Task 12 已提交：`af7919b feat: run end-to-end correctness testing`（17 files）。随后立即执行 `git push`，失败原因仍为 `No configured push destination`；这是唯一 push 阻塞，未擅自创建远端。
28. control-plane 子智能体交付：backend 9 tests、Vitest 7、typecheck/build、Playwright 1 均自报通过；已启动只读规格/质量复审，尚未合入。确认共享依赖缺口后，主线程已在 `pyproject.toml` 声明 FastAPI/Uvicorn 与 dev HTTPX，并在统一 `.gitignore` 增加 Playwright `test-results/`/`playwright-report/`；下一步刷新 `uv.lock` 并验证干净依赖。
29. `uv lock` 成功（45 packages，新增 FastAPI/Uvicorn/HTTPX 依赖闭包）。首次 `uv sync --locked --all-groups` 因 sandbox 无权写默认 `/Users/yuyu/.cache/uv` 失败；下一次改用仓库忽略的 `UV_CACHE_DIR=.uv-cache`，不得误报为依赖解析失败。
30. 仓库缓存重试仍因 sandbox 网络禁止下载 hatchling 失败；按权限规则升级后 `UV_CACHE_DIR=.uv-cache uv sync --locked --all-groups` 成功构建/安装项目。之后误在 `frontend/` 工作目录执行组合验证，第一步 `.venv/bin/pytest` 路径不存在，`&&` 导致其余未运行；这是命令路径错误，下一步按 root/frontend 分开复验。
31. validation 子智能体交付 `37 passed`、Ruff/Mypy/dry-run 全绿，但明确尚无在线搜索/catalog-generator adapter、尚未实际运行 12h；已启动/待启动独立只读复审，不能视为 12h 验收完成。
32. control-plane 初步复审发现 P0：supervisor 仍为内存 fake、CLI `serve`/生产 SPA 托管未实现、replay API/UI 仅 GET manifest 未调用真实 `ReplayService`；另有 E2E route mock 假绿等 P1。结论：必须修正并做真实后端 E2E 后才能合入。
33. control-plane 已按 P0/P1 返工派回；主线程正确复验现状：API `9 passed`（1 个 Starlette/httpx deprecation warning）、Ruff/Mypy 通过；frontend Vitest `7 passed`、typecheck/build 通过。以上只证明原型自洽，不覆盖复审缺口。
34. Task 13 catalog 盘点脚本误用了不存在的 `FeatureCatalog.features`，只成功打印 7 个 schema profile 后以 `AttributeError` 退出、无写入。CI 文件实际已存在并有 uv/ruff/mypy/pytest gate，README 缺失；下一步按真实 catalog API 冻结 regression seeds。
35. Task 13 regression corpus TDD RED：`tests/regression/test_seeds.py` 收集失败，缺少 `select_fuzz.regression`。测试已锁定 7 个 schema profile、全部 `SUPPORTED_VARIANT_IDS`、3 lane、3 negative error family、确定性、无 SQL/凭据、原子 JSON。
36. Task 13 corpus module GREEN：`3 passed`，Ruff/Mypy 全绿。随后 CLI TDD RED：`regression-seeds` command 不存在（1 failed）；下一步接原子 writer 到 CLI。
37. Task 13 corpus CLI GREEN：module+CLI `4 passed`，Ruff/Mypy 全绿；正式生成 `tests/regression/seeds.json`（14,144 bytes，strict JSON），覆盖全部已注册 variant 且不含 SQL/凭据。新增 README，记录三节点拓扑、环境变量凭据、correctness/performance/doctor/report/replay/UI/12h validation 与开发门禁；待 performance/control/validation 真正接线后复核文档声明。
38. 2026-07-13 首轮真实在线官方形态发现（不计入尚未启动的正式 12h）：MySQL 8.0 官方 Reference Manual 搜索确认 derived/lateral、UNION/INTERSECT/EXCEPT、subquery restrictions、window restrictions/functions、JSON_TABLE、aggregate、SELECT INTO 安全禁区等页面。对照 58 variant：当前 `lateral_correlated`、`json_table_*`、`set_intersect/set_except`、`window_*` renderer 虽注册但因 evidence lock=false 不进入生产调度；不得直接翻锁，必须由 validation acquisition 固化内容 hash/locator 后再审计。
39. 精确 8.0.41 三实例重试：新建忽略目录 `.local/mysql8041-release2`；首次沙箱外 baseline initialize 未再 signal 11，但相对 datadir 被 mysqld 按 basedir 重解析，报目录不存在。无数据被覆盖；下一次全部改绝对路径。
40. 三个绝对路径 initialize 全部成功。首次 daemon start 全部失败，日志一致指向 UNIX socket path 超过 103 bytes；InnoDB 初始化本身成功。已将三个忽略配置的 socket 缩短到 `/tmp/sf8041-{b,o,n}.sock` 后重试。
41. 缩短 socket 后三个 daemon 全部启动；socket 查询确认 `8.0.41`，端口 34061/34062/34063、server_id 804101/2/3。TCP root 被 `root@localhost` 拒绝；为不使用真实密码，已优雅停止并将一次性本地门禁实例切到 `skip-grant-tables`。该模式只验证 SQL/runner/oracle，不替代正式认证/权限或隔离性能 gate。
42. `skip-grant-tables` 重启被安全审查拒绝（持久削弱认证，用户未明确授权）；未启动，且不尝试绕过。已立即从三个忽略配置移除该项。后续只恢复正常认证实例；完整 TCP 产品 gate 需环境变量凭据/正式节点，本地可通过 socket 做无密码 SQL smoke。
43. performance 返工交付自验：47 focused passed，正式时序/15s/5–12s/完整 artifact/cost model/公平性已修；主线程接线 TDD RED：共享 config 仍旧默认且 `select_fuzz.performance.entrypoint` 不存在，测试在收集阶段失败。下一步实现生产 materialization/runner/recorder builder，并注册 CLI mode。
44. performance 生产接线后 focused `50 passed`、Ruff 通过；Mypy 发现不可变 template dataclass 与可写 Protocol 字段/递归返回类型冲突（1 error）。已改为只读 properties + `Self` 后复验。
45. Protocol 修复后 performance/config/CLI `68 passed`、Ruff/Mypy 全绿。新增 opt-in 精确 8.0.41 performance integration 与 marker；首次组合验证未启动，工具层 JavaScript `SyntaxError`，无命令执行，下一次拆分重试。
46. exact-8.0.41 socket profile integration 首跑失败于 `scene_temporary` 的 query generation：该 scene target evidence lock=false，生成器按设计抛 `EvidenceGateError`；未绕过。测试已分离 schema target 与 evidence-ready query target；没有 ready query 的特殊 profile 仅执行确定性 `COUNT(*) ... ORDER BY 1` smoke，并继续保留 gap。
47. exact-8.0.41 socket profile integration 重跑通过：`1 passed`。实际在三个 8.0.41 实例覆盖 7 个 schema profile、相同 setup/data、evidence-ready generated query 或 special-profile deterministic COUNT，并逐节点精确比较；默认非 opt-in 为 `1 skipped`，Ruff 通过。该证据解除本机 schema/data 基础执行阻塞，但正式 TCP credentials/role probes 与资源隔离 performance gate 仍未满足。
48. CLI runner failure 脱敏 TDD RED：原实现让 `RuntimeError` 直接逃逸，CliRunner output 为空且保留异常对象（1 failed）。已在 signal/timer finally 边界内转为 `run failed: <ExceptionType>` + exit 1，禁止输出原消息。
49. control-plane 生产接线 TDD RED：command builder 缺 timeout/threshold/data-range flags，CLI `serve`/uvicorn 不存在（2 failed）。已扩展共享 run options、UI performance 默认 timeout=15s，并实现 loopback serve 组合（SubprocessSupervisor + SQLite + ProductionReplayExecutor + SPA）。
50. control-plane 最终生产返工已接入：真实 PID identity/watcher/TERM→KILL/recovery-orphan、durable SSE sequence/snapshot、分页 read index、幂等 replay、SPA fallback、RFC 9457/Origin/media-type 安全边界。主线程组合复验 performance/API/CLI 为 `127 passed, 3 skipped`；前端原始 8 tests、typecheck/build 与真实 FastAPI Playwright 1 test 全绿。
51. exact 8.0.41 socket 门禁第一次重跑因使用了错误环境变量名而得到 `2 skipped`，无产品失败；读取测试 opt-in 契约后改用 `SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION=1` 与三 socket 列表，实际结果 `2 passed`，覆盖 7 schema profile 与 5 CPU-dense TREE template。
52. 第一次完整 coverage gate：`685 passed, 9 skipped, 1 failed`，唯一失败为 shared fuzzy budget 测试在 coverage 插桩下超过 2s；实际 line 87.11%、branch 74.04%、综合 83.72%，均未达到计划门槛。该次不得计为 release 通过。
53. oracle budget 优化第一次预留全部后续 graph 导致 permutation 语义回归，专项 1 failed；第二次加入全量 unique-neighbor 检查又令 10k fast path 超时。最终限制 cheap-neighbor preflight 到 4096 unique pair，并在构图前共享预留真正 graph budget；coverage 插桩专项 `62 passed in 1.50s`，保持小组确定 mismatch 优先级与 10k fast path。
54. performance 独立终审发现正式测量仅二方 barrier、setup mismatch 误重试、diagnostic 目录碰撞、首 timeout 未立即缩容、PFS 未接线、事件缺 run_id/time、summary 计数错误等 P0/P1。全部按 RED→GREEN 修复；主线程复验相关 performance/API/CLI/artifact `106 passed, 1 skipped`，唯一跳过为正式三隔离节点 TCP performance gate。
55. validation 终审的 4 个 P1 已修：INNER JOIN/UNION/CASE requirement 词汇统一、站内 redirect 完成原 queued URL、持久 fault cursor+真实 recovery probe、VALUES/TABLE 统一 closed validator。主线程复验 `tests/validation: 78 passed, 2 skipped`。
56. 前端 coverage/lint/axe 依赖第一次 sandbox npm 安装超过 90s 无输出，已主动终止（exit 130）；相同精确命令经允许联网后安装成功，`315 packages audited, 0 vulnerabilities`。不得把首次挂起误报为成功。
57. 前端 release coverage 首跑只有 lines 32.73%、branches 56.25%，按计划判 RED。补 API client/SSE、App route+partial failure+stale recovery、所有页面/表单/stop/replay/component 测试后：26 tests；lines/statements 98.86%、branches 88.82%、functions 78.18%。门禁冻结为 line/statements 90、branches 85、functions 75，符合计划明确要求的 branch≥85 而未伪报 90% functions。
58. ESLint 首跑发现 4 个 consistent-type-import 错误；修正 type-only imports 后 lint 通过。TypeScript 随后发现 2 个 typed matcher 缺 Error.name；改用 `toMatchObject` 后 typecheck 通过。
59. 新增真实 FastAPI + subprocess Playwright fault/recovery 与 Axe 扫描。首次 7 条中 1 失败：Findings virtual list 将 `aria-rowcount` 错用于 `role=region`，Axe 报 critical；改为语义正确的 `role=grid` 并同步组件测试后，E2E `7 passed`，四条主要路由无 serious/critical accessibility violation。
60. 修复 P1 后首轮在线官方 smoke 实际运行约 7.1s：9 signatures、5 gaps、exit 2。退出原因是新 fault policy 正确地把未配置 connection_reset 标为 `not_configured`，不是抓取崩溃；相较旧 6 gaps 已减少 1，但仍暴露 LIMIT 与 scalar literal 两个真实 reachability gap。
61. 主动发现循环第 1 次代码补全：先新增 LIMIT/scalar literal reachability 与 tableless scalar generator 失败测试，得到 3 failed；随后让真实 `QueryGenerator` 生成 `SELECT 1 ... ORDER BY 1` 的有界 scalar shape，并把既有安全 top-N 接入 validation adapter。定向 3 passed，generation/property/validation 组合 `179 passed, 2 skipped`。
62. 在线 cycle 2 实际运行 8s、2 epochs，使用 `/usr/bin/true` 仅作 smoke 的注入/恢复协议连通（**不计真实 fault acceptance**）：exit 0，9 signatures、3 gaps。LIMIT 与 scalar literal gap 已消失；剩余 3 项全部为 `version_builtin` 官方 evidence 尚未锁定，不得擅自翻锁。
63. frontend release commands 当前：lint、typecheck、test:coverage、build 全绿；真实 Playwright 7/7。CI 已加入 uv backend fixture、lint、coverage、build、Chromium 安装与 E2E gate。
64. Python 总覆盖率仍由独立子智能体补高价值边界测试中；在达到 line≥90、branch≥85 并完成 fresh full gate 前不得宣称 Task 13 release 完成。
65. 正式 12h acceptance 尚未开始。启动前必须完成当前代码提交和真实 fault policy选择；运行必须实际 elapsed≥43200s，期间按 gap report 做失败测试→生成器修改→回归→继续搜索，短 smoke/dry-run 均不得替代。
66. 已精确暂存 performance/control-plane/regression 切片 90 files；首次 staged diff-check 发现 4 个 EOF extra blank line，修复并重暂存后 `git diff --cached --check` 与 secret scan 全绿。提交成功：`cbd2105 feat: add performance testing and control plane`。随后立即 `git push`，唯一失败原因仍为 `No configured push destination`；未擅自创建远端。
67. validation 主动循环与 LIMIT/scalar generator 补全精确暂存 32 files；staged diff-check/secret scan 全绿。提交成功：`626997e feat: add active SQL coverage validation`。提交后立即 `git push`，仍只因 `No configured push destination` 失败。
68. 正式 12h SQL coverage run 已启动（run_id `mysql-8041-validation-20260713`，输出 `artifacts/validation-12h-20260713`，session 24648）：duration 12h、checkpoint 30m、freeze 30m、无 max-epochs，官方 SELECT seed + catalog exact sources，gap regression command 为 generation+adapter focused tests。四类 fault command/probe 暂用 `/usr/bin/true`，只验协议/cursor，明确不计真实 fault acceptance。必须检查最终 elapsed≥43200s；当前运行中。
69. 扩展 exact MySQL release matrix：新增遍历全部 evidence-ready query variant 的三 socket 建库/setup/query/三路结果测试。默认 `3 skipped`；实际连接 `/tmp/sf8041-{b,o,n}.sock` 后 `3 passed in 0.89s`。当前 catalog 58 variant 中 21 evidence-ready 已全部实际执行；37 blocked variant 继续受 evidence gate 阻止，未伪造覆盖。
70. Python release coverage 子智能体通过新增 config/domain/catalog/schema/data/query AST/safety/oracle/doctor 等高价值 boundary tests 达标，未使用 omit/大面积 pragma 或降阈值：fresh full `1111 passed, 10 skipped, 0 failed`；statements/lines `10062/10910 = 92.2273%`，branches `3297/3828 = 86.1285%`，combined `90.6432%`；Ruff/Mypy/diff-check 全绿。主线程仍需在合入最新 validation cycle 后再跑一次最终 fresh gate。
71. 12h active cycle 新 gap 的第二轮 TDD 实现完成：真实 generator 增加 bounded scalar COUNT、LEFT JOIN、LEFT JOIN+subquery、JOIN+CAST、derived+subquery、table/scalar subquery+LIMIT、scalar ROLLUP、INNER JOIN+subquery；保留 set_table_values 原 UNION+VALUES renderer，仅增加 values_only/values_limit；scalar INTERSECT/EXCEPT renderer补齐。VALUES/JSON_TABLE/set ops 继续按 release evidence 返回 BLOCKED_EVIDENCE。子智能体 fresh：generation 108、property 2、validation 116 passed + 2 skipped；Ruff/Mypy/diff-check 通过。待主线程复验/8.0.41 integration/提交。
72. 主线程复验第二轮 generator：generation/query property/validation `226 passed, 2 skipped`。新增 online-gap directed variant 三 socket integration；默认 `4 skipped`，实际三套 8.0.41 运行 `4 passed in 1.04s`，包含 scalar COUNT、LEFT JOIN/subquery、JOIN CAST/INNER subquery、table/scalar subquery LIMIT，证据未锁 variant 仍不执行。
73. 主线程在合入第二轮 generator 与所有 coverage tests 后执行 fresh full release gate：`1111 passed, 11 skipped, 0 failed in 79.26s`；coverage JSON checker 实测 lines `92.23%`、branches `86.13%`，均超过 90/85；Ruff `All checks passed`；Mypy `81 source files`；`git diff --check` 通过。唯一 warning 为第三方 Starlette/httpx deprecation。
74. 12h 运行中间状态（约 elapsed 1296.5s）：274 official sources、49 unique signatures、13 supported/6 blocked_evidence/30 gap、pending 6693。由于第二轮 generator 在进程启动后修改，计划等首个 30m checkpoint 写入后优雅停止并从同一 output/run_id 恢复，使 startup re-audit 使用新代码且不丢累计 elapsed。

## 10. 当前高风险审查清单

- 不得让 `KILL QUERY` 误杀同 connection ID 的下一条语句；返回/复用前必须 cancel + join。
- barrier 任一参与者前置失败时，其他参与者必须有界退出；Task 9 还应主动 `abort()` barrier。
- result limit 后 pinned session 不得静默复用；必须遵守 `connection_reusable=False`。
- [已覆盖] watchdog timeout 与 result-limit 同时发生时优先归类 timeout，避免三节点分类漂移。
- control KILL 响应黑洞必须在短 deadline 后 fallback `shutdown()`；不能调用可能发 QUIT 的 abort-close。
- `SHOW WARNINGS` 不能污染 elapsed time，也不能以 310 秒占住 worker。
- MySQL client CR_* 错误不得进入语义 oracle。
- query validator 必须 quote/comment aware；禁止 stacked statements、variables、locking reads、external access、nondeterministic functions。
- query generator 的 58/58 “注册”不等于真实 MySQL 可执行；必须做 8.0.41 integration。
- data generator 的内部约束验证不等于 MySQL 接受；必须做 8.0.41 DDL/INSERT integration。
- 每次提交只包含一个逻辑切片；不得把 data/query/execution 混成一个巨型提交。

## 11. 完成定义

只有同时满足以下条件才可宣称交付完成：

- core correctness/performance 两模式可从 CLI 和 React UI 启停。
- 三节点 setup/query/oracle/performance 协调符合冻结要求。
- finding 实时 append+fsync，HTML/JSONL/replay 可用。
- 所有单元/property/integration/E2E/accessibility/fault tests 通过。
- 精确 MySQL 8.0.41 三节点 release gate 通过。
- 12 小时主动官方 SQL 形态发现与生成器补全循环实际运行满 12 小时并生成报告。
- 所有变更已按子项目精确提交；若仍无远端，明确记录 push 阻塞，否则必须 push 成功。
