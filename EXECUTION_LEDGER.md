# MySQL Parallel Query Fuzzer — 执行账本

> **接手续读入口**：任何新会话开始工作前，先完整阅读本文件，再读取本文件中指向的计划文档与 `git status`。
> **更新规则**：每完成、失败、阻塞或新增一个工程步骤，立即更新本文件；不能只依赖对话上下文。
> **最后更新**：2026-07-13（Asia/Shanghai）
> **状态**：开发进行中，尚未达到产品交付/12 小时验收条件。Task 6/8 已提交，Task 7 已完成独立等价审查与全量验证，正在精确提交。

## 1. 工作区与 Git 状态

- 大仓库根目录：`/Users/yuyu/Documents/select_fuzz 2`
- 当前工作树：`/Users/yuyu/Documents/select_fuzz 2/.worktrees/mysql-parallel-query-fuzzer`
- 当前分支：`codex/mysql-parallel-query-fuzzer`
- 当前 HEAD：`b64eccd feat: generate deterministic mixed-distribution data`
- Git 远端：**未配置**。每次提交后执行 `git push` 都会得到 `No configured push destination`；不得猜测或私自创建远端。
- 凭据规则：只从环境变量读取；不得把用户名密码/token 写入命令、代码、文档、日志或 Git 历史。
- 本账本本身尚未提交，必须随下一次合适的分片提交一并提交。

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

状态：**实现与主线程独立等价审查完成，准备精确提交**。

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
- staged snapshot 检查、精确提交与提交后 `git push`。

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
- [~] Task 7 — SELECT AST / renderer / read-only validator / generator：实现与独立审查完成，待 8.0.41 release integration；当前正在提交。
- [x] Task 8 — MySQL runner / KILL watchdog：已提交 `7b4566e`；8.0.41 release integration 待正式节点。
- [ ] Task 9 — three-node setup and query coordinator。
- [x] Task 10 — typed multiset/error/timeout oracle。
- [ ] Task 11 — fsynced artifacts / JSONL reader / HTML / replay。
- [ ] Task 12 — correctness service / mode registry / doctor / CLI vertical slice。
- [ ] Task 13 — core release gate / regression corpus。

### 8.2 Performance（7 tasks）

- [ ] Task 1 — performance policy / scale knobs（仅基础 config 已有，模块未实现）。
- [ ] Task 2 — EXPLAIN ANALYZE TREE parser / shape gate。
- [ ] Task 3 — reference calibration / frozen case。
- [ ] Task 4 — synchronized formal measurement / diagnostics / KILL adapter。
- [ ] Task 5 — skew/timeout/two-comparison verdict。
- [ ] Task 6 — persistence / sequential performance service。
- [ ] Task 7 — CLI/API contracts / release gates。

### 8.3 FastAPI + React control plane（17 tasks）

- [ ] Task 1–10 — API contract、RFC 9457、durable run state、process supervisor、SSE、SQLite read index、finding/artifact/report/replay、loopback hosting。
- [ ] Task 11–15 — typed React app、overview/new run/history/detail、SSE charts、finding virtual list、replay/report workflow。
- [ ] Task 16 — component coverage、accessibility、所有故障分支。
- [ ] Task 17 — Playwright backend/frontend recovery、production build。

### 8.4 12 小时 validation（11 tasks）

- [~] Task 1 — research domain model：catalog 部分已完成，validation 专用模型未完成。
- [~] Task 2 — allowlisted acquisition / immutable cache：source lock 已有，20 source 待刷新。
- [ ] Task 3 — offline candidate isolation / SQL safety envelope integration。
- [~] Task 4 — feature signatures：catalog signatures 已有，在线提取循环未完成。
- [ ] Task 5 — generator reachability audit 自动化。
- [ ] Task 6 — transactional checkpoint / append-only gap ledger。
- [ ] Task 7 — continuous 12-hour epoch coordinator。
- [ ] Task 8 — local three-instance MySQL manager。
- [ ] Task 9 — soak telemetry / deterministic fault schedule。
- [ ] Task 10 — coverage report / operator runbook。
- [ ] Task 11 — validation release gate。

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
6. **当前下一动作**：Task 7 已完成独立审查与 focused/property/full/static；精确暂存、提交并 push。
7. 实现 Task 9 三节点 coordinator；临时表必须使用 pinned sessions；看到 `connection_reusable=False` 必须丢弃并重建整个 session/round。
8. 实现 artifact/replay/CLI correctness vertical slice。
9. 再实现 performance、FastAPI/React、12h validation。
10. 获得可用三节点 MySQL 8.0.41 后执行 release matrix；8.0.45 只作 smoke。

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
