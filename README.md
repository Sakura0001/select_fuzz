# Select Fuzz

Select Fuzz is a deterministic MySQL differential test product for three
primary/replica pairs:

- `baseline`: unmodified open-source MySQL 8.0.41;
- `custom_off`: the custom engine with parallel query disabled globally;
- `custom_on`: the custom engine with parallel query enabled globally.

Each role has one primary for setup/DML and one replica for SELECT/analysis.
It has three independently registered modes: correctness comparison, performance
comparison, and concurrent read/write fuzzing. Correctness compares typed
results or normalized errors on all three replicas. Both comparison modes use seed-reproducible
weighted random schemas, tables, ordinary MySQL types and ranges, indexes, data,
and table-reading SELECTs. Performance uses exactly one logical worker. Each
performance round fills its random tables once through bounded deterministic
stored procedures, waits for all replicas, and then executes the configured
number of distinct `EXPLAIN ANALYZE FORMAT=TREE` queries sequentially. Each query
is launched concurrently on the three replicas and reports regressions against
both baseline and `custom_off`. JSON, FULLTEXT, SPATIAL, and multi-valued
JSON-array indexes are excluded from the default fuzz scope.

## Install

Source-checkout development requires Python 3.11 and Node.js. Credentials are
resolved only from shell environment variables and must never be written into
YAML or Git. For a target machine without Python, use the CentOS 7 bundle below.

```bash
UV_CACHE_DIR=.uv-cache uv sync --locked --all-groups
npm --prefix frontend ci
npm --prefix frontend run build
export SELECT_FUZZ_MYSQL_USER='<local user>'
export SELECT_FUZZ_MYSQL_PASSWORD='<set in shell only>'
cp config/example.yaml config/local.yaml
cp config/replica-parameters.example.yaml config/replica-parameters.yaml
```

Edit the six endpoints and optional role probes in the ignored
`config/local.yaml`, then point `replica_parameters_file` at the separate
replica parameter file. Only typed `SET SESSION` values are accepted there;
credentials remain environment-only. The three role pairs must have isolated,
comparable resources and working primary-to-replica replication. For a fuzz-only
load-balancing proxy, copy `config/intranet-fuzz.example.yaml`; fuzz uses only
`fuzz.target_role` and permits one proxy endpoint to represent both primary and
replica.

## CentOS 7 without a system Python

The [`python/`](python) directory contains a builder for an x86_64 bundle that
includes CPython 3.11, runtime dependencies, the `select_fuzz` package, and the
bundled SQL catalogs. The target CentOS 7 machine does not need Python, pip, or
uv. Build it on any machine with Docker:

```bash
./python/build-centos7-bundle.sh
```

Copy `python/output/select-fuzz-centos7-x86_64.tar.gz` to the CentOS 7 host,
extract it, and run the included `./select-fuzz` launcher. The manual GitHub
Actions workflow `build-centos7-bundle` produces the same archive when Docker is
not available locally. The bundle is for Linux x86_64; ARM64 requires a separate
build.

从 GitHub Actions 下载 artifact 后，可以在 CentOS 7 上按以下方式运行：

```bash
# GitHub Actions artifact 通常先下载为 zip，解压后使用其中的 tar.gz
unzip select-fuzz-centos7-x86_64.zip
tar -xzf select-fuzz-centos7-x86_64.tar.gz
cd select-fuzz-centos7-x86_64

cp config/intranet-fuzz.example.yaml config/intranet-fuzz.yaml
vi config/intranet-fuzz.yaml

export SELECT_FUZZ_MYSQL_USER=root
export SELECT_FUZZ_MYSQL_PASSWORD='<set-in-shell-only>'

./select-fuzz doctor \
  --mode fuzz \
  --config config/intranet-fuzz.yaml

./select-fuzz run \
  --mode fuzz \
  --config config/intranet-fuzz.yaml \
  --duration-seconds 300 \
  --full-thread-sql-log \
  --artifacts artifacts/intranet-fuzz
```

`doctor` 建议在每次压测前先执行；确认连接和配置正常后，再执行 `run`。
密码只通过当前 shell 环境变量提供，不要写入配置文件或提交到 Git。

