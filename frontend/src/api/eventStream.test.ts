import {afterEach, describe, expect, it, vi} from "vitest";
import {connectEvents, SequenceStore} from "./eventStream";

describe("SequenceStore", () => {
  it("suppresses duplicates and recovers a gap from snapshot", async () => {
    const apply = vi.fn();
    const snapshot = vi.fn(async () => ({sequence: 7, runs: []}));
    const applySnapshot = vi.fn();
    const store = new SequenceStore(4, apply, snapshot, applySnapshot);
    await store.accept({sequence: 4, kind: "duplicate", payload: {}});
    await store.accept({sequence: 6, kind: "gap", payload: {}});
    expect(apply).not.toHaveBeenCalled();
    expect(snapshot).toHaveBeenCalledOnce();
    expect(applySnapshot).toHaveBeenCalledWith({sequence: 7, runs: []});
    expect(store.sequence).toBe(7);
  });

  it("applies the next event and persists its sequence", async () => {
    const apply = vi.fn();
    const store = new SequenceStore(1, apply, vi.fn());
    await expect(store.accept({sequence: 2, kind: "run.state", payload: {}})).resolves.toBe("applied");
    expect(apply).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem("select-fuzz-sequence")).toBe("2");
  });

  it("reconnects after stream errors and closes cleanly", async () => {
    vi.useFakeTimers();
    class FakeEventSource {
      static instances: FakeEventSource[] = [];
      onopen: (() => void) | null = null;
      onmessage: ((message: MessageEvent<string>) => void) | null = null;
      onerror: (() => void) | null = null;
      listeners = new Map<string, EventListener>();
      close = vi.fn();
      constructor(readonly url: string) {FakeEventSource.instances.push(this);}
      addEventListener(kind: string, listener: EventListener) {this.listeners.set(kind, listener);}
    }
    vi.stubGlobal("EventSource", FakeEventSource);
    const states: string[] = [];
    const apply = vi.fn();
    const store = new SequenceStore(0, apply, vi.fn(async () => ({sequence: 0, runs: []})));
    const disconnect = connectEvents(store, (state) => states.push(state));
    const first = FakeEventSource.instances[0];
    expect(first.url).toContain("after=0"); first.onopen?.();
    first.listeners.get("run.state")?.({lastEventId: "1", data: "{}", type: "run.state"} as unknown as Event);
    await vi.waitFor(() => expect(apply).toHaveBeenCalledOnce());
    Object.defineProperty(navigator, "onLine", {configurable: true, value: false});
    first.onerror?.();
    expect(states).toContain("offline");
    await vi.advanceTimersByTimeAsync(1500);
    expect(FakeEventSource.instances).toHaveLength(2);
    disconnect();
    expect(FakeEventSource.instances[1].close).toHaveBeenCalledOnce();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
});
