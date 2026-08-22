# TaurusDB 定向特性 campaign

该 campaign 用于通用本地 MySQL 8.0.22 / TaurusDB 对比阶段之后的定向测试。每个
case 使用新的保留数据库，`events.jsonl` 和 `cases/*.json` 追加保存 setup SQL、查询 SQL、
两端结果、列元数据和异常原文；不会把凭据写入配置或日志。

```bash
export SELECT_FUZZ_LOCAL_MYSQL_USER='...'
export SELECT_FUZZ_LOCAL_MYSQL_PASSWORD='...'
export SELECT_FUZZ_TAURUS_MYSQL_USER='...'
export SELECT_FUZZ_TAURUS_MYSQL_PASSWORD='...'
uv run python scripts/run_taurus_feature_campaign.py \
  --duration-seconds 14400 \
  --workers 64 \
  --artifacts artifacts/phase2-taurus-features
```

覆盖的场景包括：

- PQ/left-join elimination、聚合、窗口函数和 `EXPLAIN FORMAT=JSON`；
- `BACKQUERY=1` 以及 `AS OF TIMESTAMP` 闪回读取；
- RANGE/LIST、RANGE/HASH、LIST/HASH、HASH/LIST、KEY/RANGE、RANGE/KEY 二级分区组合；
- TaurusDB optimizer_switch 扩展项的 on/off 执行与能力记录。

异常分类遵循以下边界：

- 查询执行阶段的非超时 2006/2013/2055 标为 `connection_lost_infra`，保留完整 SQL/case
  供外部进程监控复核，但不会仅凭断链证据生成 crash finding；
- watchdog timeout 后产生的 lost connection 标为 `timeout_connection`；
- 建连或 setup 阶段的 lost connection 同样标为 `connection_lost_infra`，不会直接误报 crash；
- 只有上游明确提供 `crash_candidate` 分类时才生成 crash finding；
- 本地节点预期不支持 Taurus-only 变量/DDL 时记录为 `capability_probe`，不生成正确性 finding。

停止方式：`Ctrl-C` 或发送 SIGTERM。当前 case 完成后 campaign 写入 `run_finished`，已创建的
测试数据库保留，便于人工复现。
