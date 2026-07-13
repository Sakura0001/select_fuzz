import {useEffect, useMemo, useState} from "react";
import {api, type ReplayJob, type RunView, type Snapshot} from "../api/client";
import {connectEvents, SequenceStore} from "../api/eventStream";
import {AsyncPanel, type PanelState} from "../components/AsyncPanel";
import {FindingDetailPage} from "../pages/FindingDetailPage";
import {FindingsPage} from "../pages/FindingsPage";
import {NewRunPage} from "../pages/NewRunPage";
import {OverviewPage} from "../pages/OverviewPage";
import {ReplayPage} from "../pages/ReplayPage";
import {ReportsPage} from "../pages/ReportsPage";
import {RunDetailPage} from "../pages/RunDetailPage";
import {RunsPage} from "../pages/RunsPage";

type Resource = Record<string, unknown> | ReplayJob | null;

export function App() {
  const [runs, setRuns] = useState<RunView[]>([]);
  const [findings, setFindings] = useState<{id: string}[]>([]);
  const [reports, setReports] = useState<{id: string; filename: string}[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [resource, setResource] = useState<Resource>(null);
  const [state, setState] = useState<PanelState>("loading");
  const [partialErrors, setPartialErrors] = useState<string[]>([]);
  const [connection, setConnection] = useState<"connected" | "reconnecting" | "offline">("reconnecting");
  const path = location.pathname;

  const reload = async () => {
    setState("loading"); setPartialErrors([]);
    const results = await Promise.allSettled([api.runs(), api.findings(), api.reports(), api.snapshot()]);
    const errors: string[] = [];
    if (results[0].status === "fulfilled") setRuns(results[0].value.items); else errors.push("Run history unavailable");
    if (results[1].status === "fulfilled") setFindings(results[1].value.items); else errors.push("Finding index unavailable");
    if (results[2].status === "fulfilled") setReports(results[2].value.items); else errors.push("Reports unavailable");
    if (results[3].status === "fulfilled") setSnapshot(results[3].value); else errors.push("Live snapshot unavailable");
    const findingMatch = path.match(/^\/findings\/([^/]+)$/);
    const replayMatch = path.match(/^\/replays\/([^/]+)$/);
    if (findingMatch) {
      try {setResource(await api.finding(findingMatch[1]));} catch {errors.push("Finding detail unavailable");}
    } else if (replayMatch) {
      try {setResource(await api.replayJob(replayMatch[1]));} catch {errors.push("Replay status unavailable");}
    }
    setPartialErrors(errors);
    setState(results.every((result) => result.status === "rejected") && path !== "/runs/new" ? "error" : "data");
  };

  useEffect(() => {void reload();}, []);
  const sequenceStore = useMemo(() => new SequenceStore(
    Number(sessionStorage.getItem("select-fuzz-sequence") ?? 0),
    (event) => {
      if (event.kind === "run.state") void api.runs().then((page) => setRuns(page.items));
      if (event.kind === "finding.created") void api.findings().then((page) => setFindings(page.items));
    },
    api.snapshot,
    (recovered) => {setSnapshot(recovered); setRuns(recovered.runs);},
  ), []);
  useEffect(() => connectEvents(sequenceStore, (next) => {
    setConnection(next);
    if (next !== "connected") setState((current) => current === "data" ? "stale" : current);
    else setState((current) => current === "stale" ? "data" : current);
  }), [sequenceStore]);

  useEffect(() => {
    if (resource && "state" in resource && ["queued", "running"].includes(String(resource.state))) {
      const timer = setInterval(() => void api.replayJob(String(resource.id)).then(setResource), 1000);
      return () => clearInterval(timer);
    }
  }, [resource]);

  let content;
  if (path === "/") content = <OverviewPage runs={runs} connection={connection} snapshot={snapshot}/>;
  else if (path === "/runs/new") content = <NewRunPage onCreated={(run) => location.assign(`/runs/${run.id}`)}/>;
  else if (path === "/runs") content = <RunsPage runs={runs}/>;
  else if (path === "/findings") content = <FindingsPage items={findings}/>;
  else if (path === "/reports") content = <ReportsPage items={reports}/>;
  else {
    const runId = path.match(/^\/runs\/([^/]+)$/)?.[1];
    const findingId = path.match(/^\/findings\/([^/]+)$/)?.[1];
    const replayId = path.match(/^\/replays\/([^/]+)$/)?.[1];
    if (runId) {const run = runs.find((item) => item.id === runId); content = run ? <RunDetailPage run={run} onStopped={(next) => setRuns((items) => items.map((item) => item.id === next.id ? next : item))}/> : <p role="alert">Run not found</p>;}
    else if (findingId && resource) content = <FindingDetailPage finding={resource as Record<string, unknown>}/>;
    else if (replayId && resource && "state" in resource) content = <ReplayPage job={resource as ReplayJob}/>;
    else content = <p role="alert">Page not found</p>;
  }
  return <><a className="skip-link" href="#content">Skip to content</a><aside><a className="brand" href="/"><b>SF</b><span>select-fuzz</span></a><nav aria-label="Primary"><a href="/">Overview</a><a href="/runs">Runs</a><a href="/runs/new">New run</a><a href="/findings">Findings</a><a href="/reports">Reports</a></nav></aside><div id="content" className="content">{partialErrors.map((error) => <p className="notice" role="status" key={error}>{error}</p>)}<AsyncPanel state={state} onRetry={() => void reload()}>{content}</AsyncPanel></div></>;
}
