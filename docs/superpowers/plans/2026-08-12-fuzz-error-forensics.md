# Fuzz Error Forensics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 fuzz 在错误风暴发生时保留首次完整异常现场、周期聚合趋势和明确中文根因，使下一次仅凭终端与 `events.jsonl` 即可定位客户端快速失败原因。

**Architecture:** 新建独立 `forensics.py` 负责异常证据、稳定指纹和有界聚合；执行层返回分阶段失败证据，watchdog 暴露只读动作快照；服务层负责关联 worker/SQL/连接并写首次样本与周期摘要；现有 reporter 只消费结构化摘要做中文判因。错误取证不改变查询、超时或重连决策。

**Tech Stack:** Python 3.11、dataclasses、threading.Lock、hashlib、json、pytest、Ruff、Mypy、GitHub Actions。

---

### Task 1: 异常证据和 watchdog 动作快照

**Files:**
- Create: `src/select_fuzz/modes/fuzz/forensics.py`
- Modify: `src/select_fuzz/execution/timeout.py`
- Modify: `src/select_fuzz/modes/fuzz/models.py`
- Modify: `src/select_fuzz/modes/fuzz/execution.py`
- Test: `tests/execution/test_timeout.py`
- Test: `tests/modes/fuzz/test_execution.py`
- Create: `tests/modes/fuzz/test_forensics.py`

- [ ] **Step 1: 写失败测试——异常证据保留原文、repr、args、errno、SQLSTATE、异常链和 traceback frame**

构造带 `__cause__` 的 connector 风格异常，在 execute/fetch/cursor close 三个位置分别抛出，断言
`FuzzExecutionResult.failure_evidence` 包含 `failure_stage`、完整消息、异常链、清理异常和 connection ID。

- [ ] **Step 2: 运行执行层测试确认因新字段/模块缺失而失败**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz/test_forensics.py tests/modes/fuzz/test_execution.py -q`

Expected: FAIL，提示 `failure_evidence`、`capture_exception_evidence` 或模块不存在。

- [ ] **Step 3: 实现有界异常证据模型**

在 `forensics.py` 提供 `capture_exception_evidence(error, stage)`，字符串限制 4096 字符、异常链最多
8 层、traceback frame 最多 32 个；`FuzzExecutionResult` 新增可选 `failure_evidence`，现有字段和值不变。

- [ ] **Step 4: 写失败测试——watchdog 快照包含 KILL QUERY、abort 和动作线程结果**

覆盖 KILL 成功、控制连接失败、fallback abort 成功/失败以及 KILL CONNECTION fallback，断言
`handle.diagnostic_snapshot()` 返回原始异常类型与消息而不是只有 `kill_error_type`。

- [ ] **Step 5: 运行 watchdog 测试确认快照接口缺失**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/execution/test_timeout.py -q`

Expected: FAIL，提示 `diagnostic_snapshot` 不存在或字段缺失。

- [ ] **Step 6: 实现锁保护的 watchdog 诊断快照**

记录动作类型、KILL QUERY started/finished/succeeded/error、abort attempted/succeeded/error、
KILL CONNECTION attempted/succeeded/error 和 completed；不改变现有线程和超时决策。

- [ ] **Step 7: 把分阶段证据与 watchdog 快照接入执行层**

分别标记 `connection_id`、`watchdog_arm`、`execute`、`fetch`、`cursor_close`、
`watchdog_cancel`；cursor close 失败不覆盖主异常，成功查询发生 close 失败时返回失败证据。

- [ ] **Step 8: 运行 Task 1 测试**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz/test_forensics.py tests/modes/fuzz/test_execution.py tests/execution/test_timeout.py -q`

Expected: PASS。

### Task 2: 稳定指纹和有界错误聚合

**Files:**
- Modify: `src/select_fuzz/modes/fuzz/forensics.py`
- Test: `tests/modes/fuzz/test_forensics.py`

- [ ] **Step 1: 写失败测试——动态连接 ID/地址/耗时归一化为同一指纹**

断言相同异常类型与消息模板、不同 connection ID/耗时得到同一 12 位指纹；不同 errno、stage 或
关键消息得到不同指纹。

- [ ] **Step 2: 写失败测试——聚合首次样本、周期增量和重复抑制**

使用可控时钟记录同一指纹 10 次，断言首次返回 `is_new=True`，后续不重复完整样本；snapshot
给出累计、周期、速率、worker/database/endpoint 影响数，第二次 snapshot 周期增量归零。

- [ ] **Step 3: 写失败测试——64 指纹容量与 other 桶有界**

记录 70 个不同指纹，断言活跃详情不超过 64、其余进入 `other`，代表样本每个最多 3 条。

- [ ] **Step 4: 运行测试确认指纹和聚合接口缺失**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz/test_forensics.py -q`

Expected: FAIL，提示 `error_fingerprint` 或 `FuzzErrorAggregator` 不存在。

- [ ] **Step 5: 实现指纹与线程安全聚合器**

使用规范化消息和稳定 JSON 计算 SHA-256；聚合器锁内只更新有界计数与短样本，不格式化
traceback、不写文件、不访问数据库。

