export type TaskStatus = "新建" | "连接实例" | "准备基表" | "执行 SQL" | "恢复检测" | "已暂停" | "失败" | "已停止";
export type BaseTableGeneratorVersion = "v1";
export type QueryGeneratorVersion = "v1";
export type CrudGeneratorVersion = "v1";

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
  database: string;
  primary_target: string;
  replica_target: string;
  replica_host: string | null;
  replica_port: number | null;
  status: TaskStatus;
  phase: string;
  last_error?: string | null;
  jump_host?: string | null;
  thread_count: number;
  enable_crud: boolean;
  query_seed: string | null;
  query_generator_version: QueryGeneratorVersion | null;
  crud_seed: string | null;
  crud_generator_version: CrudGeneratorVersion | null;
  query_worker_total: number;
  crud_worker_total: number;
  worker_total: number;
  expand_base_table_columns: boolean;
  base_table_seed: string | null;
  base_table_generator_version: BaseTableGeneratorVersion | null;
  sql_total: number;
  success_query_total: number;
  failed_query_total: number;
  ordinary_error_total: number;
  insert_success_total: number;
  insert_failed_total: number;
  update_success_total: number;
  update_failed_total: number;
  delete_success_total: number;
  delete_failed_total: number;
  crud_success_total: number;
  crud_failed_total: number;
  primary_reconnect_total: number;
  replica_reconnect_total: number;
  primary_reconnecting: number;
  replica_reconnecting: number;
  lost_connection_total: number;
  sql_rate: number;
  events: LostConnectionEvent[];
  worker_states: WorkerState[];
}

type LegacyTaskIdentity = "task_id" | "node_name" | "target" | "status";

export type RawFuzzTask = Pick<FuzzTask, LegacyTaskIdentity> &
  Partial<Omit<FuzzTask, LegacyTaskIdentity | "replica_target">> & {
    replica_target?: string | null;
  };

export interface TaskLoadResult {
  backendConnected: boolean;
  tasks: FuzzTask[];
}

export interface JumpHost {
  name: string;
  host: string;
  port: number;
  username: string;
  password?: string | null;
  private_key_path?: string | null;
}

export interface WorkerState {
  worker_id: number;
  worker_key?: string | null;
  worker_type?: "query" | "dml" | string | null;
  db_role?: "primary" | "replica" | string | null;
  target?: string | null;
  table_name?: string | null;
  operation?: "insert" | "update" | "delete" | string | null;
  generator_seed?: string | null;
  generator_version?: string | null;
  state: string;
  last_heartbeat?: string | null;
  current_sql?: string | null;
  current_sql_started_at?: string | null;
  last_error?: string | null;
  sql_total: number;
  stalled_total: number;
  needs_reconnect?: boolean | null;
  reconnecting?: boolean | null;
  reconnect_total?: number | null;
  next_retry_at?: string | null;
  thread_alive?: boolean | null;
  thread_name?: string | null;
  connection_open?: boolean | null;
  connection_id?: number | null;
  connection_connect_count?: number | null;
  connection_close_count?: number | null;
  connection_ping_reconnect_count?: number | null;
  last_connection_close_reason?: string | null;
}

export interface CreateTaskPayload {
  node_name: string;
  host: string;
  port: number;
  username: string;
  password: string;
  jump_host?: string | null;
  replica_host?: string | null;
  replica_port?: number | null;
  thread_count: number;
  enable_crud: boolean;
  expand_base_table_columns: boolean;
  base_table_seed: string | null;
  base_table_generator_version: BaseTableGeneratorVersion | null;
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