## CLI

Always run `doctor` first. Comparison modes probe all six distinct endpoints;
fuzz probes only the selected role and deduplicates a shared proxy endpoint.
Version and configuration differences are reported but do not hard-gate startup;
missing runtime capabilities or required permissions remain fatal.

```bash
uv run select-fuzz doctor --mode correctness --config config/local.yaml
uv run select-fuzz run --mode correctness --config config/local.yaml --rounds 1
uv run select-fuzz run --mode performance --config config/local.yaml --rounds 1
uv run select-fuzz run --mode fuzz --config config/local.yaml --duration-seconds 300
```

For the internal proxy template:

```bash
cp config/intranet-fuzz.example.yaml config/intranet-fuzz.yaml
uv run select-fuzz doctor --mode fuzz --config config/intranet-fuzz.yaml
uv run select-fuzz run --mode fuzz --config config/intranet-fuzz.yaml \
  --duration-seconds 300 --full-thread-sql-log \
  --artifacts artifacts/intranet-fuzz
```

## 三种模式：使用说明与能力边界

### 通用启动流程

1. 复制 `config/example.yaml` 为未纳入 Git 的 `config/local.yaml`，填写六个
   endpoint；用户名和密码只通过环境变量提供。
2. 确认三组主备复制已经由外部环境配置完成，PQ 等目标开关已经由服务端配置完成。
   本程序只验证连接和副本追平，不会创建复制拓扑，也不会修改 PQ 开关。
3. 先执行 `doctor`，再执行目标模式：

   ```bash
   export SELECT_FUZZ_MYSQL_USER='<local user>'
   export SELECT_FUZZ_MYSQL_PASSWORD='<set in shell only>'

   uv run select-fuzz doctor --mode correctness --config config/local.yaml
   uv run select-fuzz run --mode correctness --config config/local.yaml --rounds 1
   ```

4. 所有运行都可以用 `--seed` 复现；使用 `--artifacts` 指定产物目录。
   命令行 `--mode` 会覆盖 YAML 顶层的 `mode`。
   correctness/performance 用 `--rounds` 控制轮数，fuzz 用
   `--duration-seconds` 控制持续时间。`--workers` 和 `--queries-per-round` 主要
   覆盖 correctness/performance；`--timeout-seconds`、`--data-rows-min/max`、
   `--databases`、`--writer-threads-per-database` 和
   `--reader-threads-per-database` 按所选模式覆盖对应配置。

### correctness：三节点结果正确性对比

启动示例：

```bash
uv run select-fuzz doctor --mode correctness --config config/local.yaml
uv run select-fuzz run --mode correctness --config config/local.yaml \
  --rounds 10 --seed 20260727 --workers 10
```

运行方式：

- 使用 `baseline`、`custom_off`、`custom_on` 三组主备，共六个 endpoint。
- 在三个 primary 上创建同构数据库、表、索引和初始数据；在三个 replica 上执行
  SELECT 并比较 typed result 或规范化错误。
- 每个 worker 每完成十个逻辑查询后，触发一个确定性的 1～3 语句 DML 事务；
  三个 primary 按相同顺序执行，事务和 marker 追平后再继续读。
- 发现结果、错误、受影响行数、DDL/DML 或复制可见性不一致时，保留数据库和最小
  复现用例，当前 worker 转入新 round。

当前边界：

- `workers` 为 1～64，默认 10；每轮查询数默认 1000；单条查询超时不超过 300 秒。
- 每张表默认生成 10～500 行、1～8 张表、2～16 列；单个 query block 默认最多
  绑定 4 张表。
- 每张表最多 65 个索引（默认上限 8）；单节点结果默认限制为 10000 行或 32 MiB。
- 使用 MySQL 8.0.41 SELECT grammar 和 schema-aware 绑定；默认生产范围排除
  JSON、FULLTEXT、SPATIAL 和 multi-valued JSON-array index。
- `query_grammar_path` 可以指向自定义 grammar；`grammar_compatible_type_percent`
  默认 80%，用于控制严格类型兼容表达式的选择概率。
- 这是结果差分模式，不以查询耗时退化作为 finding；数据库状态差异会进入 finding。
- DML 是小事务锁步差分，不是持续高并发写压测；不负责读取服务端 crash/error 日志。

