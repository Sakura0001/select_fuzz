# 本地 MySQL 三模式实测

使用本机 Homebrew MySQL **8.0.45** 初始化了六个隔离实例，组成三组 GTID 主备。所有实例只监听 `127.0.0.1`，端口为 **33981–33986**，数据位于 `.local/mysql-validation/`。没有使用原有 MySQL 服务或内网配置。

数据库参数在启动前通过各实例的 `my.cnf` 配置；测试过程中不执行数据库调优参数修改。服务器保持 `time_zone=SYSTEM`（本机 CST）、`max_execution_time=0`，查询超时依靠客户端 watchdog。普通 MySQL 的三种主模式和 replay 均不再强制要求 PQ。

## 运行结果

| 验证 | 结果 | 证据目录（相对仓库根目录） |
|---|---|---|
| correctness | 相同种子三轮完成 120 条查询，差异 0，退出码 0 | `artifacts/local-mysql-20260908/correctness-fixed/` |
| performance | 三轮完成 30 条性能查询，拒绝 0、告警 0，退出码 0 | `artifacts/local-mysql-20260908/performance-fixed/` |
| fuzz 宽表连续换代 | 200–500 列、两库、4 个主库写线程、4 个主库读线程、8 个备库读线程，约 126 秒完成三代；23,337 次成功读取、18,917 个成功写事务 | `artifacts/local-mysql-20260908/fuzz-wide-bounded/` |
| 主备数据核对 | 上述六个生成库的 18 张表，行数和 DML 核心列聚合摘要均一致；每库均为 2,000 行，未超过预算 | `fuzz-wide-bounded/replica-consistency.json` |
| 主备连接故障 | 主动断开一个主库 writer 和一个备库 reader，均自动重连；保留具体连接及时间 | `fuzz-wide-fixed/injected-disconnects.json` |
| Python 驱动与 SIGINT | 578 次成功读取、5,541 个成功写事务；13 次超时后重连，未再出现连续 Unread result 错误；发 SIGINT 后约 1.05 秒完成退出 | `artifacts/local-mysql-20260908/fuzz-python-fixed/` |
| 真实驱动边界回归 | C/Python 驱动的未读结果清理截止时间、255 字符 payload 插入均通过 | `artifacts/local-mysql-20260908/live-regressions.log` |
| 最终代码 C 驱动复测 | 约 66 秒完成两代，11,415 次成功读取、13,507 个成功写事务；四个生成库主备摘要一致且均为 2,000 行 | `artifacts/local-mysql-20260908/fuzz-final/` |

宽表连续换代的 SQL 日志覆盖 INSERT 4,805 次、UPDATE 25,586 次、DELETE 5,564 次、UPSERT 5,783 次尝试；主库 SELECT 尝试 7,196 次，备库 16,495 次。这些是 SQL **尝试数**，与成功事务数、成功查询数分开统计。

fuzz 保留类型转换、数值溢出、死锁、唯一键冲突及慢查询超时等压力反馈。上述宽表运行记录了 369 次操作错误，其中 196 次为受控超时；这些没有导致工作线程永久停止。三次需要丢弃连接的超时均发生了重连，最终登记工作连接为 0。

## 复现和修复的问题

1. **普通 MySQL 查询被 PQ 准入阻挡**：移除 correctness、performance、fuzz、replay 生产构造中的强制 PQ 开关，保留独立显式 PQ 功能。
2. **TIMESTAMP 初始化在非 UTC 时区失败**：原来的 epoch 下界作为本地时间写入，东八区转换后越界，导致两轮初始化失败。生成的 TIMESTAMP 现在携带 `+00:00`，保留精度和真实上下界；DATE/DATETIME 不受影响，也不改变会话时区。实机验证上下界写入无警告，UNIX_TIMESTAMP 与预期一致。
3. **游标清理绕过超时保护**：真实 `SHOW FULL PROCESSLIST` 采到 121 秒查询，线程卡在驱动 `MySQL_consume_result`。原实现先撤销 watchdog，再调用可能继续读取结果的 `cursor.close()/nextset()`。现在直到清理结束才取消并等待 KILL 完成，三模式共用执行器同步修复。原查询与原生线程栈保存在 `fuzz-wide-01/stuck-query.json`、`native-stack.txt`。
4. **清理失败后复用损坏连接**：Python 驱动超时清理返回 `Unread result found` 后继续复用连接，15 秒内累计 12,670 次错误。现在清理失败、主动 abort 或 KILL CONNECTION 后强制重连；仅成功 KILL QUERY 且清理正常的连接可复用。复跑 25 秒只有 17 次操作错误（13 次超时），没有重复 Unread result 错误。
5. **事务回滚污染行数预算**：INSERT/DELETE 之前按语句即时修改共享预算，后续死锁或唯一键失败回滚后预算无法恢复。现在按事务结算，提交前保留 INSERT 预留、提交后才释放 DELETE 容量；确认回滚只释放本事务预留。提交/回滚结果不确定时保守保留容量，避免突破上限。
6. **克隆长 payload 导致截断错误**：对 VARCHAR(255) 直接追加后缀可能超长。现在按后缀长度截取原字符串，255 字符输入可成功插入且无警告。

