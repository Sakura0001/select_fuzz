import type {ReactNode} from "react";

export type PanelState = "loading" | "empty" | "data" | "stale" | "error";

export function AsyncPanel({state, onRetry, children}: {state: PanelState; onRetry: () => void; children?: ReactNode}) {
  if (state === "loading") return <section className="panel" data-testid="panel-loading" aria-busy="true">Loading…</section>;
  if (state === "empty") return <section className="panel" data-testid="panel-empty">No data yet.</section>;
  if (state === "error") return <section className="panel error" data-testid="panel-error" role="alert">Unable to load. <button onClick={onRetry}>Retry</button></section>;
  return <section className="panel" data-testid={`panel-${state}`} aria-live="polite">
    {state === "stale" && <p className="notice">Showing saved data while reconnecting.</p>}{children}
  </section>;
}