### performance：三节点性能对比

启动示例：

```bash
uv run select-fuzz doctor --mode performance --config config/local.yaml
uv run select-fuzz run --mode performance --config config/local.yaml \
  --rounds 3 --seed 20260727 --queries-per-round 100
```

运行方式：

- 同样使用三组 primary/replica；每轮只在三个 primary 上完成一次物化，并等待三个
  replica 追平。
- 性能模式固定一个逻辑 worker，按顺序选择不同查询，然后在三个 replica 上并发执行
  `EXPLAIN ANALYZE FORMAT=TREE`。
- 以 `baseline` 为基准，同时与 `custom_off` 对比；超过
  `performance.regression_threshold` 的耗时退化会保留数据库和报告。

当前边界：

- `workers` 固定为 1，不能改成并发性能 worker；并发只发生在同一查询发往三个 replica
  的阶段。
- 默认每轮 100 条查询；每张表初始 100000 行，允许扩展到 50000000 行，整轮总行数默认
  不超过 100000000 行。
- 性能 schema 为 1～16 张表、每表 2～1017 列、最多 65 个索引；单条查询最多 16 张表，
  query tree 深度为 1～16。
- 性能模式只做 `EXPLAIN ANALYZE` 性能判定，物化完成后没有周期性 INSERT/UPDATE/DELETE；
  不提供 correctness 模式那样的结果逐行差分。
- 校准相关旧配置字段仍接受以保持兼容，但当前生产 shared-round 流程不使用它们。
- 需要服务端允许 `EXPLAIN ANALYZE`，查询超时、物化失败、节点启动偏差和副本追平失败
  会使本轮失败并保留现场。

### fuzz：单集群并发读写压力

启动示例：

```bash
uv run select-fuzz doctor --mode fuzz --config config/local.yaml
uv run select-fuzz run --mode fuzz --config config/local.yaml \
  --duration-seconds 300 --seed 20260727 \
  --databases 4 --writer-threads-per-database 4 \
  --reader-threads-per-database 12
```

运行方式：

- 只使用 `fuzz.target_role` 指定的一组 primary/replica；多个 database 在同一组集群上
  并行创建，不会自动创建多个集群。
- fuzz 模式允许 primary 和 replica 使用同一个负载均衡代理 host/port；`doctor` 只探测
  `target_role`，不会因为未使用的 baseline/custom_off 地址不可达而阻塞启动。
- 每个 database 有独立 writer 和 reader；writer 全部连接 primary，reader 严格按
  primary:replica = 1:2 分配。
- 每次读查询 50% 选择负载型查询，50% 选择完全随机 SQL grammar。负载型查询覆盖扫描、
  聚合、JOIN、GROUP BY、窗口函数和子查询；随机 grammar 覆盖 CTE、LATERAL、派生表、
  嵌套子查询、窗口、HAVING、Hints、类型转换和随机表达式。
- reader 的下一条 SELECT 由有界多进程流水线提前生成，每个 reader 最多预取三条；
  每代内 seed、SQL 顺序、长期连接和固定 endpoint 保持稳定，换代时使用新 schema seed
  并重建连接。writer 的 DML 仍在线程内按事务即时生成。
- `schema_refresh_interval_seconds` 默认 1800 秒，并从本代开始创建数据库时计时。到点后
  先停止旧批次派发、等待当前 SQL 在既有超时内结束并关闭连接，再同时创建完整的新
  `databases` 批次；全部主库初始化、备库可见和首批 SELECT 预生成完成后才启动新连接。
  设置为 `0` 可关闭周期换代。
- 查询使用独立 pure-Python 控制连接执行 wall-clock watchdog；超时或停止时先
  `KILL QUERY`，宽限期后仍未结束再安全断开服务端会话。控制连接并发不超过
  `control_connection_reserve`。事件日志周期记录等待生成、执行、结果拉取和重连阶段。
- 每条 fuzz worker 会话设置只用于观测的 `@select_fuzz_worker` 标签：
  `primary_writer`、`primary_reader` 或 `replica_reader`。共享代理 endpoint 下也能通过
  `performance_schema.user_variables_by_thread` 核对逻辑主备连接分布。
