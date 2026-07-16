# MySQL 8.0.41 并行查询差分测试平台测试计划

日期：2026-07-12

适用范围：Python 测试内核、三节点执行器、正确性与性能 oracle、报告/replay、FastAPI 控制面、React SPA、12 小时主动查询形态补全工具。

## 1. 测试目标

1. 证明生成器在给定 seed、版本与配置下可复现。
2. 证明三个相同 MySQL 节点不会产生平台自身造成的结果误报。
3. 证明受控的结果、错误、超时、连接和性能差异能被正确分类。
4. 证明所有启用的 schema、类型、索引、数据分布与 SELECT 特性均实际执行，而非仅存在代码分支。
5. 证明 7×24 运行所需的恢复、追加报告、资源稳定与前端状态恢复能力。
6. 证明连续 12 小时主动发现循环能够发现、分类、补齐并回归查询形态缺口。

## 2. 测试环境

### 2.1 快速开发环境

- 本机现有 Homebrew MySQL 8.0.45，监听 `127.0.0.1:3306`。
- 凭据从 `SELECT_FUZZ_MYSQL_USER` 与 `SELECT_FUZZ_MYSQL_PASSWORD` 环境变量读取。
- 用途：快速 smoke、DDL/SELECT 兼容测试、前端真实后端 E2E。
- 限制：不能证明 MySQL 8.0.41 特有行为。

### 2.2 精确版本发布环境

- 三个独立 MySQL 8.0.41 实例。
- 端口默认 `33361/33362/33363`。
- 每实例独立 datadir、socket、pid、tmpdir 和 error log。
- 语义相关配置尽量一致；差异由产品预检警告并写入 finding。
- 三实例均为开源 MySQL 时，用于验证假阳性、故障分类和性能流程。

### 2.3 最终目标环境

- `baseline`：开源 MySQL 8.0.41。
- `custom_off`：自研引擎并行关闭。
- `custom_on`：自研引擎并行开启。
- 三台独立同规格服务器。
- 用途：最终正确性和性能验收。

## 3. 测试层级与频率

| 层级 | 触发频率 | 门禁 |
|---|---|---|
| Python/TypeScript unit | 每次修改 | 全绿 |
| Property 与固定 seed regression | 每次提交 | 全绿 |
| API contract 与组件测试 | 每次提交 | 全绿 |
| 本机单 MySQL smoke | 每次功能提交 | 全绿 |
| 三实例 MySQL 8.0.41 integration | 合并前 | 全绿 |
| Playwright 模拟后端 E2E | 每次前端提交 | 全绿 |
| Playwright 真实后端 E2E | 合并前 | 全绿 |
| 故障注入和十分钟加速 soak | 合并前 | 全绿 |
| 50,000 查询同构三节点验真 | Release | 确认型假阳性为 0 |
| 12 小时主动补全 + soak | Release | 满足第 13 节 |
| 72 小时稳定性运行 | 首次交付后建议 | 无持续资源泄漏 |

## 4. 静态门禁

执行：

```bash
uv run ruff check .
uv run mypy src
uv run pytest --collect-only -q
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run build
git diff --check
```

要求：

- 所有命令退出码为 0。
- 不存在未知 pytest marker。
- 前端生产构建无 TypeScript 错误。
- 仓库不包含密码、token、`.env` 或生成报告。

## 5. Python 单元与 Property 测试

### 5.1 配置与安全

- YAML 与 CLI override 优先级。
- 环境变量 secret 引用；序列化、异常和日志中不可出现 secret。
- 三组主备六端点的角色、host、port 和可选 role probe；六端点连通性。
- 独立 replica parameter 文件的严格结构、typed `SET SESSION` 与摘要。
- 版本不一致报告错误但不作为硬门禁。
- 正确性与性能模式互斥。
- 关键权限缺失拒绝启动；配置差异只警告。

### 5.2 Seed 与覆盖调度

- 同 seed 产生字节级相同的 schema、数据和查询 manifest。
- worker 数变化不造成 case ID 重复或 seed 丢失。
- 覆盖欠债优先选择低命中特性。
- 每个启用特性达到 `min_hits=10` 后才结束覆盖周期。
- checkpoint 恢复后调度状态一致。

### 5.3 Schema 与索引兼容矩阵

对每条官方限制至少包含一个合法和一个非法 property：

- 普通、分区、FK、临时、FULLTEXT、SPATIAL、JSON 多值索引场景。
- 分区 + FK/FULLTEXT/SPATIAL 禁止。
- 临时表 + 分区/FK 禁止。
- 分区唯一键包含全部分区列。
- FK 类型、符号、字符集、排序规则与左前缀索引。
- BLOB/TEXT 前缀索引。
- 函数、降序、不可见、空间、全文和多值索引限制。
- page size、row format、字符集字节数与索引长度。

