# 本地 MySQL 三模式验证

目标：在隔离的原生 MySQL 8.0 环境中验证 correctness、performance、fuzz，修复实际异常。主流程不要求 PQ；测试期间不修改数据库调优参数。

1. 保存当前工作区快照，解除主流程的 PQ 强制准入并补充普通 MySQL 回归。
2. 使用本机 MySQL 8.0.45，预先配置三组 GTID 主备（六个独立实例），只监听回环地址。保留启动、停止和状态检查方法。
3. 分别执行三个模式，采集 SHOW FULL PROCESSLIST、复制状态、数据库错误日志、线程 SQL 和运行摘要。
4. 重点验证 fuzz 主库 INSERT/UPDATE/DELETE/UPSERT、主备 SELECT、宽表、schema 轮换、超时和退出。对复现的问题先建立回归，再修复并重跑。
5. 执行相关离线测试，记录各模式的运行结果、慢 SQL 分析、已知限制及回滚路径，停止本次创建的实例。

本轮修改前快照：`artifacts/local-mysql-20260908/baseline/`。不得操作本机已有的 MySQL 实例，也不使用已有内网或本机压力配置。
