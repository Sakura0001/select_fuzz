export type RunMode = "correctness" | "performance";
export type RunState = "queued" | "starting" | "running" | "stopping" | "recovering" | "orphaned" | "stopped" | "completed" | "failed";

export interface RunRequest {
  mode: RunMode; seed: number; workers: number; rounds: number | null;
  queries_per_round: number; timeout_seconds: number; degradation_ratio: number;
  data_rows_min: number; data_rows_max: number;
}
export interface RunView {id: string; state: RunState; request: RunRequest; created_at: string; updated_at: string; version: number}
export interface EventEnvelope {sequence: number; kind: string; payload: Record<string, unknown>}
export interface Snapshot {sequence: number; runs: RunView[]; nodes?: Array<Record<string, unknown>>; recent_findings?: Array<Record<string, unknown>>}
export interface ReplayJob {id: string; case_id: string; state: "queued" | "running" | "reproduced" | "not_reproduced" | "failed"; result: Record<string, unknown> | null}

export class ApiProblem extends Error {
  constructor(public status: number, public type: string, message: string) {super(message);}
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as {type?: string; detail?: string};
    throw new ApiProblem(response.status, body.type ?? "about:blank", body.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<{status: string}>("/health"),
  runs: () => request<{items: RunView[]}>("/runs"),
  run: (id: string) => request<RunView>(`/runs/${encodeURIComponent(id)}`),
  createRun: (body: Partial<RunRequest>) => request<RunView>("/runs", {
    method: "POST", headers: {"Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID()},
    body: JSON.stringify(body),
  }),
  stopRun: (id: string) => request<RunView>(`/runs/${encodeURIComponent(id)}/stop`, {method: "POST"}),
  snapshot: () => request<Snapshot>("/snapshot"),
  findings: () => request<{items: {id: string}[]}>("/findings"),
  finding: (id: string) => request<Record<string, unknown>>(`/findings/${encodeURIComponent(id)}`),
  createReplay: (caseId: string) => request<ReplayJob>("/replays", {
    method: "POST", headers: {"Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID()},
    body: JSON.stringify({case_id: caseId}),
  }),
  replayJob: (id: string) => request<ReplayJob>(`/replays/jobs/${encodeURIComponent(id)}`),
  reports: () => request<{items: {id: string; filename: string}[]}>("/reports"),
};
