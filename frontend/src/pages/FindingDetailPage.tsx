import {useState} from "react";
import {api} from "../api/client";

export function FindingDetailPage({finding}: {finding: Record<string, unknown>}) {
  const id = String(finding.id ?? "finding");
  const reproduction = (finding.reproduction ?? {}) as Record<string, unknown>;
  const nodes = (finding.nodes ?? {}) as Record<string, unknown>;
  const [error, setError] = useState("");
  const replay = async () => {
    try {const job = await api.createReplay(id); location.assign(`/replays/${job.id}`);}
    catch (reason) {setError(reason instanceof Error ? reason.message : "Replay failed");}
  };
  return <main><p><a href="/findings">← Findings</a></p><h1>{id}</h1>
    <button onClick={() => void replay()}>Replay</button>{error && <p role="alert">{error}</p>}
    <h2>Query SQL</h2><pre>{String(reproduction.query_sql ?? "Unavailable")}</pre>
    <h2>Reproduction</h2><pre>{JSON.stringify(reproduction, null, 2)}</pre>
    <h2>Three-node outcome</h2><div className="nodes">{Object.entries(nodes).map(([node, outcome]) => <section aria-label={`${node} node`} key={node}><h3>{node}</h3><pre>{JSON.stringify(outcome, null, 2)}</pre></section>)}</div>
  </main>;
}