- `diagnostics_interval_seconds` 默认 `5`。运行期间终端每 5 秒向 `stderr` 输出一行中文
  `[fuzz状态]`，联合展示读写增量、线程阶段及最长停留、SQL 生成进程和待处理请求、
  主备登记连接以及这些连接在 `PROCESSLIST` 中的 Sleep/Query 数量。最终 `stdout` 仍只输出
  汇总 JSON，现有脚本可以继续解析。
- 连续 15 秒没有完成读查询时，程序额外输出 `[fuzz警告]` 和 `初步原因`。常见判断包括
  SQL 生成速度不足、生成进程退出、查询仍在 MySQL 执行、客户端拉取结果、连接重试、
  工作线程缺失，以及“程序标记执行但 MySQL 显示 Sleep”的状态矛盾。诊断采样失败只显示
  原始错误，不会中断 fuzz。
- 每张表默认随机生成 200～500 列，包含固定业务列和随机类型列；候选类型池包含整数、精确数值、
  浮点、BIT、日期时间、字符、二进制、TEXT/BLOB、ENUM、SET 等 56 个变体。每张表随机
  抽样，不保证单表一次运行出现全部 56 个变体。
- 每张表至少包含主键、降序索引、唯一索引、表达式索引，并追加随机普通索引。JSON 和
  空间数据类型不进入表字段类型池。
- writer 混合执行 INSERT、UPDATE、UPSERT 和 DELETE。DELETE 支持点删和 10～100 行的
  小批量删除，但固定保留初始化的 `id = 1`，因此不会把整张表清空。
- 读结果只流式消费后丢弃，不比较结果正确性，也不执行 `EXPLAIN ANALYZE`。普通 SQL
  错误记为 fuzz error，当前长连接继续复用；只有 lost connection 或连接失效异常才
  触发指数退避重连。

当前边界：

- `databases` 为 1～32；每个 database 的 writer 为 1～64，reader 为 3～192，reader
  数必须是 3 的倍数；总连接数受 `max_total_connections` 限制，默认上限为 1024，最大可配 4096。
- 每个 database 默认 4 张表（允许 1～16 张）、每表 10000 行，累计行数受
  `max_rows_per_database` 限制；初始行数配置下限为 20。
- 每表列数配置范围为 50～500，本地压力配置默认随机 200～500；索引数范围为 4～64，默认随机 4～12。
- INSERT/UPDATE/UPSERT 批量默认 100～100000 行；DELETE 默认 10～100 行且有 `id=1`
  保护。DML 权重四项之和必须为 100，默认 INSERT/UPDATE/DELETE/UPSERT = 35/45/10/10。
- 查询超时不超过 300 秒；连接重连退避初始值不超过 30 秒，最大值不超过 60 秒。
- `query_generator_processes=0` 时按总 reader 数与 CPU 核数自动选择生成进程，自动模式
  最多 32 个；同一数据库的生成任务也会分散到全部进程，避免 reader 在单进程队列中
  串行等待。每个 reader 的预取深度固定为 3。`connector_implementation=auto` 在 fuzz
  模式优先使用 Connector C 扩展，不可用时回退 pure Python。
- 周期换代不会叠加两代 worker 连接，也没有代数上限。旧批次数据库、失败批次中已经
  创建的数据库和半成品对象均不删除；新批次任一数据库初始化或主备同步失败都会使整个
  fuzz 运行失败，不会回退到旧批次。
- `doctor` 按 primary/replica 的真实 host/port 汇总 worker 连接，并加上
  `control_connection_reserve` 后与服务端 `max_connections` 比较；同一代理 endpoint
  会合并计算。例如 12 ×（4 writer + 12 reader）在共享 endpoint 上需要 192 条 worker
  连接。
- fuzz 不判断结果正确性、不做三节点差分、不读取服务端错误日志、不自动确认数据库是否
  crash；程序只记录自身观察到的 SQL 错误、连接断开、重连和吞吐统计。
- 随机 grammar 可能产生数据库拒绝的 SQL，这是 fuzz 输入的一部分；普通拒绝不会停止
  worker。不可恢复的 setup/程序级失败才会使运行失败。

