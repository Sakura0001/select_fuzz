import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {api, type ReplayJob, type RunView} from "../api/client";
import type * as ClientModule from "../api/client";
import {FindingDetailPage} from "./FindingDetailPage";
import {FindingsPage} from "./FindingsPage";
import {OverviewPage} from "./OverviewPage";
import {ReplayPage} from "./ReplayPage";
import {ReportsPage} from "./ReportsPage";
import {RunDetailPage} from "./RunDetailPage";
import {RunsPage} from "./RunsPage";

vi.mock("../api/client", async (loadOriginal) => {
  const original = await loadOriginal<typeof ClientModule>();
  return {api: {...original.api, stopRun: vi.fn(), createReplay: vi.fn()}};
});

const run: RunView = {
  id: "run-1", state: "running", created_at: "2026-01-01", updated_at: "2026-01-01", version: 3,
  request: {mode: "correctness", seed: 7, workers: 10, rounds: null, queries_per_round: 1000, timeout_seconds: 15, degradation_ratio: 0.2, data_rows_min: 10, data_rows_max: 500},
};

describe("read-only pages", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders overview topology, metrics, chart table, and empty alternatives", () => {
    const {rerender} = render(<OverviewPage runs={[run]} connection="connected" snapshot={{sequence: 2, runs: [run], nodes: [{role: "baseline", status: "healthy"}]}}/>);
    expect(screen.getByText("baseline")).toBeInTheDocument();
    expect(screen.getByRole("img", {name: /Persisted run versions chart/})).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("3");
    rerender(<OverviewPage runs={[]} connection="offline" snapshot={null}/>);
    expect(screen.getByText("No node health snapshot is available.")).toBeInTheDocument();
    expect(screen.getByText("No measured run series yet.")).toBeInTheDocument();
  });

  it("renders empty and populated history, reports, findings and replay states", () => {
    const {rerender} = render(<RunsPage runs={[]}/>);
    expect(screen.getByText("No runs yet.")).toBeInTheDocument();
    rerender(<RunsPage runs={[run]}/>);
    expect(screen.getByRole("link", {name: "run-1"})).toHaveAttribute("href", "/runs/run-1");
    rerender(<ReportsPage items={[]}/>);
    expect(screen.getByText("No reports yet.")).toBeInTheDocument();
    rerender(<ReportsPage items={[{id: "report-1", filename: "report.html"}]}/>);
    expect(screen.getByRole("link", {name: "report.html"})).toHaveAttribute("href", "/api/v1/artifacts/report-1");
    rerender(<FindingsPage items={[{id: "finding-1"}]}/>);
    expect(screen.getByLabelText("Query text")).toBeInTheDocument();
    const queued: ReplayJob = {id: "replay-1", case_id: "case-1", state: "queued", result: null};
    rerender(<ReplayPage job={queued}/>);
    expect(screen.getByText(/queued or running/)).toBeInTheDocument();
    rerender(<ReplayPage job={{...queued, state: "reproduced", result: {matched: true}}}/>);
    expect(screen.getByText(/matched/)).toBeInTheDocument();
  });

  it("stops an active run and disables stop for a terminal run", async () => {
    vi.mocked(api.stopRun).mockResolvedValue({...run, state: "stopped"});
    const stopped = vi.fn();
    const {rerender} = render(<RunDetailPage run={run} onStopped={stopped}/>);
    await userEvent.click(screen.getByRole("button", {name: "Stop run"}));
    expect(api.stopRun).toHaveBeenCalledWith("run-1");
    expect(stopped).toHaveBeenCalledWith(expect.objectContaining({state: "stopped"}));
    rerender(<RunDetailPage run={{...run, state: "completed"}} onStopped={stopped}/>);
    expect(screen.getByRole("button", {name: "Stop run"})).toBeDisabled();
  });

  it("renders complete finding evidence and handles replay failure", async () => {
    vi.mocked(api.createReplay).mockRejectedValue(new Error("replay unavailable"));
    render(<FindingDetailPage finding={{id: "finding-1", reproduction: {query_sql: "SELECT 1 ORDER BY 1"}, nodes: {baseline: {rows: 1}}}}/>);
    expect(screen.getByText("SELECT 1 ORDER BY 1")).toBeInTheDocument();
    expect(screen.getByLabelText("baseline node")).toHaveTextContent("rows");
    await userEvent.click(screen.getByRole("button", {name: "Replay"}));
    expect(screen.getByRole("alert")).toHaveTextContent("replay unavailable");
  });
});