- [ ] **Step 6: 运行 Task 2 测试**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz/test_forensics.py -q`

Expected: PASS。

### Task 3: 服务事件、连接可见性和重复日志抑制

**Files:**
- Modify: `src/select_fuzz/modes/fuzz/service.py`
- Modify: `src/select_fuzz/modes/fuzz/diagnostics.py`
- Test: `tests/modes/fuzz/test_service.py`
- Test: `tests/modes/fuzz/test_diagnostics.py`

- [ ] **Step 1: 写失败测试——新指纹写完整 `fuzz_error_sample`**

调用服务错误记录路径，断言首次事件包含完整 SQL、异常原文、异常链、traceback、连接 ID、
watchdog 和 fingerprint；相同指纹立即重复时不写第二条 sample。

- [ ] **Step 2: 写失败测试——兼容错误事件按 30 秒采样并记录 suppressed_repeats**

用可控时钟连续记录相同错误，断言首次保留原 `fuzz_operation_error` 字段，30 秒内无重复事件，
到期代表事件带 `suppressed_repeats`；counters 仍精确累计每次错误。

- [ ] **Step 3: 写失败测试——周期 summary 包含错误率和 Top 8**

断言 `_append_stage_snapshot()` 追加 `fuzz_error_summary`，同时 stage snapshot 新增 `errors_summary`
且已有字段不变。

- [ ] **Step 4: 写失败测试——首次指纹关联周期 PROCESSLIST 的 connection ID 可见性**

模拟后台 PROCESSLIST 的新鲜、陈旧、未采样和采集失败结果，断言精确 connection ID 关联只读取
内存快照，不发起工作线程控制查询；权限错误只进入诊断字段，不改变 fuzz 结果。

- [ ] **Step 5: 运行服务测试确认事件和聚合缺失**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz/test_service.py tests/modes/fuzz/test_diagnostics.py -q`

Expected: FAIL，提示新事件、摘要或探测字段缺失。

- [ ] **Step 6: 在服务层接入聚合器和事件采样**

`_record_error()` 接收 `failure_evidence`，精确增加 counter；首次指纹生成完整 traceback 文本并写
sample，重复指纹按 30 秒采样兼容事件；周期快照写 summary。

- [ ] **Step 7: 实现后台 PROCESSLIST 快照的 MySQL 可见性关联**

后台采集时保留登记 connection ID 中 MySQL 可见的集合，错误路径仅做内存关联并记录样本年龄；
写周期事件前移除该内部集合，禁止工作线程退化为无界工作连接 ping 或额外控制查询。

- [ ] **Step 8: 运行 Task 3 测试**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz/test_service.py tests/modes/fuzz/test_diagnostics.py -q`

Expected: PASS。

### Task 4: 中文错误风暴判因和文档

**Files:**
- Modify: `src/select_fuzz/modes/fuzz/diagnostics.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-12-fuzz-error-forensics-design.md`
- Test: `tests/modes/fuzz/test_diagnostics.py`

- [ ] **Step 1: 写失败测试复现内网错误风暴时间线**

构造读取增量为 0、错误率 500/s、72 reader 快速 execute、备节点全 Sleep 的 snapshot，断言状态行
和警告包含“客户端错误风暴”“查询未发送到 MySQL，客户端快速失败”、指纹、异常原文、stage、
连接可见性、watchdog 和影响范围。

- [ ] **Step 2: 运行 reporter 测试确认仍判断证据不足**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz/test_diagnostics.py -q`

Expected: FAIL，实际判断为“读取无进展但现有证据不足”。

- [ ] **Step 3: 实现错误风暴优先级和有界中文摘要**

错误率至少 10/s 且无读取 15 秒时优先于 MySQL 执行/矛盾判因；仅在 PROCESSLIST 新鲜并有 Sleep
证据时追加“查询未发送”结论，避免无证据断言。

- [ ] **Step 4: 更新 README 和设计规格实现说明**

说明新事件、重复抑制、准确错误总数来源、终端示例、数据边界和不改变负载语义。

- [ ] **Step 5: 运行 fuzz 专项测试**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/modes/fuzz tests/execution/test_timeout.py -q`

Expected: PASS。

### Task 5: 质量门、审查和发布

**Files:**
- Review all modified source, tests, docs, and workflow output.

- [ ] **Step 1: 运行静态检查**

Run: `UV_CACHE_DIR=.uv-cache uv run ruff check .`

Run: `UV_CACHE_DIR=.uv-cache uv run mypy`

Expected: both PASS。

- [ ] **Step 2: 运行全量测试和本地构建**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q`

Run: `UV_CACHE_DIR=.uv-cache uv build`

Expected: all tests PASS and wheel/sdist build succeeds。

- [ ] **Step 3: 进行只读代码审查**

审查 correctness、异常链、锁竞争、错误风暴内存/磁盘上界、敏感信息、事件兼容、watchdog race 和
缺失测试；修复全部 Critical/Important 后重新执行质量门。

- [ ] **Step 4: 提交并推送当前分支**

Run: `git add <explicit changed files>`

Run: `git commit -m "feat: add fuzz error forensics"`

Run: `git push origin agent/publish-fuzz-mode`

- [ ] **Step 5: 触发并观察 CentOS 7 Action**

Run: `gh workflow run build-centos7-bundle.yml --ref agent/publish-fuzz-mode`

等待成功后核对 `select-fuzz-centos7-x86_64` artifact 名称和实际大小。
