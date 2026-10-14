import type { ReactNode } from 'react';
import type { RepairAttempt, RunEvent, ToolRegistryEntry } from '../types';
import { activeVersionMatches, operationSummary, patchOperations, requestPair, reuseEvidence } from '../state/patchState';
import { safeDisplay, safeText } from '../state/runState';

const value = (item: unknown) => item === null || item === undefined || item === '' ? 'Unavailable' : safeText(item);
const displayJson = (item: unknown) => item === null || item === undefined ? 'Unavailable' : JSON.stringify(safeDisplay(item), null, 2);
const record = (item: unknown): Record<string, unknown> | null => item && typeof item === 'object' && !Array.isArray(item) ? item as Record<string, unknown> : null;

function Detail({ label, data }: { label: string; data: unknown }) {
  return <details className="json-details"><summary>{label}</summary><pre>{displayJson(data)}</pre></details>;
}

function Step({ label, state, children }: { label: string; state: 'blue' | 'amber' | 'green' | 'red' | 'muted'; children: ReactNode }) {
  return <div className={`lifecycle-step lifecycle-${state}`}><span>{label}</span><strong>{children}</strong></div>;
}

export function RequestDiff({ patch, events, tools }: { patch: RepairAttempt | null; events: RunEvent[]; tools: ToolRegistryEntry[] }) {
  if (!patch) return <div className="empty-panel">No recorded candidate for this run. A selected scenario alone is not patch evidence.</div>;
  const pair = requestPair(patch, events);
  const active = tools.find((tool) => tool.name === patch.tool_name);
  const originalSchema = active && active.version === patch.base_version && active.fingerprint === patch.base_fingerprint
    ? active.argument_schema : null;
  const candidateSchema = patch.candidate_definition?.argument_schema ?? null;
  return <>
    <p className="quiet-copy">Recorded run <code>{patch.run_id}</code> · {patch.tool_name} · base {patch.base_version} → candidate {patch.candidate_definition?.version ?? patch.candidate_version ?? 'unavailable'}</p>
    <div className="request-grid"><div className="request-box"><span className="eyebrow">Original caller arguments</span><pre>{displayJson(pair.original)}</pre></div><div className="request-box candidate-box"><span className="eyebrow">Validated candidate arguments</span><pre>{displayJson(pair.candidate)}</pre></div></div>
    <p className="subtle-note">Arguments come from this run’s tool event and validator record. Missing snapshots remain unavailable. Header values are redacted.</p>
    <div className="request-grid"><Detail label="Original active schema, when version still matches" data={originalSchema} /><Detail label="Recorded candidate schema" data={candidateSchema} /></div>
  </>;
}

