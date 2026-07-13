import {act, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {EventEnvelope, RunView, Snapshot} from "../api/client";
import type * as ClientModule from "../api/client";
import {api} from "../api/client";
import {App} from "./App";

const stream = vi.hoisted(() => ({
  connection: undefined as undefined | ((state: "connected" | "reconnecting" | "offline") => void),
  apply: undefined as undefined | ((event: EventEnvelope) => void),
  recover: undefined as undefined | ((snapshot: Snapshot) => void),
}));

vi.mock("../api/client", async (loadOriginal) => {
  const original = await loadOriginal<typeof ClientModule>();
  return {api: Object.fromEntries(Object.keys(original.api).map((key) => [key, vi.fn()]))};
});

vi.mock("../api/eventStream", () => ({
  SequenceStore: class {
    constructor(_sequence: number, apply: (event: EventEnvelope) => void, _snapshot: () => Promise<Snapshot>, recover: (snapshot: Snapshot) => void) {
      stream.apply = apply; stream.recover = recover;
    }
  },
  connectEvents: vi.fn((_store, connection) => {
    stream.connection = connection;
    connection("connected");
    return vi.fn();
  }),
}));

const run: RunView = {
  id: "run-1", state: "running", created_at: "2026-01-01", updated_at: "2026-01-01", version: 1,
  request: {mode: "correctness", seed: 1, workers: 10, rounds: 1, queries_per_round: 10, timeout_seconds: 15, degradation_ratio: 0.2, data_rows_min: 10, data_rows_max: 500},
};

function baseSuccess() {
  vi.mocked(api.runs).mockResolvedValue({items: [run]});
  vi.mocked(api.findings).mockResolvedValue({items: [{id: "finding-1"}]});
  vi.mocked(api.reports).mockResolvedValue({items: [{id: "report-1", filename: "report.html"}]});
  vi.mocked(api.snapshot).mockResolvedValue({sequence: 1, runs: [run], nodes: [{role: "baseline", status: "healthy"}]});
  vi.mocked(api.finding).mockResolvedValue({id: "finding-1", reproduction: {query_sql: "SELECT 1"}, nodes: {}});
  vi.mocked(api.replayJob).mockResolvedValue({id: "replay-1", case_id: "case-1", state: "reproduced", result: {matched: true}});
}

describe("App fault and route matrix", () => {
  beforeEach(() => {vi.clearAllMocks(); sessionStorage.clear(); baseSuccess(); history.pushState({}, "", "/");});

  it("loads independent resources, reacts to stream state and applies stream updates", async () => {
    render(<App/>);
    expect(await screen.findByRole("heading", {name: "Control room"})).toBeInTheDocument();
    expect(screen.getByText("baseline")).toBeInTheDocument();
    act(() => stream.connection?.("reconnecting"));
    expect(screen.getByTestId("panel-stale")).toHaveTextContent("Showing saved data");
    act(() => stream.connection?.("connected"));
    expect(screen.getByTestId("panel-data")).toBeInTheDocument();
    act(() => stream.apply?.({sequence: 2, kind: "run.state", payload: {}}));
    act(() => stream.apply?.({sequence: 3, kind: "finding.created", payload: {}}));
    act(() => stream.recover?.({sequence: 4, runs: [{...run, state: "completed"}]}));
    await waitFor(() => expect(api.runs).toHaveBeenCalledTimes(2));
    expect(api.findings).toHaveBeenCalledTimes(2);
  });

  it("isolates a partial base failure while rendering finding detail", async () => {
    history.pushState({}, "", "/findings/finding-1");
    vi.mocked(api.reports).mockRejectedValue(new Error("offline"));
    render(<App/>);
    expect(await screen.findByRole("heading", {name: "finding-1"})).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Reports unavailable");
  });

  it("shows the retryable error panel when all base resources fail", async () => {
    history.pushState({}, "", "/runs");
    vi.mocked(api.runs).mockRejectedValue(new Error("offline"));
    vi.mocked(api.findings).mockRejectedValue(new Error("offline"));
    vi.mocked(api.reports).mockRejectedValue(new Error("offline"));
    vi.mocked(api.snapshot).mockRejectedValue(new Error("offline"));
    render(<App/>);
    expect(await screen.findByTestId("panel-error")).toBeInTheDocument();
    baseSuccess();
    await userEvent.click(screen.getByRole("button", {name: "Retry"}));
    expect(await screen.findByRole("heading", {name: "Run history"})).toBeInTheDocument();
  });

  it.each([
    ["/runs/run-1", "run-1"],
    ["/findings", "Findings"],
    ["/reports", "Reports"],
    ["/replays/replay-1", "Replay case-1"],
    ["/unknown", "Page not found"],
  ])("renders route %s", async (path, text) => {
    history.pushState({}, "", path);
    render(<App/>);
    expect(await screen.findByText(text, {exact: true})).toBeInTheDocument();
  });
});
