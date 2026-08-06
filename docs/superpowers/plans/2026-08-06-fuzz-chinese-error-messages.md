# Fuzz Chinese Error Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 fuzz 运行失败链路的人类可读提示改为中文，同时逐字保留异常类型、底层异常原文和结构化事件接口。

**Architecture:** 只修改现有异常构造点和 CLI 渲染点，不增加翻译层，也不修改异常类型。每个输出边界先增加中文断言并验证失败，再替换工具自身的英文上下文；JSON 事件继续写入原有字段和值。

**Tech Stack:** Python 3.11、Typer、pytest、Ruff、Mypy、uv、GitHub Actions

## Global Constraints

- 只中文化 fuzz 运行链路的人类可读文本。
- 保留异常类名以及 MySQL、Python、连接器返回的原始异常文本。
- 保持 `type`、`error_type`、`query_timeout` 等 JSON 字段名和值不变。
- 不修改 `doctor`、`report`、`replay`、`cleanup` 的提示。
- 所有生产代码修改必须先有能够正确失败的测试。

---

### Task 1: 中文化 CLI 顶层运行失败提示

**Files:**
- Modify: `tests/cli/test_cli.py`
- Modify: `src/select_fuzz/cli.py`

**Interfaces:**
- Consumes: `run_command()` 捕获的任意 `Exception`。
- Produces: fuzz 模式 stderr 文本 `运行失败：<异常类型>：<异常原文>`；退出码仍为 1；其他模式继续使用原英文格式。

- [ ] **Step 1: 新增 fuzz 专用测试，要求中文提示并保留异常原文**

```python
def test_fuzz_run_cli_prints_runner_failure_in_chinese(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    class FailingRunner:
        def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
            raise RuntimeError("must-not-leak-database-error-detail")

    monkeypatch.setitem(MODE_RUNNERS, "fuzz", lambda config, root: FailingRunner())
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--mode",
            "fuzz",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--rounds",
            "1",
            "--artifacts",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "运行失败：RuntimeError：must-not-leak-database-error-detail" in result.output
    assert "Traceback" not in result.output
```

- [ ] **Step 2: 运行测试并确认因旧英文前缀失败**

Run: `.venv/bin/pytest -q tests/cli/test_cli.py::test_fuzz_run_cli_prints_runner_failure_in_chinese`

Expected: FAIL，实际输出仍以 `run failed:` 开头。

- [ ] **Step 3: 最小修改 CLI 渲染文本**

```python
except Exception as error:
    if selected_mode is RunMode.FUZZ:
        message = f"运行失败：{type(error).__name__}：{error}"
    else:
        message = f"run failed: {type(error).__name__}: {error}"
    typer.echo(message, err=True)
    raise typer.Exit(code=1) from None
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `.venv/bin/pytest -q tests/cli/test_cli.py::test_fuzz_run_cli_prints_runner_failure_in_chinese tests/cli/test_cli.py::test_run_cli_prints_runner_failure_without_traceback`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/cli/test_cli.py src/select_fuzz/cli.py
git commit -m "feat: localize fuzz run failure output"
```

### Task 2: 中文化 fuzz 批次创建失败

**Files:**
- Modify: `tests/modes/fuzz/test_service.py`
- Modify: `src/select_fuzz/modes/fuzz/service.py`

**Interfaces:**
- Consumes: `_GenerationFailure = tuple[int, str, str, str]`。
- Produces: `_GenerationBuildError` 中文消息；`failures` 属性及事件 JSON 完全不变。

- [ ] **Step 1: 修改批次失败测试，要求中文标签**

```python
message = str(captured.value)
assert "fuzz 批次创建失败：" in message
assert "数据库[0]=sf_f_" in message
assert "数据库[1]=sf_f_" in message
assert message.count(
    "异常类型=RuntimeError，原始错误=simulated kernel setup failure"
) == 2
```

将刷新失败断言同步改为：

```python
with pytest.raises(RuntimeError, match="fuzz 批次创建失败"):
```

