import type { CoverageItem, CreateTaskPayload, FuzzTask, JumpHost, LostConnectionEvent, SummaryMetric } from "./types";

const fallbackTasks: FuzzTask[] = [
  {
    task_id: "task-1042",
    node_name: "polardb-node-a",
    target: "10.23.8.41:3306",
    status: "执行 SQL",
    jump_host: null,
    thread_count: 8,
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
    thread_count: 16,
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
        sql: "WITH cte AS (...) SELECT DISTANCE(vector_col, STRING_TO_VECTOR('[0.1,0.2,0.3,0.4]'), 'COSINE') ...",
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
    thread_count: 4,
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
    return Promise.all(
      tasks.map(async (task) => ({
        ...task,
        thread_count: task.thread_count ?? 1,
        sql_rate: task.sql_rate ?? 0,
        events: await loadLostConnections(task.task_id)
      }))
    );
  } catch {
    return fallbackTasks;
  }
}

export async function loadLostConnections(taskId: string): Promise<LostConnectionEvent[]> {
  try {
    const response = await fetch(`/api/tasks/${taskId}/lost-connections`);
    if (!response.ok) {
      return [];
    }
    return (await response.json()) as LostConnectionEvent[];
  } catch {
    return [];
  }
}

export async function loadCoverage(): Promise<CoverageItem[]> {
  try {
    const response = await fetch("/api/coverage");
    if (!response.ok) {
      return [];
    }
    return (await response.json()) as CoverageItem[];
  } catch {
    return [];
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
  const response = await fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    throw new Error("后端创建任务失败");
  }
  const task = (await response.json()) as FuzzTask;
  return { ...task, thread_count: task.thread_count ?? 1, sql_rate: task.sql_rate ?? 0, events: task.events ?? [] };
}

export function summarize(tasks: FuzzTask[]): SummaryMetric {
  return {
    activeTasks: tasks.filter((task) => task.status !== "已停止").length,
    sqlTotal: tasks.reduce((total, task) => total + task.sql_total, 0),
    lostConnection: tasks.reduce((total, task) => total + task.lost_connection_total, 0),
    clusterRate: tasks.reduce((total, task) => total + task.sql_rate, 0)
  };
}
