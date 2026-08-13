# Task-Level Base Table Column Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在创建任务时按开关生成核心或扩展基表，并通过冻结的 `v1 + seed` 契约跨版本复现扩展结构。

**Architecture:** 把基表 SQL 抽象为一次加载、全任务复用的不可变 `BaseSqlBundle`。内置扩展模式由版本注册表在内存生成；`v1` 使用带用途标签的 SHA-256 派生值和 rejection sampling，不依赖解释器随机状态。API 在连接数据库前准备并校验 bundle，`FuzzTask` 的启动、worker 初始化和恢复路径只消费同一个 bundle。

**Tech Stack:** Python、FastAPI、Pydantic、pytest、React、TypeScript、Ant Design、Vite

---

## 文件结构

- 新建 `select_fuzz/base_tables/models.py`：bundle 数据模型。
- 新建 `select_fuzz/base_tables/loader.py`：目录加载、解析和通用预校验。
- 新建 `select_fuzz/base_tables/deterministic.py`：冻结的 SHA-256 派生原语。
- 新建 `select_fuzz/base_tables/v1.py`：`v1` 核心及扩展 SQL 生成器。
- 新建 `select_fuzz/base_tables/registry.py`：版本登记、种子规范化及生成入口。
- 修改 `tools/generate_sql_base_tables.py`：保留离线 CLI，转调包内生成器。
- 修改 `tools/validate_sql_base_tables.py`：分别校验 42 列核心模式与 200～500 列扩展模式。
- 修改 `select_fuzz/api/schemas.py`、`service.py`：请求校验、任务快照和 bundle 准备。
- 修改 `select_fuzz/runner/task.py`、`monitor/logs.py`：内存 bundle 生命周期与日志复现元信息。
- 修改 `web/src/types.ts`、`api.ts`、`App.tsx`、`styles.css`：表单开关、种子输入、卡片展示和复制。
- 重新生成 `sql_base_tables/`：提交默认 42 核心列基线。

### Task 1: 建立内存基表包及预校验边界

**Files:**
- Create: `select_fuzz/base_tables/__init__.py`
- Create: `select_fuzz/base_tables/models.py`
- Create: `select_fuzz/base_tables/loader.py`
- Modify: `select_fuzz/metadata/base_sql.py`
- Test: `tests/test_base_table_bundle.py`
- Test: `tests/test_metadata.py`

- [ ] **Step 1: 写 bundle 顺序、解析和失败测试**

测试应构造内存 `BaseSqlFile`，验证自然顺序、种子文件排除、表元数据只解析一次，以及没有可解析表时抛出中文错误：

```python
def test_内存基表包保存有序文件和已解析表() -> None:
    files = (
        BaseSqlFile(Path("t0.sql"), "CREATE TABLE t0 (id int);"),
        BaseSqlFile(Path("zz_seed_fk_data.sql"), "CREATE TABLE `_select_fuzz_seed_numbers` (`n` int); /* t0:rows=10 */"),
    )

    bundle = build_base_sql_bundle(files)

    assert [item.path.name for item in bundle.files] == ["t0.sql", "zz_seed_fk_data.sql"]
    assert [table.name for table in bundle.tables] == ["t0"]
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `.venv/bin/python -m pytest tests/test_base_table_bundle.py tests/test_metadata.py -q`

Expected: FAIL，缺少 `select_fuzz.base_tables` 或目标 API。

- [ ] **Step 3: 实现最小不可变模型与加载器**

公开接口固定为：

```python
@dataclass(frozen=True)
class BaseSqlBundle:
    files: tuple[BaseSqlFile, ...]
    tables: tuple[TableMetadata, ...]
    expand_base_table_columns: bool = False
    generator_version: str | None = None
    seed: str | None = None

def build_base_sql_bundle(
    files: Iterable[BaseSqlFile],
    *,
    expand_base_table_columns: bool = False,
    generator_version: str | None = None,
    seed: str | None = None,
) -> BaseSqlBundle:
    ordered_files = tuple(files)
    tables = []
    for sql_file in ordered_files:
        if is_generated_seed_sql(sql_file.sql):
            continue
        try:
            tables.append(parse_create_table(sql_file.sql))
        except ValueError:
            continue
    if not tables:
        raise RuntimeError("至少需要一张可解析的基表")
    return BaseSqlBundle(
        files=ordered_files,
        tables=tuple(tables),
        expand_base_table_columns=expand_base_table_columns,
        generator_version=generator_version,
        seed=seed,
    )

def load_base_sql_bundle(base_dir: Path | str) -> BaseSqlBundle:
    return build_base_sql_bundle(load_base_sql_files(base_dir))