- [ ] **Step 2: 运行测试并确认因旧英文消息失败**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_service.py::test_generation_build_waits_for_all_failures_and_never_starts_workers tests/modes/fuzz/test_service.py::test_failed_replacement_batch_does_not_fall_back_to_old_workers`

Expected: FAIL，消息仍包含 `fuzz generation build failed` 和 `database[...]`。

- [ ] **Step 3: 最小修改 `_GenerationBuildError`**

```python
rendered = "；".join(
    f"数据库[{ordinal}]={database}，异常类型={error_type}，原始错误={error_message}"
    for ordinal, database, error_type, error_message in failures
)
super().__init__(f"fuzz 批次创建失败：{rendered}")
```

- [ ] **Step 4: 运行两个测试并确认通过，同时核对结构化事件断言未改变**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_service.py::test_generation_build_waits_for_all_failures_and_never_starts_workers tests/modes/fuzz/test_service.py::test_failed_replacement_batch_does_not_fall_back_to_old_workers`

Expected: PASS；事件中的 `error_type == "RuntimeError"`、`error == "simulated kernel setup failure"` 仍通过。

- [ ] **Step 5: 提交**

```bash
git add tests/modes/fuzz/test_service.py src/select_fuzz/modes/fuzz/service.py
git commit -m "feat: localize fuzz generation failures"
```

### Task 3: 中文化主备同步超时

**Files:**
- Modify: `tests/modes/fuzz/test_materialization.py`
- Modify: `src/select_fuzz/modes/fuzz/materialization.py`

**Interfaces:**
- Consumes: `_wait_for_replica(database: str)` 中的超时秒数、数据库名和 `last_error`。
- Produces: 同一 `TimeoutError` 类型及完整中文上下文。

- [ ] **Step 1: 将两种超时分支的期望值改为中文**

```python
assert str(captured.value) == (
    "等待备节点同步超时：已等待 0.001 秒；数据库=sf_f_timeout；"
    "最后一次探测异常=RuntimeError：replica route unavailable"
)
```

```python
assert str(captured.value) == (
    "等待备节点同步超时：已等待 0.001 秒；数据库=sf_f_timeout；"
    "主节点同步标记在备节点尚不可见"
)
```

- [ ] **Step 2: 运行测试并确认因旧英文消息失败**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_materialization.py::test_replica_timeout_reports_last_probe_exception tests/modes/fuzz/test_materialization.py::test_replica_timeout_reports_marker_not_visible`

Expected: FAIL，实际值仍以 `replica synchronization timeout` 开头。

- [ ] **Step 3: 最小修改超时消息**

```python
detail = "主节点同步标记在备节点尚不可见"
if last_error is not None:
    detail = f"最后一次探测异常={type(last_error).__name__}：{last_error}"
raise TimeoutError(
    "等待备节点同步超时：已等待 "
    f"{self._replica_sync_timeout_seconds:g} 秒；"
    f"数据库={database}；{detail}"
)
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_materialization.py`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/modes/fuzz/test_materialization.py src/select_fuzz/modes/fuzz/materialization.py
git commit -m "feat: localize replica synchronization timeout"
```

### Task 4: 中文化 fuzz 查询生成基础设施失败

**Files:**
- Modify: `tests/modes/fuzz/test_query_pipeline.py`
- Modify: `tests/modes/fuzz/test_service.py`
- Modify: `src/select_fuzz/modes/fuzz/query_pipeline.py`
- Modify: `src/select_fuzz/modes/fuzz/service.py`

**Interfaces:**
- Consumes: `ProcessQueryPipeline.assert_healthy()` 的失败进程名；`_prewarm_generation()` 的 100 次拒绝上限。
- Produces: 中文 `QueryGenerationProcessDied` 和 `RuntimeError` 文本；异常类型不变。

- [ ] **Step 1: 增加进程异常退出消息测试**

在 `tests/modes/fuzz/test_query_pipeline.py` 导入 `QueryGenerationProcessDied`，复用 `_RollbackProcess` 构造非零退出进程：

```python
def test_pipeline_reports_dead_generator_process_in_chinese() -> None:
    pipeline = ProcessQueryPipeline(
        process_count=1,
        max_tables_per_query_block=1,
        reader_keys=((0, 0),),
    )
    process = _RollbackProcess(fail_start=False)
    process.name = "sf-query-generator-0"
    process.exitcode = 1
    pipeline._processes = [process]  # type: ignore[assignment]

    with pytest.raises(
        QueryGenerationProcessDied,
        match="查询生成进程异常退出：sf-query-generator-0",
    ):
        pipeline.assert_healthy()
```

在 `tests/modes/fuzz/test_service.py` 增加固定返回拒绝结果的 ticket 和 pipeline：

