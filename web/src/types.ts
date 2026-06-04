export type TaskStatus = "执行 SQL" | "恢复检测" | "已停止";

export interface LostConnectionEvent {
  timestamp: string;
  task_id: string;
  node_name: string;
  jump_host?: string | null;
  target: string;
  sql: string;
  window_start: string;
}

export interface FuzzTask {
  task_id: string;
  node_name: string;
  target: string;
  status: TaskStatus;
  jump_host?: string | null;
  sql_total: number;
  lost_connection_total: number;
  sql_rate: number;
  events: LostConnectionEvent[];
}

export interface SummaryMetric {
  activeTasks: number;
  sqlTotal: number;
  lostConnection: number;
  clusterRate: number;
}
