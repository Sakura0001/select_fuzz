# Fuzz Live Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 默认输出可直接解释 MySQL 大量 Sleep 原因的 fuzz 中文实时诊断。

**Architecture:** 在既有阶段遥测上增加阶段年龄和分组，在查询生成流水线上暴露有界快照；新建 fuzz 诊断模块维护连接登记、PROCESSLIST 最近采样、运行阶段、最近错误和状态格式化。`FuzzModeService` 只负责在生命周期边界更新状态并每 5 秒组合结构化快照，生产入口把中文行写到 `stderr`。

**Tech Stack:** Python 3.11、threading、mysql-connector-python、Pydantic、pytest、Ruff、Mypy。

## Global Constraints

- fuzz 模式默认开启，`fuzz.diagnostics_interval_seconds` 默认 `5`，范围 `(0, 60]`。
- 最终 `stdout` 汇总 JSON、已有事件类型、已有结构化字段和值完全不变。
- 诊断失败不能中断 fuzz，不改变读写路由、SQL 语义、预取深度、查询超时或换代逻辑。
- 每个有界列表最多保留 3 项，SQL 最多 300 字符。
- 连续 15 秒无读取进展才告警，同一原因最多每 30 秒重复一次。

---

### Task 1: 阶段年龄与 SQL 生成流水线快照

**Files:**
- Modify: `src/select_fuzz/modes/fuzz/telemetry.py`
- Modify: `src/select_fuzz/modes/fuzz/query_pipeline.py`
- Test: `tests/modes/fuzz/test_telemetry.py`
- Test: `tests/modes/fuzz/test_query_pipeline.py`

**Interfaces:**
- Produces: `QueryPipelineSnapshot(processes_total, processes_alive, registered_databases, pending_requests, pending_readers, oldest_pending_ns, max_pending_per_reader)`。
- Produces: `QueryGenerationPipeline.snapshot() -> QueryPipelineSnapshot`。
- Produces: `FuzzStageTelemetry.snapshot()` 新增 `stage_details` 和 `worker_groups`。

- [ ] **Step 1: 写阶段年龄失败测试**

```python
def test_same_stage_update_preserves_original_entry_time() -> None:
    now = 100
    telemetry = FuzzStageTelemetry(clock_ns=lambda: now)
    telemetry.set_stage("db0:reader-primary:0", "waiting_for_generated_sql")
    now = 150
    telemetry.set_stage("db0:reader-primary:0", "waiting_for_generated_sql")
    now = 200
    snapshot = telemetry.snapshot()
    assert snapshot["stage_details"]["waiting_for_generated_sql"]["max_age_ns"] == 100
    assert snapshot["worker_groups"]["reader_primary"] == {
        "waiting_for_generated_sql": 1
    }
```

- [ ] **Step 2: 运行阶段遥测测试并确认因构造参数或字段缺失而失败**

Run: `uv run pytest tests/modes/fuzz/test_telemetry.py -q`

- [ ] **Step 3: 实现阶段进入时间、分组和最老 3 个线程**

在 `set_stage()` 只在阶段改变时替换进入时间；`remove_worker()` 同时清理。`snapshot()` 使用一次
`clock_ns()` 计算非负年龄，保留已有字段并按年龄倒序截取 3 个 worker。

- [ ] **Step 4: 写流水线快照失败测试**

```python
snapshot = pipeline.snapshot()
assert snapshot.processes_total == 1
assert snapshot.processes_alive == 1
assert snapshot.pending_requests == 3
assert snapshot.pending_readers == 1
assert snapshot.max_pending_per_reader == 3
assert snapshot.oldest_pending_ns >= 0
```

并验证 `cancel_reader()` 后 pending 字段全部归零，inline pipeline 返回进程数 `0/0`。

- [ ] **Step 5: 运行流水线测试并确认 `snapshot` 缺失失败**

Run: `uv run pytest tests/modes/fuzz/test_query_pipeline.py -q`