```

`metadata/base_sql.py` 增加按 `BaseSqlFile.sql` 判断种子脚本的接口，保留现有按路径接口以兼容调用者。

- [ ] **Step 4: 运行聚焦测试并重构到清晰边界**

Run: `.venv/bin/python -m pytest tests/test_base_table_bundle.py tests/test_metadata.py -q`

Expected: PASS。

- [ ] **Step 5: 提交 Task 1**

```bash
git add select_fuzz/base_tables select_fuzz/metadata/base_sql.py tests/test_base_table_bundle.py tests/test_metadata.py
git commit -m "建立内存基表包"
```

### Task 2: 实现冻结的 `v1` 扩列生成器

**Files:**
- Create: `select_fuzz/base_tables/deterministic.py`
- Create: `select_fuzz/base_tables/v1.py`
- Create: `select_fuzz/base_tables/registry.py`
- Modify: `tools/generate_sql_base_tables.py`
- Modify: `tools/validate_sql_base_tables.py`
- Modify: `tests/test_base_table_generator.py`
- Modify: `tests/test_metadata.py`
- Modify: `README.md`
- Regenerate: `sql_base_tables/t0.sql` through `sql_base_tables/t78.sql`
- Regenerate: `sql_base_tables/zz_seed_fk_data.sql`
- Regenerate: `sql_base_tables/执行顺序说明.md`

- [ ] **Step 1: 写核心/扩展模式和确定性契约测试**

测试至少覆盖：默认 42 列且没有 `extra_t*`；开启后每表 200～500 列；同一 `v1 + seed` 完全相同；不同 seed 不同；并发生成不串扰；核心列不受任务 seed 影响；非法 seed 和未知版本失败。

```python
def test_v1_相同种子生成字节级一致的扩展基表包() -> None:
    first = generate_base_sql_bundle("v1", "18446744073709551615")
    second = generate_base_sql_bundle("v1", "18446744073709551615")

    assert serialize_bundle(first) == serialize_bundle(second)
    assert all(200 <= len(table.columns) <= 500 for table in first.tables)
```

- [ ] **Step 2: 运行聚焦测试并确认缺少版本生成入口**

Run: `.venv/bin/python -m pytest tests/test_base_table_generator.py tests/test_base_table_bundle.py -q`

Expected: FAIL，缺少版本注册、稳定派生或核心模式参数。

- [ ] **Step 3: 实现用途隔离的 SHA-256 派生原语**

输入和字节序固定，不维护可变 PRNG 状态：

```python
def derive_uint64(*, version: str, seed: str, table_index: int, offset: int, purpose: str) -> int:
    payload = "\0".join(("select-fuzz-base-table", version, seed, str(table_index), str(offset), purpose, "0"))
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")

def derive_range(
    *,
    version: str,
    seed: str,
    table_index: int,
    offset: int,
    purpose: str,
    minimum: int,
    maximum: int,
) -> int:
    width = maximum - minimum + 1
    limit = (1 << 64) - ((1 << 64) % width)
    attempt = 0
    while True:
        payload = "\0".join(
            ("select-fuzz-base-table", version, seed, str(table_index), str(offset), purpose, str(attempt))
        )
        value = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")
        if value < limit:
            return minimum + value % width
        attempt += 1
```

每个属性使用独立 `purpose`，新增属性不得移动现有输出序列。

- [ ] **Step 4: 迁移生成器并实现核心/扩展双模式**

`v1.py` 保存完整且冻结的 79 张表生成规则和 SQL 渲染格式。把当前 79 份核心 `TableColumnProfile` 与 10～100 行 `row_count` 物化为常量，禁止在运行时调用 Python `random`。任务 seed 只传入目标列数和 `extra_tN_NNN` 的类型、参数及值表达式。`t0` 和 `t1` 分别固定覆盖 200、500 列边界，其余表由 seed 派生。

注册表公开：

```python
CURRENT_BASE_TABLE_GENERATOR_VERSION = "v1"
MAX_BASE_TABLE_SEED = 2**64 - 1

_GENERATORS = {"v1": v1.generate_base_sql_bundle}

def normalize_base_table_seed(seed: str) -> str:
    if not re.fullmatch(r"0|[1-9][0-9]*", seed):
        raise ValueError("基表种子必须是无符号十进制整数")
    value = int(seed)
    if value > MAX_BASE_TABLE_SEED:
        raise ValueError(f"基表种子不能大于 {MAX_BASE_TABLE_SEED}")
    return str(value)

