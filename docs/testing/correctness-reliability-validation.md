# correctness 连接与误报可靠性验证

验证日期：2026-08-22

## 验证目标

- correctness 每轮只建立一对 `custom_off/custom_on` 会话，setup、EXPLAIN、SELECT 和 mutation 复用同一对连接。
- 单侧基础设施失败不复制为双侧同源错误，不生成 correctness finding。
- 普通表轮次断连后只重连并选择已有数据库；临时表轮次才重放 setup。
- 非确定性非零 LIMIT 不进入 MySQL 差分执行。
- 大于旧 64 MiB manifest 上限的 SQL 可写入、读取和回放。
- 致命错误保留异常原文与 traceback，并主动中止活动连接。

## 自动化验证

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv build
```

首次全量结果：`1537 passed, 13 skipped, 1 warning`；Ruff、Mypy、wheel 和 sdist 构建通过。

## MySQL 8.0.22 双实例

```bash
docker run -d --name sf-mysql8022-off \
  -e MYSQL_ROOT_PASSWORD=test_only_password \
  -p 127.0.0.1:13307:3306 mysql:8.0.22 \
  --max-connections=512 --performance-schema=ON

docker run -d --name sf-mysql8022-on \
  -e MYSQL_ROOT_PASSWORD=test_only_password \
  -p 127.0.0.1:13308:3306 mysql:8.0.22 \
  --max-connections=512 --performance-schema=ON
```

两个实例的 `SELECT VERSION()` 均返回 `8.0.22`。验证配置使用 8 worker、每轮 50 条成功查询、10 秒语句上限、20 秒 socket read timeout、8 路并发握手上限，并默认开启查询尝试 JSONL 和 5 秒诊断事件。

正常运行命令：

```bash
SELECT_FUZZ_MYSQL_USER=root \
SELECT_FUZZ_MYSQL_PASSWORD=test_only_password \
uv run select-fuzz run \
  --mode correctness \
  --config artifacts/correctness-reliability-smoke.yaml \
  --duration-seconds 300 \
  --seed 20260823 \
  --artifacts artifacts/correctness-reliability-smoke-run-2
```

## 首轮实测发现并修复的问题

旧恢复路径在查询连接失效后，会在同一个普通表数据库上重放 setup，导致 `CREATE TABLE` 变成 `rejected_generation`，随后 EXPLAIN 抛出 `round is not ready`。现在普通表轮次只并发建立新会话并执行 `USE existing_database`；临时表仍完整重建。

首轮还确认旧 310 秒 connector read timeout 会拖慢 macOS amd64 模拟容器下的致命退出。correctness 现在按配置使用 `语句上限 + 10 秒`，同时继续由 watchdog 提前中止超时 SQL。

## 最终观测

第三轮 5 分钟正常运行（seed `20260824`）完成：

- `findings=0`、`run_failed=0`、`queries_completed=18063`、`rounds_completed=364`。
- 期间出现 20 次基础设施重试，但没有致命退出；两个实例始终存活。
- 采样时每个实例约 7～9 个连接，Sleep 最大年龄为 1 秒，`Aborted_connects=0`。

第四轮故障注入（seed `20260825`）先暴露并定位了一个误报：主动停止 `custom_off` 时 MySQL 返回 `1053/08S01 Server shutdown in progress`，旧逻辑将它当普通 SQL error，产生 5 个错误 finding。finding 中的异常证据、SQL 和栈帧已经证明这是停机窗口，不是差分结果问题。

修复 `08xxx` 连接异常归类后，第五轮重复故障注入（seed `20260826`，120 秒）完成：

- `findings=0`、`run_failed=0`、`queries_completed=5545`、`rounds_completed=113`。
- 停机/恢复窗口产生 54 次 `infrastructure_pause`，客户端持续退避重试；恢复后正常继续执行。
- 收尾时两个实例均只有 1 个控制连接，Sleep 数量和最大 Sleep 时间均为 0。

这组结果说明：基础设施故障会保留原始异常并暂停当前轮次，恢复后复用普通表数据库而不重放 setup；不会把连接故障转换成 correctness finding，也不会留下长期 Sleep 查询线程。