### 5.4 数据分布

- 边界、均匀、Zipf、低基数、高/低 NULL、唯一、相关列和 FK 分布。
- 整数、DECIMAL、FLOAT/DOUBLE、BIT、时间、字符、二进制、ENUM/SET、LOB、JSON 和空间值。
- 普通 LOB 单值不超过默认 64 KiB。
- 三台主机加载使用同一数据文件与摘要，备机 marker 在 10 秒内追平。
- 每 worker 独立每 10 个逻辑查询触发 DML；1–3 条、12–50 行和 2:1:1 权重。
- 三主 DML success/error/affected rows 对比、同错回滚、任一不一致终止 round。
- correctness/replay/performance 主写备读，以及 performance 不触发周期 DML。
- performance 单 worker，每轮只物化一次，多条不同查询共享同一 schema/数据库/行数，并在三备机并发执行 `EXPLAIN ANALYZE`。
- `rounds/<database>.sql` 只有头部注释、SQL 单行、DML 前后空行且无未来 SQL。

### 5.5 查询 AST 与安全

- 每个 query block 的作用域、alias、类型与 NULLability。
- Join、子查询、派生表、LATERAL、CTE、递归 CTE、集合运算、聚合、ROLLUP、窗口和 hints。
- Catalog 仍保留 JSON_TABLE/FULLTEXT/SPATIAL 的定向 renderer 与独立集成见证；默认生产
  correctness scope 明确排除这 13 个 target。当前精确范围以
  `docs/testing/query-generation-coverage-checklist.md` 为准。
- `LIMIT`、top-N 和窗口顺序必须存在唯一 tie-breaker。
- 拒绝多语句、DML、锁定读、INTO、当前时间、随机、文件、锁、SLEEP/BENCHMARK、用户变量、存储函数和 UDF。
- 自由随机和负向变异仍受 15 秒、10,000 行和 32 MiB 保护。

运行：

```bash
uv run pytest -q tests \
  --hypothesis-show-statistics \
  --cov=select_fuzz --cov-report=term-missing --cov-fail-under=90
```

要求：至少 10,000 个 property 样本无未分类失败；核心 Python 行覆盖率至少 90%，整体分支覆盖率至少 85%。

## 6. 正确性 Oracle 测试矩阵

### 6.1 成功结果

- 空结果、单行、多行和重复行。
- 全 NULL、部分 NULL、宽行、LOB、JSON、二进制和日期时间。
- 返回顺序不同但多重集合相同：必须通过。
- 重复次数不同：必须失败。
- 列数、列名、类型、signed/binary 或 NULL 元数据不同：必须失败。
- FLOAT/DOUBLE 在容差内：必须通过；容差外：必须失败。
- 浮点重复行使用确定性一对一匹配，不得因贪心顺序产生不稳定 verdict。

### 6.2 错误结果

- VALID lane 三路相同错误：`GENERATOR_FINDING`，不得记 pass 或 coverage。
- NEGATIVE lane 仅在三路 errno + SQLSTATE 精确匹配 expected error 时记契约命中；
  任一路身份不同优先记 `RESULT_MISMATCH`。
- 两路成功一路错误：`RESULT_MISMATCH`。
- errno 相同但 SQLSTATE/规范化消息不同：`RESULT_MISMATCH`。
- 消息只含连接 ID 或 host 差异：规范化后通过，同时保留原文。
- warning 不同但结果和错误一致：warning 不改变 verdict。

### 6.3 超时

- 三路超过正确性 15 秒：`OVER_BUDGET`。
- 一路或两路超时：不一致 finding。
- KILL 只终止目标查询，不误杀下一条或其他 worker。
- KILL 后 5 秒内无目标 active statement，连接池恢复到基线。

## 7. MySQL 场景实跑矩阵

每个场景至少要求 10 个成功 setup、每 setup 至少 100 个 SELECT；Release 运行使用配置中的更大查询数。

| 场景 | 必跑内容 |
|---|---|
| 普通表 | 有/无 PK、复合 PK、unique nullable、无索引、覆盖/前缀/降序/函数/不可见索引 |
| 分区表 | RANGE、LIST、HASH、KEY、RANGE/LIST COLUMNS、唯一键约束、partition pruning |
| FK 图 | 1:1、1:N、N:M、复合 FK、NULL FK、未匹配 outer join |
| 临时表 | 固定三路 session、遮蔽持久表、连接丢失后整轮重建 |
| FULLTEXT（独立 opt-in） | natural/boolean 受控谓词、配置指纹、分区互斥；不进入默认生产 scope |
| SPATIAL（独立 opt-in） | 合法 SRID 0 geometry、关系布尔谓词、SPATIAL 索引；不进入默认生产 scope |
| JSON（独立 opt-in） | 标量路径、JSON_TABLE、生成列/函数索引、多值索引；不进入默认生产 scope |
| 大值 | 分层 LOB/JSON、结果 32 MiB 边界、max_allowed_packet 安全上限 |

