import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const sourceDirectory = fileURLToPath(new URL(".", import.meta.url));

function source(name) {
  return readFileSync(new URL(name, import.meta.url), "utf8");
}

test("前端契约覆盖主备路由、CRUD、种子和 worker 角色", () => {
  const types = source("types.ts");

  for (const field of [
    "primary_target",
    "replica_target",
    "replica_host",
    "replica_port",
    "enable_crud",
    "query_seed",
    "query_generator_version",
    "crud_seed",
    "crud_generator_version",
    "query_worker_total",
    "crud_worker_total",
    "insert_success_total",
    "update_success_total",
    "delete_success_total",
    "primary_reconnecting",
    "replica_reconnecting"
  ]) {
    assert.match(types, new RegExp(`\\b${field}\\??:`), `缺少 ${field}`);
  }
  for (const field of ["worker_type", "db_role", "target", "table_name", "reconnecting", "reconnect_total"]) {
    assert.match(types, new RegExp(`\\b${field}\\??:`), `worker 缺少 ${field}`);
  }
  assert.doesNotMatch(types, /replication_lag|复制延迟/i);
});

test("旧任务缺少新字段时使用安全默认值", () => {
  const api = source("api.ts");

  assert.match(api, /primary_target:\s*task\.primary_target\s*\?\?\s*task\.target/);
  assert.match(api, /replica_target:\s*task\.replica_target\s*\?\?/);
  assert.match(api, /enable_crud:\s*task\.enable_crud\s*\?\?\s*false/);
  assert.match(api, /query_worker_total:\s*task\.query_worker_total\s*\?\?/);
  assert.match(api, /crud_worker_total:\s*task\.crud_worker_total\s*\?\?\s*0/);
  assert.match(api, /primary_reconnecting:\s*task\.primary_reconnecting\s*\?\?\s*0/);
  assert.match(api, /replica_reconnecting:\s*task\.replica_reconnecting\s*\?\?\s*0/);
});

test("旧任务响应可在运行时规范化", async () => {
  const apiPath = `${sourceDirectory}api.ts`;
  const { normalizeTask } = await import(pathToFileURL(apiPath).href);
  const task = normalizeTask({
    task_id: "legacy-1",
    node_name: "旧任务",
    target: "127.0.0.1:3306",
    status: "执行 SQL",
    sql_total: 12,
    lost_connection_total: 0
  });

  assert.equal(task.primary_target, "127.0.0.1:3306");
  assert.equal(task.replica_target, "127.0.0.1:3306");
  assert.equal(task.database, "test");
  assert.equal(task.enable_crud, false);
  assert.equal(task.query_worker_total, 1);
  assert.equal(task.crud_worker_total, 0);
  assert.equal(task.success_query_total, 12);
  assert.deepEqual(task.worker_states, []);
});

test("主备表单按可选备节点规范化且新任务不提交固定查询或 CRUD 种子", async () => {
  const helperPath = `${sourceDirectory}crudRoutingForm.ts`;
  assert.ok(existsSync(helperPath), "缺少主备表单规范化模块");
  const { normalizeCrudRoutingFormFields } = await import(pathToFileURL(helperPath).href);

  assert.deepEqual(
    normalizeCrudRoutingFormFields({ replica_host: "", replica_port: 3307, enable_crud: false }),
    { replica_host: null, replica_port: null, enable_crud: false }
  );
  assert.deepEqual(
    normalizeCrudRoutingFormFields({ replica_host: " 10.0.0.12 ", replica_port: 3307, enable_crud: true }),
    { replica_host: "10.0.0.12", replica_port: 3307, enable_crud: true }
  );

  const app = source("App.tsx");
  assert.match(app, /thread_count:\s*16/);
  assert.match(app, /enable_crud:\s*false/);
  assert.match(app, /name="replica_host"/);
  assert.match(app, /name="replica_port"/);
  assert.match(app, /name="enable_crud"/);
  assert.match(app, /备节点查询线程数/);
  assert.doesNotMatch(app, /name="query_seed"/);
  assert.doesNotMatch(app, /name="crud_seed"/);
});

test("任务卡片显示路由、三类复现标识、CRUD 汇总和重连状态", () => {
  const app = source("App.tsx");
  const styles = source("styles.css");

  assert.match(app, /主写/);
  assert.match(app, /备读/);
  assert.match(app, /查询种子/);
  assert.match(app, /CRUD 种子/);
  assert.match(app, /基表种子/);
  assert.match(app, /INSERT/);
  assert.match(app, /UPDATE/);
  assert.match(app, /DELETE/);
  assert.match(app, /主节点重连/);
  assert.match(app, /备节点重连/);
  assert.match(app, /逐表 DML worker/);
  assert.match(app, /dmlWorkers/);
  assert.match(app, /queryWorkers/);
  assert.match(app, /<Collapse[^>]*className="dml-worker-collapse"/s);
  assert.match(
    app,
    /canPause\s*=\s*\["新建",\s*"连接实例",\s*"准备基表",\s*"执行 SQL",\s*"恢复检测"\]\.includes\(task\.status\)/
  );
  assert.doesNotMatch(app, /复制延迟|replication_lag/i);
  assert.match(styles, /\.route-summary/);
  assert.match(styles, /\.generator-identities/);
  assert.match(styles, /\.crud-summary/);
});

test("页面在桌面、平板和手机宽度下使用响应式栅格且不强制 1180 像素", () => {
  const app = source("App.tsx");
  const styles = source("styles.css");

  assert.doesNotMatch(styles, /body\s*\{[^}]*min-width:\s*1180px/s);
  assert.match(app, /<Sider[^>]*breakpoint="lg"[^>]*collapsedWidth=\{0\}/s);
  assert.match(app, /<Col[^>]*xs=\{24\}[^>]*xl=\{16\}/s);
  assert.match(app, /<Col[^>]*xs=\{24\}[^>]*xl=\{8\}/s);
  assert.match(app, /<Col[^>]*xs=\{24\}[^>]*sm=\{12\}[^>]*lg=\{6\}/s);
  assert.match(styles, /@media\s*\(max-width:\s*767px\)/);
  assert.match(styles, /\.main[^}]*min-width:\s*0/s);
});

test("任务、告警和详情卡样式使用高于 Ant Design 基础卡片的特异度", () => {
  const styles = source("styles.css");

  assert.match(styles, /\.ant-card\.task-card\s*\{[^}]*background:[^}]*0\.74[^}]*\}/s);
  assert.match(styles, /\.ant-card\.task-card\.task-card-alert\s*\{[^}]*border-color:[^}]*248,\s*113,\s*113[^}]*background:[^}]*48,\s*12,\s*18[^}]*\}/s);
  assert.match(styles, /\.ant-card\.detail-card\s*\{[^}]*background:[^}]*2,\s*6,\s*23[^}]*\}/s);
});

test("旧响应使用独立 wire 类型规范化并与后端任务字段对齐", () => {
  const types = source("types.ts");
  const api = source("api.ts");

  assert.match(types, /export type RawFuzzTask\s*=/);
  assert.match(types, /\bdatabase:\s*string;/);
  assert.match(types, /\breplica_target:\s*string;/);
  assert.match(api, /as RawFuzzTask\[\]/);
  assert.match(api, /normalizeTask\(task:\s*RawFuzzTask\):\s*FuzzTask/);
});

test("CRUD 汇总明确数字顺序为成功和失败", () => {
  const app = source("App.tsx");

  assert.match(app, /CRUD 统计/);
  assert.match(app, /成功\s*\/\s*失败/);
});
