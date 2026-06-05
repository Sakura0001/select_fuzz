export type TaskStatus = "新建" | "连接实例" | "准备基表" | "执行 SQL" | "恢复检测" | "已停止";

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
  thread_count: number;
  sql_total: number;
  lost_connection_total: number;
  sql_rate: number;
  events: LostConnectionEvent[];
}

export interface JumpHost {
  name: string;
  host: string;
  port: number;
  username: string;
  private_key_path?: string | null;
}

export interface CreateTaskPayload {
  node_name: string;
  host: string;
  port: number;
  username: string;
  password: string;
  jump_host?: string | null;
  thread_count: number;
}

export interface SummaryMetric {
  activeTasks: number;
  sqlTotal: number;
  lostConnection: number;
  clusterRate: number;
}

export interface CoverageItem {
  name: string;
  category: string;
  implemented: boolean;
  hit_count: number;
  recent: boolean;
}