def available_base_table_generator_versions() -> tuple[str, ...]:
    return tuple(_GENERATORS)

def generate_base_sql_bundle(version: str, seed: str) -> BaseSqlBundle:
    normalized_seed = normalize_base_table_seed(seed)
    try:
        generator = _GENERATORS[version]
    except KeyError as exc:
        raise ValueError(f"未知基表生成器版本: {version}") from exc
    return generator(normalized_seed, expand_base_table_columns=True)

def generate_core_base_sql_bundle() -> BaseSqlBundle:
    return v1.generate_base_sql_bundle("0", expand_base_table_columns=False)

def serialize_bundle(bundle: BaseSqlBundle) -> bytes:
    chunks = []
    for sql_file in bundle.files:
        chunks.extend((sql_file.path.name.encode("utf-8"), b"\0", sql_file.sql.encode("utf-8"), b"\0"))
    return b"".join(chunks)
```

- [ ] **Step 5: 改造离线 CLI 和校验器**

默认命令生成 42 列核心模式；扩展命令要求版本和 seed：

```bash
.venv/bin/python tools/generate_sql_base_tables.py --output-dir /tmp/core
.venv/bin/python tools/generate_sql_base_tables.py --output-dir /tmp/expanded --expand-columns --generator-version v1 --seed 12345
```

校验器增加对应 `--expanded-columns`、`--generator-version` 和 `--seed` 参数，核心模式拒绝扩展列，扩展模式验证 200～500 列及 seed SQL。

- [ ] **Step 6: 运行聚焦测试并修复到通过**

Run: `.venv/bin/python -m pytest tests/test_base_table_generator.py tests/test_base_table_bundle.py tests/test_metadata.py -q`

Expected: PASS。

- [ ] **Step 7: 建立 `v1` 金标并验证新进程一致性**

为固定 seed `0`、`12345` 和最大值至少选择一个完整 bundle 摘要作为金标；提交 80 个逻辑 SQL 文件的 SHA-256 manifest 和带长度前缀的完整 bundle 摘要。摘要覆盖文件名、UTF-8/LF SQL 原始字节和末尾换行。先生成期望值并写入兼容性测试，再用独立 Python 进程及至少两个 `PYTHONHASHSEED` 复核。

- [ ] **Step 8: 重新生成并校验仓库核心基线**

Run:

```bash
.venv/bin/python tools/generate_sql_base_tables.py --output-dir sql_base_tables
.venv/bin/python tools/validate_sql_base_tables.py --sql-dir sql_base_tables
```

Expected: 79 张表均为 42 列，验证器返回 0。

- [ ] **Step 9: 提交 Task 2**

```bash
git add select_fuzz/base_tables tools tests README.md sql_base_tables
git commit -m "增加可复现的任务级基表扩列生成器"
```

### Task 3: 接入 API、任务生命周期和日志

**Files:**
- Modify: `select_fuzz/api/schemas.py`
- Modify: `select_fuzz/api/service.py`
- Modify: `select_fuzz/api/app.py`
- Modify: `select_fuzz/runner/task.py`
- Modify: `select_fuzz/monitor/logs.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_monitor.py`
- Modify: `tests/test_end_to_end.py`

- [ ] **Step 1: 写请求校验和任务快照失败测试**

覆盖默认关闭、自动 seed、手动边界 seed、关闭时字段冲突、负数/空白/小数/越界 seed、未知版本、自定义目录扩展失败，以及生成校验失败时数据库从未连接或执行 SQL。

```python
def test_扩展基表生成失败发生在数据库连接之前(tmp_path: Path) -> None:
    db = RecordingDatabase()
    response = client.post("/api/tasks", json={**payload, "expand_base_table_columns": True})

    assert response.json()["phase"] == "准备基表"
    assert response.json()["base_table_seed"] is not None
    assert db.connect_count == 0
    assert db.executed == []
