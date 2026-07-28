import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {api} from "../api/client";
import type * as ClientModule from "../api/client";
import {NewRunPage} from "./NewRunPage";

vi.mock("../api/client", async (loadOriginal) => {
  const original = await loadOriginal<typeof ClientModule>();
  return {api: {...original.api, createRun: vi.fn()}};
});

describe("NewRunPage", () => {
  beforeEach(() => vi.clearAllMocks());
  it("forces one worker for performance mode", async () => {
    render(<NewRunPage onCreated={() => undefined}/>);
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "performance");
    expect(screen.getByLabelText("Workers")).toHaveValue(1);
    expect(screen.getByLabelText("Workers")).toBeDisabled();
    expect(screen.getByLabelText("Queries per round")).toHaveValue(100);
    expect(screen.getByLabelText("Timeout seconds")).toHaveValue(15);
    expect(screen.getByLabelText("Minimum rows")).toHaveValue(100000);
    expect(screen.getByLabelText("Maximum rows")).toHaveValue(50000000);
    expect(screen.getByLabelText("Rounds")).toBeInTheDocument();
    expect(screen.getByLabelText("Degradation threshold")).toBeInTheDocument();
    expect(screen.getByLabelText("Minimum rows")).toBeInTheDocument();
    expect(screen.getByLabelText("Maximum rows")).toBeInTheDocument();
  });

  it("submits correctness fields and reports an API failure", async () => {
    vi.mocked(api.createRun).mockRejectedValue(new Error("node unavailable"));
    render(<NewRunPage onCreated={vi.fn()}/>);
    await userEvent.clear(screen.getByLabelText("Seed"));
    await userEvent.type(screen.getByLabelText("Seed"), "42");
    await userEvent.click(screen.getByRole("button", {name: "Start run"}));
    expect(api.createRun).toHaveBeenCalledWith(expect.objectContaining({mode: "correctness", seed: 42, workers: 10}));
    expect(await screen.findByRole("alert")).toHaveTextContent("node unavailable");
  });

  it("submits performance defaults and switches back to correctness defaults", async () => {
    const created = vi.fn();
    vi.mocked(api.createRun).mockResolvedValue({
      id: "run-1", state: "queued", created_at: "", updated_at: "", version: 1,
      request: {mode: "performance", seed: 0, workers: 1, rounds: 1, queries_per_round: 100, timeout_seconds: 15, degradation_ratio: 0.2, data_rows_min: 100000, data_rows_max: 50000000},
    });
    render(<NewRunPage onCreated={created}/>);
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "performance");
    await userEvent.click(screen.getByRole("button", {name: "Start run"}));
    expect(created).toHaveBeenCalledWith(expect.objectContaining({id: "run-1"}));
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "correctness");
    expect(screen.getByLabelText("Workers")).toHaveValue(10);
    expect(screen.getByLabelText("Minimum rows")).toHaveValue(10);
  });

  it("exposes the 1:2 reader split for fuzz mode", async () => {
    vi.mocked(api.createRun).mockRejectedValue(new Error("stopped"));
    render(<NewRunPage onCreated={() => undefined}/>);
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "fuzz");
    expect(screen.getByLabelText("Workers")).toHaveValue(1);
    expect(screen.getByLabelText("Workers")).toBeDisabled();
    expect(screen.getByLabelText("Readers per database (1:2 primary/replica)")).toHaveValue(6);
    await userEvent.clear(screen.getByLabelText("Databases"));
    await userEvent.type(screen.getByLabelText("Databases"), "3");
    await userEvent.click(screen.getByRole("button", {name: "Start run"}));
    expect(api.createRun).toHaveBeenCalledWith(expect.objectContaining({
      mode: "fuzz", workers: 1, rounds: null, databases: 3,
      writer_threads_per_database: 2, reader_threads_per_database: 6,
    }));
  });
});