- [ ] **Step 6: 实现统一流水线快照**

使用每 reader 最多 3 个时间戳的 `deque[int]`；submit 追加，release 左侧删除，cancel/close
整体删除。快照持锁复制数值，锁外调用 `process.is_alive()`，避免长时间占用内部锁。

- [ ] **Step 7: 运行 Task 1 测试**

Run: `uv run pytest tests/modes/fuzz/test_telemetry.py tests/modes/fuzz/test_query_pipeline.py -q`

### Task 2: 有界运行状态与 PROCESSLIST 采样

**Files:**
- Create: `src/select_fuzz/modes/fuzz/diagnostics.py`
- Create: `tests/modes/fuzz/test_diagnostics.py`

**Interfaces:**
- Produces: `FuzzRuntimeDiagnostics` 的 `set_phase()`、`register_connection()`、`unregister_connection()`、`record_issue()`、`snapshot()` 和 `connections()`。
- Produces: `FuzzProcesslistCollector.collect() -> dict[str, object]`。
- Produces: `FuzzProgressReporter.render(document) -> tuple[str, ...]`。

- [ ] **Step 1: 写运行状态和连接登记失败测试**

```python
tracker.register_connection(
    worker="db0:reader-replica:0", endpoint="replica", worker_kind="reader",
    database="sf_f_case", connection_id=42,
)
snapshot = tracker.snapshot()
assert snapshot["connection_groups"] == {"replica_reader": 1}
tracker.unregister_connection("db0:reader-replica:0", 42)
assert tracker.snapshot()["connections"] == 0
```

验证只保留最近 3 个 issue，SQL 截断到 300 字符，旧连接 ID 不能注销重连后的新连接。

- [ ] **Step 2: 写 PROCESSLIST 失败测试**

模拟控制会话返回 `(ID, DB, COMMAND, TIME, STATE, INFO)`，断言 SQL 只包含 tracker 登记的整数
ID；结果分别汇总 `primary` 和 `replica` 的 registered、visible、missing、commands、
longest_sleep_seconds、longest_query_seconds 和最慢 3 项。模拟权限错误时断言错误被放入快照而
不向外抛出。

- [ ] **Step 3: 写中文格式和原因分类失败测试**

构造 `running`、reads 15 秒无增长、80% reader 位于 `waiting_for_generated_sql`、生成进程
全部存活的文档，断言状态行含 `判断=SQL生成速度不足`，警告含 `初步原因`、最久等待、最近
错误；再次在 30 秒内 render 不重复警告。再覆盖生成进程死亡、执行、拉取、重连、线程缺失、
状态矛盾和初始化阶段。

- [ ] **Step 4: 运行新测试并确认模块缺失失败**

Run: `uv run pytest tests/modes/fuzz/test_diagnostics.py -q`

- [ ] **Step 5: 实现诊断模块**

所有共享状态使用 `Lock`；连接和 issue 按固定上限保存。PROCESSLIST SQL 使用 `int()` 后的
连接 ID 拼接，结果 INFO 截断。Reporter 使用单调时钟计算区间吞吐和无读取时长，正常行每次
返回，警告按原因和 30 秒冷却返回。

- [ ] **Step 6: 运行 Task 2 测试**

Run: `uv run pytest tests/modes/fuzz/test_diagnostics.py -q`

### Task 3: 服务生命周期接线与默认 stderr 输出

**Files:**
- Modify: `src/select_fuzz/config/models.py`
- Modify: `src/select_fuzz/modes/fuzz/service.py`
- Modify: `src/select_fuzz/modes/fuzz/entrypoint.py`
- Modify: `config/example.yaml`
- Modify: `config/intranet-fuzz.example.yaml`
- Modify: `README.md`
- Test: `tests/config/test_loader.py`
- Test: `tests/modes/fuzz/test_service.py`
- Test: `tests/modes/fuzz/test_entrypoint.py`
- Test: `tests/cli/test_cli.py`