export function PatchInspector({ patch, events, tools }: { patch: RepairAttempt; events: RunEvent[]; tools: ToolRegistryEntry[] }) {
  const operations = patchOperations(patch);
  const details = patch.validation_details;
  const reasons = Array.isArray(details?.rejection_reasons) ? details.rejection_reasons.filter((item): item is string => typeof item === 'string') : [];
  const checks = Array.isArray(details?.checks) ? details.checks : [];
  const retryStarted = events.some((event) => event.run_id === patch.run_id && event.tool === patch.tool_name && event.type === 'retry_started');
  const verifiedEvent = events.find((event) => event.run_id === patch.run_id && event.tool === patch.tool_name && event.type === 'recovery_verified');
  const active = activeVersionMatches(patch, tools);
  return <article className="patch-inspector">
    <div className="patch-title"><div><span className="eyebrow">Recorded repair · {patch.tool_name}</span><h3>{patch.run_id}</h3><code>Proposal {patch.proposal_fingerprint}</code></div><span className={`pill ${patch.registry_committed ? 'pill-green' : patch.validation_status === 'rejected' ? 'pill-red' : 'pill-amber'}`}>{patch.registry_committed ? active ? 'Active committed version' : 'Historical commit' : patch.validation_status === 'rejected' ? 'Rejected candidate' : 'Candidate · not committed'}</span></div>
    <div className="lifecycle-grid"><Step label="1 · Stage candidate" state={patch.validation_status === 'rejected' ? 'red' : patch.validation_status === 'accepted' ? 'amber' : 'muted'}>{patch.validation_status === 'accepted' ? 'Validated; candidate recorded' : patch.validation_status === 'rejected' ? 'Rejected before retry' : value(patch.validation_status)}</Step>
      <Step label="2 · Retry" state={retryStarted || patch.recovery_status === 'recovered' ? 'blue' : patch.validation_status === 'rejected' ? 'muted' : 'red'}>{retryStarted ? 'Retry started · ' : ''}{value(patch.recovery_status)}</Step>
      <Step label="3 · Verify" state={patch.verification_succeeded ? 'green' : 'muted'}>{patch.verification_succeeded ? 'Independent verification succeeded' : 'Not verified'}</Step>
      <Step label="4 · Commit" state={patch.registry_committed ? 'green' : 'muted'}>{patch.registry_committed ? `${value(patch.committed_version)} committed${active ? ' · active now' : ' · not active now'}` : 'No registry commit'}</Step></div>
    <div className="patch-meta"><span>Base version <strong>{value(patch.base_version)}</strong></span><span>Candidate ID <strong>{value(patch.candidate_id)}</strong></span><span>Active version <strong>{value(tools.find((tool) => tool.name === patch.tool_name)?.version)}</strong></span></div>
    <h4>Changed fields and evidence</h4>
    {operations.length ? <ol className="operation-list">{operations.map((operation, index) => <li key={`${index}:${operation.operation_type}:${operation.target_path}`}><strong>{operationSummary(operation)}</strong><span>Evidence: {value(operation.evidence_summary)} {operation.evidence_path && <code>({safeText(operation.evidence_path)})</code>}</span><small>Value source: {operation.operation_type === 'set_header' ? 'Configured credential reference' : value(operation.value_provenance)}{operation.conversion_policy ? ` · Conversion: ${value(operation.conversion_policy)}` : ''}</small></li>)}</ol> : <p className="quiet-copy">No structured operations were recorded for this repair attempt.</p>}
    <div className="audit-facts"><div><span>Validation decision</span><strong>{value(patch.validation_status)}</strong><p>{reasons.length ? reasons.map(safeText).join(' · ') : patch.validation_status === 'accepted' ? checks.length ? `${checks.filter((check) => record(check)?.passed === true).length} recorded checks passed; candidate authorized: ${value(details?.authorized_for_recovery)}.` : 'Accepted; check details unavailable.' : value(patch.termination_reason)}</p></div>
      <div><span>Retry outcome</span><strong>{value(patch.recovery_status)}</strong><p>{value(patch.termination_reason)}</p></div>
      <div><span>Business verification</span><strong>{patch.verification_succeeded ? 'Succeeded' : 'Not confirmed'}</strong><p>{verifiedEvent ? `Method: ${value(verifiedEvent.data.verification_method)}; ticket ${value(verifiedEvent.data.verified_ticket_id)}; assignee ${value(verifiedEvent.data.verified_assignee_id)}.` : patch.verification_succeeded ? 'Audit record confirms verification; event detail is unavailable here.' : 'No successful independent verification is recorded.'}</p></div></div>
    <RequestDiff patch={patch} events={events} tools={tools} />
    <div className="patch-json"><Detail label="Proposal and evidence JSON" data={patch.proposal} /><Detail label="Validation decision JSON" data={patch.validation_details} /><Detail label="Candidate definition JSON" data={patch.candidate_definition} /><Detail label="Full audit record JSON" data={patch} /></div>
  </article>;
}

export function ReuseEvidence({ runId, events, patches, tools }: { runId: string | null; events: RunEvent[]; patches: RepairAttempt[]; tools: ToolRegistryEntry[] }) {
  const evidence = reuseEvidence(runId, events, patches, tools);
  return <section className="panel"><div className="section-heading"><div><span className="eyebrow">Second task</span><h2>Committed definition reuse</h2></div></div>
    {!runId ? <div className="empty-panel">Run a live task after a recovery without resetting the backend to inspect reuse evidence.</div> : evidence ? <><p className="quiet-copy">Run <code>{runId}</code> called <strong>{evidence.used.tool}</strong> with version <strong>{evidence.used.version}</strong> and fingerprint <code>{evidence.used.fingerprint}</code>. That active definition was committed in run <code>{evidence.used.prior_run_id}</code>.</p><p className="quiet-copy">Diagnosis in this run: <strong>{evidence.diagnosisOccurred ? 'Yes · diagnosis_started event recorded' : 'No diagnosis_started event received'}</strong>.</p><p className="subtle-note">This is tool event and active registry evidence, not a frontend cache-hit claim.</p></> : <div className="empty-panel">No matching prior commit, active registry version, and current tool-call event are available to prove reuse. Diagnosis and reuse are not inferred from the selected scenario.</div>}
  </section>;
}
