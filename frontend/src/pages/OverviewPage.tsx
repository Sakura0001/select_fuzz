import type {RunView, Snapshot} from "../api/client";
import {MetricChart} from "../components/MetricChart";

export function OverviewPage({runs, connection, snapshot}: {runs: RunView[]; connection: string; snapshot: Snapshot | null}) {
  const nodes = snapshot?.nodes ?? [];
  const observedProgress = runs.map((run) => run.version);
  return <main><header><div><p className="eyebrow">MySQL 8.0.41 parallel-query lab</p><h1>Control room</h1><p>Three isolated nodes, one deterministic truth trail.</p></div><span className={`status ${connection}`}>{connection}</span></header>
    <section className="metrics"><article><b>{runs.filter((run) => run.state === "running").length}</b><span>Active runs</span></article><article><b>{runs.length}</b><span>Total runs</span></article><article><b>{nodes.length}</b><span>Observed nodes</span></article></section>
    <section><h2>Node topology</h2>{nodes.length === 0 ? <p>No node health snapshot is available.</p> : <div className="nodes">{nodes.map((node, index) => <article key={String(node.role ?? index)}><i/><h3>{String(node.role ?? "node")}</h3><p>{String(node.status ?? "unknown")}</p></article>)}</div>}</section>
    {observedProgress.length > 0 ? <MetricChart label="Persisted run versions" values={observedProgress}/> : <p>No measured run series yet.</p>}
  </main>;
}
