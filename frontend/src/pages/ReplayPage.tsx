import type {ReplayJob} from "../api/client";

export function ReplayPage({job}: {job: ReplayJob}) {
  return <main data-testid={`replay-${job.state}`}><h1>Replay {job.case_id}</h1>
    <p className={`state ${job.state}`}>{job.state}</p>
    {job.result ? <><h2>Replay result</h2><pre>{JSON.stringify(job.result, null, 2)}</pre></> : <p>The three-node replay is queued or running.</p>}
  </main>;
}