查询族实跑覆盖：

- 基础过滤、表达式、CASE。
- 各 Join 与 Join 图。
- 相关/非相关子查询。
- 派生表、LATERAL。
- 普通/递归 CTE。
- UNION/INTERSECT/EXCEPT ALL/DISTINCT。
- GROUP/HAVING/ROLLUP。
- 窗口与 frame。
- JSON/JSON_TABLE。
- 合法错误路径和自由随机路径。

执行：

```bash
uv run pytest -q -m mysql tests/integration
uv run python -m select_fuzz run --mode correctness \
  --config config/local-8041.yaml --rounds 10 --queries-per-round 1000
```

## 8. 三节点同构验真与故障注入

三台均使用开源 MySQL 8.0.41：

1. 执行至少 50,000 条 correctness 查询。
2. 确认型假阳性必须为 0。
3. 轮换修改 baseline/custom_off/custom_on 中的一行数据，差异检出率 100%。
4. 轮换注入单节点 errno、SQLSTATE、超时和断连。
5. 三节点全异时保存完整三路信息，不使用多数投票伪造真值。
6. 节点恢复后主进程无需重启。

故障点：

- 连接建立前、发送 SQL 后、读取部分结果时、报告 fsync 前后。
- TCP reset、服务 stop/start、连接数耗尽。
- JSONL 尾部损坏、报告目录只读、模拟 ENOSPC。
- worker SIGTERM/SIGKILL、控制面重启。

要求：已提交事件不丢不重；最多丢失当前尚未提交的 case；基础设施故障不参与结果 verdict。

## 9. 性能测试计划

### 9.1 单元测试

- EXPLAIN TREE 普通数和科学计数格式。
- 顶层 root `actual time` 的 `b` 值提取；禁止节点耗时求和。
- 部分 TREE、缺 root、root loops 非 1 均 fail closed。
- baseline/off 每规模三次中位数。
- 两端共同 5–30 秒可行区间。
- 最多八轮校准和单表 50,000,000 行上限。
- 20% 阈值参数应用于 `on/off` 与 `on/baseline` 两条规则。
- 100 ms start skew 与各种 timeout 分类。

### 9.2 集成测试

- 扫描、range、nested-loop/hash join、聚合、filesort、窗口工作量旋钮。
- 校准期间 plan shape 变化时重新分段，不跨 discontinuity 外推。
- 正式阶段三路各执行一次。
- `custom_on` 超时且参考完成：`PERF_ALERT`。
- 三路超时：`OVER_BUDGET`。
- 参考节点正式超时：`CALIBRATION_DRIFT`。
- start skew 超阈值：`TIMING_UNRELIABLE`，不得触发性能告警。
- 每份性能报告包含 `cache_state_unverified`。

使用可控延迟 adapter 验证：

- 19.9% 不触发，20.0% 边界按精确定义处理，20.1% 触发。
- 两条参考比较分别独立触发。
- 单次正式样本只标记 `PERF_ALERT`，不标记确认回归。

## 10. 报告、Replay 与耐久性

- 通过查询仅保存摘要和强哈希。
- finding 保存三路完整压缩结果、错误、计划、配置、数据库名、seed 和 SQL。
- 每个 fuzz worker 使用独立的 `sql/worker-NNN.jsonl`；每次 triad dispatch 在执行前
  fsync `query_attempt_started`，分类后 fsync `query_attempt_finished`。
- finished 记录必须包含完整 SQL、四类 seed、target/lane、三节点 status、errno、SQLSTATE、
  message、耗时、行数和最终 verdict；infra retry 也必须逐次记录。
- started 没有对应 finished 时表示进程终止时该 SQL 已进入 dispatch 边界，但是否到达全部
  节点及其结果均未知，不得把它当作已完成执行，也不得静默丢弃。
- 每个 JSONL 事件先追加并 fsync，再更新读模型。
- 读模型删除后可从 JSONL 和用例目录完全重建。
- 每个可能的写入边界终止进程并恢复。
- 同 seed 在新数据库 replay，确定性 correctness finding 复现率 100%。
- 性能 finding 允许标记环境敏感，但必须保留完整原始测量。

执行：

```bash
uv run pytest -q tests/artifacts tests/integration/test_replay.py
uv run select-fuzz replay --config config/local.yaml \
  --artifacts artifacts --finding '<case-id>'
```