这些修复均有先失败、后通过的针对性回归。额外审查修复了本地实验脚本的采样读写超时、部分初始化重试、陈旧 PID 停机处理。

## 慢查询、复制和环境边界

- 修复前：宽表运行的一个查询达到 121 秒，旧批次无法停止，需要对已核实的实验连接手动 KILL QUERY。
- 修复后：宽表连续换代进程列表采样最长用户查询 4 秒；Python 驱动复跑最长 3 秒。3 秒客户端截止时间加取消清理需要少量额外时间，数值是每秒采样到的整数秒数。
- 高压力宽表场景（每库 20,000 行、每批 25–100 行）仍能造成复制积压：采样最大 36 秒，超过 20 秒同步等待后新一代初始化明确失败退出；复制 IO/SQL 线程没有错误，之后自行追平。保留了该失败证据，没有通过调大数据库参数隐藏它。
- 连续换代验证使用单独的 `wide-bounded` 负载配置（每库 2,000 行、每批 1–5 行），保持相同列宽和 16 个工作线程，最大复制延迟 2 秒。这里改变的是预先指定的测试数据规模，服务器配置保持不变。
- performance 首次运行曾报告一个亚毫秒 SQL 的比例告警：custom_off 为 0.463 ms，custom_on 为 0.719 ms；三实例的配置指纹一致。后续三轮无告警。该环境用于验证性能测试流程，不代表真实硬件上的性能结论。
- 参数审计对比完整 `SHOW GLOBAL VARIABLES` 快照，仅排除只读的事务进度值 `gtid_executed`、`gtid_purged`、`gtid_owned`。初次原始结果直接比较全部变量会显示变化；汇总审计区分了事务进度与调优参数。

## 验证命令

在仓库根目录执行；实验配置、日志和随机测试凭据由脚本生成，凭据只保存在权限为 0600 的忽略文件中。

```bash
.venv/bin/python scripts/local_mysql_lab.py start
.venv/bin/python scripts/local_mysql_lab.py run --mode correctness --rounds 3 --seconds 120 --output artifacts/local-check-correctness
.venv/bin/python scripts/local_mysql_lab.py run --mode performance --rounds 3 --seconds 120 --output artifacts/local-check-performance
.venv/bin/python scripts/local_mysql_lab.py run --mode fuzz --profile wide-bounded --seconds 125 --output artifacts/local-check-fuzz
.venv/bin/python scripts/local_mysql_lab.py run --mode fuzz --connector python --seconds 60 --interrupt-after 25 --output artifacts/local-check-python
SELECT_FUZZ_LOCAL_MYSQL_TESTS=1 .venv/bin/python -m pytest -q --tb=short tests/integration/test_local_mysql_fuzz_lifecycle.py
.venv/bin/python scripts/local_mysql_lab.py status
.venv/bin/python scripts/local_mysql_lab.py stop
```

预期：doctor 允许启动；正常三模式完成并产出 JSON 摘要；fuzz 可按时停止和换代，出现受控 SQL 错误后仍继续读写；停止后实验端口关闭。performance 根据当前实测耗时触发比例告警时退出码为 1，应检查报告，不把它等同于程序崩溃。每次 `--output` 必须使用新目录，避免覆盖证据。fuzz 以 `--seconds` 限制运行时间。

离线普通 MySQL 主流程验证命令：

```bash
.venv/bin/python -m pytest -q --ignore=tests/pq -m 'not mysql and not mysql_performance and not online'
.venv/bin/python -m ruff check src tests scripts/local_mysql_lab.py
git diff --check
```

最终普通 MySQL 离线套件 **1,698 passed**，真实驱动回归 **3 passed**，Ruff 和 `git diff --check` 通过。最终六实例检查：无遗留工具会话，三组复制均追平，IO/SQL 错误号为 0；服务器日志未记录 ERROR 或崩溃。

2026-09-08 实测结束时，完整离线套件的独立 `tests/pq/test_oracle.py` 还保留 17 项既有失败，strict mypy 还保留 23 项问题；该轮原始输出保存在 `artifacts/local-mysql-20260908/`。

2026-09-09 发布检查已修复这两组问题：比较器正确定位容差之外的差异，ORDER 诊断保留数值误差证据，并检查完整数值类型元数据；其余修改补齐类型信息，不改变数据库连接参数。独立 PQ 离线测试 **376 passed**，strict mypy **135 个文件通过**。发布检查日志与修复前备份位于 `artifacts/release-20260909/`。

最终发布版本完整离线回归 **2,074 passed、18 deselected**，行覆盖率 **90.65%**、分支覆盖率 **82.22%**，通过仓库 88%/81% 的门槛；Ruff 和打包脚本语法检查通过。18 个外部环境相关用例未在本次离线发布检查运行；真实主备与驱动回归结果见上文。

## 保留与回滚

六个实验实例测试结束后停止，保留数据和证据，方便复现。`start` 可重启同一组实验实例；脚本不会停止原有 MySQL 服务。

本轮修改前文件在 `artifacts/local-mysql-20260908/baseline/`，本轮文件清单在 `artifacts/local-mysql-20260908/changes.json`。回滚代码时按清单逐个从 baseline 复制原文件；清单中标记新增的文件仅在确认没有后续修改后移除。不要使用整库 `git reset/restore`，工作区包含本轮之前的未提交修改。