### 三种模式对比

| 模式 | 目标 | 拓扑 | 读操作 | 写操作 | 结果判定 |
| --- | --- | --- | --- | --- | --- |
| correctness | 发现结果/错误/状态差异 | 三组主备，六节点 | 三 replica 结果差分 | 三 primary 锁步小事务 | typed result、规范化错误和状态一致性 |
| performance | 发现执行计划和耗时退化 | 三组主备，六节点 | 三 replica 并发 `EXPLAIN ANALYZE` | 仅初始化物化，无周期 DML | baseline/custom_off 性能阈值 |
| fuzz | 制造并发读写、资源和连接压力 | 一组指定主备 | primary:replica = 1:2，结果丢弃 | primary 多 writer，INSERT/UPDATE/UPSERT/DELETE | SQL 错误、连接状态和运行统计 |

### 产物、停止和清理

`artifacts/` 下的 JSONL 事件、SQL、finding 和报告用于审计与复现；启用
`full_thread_sql_log` 后，还会为每个 worker 追加完整 source-able SQL。fuzz 会额外
保存建库/建表/初始数据文件和每个 reader/writer 的实际执行 SQL。fuzz 成功 SELECT
的结果行不会保存，错误 SQL 和必要的上下文会保存。

数据库及每次周期换代产生的历史批次默认保留，不会在运行结束后自动删除。`cleanup` 当前只接受
correctness/performance 生成的 `sf_c_...` 或 `sf_p_...` managed ID；fuzz 生成的
`sf_f_...` 数据库不会被该命令接受，需要由授权环境的运维流程单独清理。对支持的
managed ID，先做计划，再显式执行：

```bash
uv run select-fuzz cleanup --config config/local.yaml \
  --database '<exact managed database id>'
uv run select-fuzz cleanup --config config/local.yaml \
  --database '<exact managed database id>' --execute
```

按 `Ctrl+C` 或设置有限的 `--rounds`/`--duration-seconds` 可安全停止；连接和 worker
会执行收尾，已保留的数据库和产物不会被自动删除。

### 当前不提供的能力

- 不自动部署 MySQL、复制拓扑或 PQ；不自动读取、解析或归因服务端 crash 日志。
- 不扫描公网或自动发现第三方资产；配置目标应是已授权的本地、私有网络或隔离测试环境。
- 不把 fuzz 模式当作 correctness/performance：fuzz 不比较结果，不做 `EXPLAIN ANALYZE`，
  也不保留成功查询的结果集。
- `cleanup` 当前不支持 `sf_f_...` fuzz 数据库，只支持 `sf_c_...` 和 `sf_p_...`；fuzz
  数据库默认保留，避免运行结束时误删压力测试现场。
- 不保证一次运行覆盖 schema 类型池中的每个变体；完整覆盖 56 个候选变体至少需要
  62 列（6 个固定列 + 56 个候选变体），超过 62 列后会继续从类型池随机复用变体。

这里的 multi-valued index 不是普通的多列联合索引。它是 MySQL 针对 JSON 数组的
特殊二级索引：一行 JSON 数组中的多个元素可以对应多个索引记录，通常通过
`CAST(json_expression AS <type> ARRAY)` 创建。当前项目没有生成这种 JSON 数组字段、
索引 DDL 或配套的 `MEMBER OF()`、`JSON_CONTAINS()`、`JSON_OVERLAPS()` 查询，因此将它
列为明确的能力边界。

Set `full_thread_sql_log: true` in YAML or pass `--full-thread-sql-log` to persist
SQL traffic. correctness/performance append worker traffic to `sql/worker-NNN.sql`.
fuzz additionally writes `sql/fuzz_schema_<database>.sql` for CREATE DATABASE,
USE, CREATE TABLE and initial data, plus
`sql/fuzz_<database>_<stream>_<worker>.sql` for each reader/writer stream. These
files are append-only and are never reset by random DDL.