## 11. FastAPI 与 React 测试

### 11.1 API contract

- `/api/v1/capabilities`、config schema、health、target health。
- run 创建、查询、幂等停止。
- finding 列表/详情和 cursor pagination。
- replay 创建/状态。
- report 元数据和受控产物访问。
- Problem Details：`code/message/retryable/field_errors/request_id`。
- 密码不回显、不进入 URL、浏览器 storage 或错误正文。

### 11.2 SSE

- 重复、乱序、丢失、静默、断连和游标过期。
- 前端按 sequence 去重。
- 发现缺口后重新获取快照再订阅。
- 未知事件类型安全忽略并记录诊断。

### 11.3 页面状态

每个页面覆盖 loading、empty、data、stale、error：

- 总览。
- 新建任务。
- 任务详情。
- 异常列表与详情。
- Replay。
- 历史与报告。

故障条件：400/409/429/500、请求超时、后端重启、worker 崩溃、单节点失联、报告失败、大 SQL、超长错误、10 万 finding。

运行：

```bash
npm --prefix frontend test -- --run --coverage
npm --prefix frontend run build
npm --prefix frontend run e2e:mock
npm --prefix frontend run e2e:real
```

要求：

- 前端分支覆盖率至少 85%。
- 重复点击和请求重试不产生重复任务。
- 局部失败不白屏。
- 事件到 UI p95 小于 1 秒；普通交互 p95 小于 200 ms。
- axe 无 serious/critical 问题；核心路径可仅用键盘完成。

## 12. 十分钟加速 Soak 与长稳态

合并前先用精确三个 MySQL 8.0.41 Unix socket 运行生产链路 smoke/soak：

```bash
PYTHONPATH=src uv run python scripts/run_mysql8041_socket_soak.py \
  --sockets /tmp/baseline.sock /tmp/custom-off.sock /tmp/custom-on.sock \
  --duration-seconds 600 --queries-per-round 100 --workers 1 \
  --artifact-root /tmp/select-fuzz-mysql8041-soak \
  --run-id mysql8041-query-soak
```

必须注入：节点断连、慢节点、KILL、worker 退出、控制面重启、SSE 断连、报告写入失败。

建议首次交付后补充 72 小时运行。持续采集 RSS、CPU、线程、FD、连接、吞吐、finding、报告增长和恢复时长。

要求：无持续线性资源增长；稳定期 RSS 增长不超过 20%；吞吐相对首小时下降少于 15%。

## 13. 连续 12 小时主动查询形态补全验收

12 小时内持续循环：

1. 网上搜索和抓取版本可证的 MySQL 8.0.41 查询形态。
2. 缓存来源和内容 hash，只离线抽取 SQL 结构。
3. 生成 feature signature。
4. 静态检查生成规则并定向寻找可达 seed。
5. 分类 `covered/latent/missing/unsupported/unsafe/indeterminate`。
6. 对 missing/unreachable 先新增失败测试并证明 red。
7. 最小实现、focused green、全量回归、本地 MySQL canary。
8. 一个缺口一个本地 commit；有远端时立即 push。
9. drain 当前 soak，以新 commit 开启新 epoch。
10. 继续搜索，直至连续满 12 小时。

来源优先：官方 release notes、官方手册、mysql-server 8.0.41 源码/mysql-test、官方示例、固定 commit 的成熟项目。博客和论坛只作线索。

每 30 分钟 checkpoint；最后 30 分钟禁止新代码修改，只继续搜索、审计、回归和整理。

通过标准：

- 连续运行满 12 小时，中断时间不计入。
- 无崩溃、永久 hang 或遗留查询。
- 每个 enabled feature 达到当前覆盖周期，或有明确未达原因。
- 每个修复都有版本证据、失败测试、commit 与完整测试结果。
- 生成 source manifest、signature corpus、coverage matrix、gap ledger、饱和曲线和最终 HTML/JSONL 报告。
- 不宣称数学意义上的“互联网所有 SQL 已穷尽”；用来源范围、搜索记录、缺口和新增 signature 饱和度提供证据。

## 14. 发布判定

只有以下全部成立才交付：

- 静态、unit、property、integration、frontend、E2E 和故障注入全绿。
- 三个同构 MySQL 8.0.41 的 50,000 查询验真无确认型误报。
- 所有启用 schema/query feature 均有真实 MySQL 执行证据。
- 受控正确性差异和错误差异检出率 100%。
- timeout、断线和报告恢复满足既定时限。
- 12 小时主动补全验收完成。
- 工作树干净；所有本地提交清晰且仅包含相关文件。
- 远端存在时全部推送；远端缺失时明确列出待推送提交。
