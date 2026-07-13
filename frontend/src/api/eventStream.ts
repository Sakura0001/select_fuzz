import type {EventEnvelope, Snapshot} from "./client";

export class SequenceStore {
  constructor(
    public sequence: number,
    private readonly apply: (event: EventEnvelope) => void,
    private readonly snapshot: () => Promise<Snapshot>,
    private readonly applySnapshot: (snapshot: Snapshot) => void = () => undefined,
  ) {}

  async accept(event: EventEnvelope): Promise<"applied" | "duplicate" | "recovered"> {
    if (event.sequence <= this.sequence) return "duplicate";
    if (event.sequence !== this.sequence + 1) {
      const recovered = await this.snapshot();
      this.applySnapshot(recovered);
      this.sequence = recovered.sequence;
      sessionStorage.setItem("select-fuzz-sequence", String(this.sequence));
      return "recovered";
    }
    this.apply(event);
    this.sequence = event.sequence;
    sessionStorage.setItem("select-fuzz-sequence", String(this.sequence));
    return "applied";
  }
}

export function connectEvents(
  store: SequenceStore,
  onConnection: (state: "connected" | "reconnecting" | "offline") => void,
): () => void {
  let closed = false;
  let source: EventSource | undefined;
  const connect = () => {
    if (closed) return;
    onConnection("reconnecting");
    source = new EventSource(`/api/v1/events?after=${store.sequence}`);
    source.onopen = () => onConnection("connected");
    const accept = (message: MessageEvent<string>) => {
      void store.accept({sequence: Number(message.lastEventId), kind: message.type, payload: JSON.parse(message.data)}).then((result) => {
        if (result === "recovered") {source?.close(); connect();}
      });
    };
    source.onmessage = accept;
    for (const kind of ["run.state", "finding.created", "run.metric", "run.finished"]) {
      source.addEventListener(kind, accept as EventListener);
    }
    source.onerror = () => {source?.close(); onConnection(navigator.onLine ? "reconnecting" : "offline"); setTimeout(connect, 1500);};
  };
  connect();
  return () => {closed = true; source?.close();};
}
