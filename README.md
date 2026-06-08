# sql_fuzz

`sql_fuzz` 是面向 MySQL 8.0.41 和 PolarDB MySQL 兼容向量扩展的 SQL 模糊测试工具。

项目默认使用中文文档、中文配置说明和中文界面文案。SQL、MySQL、PolarDB、函数名、错误码和数据类型保留官方英文写法。

## 第一版能力

- 读取基表 SQL 目录，并在每个任务启动时按文件名顺序全部执行。
- 基于已知表和列元数据生成随机 SELECT SQL。
- 支持一任务绑定一个 MySQL 节点。
- 支持任务级跳板机配置复用。
- 跳板机支持 SSH 账号密码登录，也保留私钥路径作为可选登录方式。
- 支持为单个实例配置并发线程数，每个 worker 使用独立数据库连接执行查询。
- 支持从前端暂停、恢复和停止单个任务。
- 持续执行查询 SQL，不校验查询结果正确性。
- 记录日期、任务、节点、执行状态和 SQL。
- 任务接口和前端任务卡片会展示成功查询、失败查询、普通错误和 lost connection 事件统计。
- 启动、建库建表、种子数据校验等环节失败时，前端会保留失败任务并展示失败环节和错误原因。
- 后台会记录每个 worker 的状态和当前 SQL；worker 执行 SQL 超过阈值时会关闭该 worker 连接并标记为“疑似卡住”。
- 普通错误和 lost connection 的失败 SQL 会额外写入 `logs/failed_sql/日期/任务.sql`，文件内容只包含原始 SQL 语句。
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

前端会读取 `/api/tasks` 获取真实任务。若本地后端未启动，页面只显示“后端未连接”和空任务状态，不会展示内置示例任务。

## 基表 SQL 目录

项目默认使用 `sql_base_tables/`。每个任务启动时，程序会读取配置中的 `base_sql_dir`，按文件名排序读取所有 `.sql` 文件，并在目标数据库上全部执行。启动阶段会执行 `DROP DATABASE IF EXISTS test`、`CREATE DATABASE test`、`USE test`，随后创建基表和插入种子数据，并对每张解析到的表执行 `SELECT COUNT(*)` 校验，发现 0 行会直接失败。

`sql_base_tables/` 包含普通表、临时表、一级分区表、二级分区表和 `VECTOR(N)` 列。由于临时表是 session 级对象，多线程任务会在每个 worker 连接中单独创建临时表并插入临时表种子数据。lost connection 恢复后也只重建临时表并重新插入临时表数据，不重建永久表。

向量查询按 PolarDB MySQL 当前公开能力生成：`STRING_TO_VECTOR`、`VECTOR_TO_STRING`、`DISTANCE(..., 'COSINE'/'EUCLIDEAN'/'DOT')`。生成器不会使用 `VEC_DISTANCE`、`VEC_FROMTEXT`，也不会把向量列用于主键、外键、唯一键、分区键、普通跨类型比较、通用分组或普通排序表达式。

## lost connection 规则

- 同一节点 10 分钟内只记录第一次 lost connection 事件。
- 大屏 lost connection 次数按去重后事件数展示。
- 发生 lost connection 后任务进入恢复检测状态。
- 恢复检测每 1 分钟执行一次。
- 数据库恢复后继续执行查询 SQL。

## 任务控制和异常展示

- 任务启动后会按“连接实例 → 准备基表 → 执行 SQL”的环节推进。任一环节失败时，任务状态变为“失败”，任务卡片停留在失败环节，并展示后端返回的错误原因。
- 任务卡片提供暂停、恢复和停止操作。暂停不会关闭数据库连接，恢复后继续按暂停前的状态执行。
- 多线程任务会展示每个 worker 的状态、已成功 SQL 数和最近错误。前端任务列表每 1 秒刷新一次。后台看门狗默认在 worker 单条 SQL 执行超过 120 秒时关闭该 worker 连接，防止线程长时间卡住且无法定位。

## Windows 运行方式

Windows 推荐使用 PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn select_fuzz.api.app:app --host 127.0.0.1 --port 8000
```

另开一个 PowerShell 启动前端：

```powershell
cd web
npm install
npm run dev -- --port 5173
```

浏览器打开 `http://localhost:5173/`。如果要通过跳板机连接内网实例，先在页面保存跳板机配置，再新建任务时选择该配置并设置并发线程数。
