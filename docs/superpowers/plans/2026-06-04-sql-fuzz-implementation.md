# sql_fuzz Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 构建一个中文 Python 工程，实现 MySQL 8.0.41 + PolarDB 向量扩展的 SELECT fuzz 生成、持续执行、lost connection 监控、FastAPI 接口和中文前端大屏。

**Architecture:** 后端按元数据、SQL 生成、任务执行、监控日志、API 分层；前端使用 React 组件展示任务操作台和监控大屏。基表 SQL 目录作为运行时输入读取，每个任务启动时按目录内 SQL 文件全部执行，不在本计划中修改基表 SQL 文件。

**Tech Stack:** Python 3.9+、FastAPI、Pydantic、PyMySQL、pytest、React、Vite、TypeScript、Ant Design、ECharts。

---

### Task 1: 初始化工程骨架和配置

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `configs/示例运行参数.yaml`
- Create: `select_fuzz/__init__.py`
- Create: `select_fuzz/config.py`
- Create: `tests/test_config.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_config.py` with tests for Chinese defaults, base table directory resolution, and task options.

- [x] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: FAIL because `select_fuzz.config` does not exist.

- [x] **Step 3: Implement project metadata and config models**

Create `pyproject.toml`, package files, and Pydantic config classes for jump hosts, target nodes, runtime settings, and base SQL directory.

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: PASS.

### Task 2: 元数据和基表 SQL 加载

**Files:**
- Create: `select_fuzz/metadata/models.py`
- Create: `select_fuzz/metadata/base_sql.py`
- Create: `select_fuzz/metadata/ddl_parser.py`
- Create: `tests/test_metadata.py`

- [x] **Step 1: Write failing tests**

Create tests that build a temporary base SQL directory, verify files load in sorted order, parse table names, columns, indexes, partitions, foreign keys, and vector columns.

- [x] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_metadata.py -q`
Expected: FAIL because metadata modules do not exist.

- [x] **Step 3: Implement metadata models and lightweight DDL parser**

Implement deterministic SQL file loading and a conservative parser for table name, column definitions, key definitions, foreign keys, partition clauses, and `VECTOR(N)`.

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_metadata.py -q`
Expected: PASS.

### Task 3: 算子覆盖矩阵和类型感知 SQL 生成器

**Files:**
- Create: `select_fuzz/sqlgen/operators.py`
- Create: `select_fuzz/sqlgen/ast.py`
- Create: `select_fuzz/sqlgen/generator.py`
- Create: `tests/test_sqlgen.py`

- [x] **Step 1: Write failing tests**

Create tests for operator registry contents, generated SQL using known table metadata, CTE generation, JOIN generation, vector distance generation, and SQL length protection.

- [x] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_sqlgen.py -q`
Expected: FAIL because SQL generator modules do not exist.

- [x] **Step 3: Implement registry, AST rendering, and recursive generator**

Implement a seeded random generator that uses table and column metadata, chooses compatible expressions by type family, supports CTE, JOIN, WHERE, GROUP BY, HAVING, ORDER BY, LIMIT, set operations, subqueries, and vector functions when vector columns exist.

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_sqlgen.py -q`
Expected: PASS.

### Task 4: 日志、指标和 lost connection 去重

**Files:**
- Create: `select_fuzz/monitor/events.py`
- Create: `select_fuzz/monitor/logs.py`
- Create: `select_fuzz/monitor/store.py`
- Create: `tests/test_monitor.py`

- [x] **Step 1: Write failing tests**

Create tests for SQL JSONL logging, event JSONL logging, 10 minute per-node lost connection deduplication, and SQLite metric snapshots.

- [x] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_monitor.py -q`
Expected: FAIL because monitor modules do not exist.

- [x] **Step 3: Implement monitor layer**

Implement SQL log writer, lost connection detector, deduplicator, event recorder, and SQLite metric store.

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_monitor.py -q`
Expected: PASS.

### Task 5: 任务执行器、跳板机抽象和恢复检测

**Files:**
- Create: `select_fuzz/runner/db.py`
- Create: `select_fuzz/runner/jump.py`
- Create: `select_fuzz/runner/task.py`
- Create: `tests/test_runner.py`

- [x] **Step 1: Write failing tests**

Create fake database and fake clock tests verifying task phases, executing every SQL file from the base SQL directory during startup, query loop logging, lost connection transition to recovery, one minute probe cadence, and resume after recovery.

- [x] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_runner.py -q`
Expected: FAIL because runner modules do not exist.

- [x] **Step 3: Implement runner layer**

Implement database adapter protocols, PyMySQL adapter, optional jump tunnel abstraction, task state machine, startup DDL execution, query loop step execution, lost connection recovery, and no schema rebuild after recovery.

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_runner.py -q`
Expected: PASS.

### Task 6: FastAPI 后端接口

**Files:**
- Create: `select_fuzz/api/app.py`
- Create: `select_fuzz/api/schemas.py`
- Create: `select_fuzz/api/service.py`
- Create: `tests/test_api.py`

- [x] **Step 1: Write failing tests**

Create tests using FastAPI TestClient for health, task list, create task, stop task, metrics summary, lost connection events, SQL logs, coverage registry, jump host listing, and event stream endpoint shape.

- [x] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_api.py -q`
Expected: FAIL because API modules do not exist.

- [x] **Step 3: Implement API layer**

Implement an in-memory runtime service wired to monitor and runner abstractions, with Chinese response messages and stable JSON schemas.

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_api.py -q`
Expected: PASS.

### Task 7: 中文前端大屏

**Files:**
- Create: `web/package.json`
- Create: `web/index.html`
- Create: `web/tsconfig.json`
- Create: `web/vite.config.ts`
- Create: `web/src/main.tsx`
- Create: `web/src/App.tsx`
- Create: `web/src/styles.css`
- Create: `web/src/types.ts`
- Create: `web/src/api.ts`

- [x] **Step 1: Create frontend files**

Build the confirmed Chinese dark operations console with left navigation, top metrics, task cards, right-side task form, expandable task details, jump host panel, and scrollable lost connection event list.

- [x] **Step 2: Run frontend build**

Run: `cd web && npm install && npm run build`
Expected: PASS and `web/dist/` generated.

### Task 8: 文档、端到端验证和提交

**Files:**
- Modify: `README.md`
- Create: `tests/test_end_to_end.py`

- [x] **Step 1: Write end-to-end tests**

Create a test that uses temporary base SQL files, starts a task with fake DB, executes startup DDL, generates and logs SQL, handles lost connection deduplication, and exposes task data through API service.

- [x] **Step 2: Run full verification**

Run: `python3 -m pytest -q`
Expected: PASS.

Run: `cd web && npm run build`
Expected: PASS.

- [x] **Step 3: Commit implementation**

Run: `git status --short`, then stage only implementation files and commit with message `实现 sql_fuzz 工具首版`。
