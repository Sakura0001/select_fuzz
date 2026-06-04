# sql_fuzz

`sql_fuzz` 是面向 MySQL 8.0.41 和 PolarDB MySQL 兼容向量扩展的 SQL 模糊测试工具。

项目默认使用中文文档、中文配置说明和中文界面文案。SQL、MySQL、PolarDB、函数名、错误码和数据类型保留官方英文写法。

## 第一版能力

- 读取基表 SQL 目录，并在每个任务启动时按文件名顺序全部执行。
- 基于已知表和列元数据生成随机 SELECT SQL。
- 支持一任务绑定一个 MySQL 节点。
- 支持任务级跳板机配置复用。
- 持续执行查询 SQL，不校验查询结果正确性。
- 记录日期、任务、节点、执行状态和 SQL。
- lost connection 按同一节点 10 分钟窗口去重。
- lost connection 后每 1 分钟探测数据库状态，恢复后继续执行查询。
- 提供 FastAPI 接口和中文前端大屏。

## 本地测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## 后端启动

```bash
.venv/bin/uvicorn select_fuzz.api.app:app --host 127.0.0.1 --port 8000
```

默认 API 会使用当前目录下的 `logs/` 保存指标和事件索引。

## 前端启动

```bash
cd web
npm install
npm run dev -- --port 5173
```

前端会优先读取 `/api/tasks`。如果本地后端未启动，会展示内置示例数据，便于先查看中文大屏布局。

## 基表 SQL 目录

基表 SQL 目录不由本实现生成。每个任务启动时，程序会读取配置中的 `base_sql_dir`，按文件名排序读取所有 `.sql` 文件，并在目标数据库上全部执行。后续持续执行阶段只生成并执行 SELECT SQL，不重建表、不重新插入数据。

## lost connection 规则

- 同一节点 10 分钟内只记录第一次 lost connection 事件。
- 大屏 lost connection 次数按去重后事件数展示。
- 发生 lost connection 后任务进入恢复检测状态。
- 恢复检测每 1 分钟执行一次。
- 数据库恢复后继续执行查询 SQL。