```python
class _RejectingTicket:
    def result(self, stop_event: Event) -> GenerationOutcome:
        del stop_event
        return GenerationOutcome(None, "CandidateRejected", 1, 1)


class _RejectingPipeline(_PrefetchPipeline):
    def submit(
        self,
        database_ordinal: int,
        reader_id: int,
        operation: int,
        *,
        seed: int,
    ) -> _RejectingTicket:
        del database_ordinal, reader_id, operation, seed
        return _RejectingTicket()
```

在 service 测试中导入 `_GenerationDatabase`，直接调用预热边界：

```python
def test_prewarm_rejection_limit_is_reported_in_chinese(tmp_path) -> None:  # type: ignore[no-untyped-def]
    schema = _schema("sf_f_rejected")
    service = FuzzModeService(
        config=FuzzConfig(
            databases=1,
            writer_threads_per_database=1,
            reader_threads_per_database=3,
            initial_tables=1,
            initial_rows_per_table=100,
            max_rows_per_database=1000,
        ),
        primary=NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        replica=NodeConfig(
            role=NodeRole.CUSTOM_ON,
            host="replica",
            port=3307,
        ),
        factory=_NoopFactory(),
        records=JsonlWriter(tmp_path / "events.jsonl"),
        query_generator=WeightedQueryGenerator((("test", _QueryGenerator(), 1),)),
        materializer_factory=lambda: _Materializer([], Lock()),
    )
    service._query_pipeline = _RejectingPipeline()  # type: ignore[attr-defined]

    request = RunRequest("run-fuzz-prewarm-rejected", "fuzz", 7, 1, None, 1)
    with pytest.raises(
        RuntimeError,
        match="尝试 100 次后仍无法为读线程预生成查询",
    ):
        service._prewarm_generation(  # type: ignore[attr-defined]
            request,
            (_GenerationDatabase(0, schema.database, 7, schema),),
            Event(),
        )
```

- [ ] **Step 2: 运行新增测试并确认因旧英文消息失败**

Run: `.venv/bin/pytest -q tests/modes/fuzz/test_query_pipeline.py tests/modes/fuzz/test_service.py -k 'dead_generator_process or prewarm_rejection'`

Expected: FAIL，消息仍为 `query generator process exited` 或 `failed to pre-generate...`。

- [ ] **Step 3: 替换两个工具自身生成的英文消息**

```python
raise QueryGenerationProcessDied(
    "查询生成进程异常退出：" + "，".join(failed)
)
```

```python
raise RuntimeError("尝试 100 次后仍无法为读线程预生成查询")
```

- [ ] **Step 4: 运行 fuzz 相关测试并确认通过**

Run: `.venv/bin/pytest -q tests/modes/fuzz tests/cli/test_cli.py`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/modes/fuzz/test_query_pipeline.py tests/modes/fuzz/test_service.py src/select_fuzz/modes/fuzz/query_pipeline.py src/select_fuzz/modes/fuzz/service.py
git commit -m "feat: localize fuzz query generation failures"
```

### Task 5: 全量验证、发布和构建

**Files:**
- Verify only: entire repository

**Interfaces:**
- Consumes: Tasks 1-4 的提交。
- Produces: 已推送的 `agent/publish-fuzz-mode` 和新的 `select-fuzz-centos7-x86_64` Action artifact。

- [ ] **Step 1: 运行完整验证**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
uv build
```

Expected: 全部通过；仅允许已有的第三方弃用警告。

- [ ] **Step 2: 核对提交范围和工作区状态**

```bash
git status -sb
git log --oneline origin/agent/publish-fuzz-mode..HEAD
```

Expected: 工作区干净，仅包含本计划的规格、计划和实现提交。

- [ ] **Step 3: 推送发布分支**

```bash
git push origin agent/publish-fuzz-mode
```

- [ ] **Step 4: 触发并等待 CentOS 7 bundle 构建**

```bash
gh workflow run build-centos7-bundle.yml --ref agent/publish-fuzz-mode
FUZZ_BUILD_RUN_ID=$(gh run list --workflow build-centos7-bundle.yml --branch agent/publish-fuzz-mode --event workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$FUZZ_BUILD_RUN_ID" --exit-status --interval 10
```

Expected: `bundle` job 成功并上传 `select-fuzz-centos7-x86_64`。
