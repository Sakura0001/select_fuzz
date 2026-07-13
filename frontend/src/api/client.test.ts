import {beforeEach, describe, expect, it, vi} from "vitest";
import {api} from "./client";

describe("API client", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({items: []}), {
      status: 200,
      headers: {"Content-Type": "application/json"},
    })));
  });

  it("builds every endpoint with encoded identifiers and JSON writes", async () => {
    await api.health(); await api.runs(); await api.run("run/1");
    await api.createRun({mode: "performance"}); await api.stopRun("run/1");
    await api.snapshot(); await api.findings(); await api.finding("finding/1");
    await api.createReplay("case/1"); await api.replayJob("replay/1"); await api.reports();
    const calls = vi.mocked(fetch).mock.calls;
    expect(calls.map(([path]) => path)).toEqual([
      "/api/v1/health", "/api/v1/runs", "/api/v1/runs/run%2F1", "/api/v1/runs",
      "/api/v1/runs/run%2F1/stop", "/api/v1/snapshot", "/api/v1/findings",
      "/api/v1/findings/finding%2F1", "/api/v1/replays", "/api/v1/replays/jobs/replay%2F1",
      "/api/v1/reports",
    ]);
    expect(calls[3][1]).toEqual(expect.objectContaining({method: "POST"}));
    expect(calls[8][1]?.body).toBe(JSON.stringify({case_id: "case/1"}));
  });

  it("raises a typed RFC problem and falls back when the body is not JSON", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({type: "urn:test", detail: "bad request"}), {status: 400}));
    await expect(api.health()).rejects.toMatchObject({status: 400, type: "urn:test", message: "bad request"});
    vi.mocked(fetch).mockResolvedValueOnce(new Response("not-json", {status: 503, statusText: "Unavailable"}));
    await expect(api.health()).rejects.toMatchObject({status: 503, type: "about:blank", message: "Unavailable"});
  });
});
