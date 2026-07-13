import {useState, type FormEvent} from "react";
import {api, type RunMode, type RunView} from "../api/client";

export function NewRunPage({onCreated}: {onCreated: (run: RunView) => void}) {
  const [mode, setMode] = useState<RunMode>("correctness");
  const [workers, setWorkers] = useState(10);
  const [seed, setSeed] = useState(0);
  const [rounds, setRounds] = useState(1);
  const [queriesPerRound, setQueriesPerRound] = useState(1000);
  const [timeoutSeconds, setTimeoutSeconds] = useState(15);
  const [degradationRatio, setDegradationRatio] = useState(0.2);
  const [minimumRows, setMinimumRows] = useState(10);
  const [maximumRows, setMaximumRows] = useState(500);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    try {onCreated(await api.createRun({
      mode, workers: mode === "performance" ? 1 : workers, seed, rounds,
      queries_per_round: queriesPerRound, timeout_seconds: timeoutSeconds,
      degradation_ratio: degradationRatio, data_rows_min: minimumRows,
      data_rows_max: maximumRows,
    }));}
    catch (reason) {setError(reason instanceof Error ? reason.message : "Unable to start run");}
  };
  return <main><h1>New run</h1><form onSubmit={(event) => void submit(event)}>
    <label>Mode<select value={mode} onChange={(event) => {const next = event.target.value as RunMode; setMode(next); if (next === "performance") {setWorkers(1); setQueriesPerRound(100); setTimeoutSeconds(15); setMinimumRows(100000); setMaximumRows(50000000);} else {setWorkers(10); setQueriesPerRound(1000); setTimeoutSeconds(15); setMinimumRows(10); setMaximumRows(500);}}}><option value="correctness">Correctness</option><option value="performance">Performance</option></select></label>
    <label>Workers<input type="number" min="1" max="64" value={mode === "performance" ? 1 : workers} disabled={mode === "performance"} onChange={(event) => setWorkers(Number(event.target.value))}/></label>
    <label>Seed<input type="number" min="0" value={seed} onChange={(event) => setSeed(Number(event.target.value))}/></label>
    <label>Rounds<input type="number" min="1" value={rounds} onChange={(event) => setRounds(Number(event.target.value))}/></label>
    <label>Queries per round<input type="number" min="1" value={queriesPerRound} onChange={(event) => setQueriesPerRound(Number(event.target.value))}/></label>
    <label>Timeout seconds<input type="number" min="0.1" max="300" step="0.1" value={timeoutSeconds} onChange={(event) => setTimeoutSeconds(Number(event.target.value))}/></label>
    <label>Degradation threshold<input type="number" min="0.01" step="0.01" value={degradationRatio} onChange={(event) => setDegradationRatio(Number(event.target.value))}/></label>
    <fieldset><legend>Generated data range</legend>
      <label>Minimum rows<input type="number" min="1" value={minimumRows} onChange={(event) => setMinimumRows(Number(event.target.value))}/></label>
      <label>Maximum rows<input type="number" min={minimumRows} value={maximumRows} onChange={(event) => setMaximumRows(Number(event.target.value))}/></label>
    </fieldset>
    <button type="submit">Start run</button>{error && <p role="alert">{error}</p>}
  </form></main>;
}
