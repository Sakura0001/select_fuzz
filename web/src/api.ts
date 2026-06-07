import type { CoverageItem, CreateTaskPayload, FuzzTask, JumpHost, LostConnectionEvent, SummaryMetric, TaskLoadResult } from "./types";

export async function loadTasks(): Promise<TaskLoadResult> {
  try {
    const response = await fetch("/api/tasks");
    if (!response.ok) {
      return { backendConnected: false, tasks: [] };
    }
    const tasks = (await response.json()) as FuzzTask[];
    const rows = await Promise.all(
      tasks.map(async (task) => ({
        ...task,
        phase: task.phase ?? task.status,
        thread_count: task.thread_count ?? 1,
        sql_rate: task.sql_rate ?? 0,
        worker_states: task.worker_states ?? [],
        events: await loadLostConnections(task.task_id)
      }))
    );
    return { backendConnected: true, tasks: rows };
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
  const task = (await response.json()) as FuzzTask;
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

function normalizeTask(task: FuzzTask): FuzzTask {
  return {
    ...task,
    phase: task.phase ?? task.status,
    thread_count: task.thread_count ?? 1,
    sql_rate: task.sql_rate ?? 0,
    worker_states: task.worker_states ?? [],
    events: task.events ?? []
  };
}

export function summarize(tasks: FuzzTask[]): SummaryMetric {
  return {
    activeTasks: tasks.filter((task) => task.status !== "已停止" && task.status !== "失败").length,
    sqlTotal: tasks.reduce((total, task) => total + task.sql_total, 0),
    lostConnection: tasks.reduce((total, task) => total + task.lost_connection_total, 0),
    clusterRate: tasks.reduce((total, task) => total + task.sql_rate, 0)
  };
}
