# 主写备读逐表 CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 79 张内置基表增加可选的 74 个主库逐表 DML worker，并把用户指定数量的随机查询 worker 固定路由到备节点。

**Architecture:** 通过角色化 worker 和连接工厂拆分主写、备读；运行期恢复从任务级状态改为 worker 独立重连。独立 DML v1 生成器负责有界 INSERT/UPDATE/DELETE，查询生成器在备库模式禁用锁定读和临时表。

**Tech Stack:** Python 3.9+、FastAPI/Pydantic、PyMySQL、pytest、React/TypeScript/Ant Design、Node test、Vite

---

### Task 1: 版本化 seed、DML 生成器和查询安全选项

**Files:**
- Create: `select_fuzz/sqlgen/seeds.py`
- Create: `select_fuzz/sqlgen/dml.py`
- Modify: `select_fuzz/sqlgen/generator.py`
- Test: `tests/test_dml.py`
- Test: `tests/test_sqlgen.py`

- [ ] 写失败测试：uint64 seed 规范化、SHA-256 worker 派生向量、同 seed DML 序列、三类操作、10 行上限、10/200 边界、永久表过滤、备查询无锁定读/临时表。
- [ ] 运行聚焦测试并确认因模块/选项缺失而 RED。
- [ ] 实现 `DML_GENERATOR_VERSION='v1'`、`QUERY_GENERATOR_VERSION='v1'`、`derive_worker_seed()`、`DMLGenerator.generate()` 和查询选项。
- [ ] 运行 `pytest tests/test_dml.py tests/test_sqlgen.py -q` 并确认 GREEN。

### Task 2: 数据库返回影响行数与 worker 级运行时

**Files:**
- Modify: `select_fuzz/runner/db.py`
- Modify: `select_fuzz/runner/task.py`
- Test: `tests/test_runner.py`

- [ ] 写失败测试：`execute()` 返回 affected rows、74 永久表 DML worker + N 备查询 worker、角色路由、每 worker 独立客户端、普通错误静默继续、pending SQL断连原文重试、0.1～5 秒退避、单 worker 故障不改变全局 RUNNING、pause/resume/stop 资源清理。
- [ ] 运行聚焦测试并确认 RED。
- [ ] 扩展 `TaskWorker`/`WorkerRuntimeState`，实现 query/dml step 和独立重连，删除运行期全局 RECOVERING 依赖，保留初始化失败终态。
- [ ] 运行 `pytest tests/test_runner.py -q` 并确认 GREEN。

### Task 3: API、服务双端点与双隧道生命周期

**Files:**
- Modify: `select_fuzz/api/schemas.py`
- Modify: `select_fuzz/api/service.py`
- Modify: `select_fuzz/api/app.py`
- Modify: `select_fuzz/config.py`
- Modify: `select_fuzz/runner/jump.py`
- Test: `tests/test_api.py`
- Test: `tests/test_jump.py`
- Test: `tests/test_end_to_end.py`

- [ ] 写失败测试：请求默认/校验、随机及显式 seed、主备节点继承、同地址允许、无 read_only 探针、自定义目录 CRUD 提前拒绝、74+N 工厂角色、双隧道、后台零成功等待、创建/停止竞态。
- [ ] 运行聚焦测试并确认 RED。
- [ ] 实现请求/响应字段、角色节点/工厂、双隧道和后台 worker 枚举；任务快照增加 CRUD、seed、路由与重连汇总。
- [ ] 运行 `pytest tests/test_api.py tests/test_jump.py tests/test_end_to_end.py -q` 并确认 GREEN。

### Task 4: 并发日志与指标

**Files:**
- Modify: `select_fuzz/monitor/logs.py`
- Modify: `select_fuzz/monitor/events.py`
- Modify: `select_fuzz/monitor/store.py`
- Test: `tests/test_monitor.py`

- [ ] 写失败测试：角色化日志字段、按路径并发 JSONL 完整性、主备重连分别去重、旧 SQLite schema 迁移和并发写入。
- [ ] 运行聚焦测试并确认 RED。
- [ ] 实现路径锁、角色事件字段、WAL/busy_timeout/写锁与兼容迁移。
- [ ] 运行 `pytest tests/test_monitor.py -q` 并确认 GREEN。

### Task 5: 前端配置与任务卡

**Files:**
- Create: `web/src/workloadForm.ts`
- Create: `web/src/workloadFeature.static.test.mjs`
- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] 写失败测试：默认 16、CRUD 默认关、replica 端口继承、uint64 seed、旧响应规范化、主写备读/三组 seed/CRUD 汇总、74 worker 默认折叠、无复制延迟文案。
- [ ] 运行 `node --test web/src/*.test.mjs` 并确认 RED。
- [ ] 实现类型、纯函数、表单、卡片和响应式样式。
- [ ] 运行 Node 测试和 `npm run build --prefix web` 并确认 GREEN。

### Task 6: 文档、回归与审查

**Files:**
- Modify: `README.md`
- Modify: `configs/示例运行参数.yaml`
- Test: all relevant test files

- [ ] 更新中文 README/示例，明确 74 主连接 + N 备连接、无只读检查/复制等待、无限独立重连、种子与停止语义。
- [ ] 运行后端全量测试、前端测试和构建、`compileall`、validator、`git diff --check`。
- [ ] 完成规格审查与代码质量审查，修复所有 Critical/Important。
- [ ] 提交到集成 worktree，快进主分支并推送 `origin/main`，确认远端只保留 main。
