import type { CoverageItem, CreateTaskPayload, FuzzTask, JumpHost, LostConnectionEvent, RawFuzzTask, SummaryMetric, TaskLoadResult } from "./types";

export async function loadTasks(): Promise<TaskLoadResult> {
  try {
    const response = await fetch("/api/tasks");
    if (!response.ok) {
      return { backendConnected: false, tasks: [] };
    }
    const tasks = (await response.json()) as RawFuzzTask[];
    return { backendConnected: true, tasks: tasks.map(normalizeTask) };
  } catch {
    return { backendConnected: false, tasks: [] };
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
  const task = (await response.json()) as RawFuzzTask;
  return normalizeTask(task);
}

export async function pauseTask(taskId: string): Promise<void> {
  await postTaskAction(taskId, "pause", "暂停任务失败");
}

export async function resumeTask(taskId: string): Promise<void> {
  await postTaskAction(taskId, "resume", "恢复任务失败");
}

export async function stopTask(taskId: string): Promise<void> {
  await postTaskAction(taskId, "stop", "停止任务失败");
}

async function postTaskAction(taskId: string, action: string, errorMessage: string): Promise<void> {
  const response = await fetch(`/api/tasks/${taskId}/${action}`, { method: "POST" });
  if (!response.ok) {
    throw new Error(errorMessage);
  }
}

export function normalizeTask(task: RawFuzzTask): FuzzTask {
  return {
    ...task,
    database: task.database ?? "test",
    phase: task.phase ?? task.status,
    thread_count: task.thread_count ?? 1,
    primary_target: task.primary_target ?? task.target,
    replica_target: task.replica_target ?? (
      task.replica_host
        ? `${task.replica_host}:${task.replica_port ?? "继承主端口"}`
        : task.target
    ),
    replica_host: task.replica_host ?? null,
    replica_port: task.replica_port ?? null,
    enable_crud: task.enable_crud ?? false,
    query_seed: task.query_seed ?? null,
    query_generator_version: task.query_generator_version ?? null,
    crud_seed: task.crud_seed ?? null,
    crud_generator_version: task.crud_generator_version ?? null,
    query_worker_total: task.query_worker_total ?? task.thread_count ?? 1,
    crud_worker_total: task.crud_worker_total ?? 0,
    worker_total: task.worker_total ?? (
      (task.query_worker_total ?? task.thread_count ?? 1) + (task.crud_worker_total ?? 0)
    ),
    expand_base_table_columns: task.expand_base_table_columns ?? false,
    base_table_seed: task.base_table_seed ?? null,
    base_table_generator_version: task.base_table_generator_version ?? null,
    sql_total: task.sql_total ?? 0,
    success_query_total: task.success_query_total ?? task.sql_total ?? 0,
    failed_query_total: task.failed_query_total ?? 0,
    ordinary_error_total: task.ordinary_error_total ?? 0,
    insert_success_total: task.insert_success_total ?? 0,
    insert_failed_total: task.insert_failed_total ?? 0,
    update_success_total: task.update_success_total ?? 0,
    update_failed_total: task.update_failed_total ?? 0,
    delete_success_total: task.delete_success_total ?? 0,
    delete_failed_total: task.delete_failed_total ?? 0,
    crud_success_total: task.crud_success_total ?? 0,
    crud_failed_total: task.crud_failed_total ?? 0,
    primary_reconnect_total: task.primary_reconnect_total ?? 0,
    replica_reconnect_total: task.replica_reconnect_total ?? 0,
    primary_reconnecting: task.primary_reconnecting ?? 0,
    replica_reconnecting: task.replica_reconnecting ?? 0,
    lost_connection_total: task.lost_connection_total ?? 0,
    sql_rate: task.sql_rate ?? 0,
    worker_states: task.worker_states ?? [],
    events: task.events ?? []
  };
}

export function summarize(tasks: FuzzTask[]): SummaryMetric {
  return {
    activeTasks: tasks.filter((task) => task.status !== "已停止" && task.status !== "失败").length,
    sqlTotal: tasks.reduce((total, task) => total + task.success_query_total, 0),
    lostConnection: tasks.reduce((total, task) => total + task.lost_connection_total, 0),
    clusterRate: tasks.reduce((total, task) => total + task.sql_rate, 0)
  };
}
