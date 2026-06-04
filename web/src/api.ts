import type { CreateTaskPayload, FuzzTask, JumpHost, SummaryMetric } from "./types";

const fallbackTasks: FuzzTask[] = [
  {
    task_id: "task-1042",
    node_name: "polardb-node-a",
    target: "10.23.8.41:3306",
    status: "执行 SQL",
    jump_host: null,
    sql_total: 1288421,
    lost_connection_total: 0,
    sql_rate: 122,
    events: []
  },
  {
    task_id: "task-1043",
    node_name: "polardb-node-b",
    target: "172.18.4.10:3306",
    status: "恢复检测",
    jump_host: "jump-prod",
    sql_total: 914206,
    lost_connection_total: 3,
    sql_rate: 0,
    events: [
      {
        timestamp: "2026-06-04 10:42:11",
        task_id: "task-1043",
        node_name: "polardb-node-b",
        jump_host: "jump-prod",
        target: "172.18.4.10:3306",
        sql: "SELECT /* node-b */ ... FROM partition_l2 p JOIN fk_child c ...",
        window_start: "2026-06-04 10:42:11"
      },
      {
        timestamp: "2026-06-04 10:21:03",
        task_id: "task-1043",
        node_name: "polardb-node-b",
        jump_host: "jump-prod",
        target: "172.18.4.10:3306",
        sql: "WITH cte AS (...) SELECT DISTANCE(vec_col, '[0.1,0.2]', 'COSINE') ...",
        window_start: "2026-06-04 10:21:03"
      },
      {
        timestamp: "2026-06-04 09:58:16",
        task_id: "task-1043",
        node_name: "polardb-node-b",
        jump_host: "jump-prod",
        target: "172.18.4.10:3306",
        sql: "SELECT JSON_EXTRACT(j, '$.a'), BIT_COUNT(flags) FROM all_types_base ...",
        window_start: "2026-06-04 09:58:16"
      }
    ]
  },
  {
    task_id: "task-1044",
    node_name: "polardb-node-c",
    target: "172.18.4.12:3306",
    status: "执行 SQL",
    jump_host: "jump-prod",
    sql_total: 712884,
    lost_connection_total: 1,
    sql_rate: 87,
    events: [
      {
        timestamp: "2026-06-04 08:11:19",
        task_id: "task-1044",
        node_name: "polardb-node-c",
        jump_host: "jump-prod",
        target: "172.18.4.12:3306",
        sql: "SELECT ST_AsText(geo), CAST(blob_col AS CHAR) FROM all_types_base ...",
        window_start: "2026-06-04 08:11:19"
      }
    ]
  }
];

export async function loadTasks(): Promise<FuzzTask[]> {
  try {
    const response = await fetch("/api/tasks");
    if (!response.ok) {
      return fallbackTasks;
    }
    const tasks = (await response.json()) as FuzzTask[];
    return tasks.length > 0 ? tasks.map((task) => ({ ...task, sql_rate: task.sql_rate ?? 0, events: task.events ?? [] })) : fallbackTasks;
  } catch {
    return fallbackTasks;
  }
}

export async function loadJumpHosts(): Promise<JumpHost[]> {
  try {
    const response = await fetch("/api/jump-hosts");
    if (!response.ok) {
      return [];
    }
    return (await response.json()) as JumpHost[];
  } catch {
    return [];
  }
}

export async function addJumpHost(payload: JumpHost): Promise<JumpHost> {
  const response = await fetch("/api/jump-hosts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    throw new Error("保存跳板机失败");
  }
  return (await response.json()) as JumpHost;
}

export async function createTask(payload: CreateTaskPayload): Promise<FuzzTask> {
  try {
    const response = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (!response.ok) {
      throw new Error("后端创建任务失败");
    }
    const task = (await response.json()) as FuzzTask;
    return { ...task, sql_rate: task.sql_rate ?? 0, events: task.events ?? [] };
  } catch {
    return {
      task_id: `local-${Date.now()}`,
      node_name: payload.node_name,
      target: `${payload.host}:${payload.port}`,
      status: "执行 SQL",
      jump_host: payload.jump_host ?? null,
      sql_total: 0,
      lost_connection_total: 0,
      sql_rate: 0,
      events: []
    };
  }
}

export function summarize(tasks: FuzzTask[]): SummaryMetric {
  return {
    activeTasks: tasks.filter((task) => task.status !== "已停止").length,
    sqlTotal: tasks.reduce((total, task) => total + task.sql_total, 0),
    lostConnection: tasks.reduce((total, task) => total + task.lost_connection_total, 0),
    clusterRate: tasks.reduce((total, task) => total + task.sql_rate, 0)
  };
}