**Interfaces:**
- `FuzzModeService(..., progress_sink: Callable[[str], None] | None = None)`。
- `FuzzConfig.diagnostics_interval_seconds: float = 5.0`。
- `build_fuzz_runner()` 注入 `_stderr_progress(message: str) -> None`。

- [ ] **Step 1: 写配置和生产接线失败测试**

断言旧 YAML 未配置时读取值为 `5`，`0` 和大于 `60` 被拒绝；entrypoint 构建的 service 带有
非空 progress sink，调用后文本只进入 `stderr`。

- [ ] **Step 2: 写服务组合快照失败测试**

使用可控 sink 和 collector，断言 `fuzz_stage_snapshot` 保留 `stages`/`durations`，新增
`counters`、`pipeline`、`runtime`、`connections`、`processlist`，sink 收到中文状态行；
collector 抛错时运行继续并在结构化字段中记录错误。

- [ ] **Step 3: 写连接生命周期失败测试**

模拟 reader/writer 长连接，断言 session 打开后 tracker 能看见连接 ID，退出或重连时旧 ID
被注销；读写分配仍保持 primary reader 1/3、replica reader 2/3、writer 全部 primary。

- [ ] **Step 4: 运行接线测试并确认失败**

Run: `uv run pytest tests/config/test_loader.py tests/modes/fuzz/test_entrypoint.py tests/modes/fuzz/test_service.py tests/cli/test_cli.py -q`

- [ ] **Step 5: 实现配置、监控线程和生命周期更新**

启动独立 `sf-fuzz-processlist` 线程更新最近采样，既有 `sf-fuzz-telemetry` 线程不等待数据库。
在 generation 开始、预热、运行、停止、失败和完成边界更新 phase；在 session context 内登记并
以 `(worker, connection_id)` 精确注销；timeout 和 connection loss 更新新增计数器；
`_append_stage_snapshot()` 组合数据、追加 JSONL 并调用 reporter。

- [ ] **Step 6: 注入 stderr 输出并更新文档示例**

`_stderr_progress()` 使用 `print(message, file=sys.stderr, flush=True)`。README 明确正常状态
每 5 秒显示、stdout 仍为最终 JSON，并列出各判断含义。

- [ ] **Step 7: 运行 Task 3 测试**

Run: `uv run pytest tests/config/test_loader.py tests/modes/fuzz tests/cli/test_cli.py -q`

### Task 4: 回归验证、代码审查和发布

**Files:**
- Modify: review 中发现的相关文件（仅限本设计范围）

**Interfaces:**
- Consumes: Tasks 1-3 的全部接口。
- Produces: 通过完整质量门并发布到当前远端分支的提交。

- [ ] **Step 1: 运行 fuzz 专项测试**

Run: `uv run pytest tests/modes/fuzz tests/config/test_loader.py tests/cli/test_cli.py -q`

- [ ] **Step 2: 运行静态检查**

Run: `uv run ruff check .`

Run: `uv run mypy`

- [ ] **Step 3: 运行全量测试和构建**

Run: `uv run pytest -q`

Run: `uv build`

- [ ] **Step 4: 审查并处理反馈**

按 requesting-code-review 检查正确性、兼容性、线程竞态、控制连接泄漏、敏感信息和缺失测试；
按 receiving-code-review 验证并修复全部 Critical/Important 发现，然后重新运行受影响测试和
全量质量门。

- [ ] **Step 5: 提交和推送**

Run: `git add docs src tests config README.md pyproject.toml`

Run: `git commit -m "feat: add live fuzz diagnostics"`

Run: `git push origin agent/publish-fuzz-mode`

- [ ] **Step 6: 触发并观察 CentOS 7 构建**

触发 `build-centos7-bundle` workflow，等待完成并记录 Action URL、artifact 名称和大小；若失败，
读取日志、修复后重新执行质量门并再次推送。