```

- [ ] **Step 2: 运行测试并确认新字段和提前校验尚不存在**

Run: `.venv/bin/python -m pytest tests/test_api.py tests/test_runner.py tests/test_monitor.py -q`

Expected: FAIL，缺少请求字段、快照字段或 bundle 注入。

- [ ] **Step 3: 实现 Pydantic 参数契约**

`TaskCreateRequest` 增加三个字段和模型级校验。空 seed 规范为 `None`；关闭时 seed/version 非空直接 `422`；开启时 version 空由服务补成 `v1`，seed 空由 `secrets.randbits(64)` 补齐。

- [ ] **Step 4: 在任何外部连接前准备 bundle**

`RuntimeService.create_task()` 的顺序固定为：规范化复现参数 → 创建 snapshot → 生成/加载并校验 bundle → 启动跳板机 → 创建数据库客户端 → 启动任务。bundle 失败返回“准备基表失败”的 snapshot，不调用隧道或 `db_factory`。

扩展模式只允许 `base_sql_dir.resolve()` 等于项目内置目录；自定义目录返回明确中文失败原因。

- [ ] **Step 5: 让 `FuzzTask` 全生命周期复用同一 bundle**

增加可选 `base_sql_bundle` 字段。没有注入时，在 `start()` 连接前从目录加载一次；有注入时直接使用。用 `_require_base_sql_bundle()` 统一替换启动、附加 worker、probe recovery 和 worker reconnect 中的目录读取。暂停保留 bundle；`stop()` 和 `fail()` 关闭连接后将 bundle 引用释放。

- [ ] **Step 6: 把复现元信息写入快照和 SQL 日志**

`TaskSnapshot`、`TaskResponse`、`FuzzTask`、`SqlLogRecord` 使用同名字段：

```python
expand_base_table_columns: bool = False
base_table_seed: str | None = None
base_table_generator_version: str | None = None
```

- [ ] **Step 7: 运行后端聚焦与全量测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_api.py tests/test_runner.py tests/test_monitor.py tests/test_end_to_end.py -q
.venv/bin/python -m pytest -q
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交 Task 3**

```bash
git add select_fuzz tests
git commit -m "接入任务级基表扩列配置"
```

### Task 4: 增加前端开关、种子输入和任务卡片展示

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: 扩展前端类型，保持 seed 为字符串**

`CreateTaskPayload` 和 `FuzzTask` 增加与 API 同名字段。`normalizeTask()` 为旧任务补 `false/null/null`，不得把 seed 转成 `number`。

- [ ] **Step 2: 构建一次并确认 UI 尚未使用字段**

Run: `npm run build`

Expected: 在类型先行改动后，因表单值或响应规范化未补齐而出现 TypeScript 错误，或通过但功能测试仍缺失；继续实现 UI。

- [ ] **Step 3: 实现条件表单和验证**

使用 Ant Design `Switch`，通过 `Form.useWatch()` 控制条件字段。seed 使用普通 `Input` 和字符串正则，不使用 `InputNumber`。关闭开关提交 `null/null`；开启且留空提交 `null`，由后端返回最终 seed。

- [ ] **Step 4: 实现卡片展示和复制**

核心模式显示 `基表模式：核心列（42 列）`。扩展模式显示版本和完整 seed，并通过 `navigator.clipboard.writeText(`${version}:${seed}`)` 复制；成功和失败任务使用相同展示逻辑。

- [ ] **Step 5: 前端构建与浏览器人工验收**

Run: `npm run build`

Expected: TypeScript 和 Vite 构建成功。

启动本地前后端后在浏览器验证：默认关闭；打开后出现版本与 seed；最大 seed 不丢精度；任务卡片展示并复制 `v1:seed`；失败卡片仍展示。

- [ ] **Step 6: 提交 Task 4**

```bash
git add web/src
git commit -m "在任务界面增加基表扩列开关"
```

### Task 5: 文档、代码审查和最终验证

**Files:**
- Modify: `README.md`
- Modify: `configs/示例运行参数.yaml` only if configuration wording needs clarification
- Verify: all changed files

- [ ] **Step 1: 更新中文使用说明**

说明默认 42 核心列、前端任务级扩展开关、`v1:seed` 复现方式、离线生成命令、自定义目录限制，以及项目不会持久化任务级 SQL。

- [ ] **Step 2: 运行格式和结构检查**

Run:

```bash
git diff --check
.venv/bin/python tools/validate_sql_base_tables.py --sql-dir sql_base_tables
```

Expected: 无空白错误，核心基线校验通过。

- [ ] **Step 3: 请求独立规格与代码质量审查**

审查范围包括：需求完整性、跨版本确定性、并发污染、数据库提前修改、bundle 生命周期、日志敏感信息、Pydantic 边界、JavaScript 大整数和缺失测试。修复所有 Critical/Important 问题后复审。

- [ ] **Step 4: 执行最终验证**

Run:

```bash
.venv/bin/python -m pytest -q
cd web && npm run build
git status --short
```

Expected: 后端全量测试通过、前端构建成功、只包含本需求预期文件。

- [ ] **Step 5: 提交收尾修改并推送**

```bash
git add README.md configs tests select_fuzz tools sql_base_tables web/src
git commit -m "完善基表扩列开关文档与验证"
git push -u origin codex/expand-base-table-columns
```
