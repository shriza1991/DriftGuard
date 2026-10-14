import { useState, type ReactNode } from 'react';
import type { RepairAttempt, RunEvent, RunSummary, SystemMetrics, ToolRegistryEntry } from '../types';
import { DEFAULT_TASK, FIXTURES, MUTATIONS, SAMPLE_REGISTRY, type MutationId } from '../fixtures/scenarios';
import { candidateState, eventTone, mergeEvents, runOutcome, safeDisplay, safeText, type RunOutcome } from '../state/runState';
import { summaryOutcome } from '../state/runState';
import type { StreamStatus } from '../services/eventStream';
import { currentRunPatch, patchKey } from '../state/patchState';
import { PatchInspector, RequestDiff, ReuseEvidence } from './PatchInspector';

export type DataSource = 'sample' | 'live';
export type ExecutionScope = 'baseline' | 'healing' | 'comparison' | null;

const outcomeLabels: Record<RunOutcome, string> = {
  idle: 'Not run', running: 'In progress', verified: 'Verified task completion',
  completed_unverified: 'Completed · verification unavailable', failed: 'Failed', safe_refusal: 'Safe refusal',
};

function Pill({ tone, children }: { tone: 'blue' | 'amber' | 'green' | 'red' | 'muted'; children: ReactNode }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

export function SampleLabel({ source }: { source: DataSource }) {
  return source === 'sample'
    ? <span className="sample-label">Sample data — backend not connected</span>
    : <span className="live-label">Live backend · connection unverified</span>;
}

function JsonDetails({ label, value }: { label: string; value: unknown }) {
  return <details className="json-details"><summary>{label}</summary><pre>{JSON.stringify(safeDisplay(value), null, 2)}</pre></details>;
}

function runTone(outcome: RunOutcome): 'blue' | 'amber' | 'green' | 'red' | 'muted' {
  if (outcome === 'verified') return 'green';
  if (outcome === 'failed') return 'red';
  if (outcome === 'safe_refusal' || outcome === 'completed_unverified') return 'amber';
  if (outcome === 'running') return 'blue';
  return 'muted';
}

function RunCard({ side, events, selected, source, summary }: { side: 'baseline' | 'healing'; events: RunEvent[]; selected: boolean; source: DataSource; summary: RunSummary | null }) {
  const outcome = source === 'live' ? summaryOutcome(summary, events) : runOutcome(events);
  const latest = events[events.length - 1];
  const isNormal = source === 'live' && summary?.execution_type === 'deterministic';
  return <article className={`run-card ${side}`}>
    <div className="run-card-top"><div><span className="eyebrow">{isNormal ? '01 / deterministic workflow' : side === 'baseline' ? '01 / fixed contract' : '02 / adaptive contract'}</span><h3>{isNormal ? 'Normal workflow' : side === 'baseline' ? 'Baseline' : 'DriftGuard healing'}</h3></div><Pill tone={runTone(outcome)}>{outcomeLabels[outcome]}</Pill></div>
    <p className="run-card-description">{isNormal ? 'Backend deterministic execution with an independent assignee read.' : side === 'baseline' ? 'Original v1 request, with no contract adaptation.' : 'Evidence → candidate → validation → independent verification → commit.'}</p>
    {!selected && <div className="empty-inline">Select {side === 'baseline' ? 'Run baseline' : 'Run healing'} or Compare both to populate this lane.</div>}
    {selected && events.length === 0 && <div className="empty-inline">Waiting for {source === 'sample' ? 'sample playback' : 'backend events'}…</div>}
    {selected && summary && <div className="run-card-keyline"><span>Run ID</span><code>{summary.run_id}</code></div>}
    {selected && summary && <div className="run-card-keyline"><span>Authoritative status</span><strong>{summary.status}</strong></div>}
    {selected && events.length > 0 && <>
      <div className="run-card-keyline"><span>Run</span><code>{events[0].run_id}</code></div>
      <div className="run-card-keyline"><span>Events received</span><strong>{events.length}</strong></div>
      <div className="latest-event"><span className="eyebrow">Latest event</span><strong>{typeof latest?.data.summary === 'string' ? safeText(latest.data.summary) : latest?.type.replace(/_/g, ' ')}</strong></div>
      {summary?.error && <p className="inline-error">{safeText(summary.error)}</p>}
    </>}
  </article>;
}

function Timeline({ events }: { events: RunEvent[] }) {
  const ordered = mergeEvents([], events);
  return <section className="panel timeline-panel">
    <div className="section-heading"><div><span className="eyebrow">Sequence of evidence</span><h2>Recovery timeline</h2></div><Pill tone="muted">{ordered.length} events</Pill></div>
    {ordered.length === 0 ? <div className="empty-panel">Run a sample to see the ordered event stream.</div> :
      <ol className="event-list">{ordered.map((event) => <li key={`${event.run_id}:${event.sequence}`} className="event-row">
        <span className={`event-marker marker-${eventTone(event.type)}`}>{String(event.sequence).padStart(2, '0')}</span>
        <div className="event-body"><div className="event-top"><strong>{event.type.replace(/_/g, ' ')}</strong><Pill tone={eventTone(event.type)}>{eventTone(event.type) === 'amber' ? 'Diagnosis' : eventTone(event.type) === 'green' ? 'Verified / complete' : eventTone(event.type) === 'red' ? 'Failure / rejection' : 'Execution'}</Pill></div>
          <p>{typeof event.data.summary === 'string' ? safeText(event.data.summary) : `${event.tool ?? 'Run'} event received.`}</p>
          <div className="event-meta">#{event.sequence} {event.tool ? `· ${event.tool}` : ''}</div>
          <JsonDetails label="View event JSON" value={event} />
        </div>
      </li>)}</ol>}
  </section>;
}

function RequestComparison({ mutation, source, healingEvents, healingSummary, patches, tools, patchError, patchLoading }: { mutation: MutationId; source: DataSource; healingEvents: RunEvent[]; healingSummary: RunSummary | null; patches: RepairAttempt[]; tools: ToolRegistryEntry[]; patchError: string | null; patchLoading: boolean }) {
  const scenario = source === 'sample' ? FIXTURES[mutation] : undefined;
  const patch = source === 'live' ? currentRunPatch(patches, healingSummary?.run_id ?? healingEvents[0]?.run_id ?? null) : null;
  const state = patch?.registry_committed ? 'committed' : patch?.validation_status === 'rejected' ? 'rejected' : patch?.validation_status === 'accepted' ? 'validated' : candidateState(healingEvents);
  return <section className="panel">
    <div className="section-heading"><div><span className="eyebrow">Contract adaptation</span><h2>Original / candidate request</h2></div><Pill tone={state === 'committed' ? 'green' : state === 'rejected' ? 'red' : state === 'validated' ? 'amber' : 'muted'}>{state === 'none' ? 'No candidate' : state === 'validated' ? 'Validated · not committed' : state === 'committed' ? 'Committed after verification' : state === 'rejected' ? 'Rejected · not committed' : 'Proposed · not committed'}</Pill></div>
    {source === 'live' ? patchError && !patch ? <div className="notice-line" role="alert">Patch history unavailable: {patchError}</div> : patchLoading && !patch ? <div className="empty-panel">Loading recorded patch details…</div> : !patch && state !== 'none' ? <div className="empty-panel">Candidate event received. Waiting for its persisted repair record before showing request arguments.</div> : <RequestDiff patch={patch} events={healingEvents} tools={tools} /> :
      <><div className="request-grid"><div className="request-box"><span className="eyebrow">Original v1</span><pre>{JSON.stringify(scenario?.originalRequest ?? null, null, 2)}</pre></div><div className="request-box candidate-box"><span className="eyebrow">Candidate · {state === 'none' ? 'not proposed' : state}</span><pre>{scenario?.candidateRequest && state !== 'none' && state !== 'rejected' ? JSON.stringify(scenario.candidateRequest, null, 2) : 'Unavailable'}</pre></div></div>
        {scenario && scenario.changes.length > 0 && <ul className="change-list">{scenario.changes.map((change) => <li key={change}>{change}</li>)}</ul>}
        {scenario && scenario.changes.length === 0 && <p className="quiet-copy">No contract change is needed for this scenario.</p>}
        <p className="subtle-note">Header names are shown for explanation. Credential values are never included.</p>
      </>}
  </section>;
}

function Verification({ events, source, summary }: { events: RunEvent[]; source: DataSource; summary: RunSummary | null }) {
  const status = source === 'live' ? summaryOutcome(summary, events) : runOutcome(events);
  const terminal = [...events].reverse().find((event) => event.type === 'run_completed' || event.type === 'run_failed');
  const outcome = source === 'live' ? summary?.outcome : terminal?.data.outcome as Record<string, unknown> | undefined;
  return <section className="panel verification-panel">
    <div className="section-heading"><div><span className="eyebrow">Independent read</span><h2>Final verification</h2></div><Pill tone={runTone(status)}>{outcomeLabels[status]}</Pill></div>
    <div className="verification-grid">
      <div><span>Ticket</span><strong>{status === 'verified' && outcome?.ticket_id != null ? String(outcome.ticket_id) : 'Unavailable'}</strong></div>
      <div><span>Expected assignee</span><strong>{status === 'verified' && outcome?.resolved_user_id != null ? String(outcome.resolved_user_id) : 'Unavailable'}</strong></div>
      <div><span>Observed assignee</span><strong>{status === 'verified' && outcome?.verified_assignee_id != null ? String(outcome.verified_assignee_id) : 'Unavailable'}</strong></div>
      <div><span>Verification method</span><strong>{status === 'verified' && outcome?.verification_method != null ? String(outcome.verification_method) : 'Unavailable'}</strong></div>
    </div>
    <p className="quiet-copy">{status === 'safe_refusal' ? 'No verified assignment is claimed. The missing business value was not invented.' : status === 'verified' ? `${source === 'sample' ? 'Sample event data shows' : 'GET /runs reports'} independent assignee verification.` : 'A verified task outcome is not available for this run.'}</p>
  </section>;
}

export interface DemoProps {
  source: DataSource; task: string; mutation: MutationId; appliedMutation: MutationId | null; busy: boolean; error: string | null;
  streamWarning: string | null; streamStatus: StreamStatus; commandStatus: string | null;
  baselineEvents: RunEvent[]; healingEvents: RunEvent[]; scope: ExecutionScope;
  baselineSummary: RunSummary | null; healingSummary: RunSummary | null;
  patches: RepairAttempt[];
  tools: ToolRegistryEntry[];
  patchError: string | null; patchLoading: boolean;
  onTask: (task: string) => void; onMutation: (mutation: MutationId) => void;
  onRun: (scope: Exclude<ExecutionScope, null> | 'normal') => void; onReset: () => void; onApplyMutation: () => void;
}

export function DemoView(props: DemoProps) {
  const scenario = FIXTURES[props.mutation];
  const unavailable = props.source === 'sample' && !scenario;
  const taskUnsupported = props.source === 'sample' && props.task.trim() !== DEFAULT_TASK;
  const runDisabled = props.busy || unavailable || taskUnsupported || !props.task.trim() || (props.source === 'live' && props.appliedMutation !== props.mutation);
  const visibleTimeline = props.healingEvents.length > 0 ? props.healingEvents : props.baselineEvents;
  const verificationEvents = props.healingEvents.length > 0 ? props.healingEvents : props.baselineEvents;
  return <div className="tab-content">
    <section className="demo-hero"><div><span className="eyebrow">Genesis 2.0 / Track 2 PS 01</span><h1>Watch a tool contract recover.</h1><p>Follow the same task through a fixed baseline and a guarded repair path. Each result is tied to its own event stream.</p></div><div className="hero-steps"><span>01 Find Maya</span><span>02 Assign ticket 81</span><span>03 Verify assignee</span></div></section>
    <section className="panel controls-panel"><div className="section-heading"><div><span className="eyebrow">Demo controls</span><h2>Run the assignment workflow</h2></div><SampleLabel source={props.source} /></div>
      <div className="control-fields"><label>Agent task<input value={props.task} onChange={(event) => props.onTask(event.target.value)} disabled={props.busy} placeholder={DEFAULT_TASK} /></label><label>Drift scenario<select value={props.mutation} onChange={(event) => props.onMutation(event.target.value as MutationId)} disabled={props.busy}>{MUTATIONS.map((mutation) => <option value={mutation.id} key={mutation.id}>{mutation.label}</option>)}</select></label></div>
      {props.source === 'live' && <p className="notice-line">Reset or apply the selected scenario before a run. The backend has no paired comparison operation; baseline and healing cannot be claimed as a controlled comparison.</p>}
      {unavailable && <p className="notice-line">No frontend sample exists for this scenario. Choose None, Rename, Combined, or Missing business value.</p>}
      {taskUnsupported && <p className="notice-line">Samples cover only the prefilled Maya → ticket 81 task. Restore that text to run a sample.</p>}
      {scenario && props.source === 'sample' && <p className="scenario-description"><strong>{scenario.title}.</strong> {scenario.description}</p>}
      <div className="action-row">{props.source === 'live' && <button className="button button-secondary" onClick={() => props.onRun('normal')} disabled={runDisabled}>Run normal</button>}<button className="button button-secondary" onClick={() => props.onRun('baseline')} disabled={runDisabled}>Run baseline</button><button className="button button-secondary" onClick={() => props.onRun('healing')} disabled={runDisabled}>Run healing</button><button className="button button-primary" onClick={() => props.onRun('comparison')} disabled={runDisabled || props.source === 'live'}>Compare both <span aria-hidden="true">↗</span></button>{props.source === 'live' && <button className="button button-secondary" onClick={props.onApplyMutation} disabled={props.busy}>Apply mutation</button>}<button className="button button-ghost" onClick={props.onReset} disabled={props.busy}>{props.source === 'live' ? 'Reset backend' : 'Reset view'}</button></div>
      {props.source === 'live' && <p className="quiet-copy">Scenario confirmation: {props.appliedMutation === props.mutation ? `${props.mutation} applied` : 'not confirmed'}. Event stream: <span className={`transport transport-${props.streamStatus}`}>{props.streamStatus}</span>.</p>}
      {props.busy && <p className="notice-line" role="status">{props.source === 'sample' ? 'Playing sample events…' : 'Waiting for backend response and events…'}</p>}
      {props.commandStatus && <p className="success-line" role="status">{props.commandStatus}</p>}
      {props.streamWarning && <p className="notice-line" role="status">{props.streamWarning}</p>}
      {props.error && <p className="error-line" role="alert">{props.error}</p>}
    </section>
    <div className="run-grid"><RunCard side="baseline" events={props.baselineEvents} selected={props.scope === 'baseline' || props.scope === 'comparison'} source={props.source} summary={props.baselineSummary} /><RunCard side="healing" events={props.healingEvents} selected={props.scope === 'healing' || props.scope === 'comparison'} source={props.source} summary={props.healingSummary} /></div>
    <div className="detail-grid"><Timeline events={visibleTimeline} /><div className="detail-stack"><RequestComparison mutation={props.mutation} source={props.source} healingEvents={props.healingEvents} healingSummary={props.healingSummary} patches={props.patches} tools={props.tools} patchError={props.patchError} patchLoading={props.patchLoading} /><Verification events={verificationEvents} source={props.source} summary={props.healingSummary ?? props.baselineSummary} /></div></div>
  </div>;
}

export function RegistryAuditView({ source, tools, events, mutation, error, patches, patchError, patchLoading, patchHasMore, currentRunId, onLoadMore }: { source: DataSource; tools: ToolRegistryEntry[]; events: RunEvent[]; mutation: MutationId; error: string | null; patches: RepairAttempt[]; patchError: string | null; patchLoading: boolean; patchHasMore: boolean; currentRunId: string | null; onLoadMore: () => void }) {
  const currentPatch = currentRunPatch(patches, currentRunId);
  const candidate = currentPatch?.registry_committed ? 'committed' : currentPatch?.validation_status === 'rejected' ? 'rejected' : currentPatch?.validation_status === 'accepted' ? 'validated' : candidateState(events);
  const list = source === 'sample' ? SAMPLE_REGISTRY : tools;
  const [chosen, setChosen] = useState<{ runId: string | null; key: string } | null>(null);
  const focused = chosen?.runId === currentRunId ? patches.find((patch) => patchKey(patch) === chosen.key) ?? null : null;
  const selectedPatch = focused ?? currentPatch;
  const requiredFields = (tool: ToolRegistryEntry) => {
    if (source === 'sample' && tool.name === 'assign_ticket' && candidate === 'committed') {
      if (mutation === 'rename') return 'ticket_id, assignee_id';
      if (mutation === 'combined') return 'ticket_id, target.id, Authorization header';
    }
    return tool.argument_schema.required?.join(', ') || 'Unavailable';
  };
  return <div className="tab-content"><div className="page-title"><div><span className="eyebrow">Contracts and decisions</span><h1>Registry & Audit</h1><p>Active definitions and the evidence trail stay separate from an uncommitted candidate.</p></div><SampleLabel source={source} /></div>
    {error && <p className="error-line" role="alert">{error}</p>}
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Active registry</span><h2>Tool contracts</h2></div><Pill tone="muted">{list.length} tools</Pill></div>
      {list.length === 0 ? <div className="empty-panel">No registry data is available.</div> : <div className="registry-grid">{list.map((tool) => <article className="registry-card" key={tool.name}><div className="registry-top"><strong>{tool.name}</strong><Pill tone="blue">{tool.method}</Pill></div><code>{tool.endpoint}</code><p>{tool.description}</p><div className="registry-bottom"><span>Active version</span><strong>{source === 'sample' && tool.name === 'assign_ticket' && candidate === 'committed' ? '1.1.0 · sample' : tool.version}</strong></div><div className="registry-bottom"><span>Required fields</span><strong>{requiredFields(tool)}</strong></div><div className="registry-bottom"><span>Fingerprint</span><strong>{source === 'sample' ? 'Unavailable · sample' : tool.fingerprint || 'Unavailable'}</strong></div><JsonDetails label="View active definition JSON" value={tool} /></article>)}</div>}
    </section>
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Evidence and decisions</span><h2>Repair audit</h2></div><Pill tone={candidate === 'committed' ? 'green' : candidate === 'rejected' ? 'red' : candidate === 'validated' ? 'amber' : 'muted'}>{candidate === 'none' ? 'No proposal' : candidate === 'committed' ? 'Committed' : candidate === 'rejected' ? 'Rejected' : `${candidate} · not committed`}</Pill></div>
      {source === 'live' ? <>
        {patchError && <p className="notice-line" role="alert">{patchError}</p>}
        {patchLoading && <p className="quiet-copy" role="status">Loading patch history…</p>}
        {patches.length === 0 && !patchLoading && !patchError && <div className="empty-panel">No repair attempts are recorded. The active registry may still contain default tools.</div>}
        {patches.length > 0 && <><div className="patch-history">{patches.map((patch) => <button key={patchKey(patch)} className={`patch-history-item ${selectedPatch && patchKey(selectedPatch) === patchKey(patch) ? 'selected' : ''}`} onClick={() => setChosen({ runId: currentRunId, key: patchKey(patch) })}><strong>{patch.tool_name} · {patch.run_id}</strong><span>{patch.validation_status} · {patch.recovery_status} · {patch.registry_committed ? 'committed in run' : 'not committed'}</span></button>)}</div>{patchHasMore && <button className="button button-secondary" disabled={patchLoading} onClick={onLoadMore}>Load older repair attempts</button>}</>}
        {selectedPatch && <PatchInspector patch={selectedPatch} events={selectedPatch.run_id === currentRunId ? events : []} tools={tools} />}
        {!selectedPatch && currentRunId && <p className="quiet-copy">Current run {currentRunId} has no recorded patch. Historical repairs remain in the list above.</p>}
      </> : events.length === 0 ? <div className="empty-panel">Run a sample scenario to see proposal evidence, validation, and verification decisions.</div> : <><div className="audit-list">{events.filter((event) => ['tool_call_failed', 'diagnosis_started', 'patch_proposed', 'patch_validated', 'patch_rejected', 'recovery_verified', 'registry_committed'].includes(event.type)).map((event) => <div className="audit-item" key={`${event.run_id}:${event.sequence}`}><span className={`audit-dot marker-${eventTone(event.type)}`} /><div><strong>{event.type.replace(/_/g, ' ')}</strong><p>{safeText(event.data.summary ?? 'No summary available.')}</p></div><JsonDetails label="Evidence JSON" value={event.data} /></div>)}</div>{candidate === 'none' && <p className="quiet-copy">No repair candidate was needed.</p>}</>}
      {source === 'sample' && FIXTURES[mutation]?.changes.map((change) => <p className="quiet-copy" key={change}>{change}</p>)}
    </section>
    {source === 'live' && <ReuseEvidence runId={currentRunId} events={events} patches={patches} tools={tools} />}
  </div>;
}

export function BenchmarkView({ source, metrics, mutation, baselineEvents, healingEvents, error }: { source: DataSource; metrics: SystemMetrics | null; mutation: MutationId; baselineEvents: RunEvent[]; healingEvents: RunEvent[]; error: string | null }) {
  const scenario = FIXTURES[mutation];
  return <div className="tab-content"><div className="page-title"><div><span className="eyebrow">Evaluation readiness</span><h1>Benchmark</h1><p>Measured benchmark results will appear here after the evaluation runner and metric contract are connected.</p></div><SampleLabel source={source} /></div>
    {error && <p className="error-line" role="alert">{error}</p>}
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Metrics</span><h2>Measurement status</h2></div><Pill tone="amber">No benchmark dataset</Pill></div>
      <div className="metric-grid"><div className="metric-card"><span>Adaptation rate</span><strong>Unavailable</strong><small>Requires evaluated healing trials</small></div><div className="metric-card"><span>Mean recovery time</span><strong>{source === 'live' && metrics?.average_recovery_time_ms != null ? `${metrics.average_recovery_time_ms} ms` : 'Unavailable'}</strong><small>Verified recoveries only</small></div><div className="metric-card"><span>Token usage</span><strong>Unavailable</strong><small>Complete per-run counts required</small></div><div className="metric-card"><span>Safe refusals</span><strong>Unavailable</strong><small>No authoritative aggregate field yet</small></div></div>
      {source === 'live' && metrics && <p className="quiet-copy">Backend run records: {metrics.total_runs}. Verified recovery records: {metrics.verified_recoveries}. These are operational counters, not a controlled benchmark.</p>}
      {source === 'sample' && <p className="quiet-copy">Sample data — backend not connected. The cards intentionally contain no invented rates, timings, or token counts.</p>}
    </section>
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Current demonstration</span><h2>Scenario comparison</h2></div><Pill tone="muted">{source === 'sample' ? scenario?.title ?? 'No sample' : 'Unavailable live'}</Pill></div>
      {source === 'live' ? <div className="empty-panel">The backend has no controlled paired comparison operation. Single-run results appear on the Demo tab.</div> : baselineEvents.length === 0 && healingEvents.length === 0 ? <div className="empty-panel">Run Compare both on the Demo tab to view sample outcomes here.</div> : <div className="benchmark-rows"><div><span>Baseline</span><Pill tone={runTone(runOutcome(baselineEvents))}>{outcomeLabels[runOutcome(baselineEvents)]}</Pill></div><div><span>DriftGuard healing</span><Pill tone={runTone(runOutcome(healingEvents))}>{outcomeLabels[runOutcome(healingEvents)]}</Pill></div></div>}
      {source === 'sample' && <p className="subtle-note">These are scenario illustrations, not benchmark trials or measured recovery rates.</p>}
    </section>
  </div>;
}