Every generated correctness or performance round also writes a directly
source-able script under `rounds/`. Both modes use exactly one
`rounds/<database>.sql`: only its opening header is comments, every later SQL
statement occupies one physical line, queries have no separating blank lines,
and each periodic DML transaction has one blank line before and after it. SQL is
appended only when attempted, in execution order. Run it with
`mysql < rounds/<case>.sql` or MySQL `SOURCE`.
True findings additionally contain a minimal `case.sql` and compact `case.diff`;
result bodies are retained only up to 100 rows and 64 KiB, otherwise only counts
and digests are stored.

Findings are appended and fsynced immediately. Rebuild a report or replay a
finding on a new retained database with:

```bash
uv run select-fuzz report --artifacts artifacts --output reports/latest.html
uv run select-fuzz replay --config config/local.yaml --artifacts artifacts \
  --finding '<case id>'
```

The local React control plane is started with:

```bash
uv run select-fuzz serve --config config/local.yaml --artifacts artifacts
```

It binds to loopback only. The UI starts and stops all three modes, streams events,
and exposes findings, reports, and replay status.

## Regression and validation

The current query-generation boundary, exact coverage counts, explicit exclusions,
and remaining gaps are maintained in
[`docs/testing/query-generation-coverage-checklist.md`](docs/testing/query-generation-coverage-checklist.md).
It defines a closed MySQL 8.0.41 read-only subset; JSON/FULLTEXT/SPATIAL renderers remain
available for isolated tests but are excluded from the default production scope.

The controlled corpus stores generator seeds and expected tags—not copied web
SQL and not credentials:

```bash
uv run select-fuzz regression-seeds \
  --output tests/regression/seeds.json --seed 20260712
```

The online validation loop accepts only allowlisted official MySQL sources,
stores content-addressed evidence, never executes discovered SQL, audits each
shape against the typed generator, checkpoints progress, and emits a gap
report. A formal acceptance run must actually complete twelve hours; a dry run
does not satisfy that gate.

```bash
uv run python scripts/validation_12h.py \
  --duration 12h --checkpoint 30m --freeze 30m \
  --output artifacts/validation-12h \
  --seed-url https://dev.mysql.com/doc/refman/8.0/en/select.html
```

Interrupted validation runs resume from their SQLite checkpoint and immutable
source cache. Review `report/coverage.json`, `report/gaps.json`,
`report/source-manifest.json`, `report/index.html`, and the append-only
telemetry/fault logs before accepting the run. A fault schedule also needs
operator-provided `--fault-command`, matching `--fault-probe`, and an optional
`--mysql-connection-probe`; unconfigured or unrecovered scheduled faults fail
acceptance instead of being reported as successful.

Exact local three-node socket integration and a production-pipeline soak can be run
without storing a password:

```bash
SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION=1 \
SELECT_FUZZ_MYSQL_SOCKETS=/tmp/baseline.sock,/tmp/custom-off.sock,/tmp/custom-on.sock \
uv run pytest -q tests/integration/test_mysql8041_*.py

PYTHONPATH=src uv run python scripts/run_mysql8041_socket_soak.py \
  --sockets /tmp/baseline.sock /tmp/custom-off.sock /tmp/custom-on.sock \
  --duration-seconds 1800 --queries-per-round 100 --workers 3 \
  --artifact-root /tmp/select-fuzz-mysql8041-soak \
  --run-id mysql8041-query-soak
```

Socket order is `baseline`, `custom_off`, `custom_on`. The soak validates exact
8.0.41 versions and retains generated databases for replay; use a fresh artifact root
for each acceptance run. Canonical round scripts are always written. Full per-worker
SQL is written only when `full_thread_sql_log` is enabled, keeping the default durable
artifact volume bounded.

## Development gates

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q --cov=select_fuzz --cov-branch \
  --cov-report=term --cov-report=json:coverage.json
uv run python scripts/check_coverage.py coverage.json \
  --min-lines 90 --min-branches 85
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test:coverage
npm --prefix frontend run build
npm --prefix frontend run e2e
git diff --check
```

Tests marked `mysql` require opt-in and environment-only credentials. The
legacy three-socket suite remains a generator/executor compatibility gate;
comparison-mode primary/replica routing and lag behavior require six configured
cloud endpoints. Fuzz mode can instead use one selected routing-proxy endpoint.
