import type { RunEvent, SystemMetrics } from '../types';
import type { DataSource } from './DashboardViews';
import { SampleLabel } from './DashboardViews';
import { currentRunMetric, measuredTokens } from '../state/metricsState';
import { runOutcome } from '../state/runState';

const available = (value: number | null | undefined, suffix = '') => value == null ? 'Unavailable' : `${value.toLocaleString()}${suffix}`;
const shortId = (id: string) => id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id;

function Metric({ label, value, context }: { label: string; value: string; context: string }) {
  return <div className="metric-card"><span>{label}</span><strong>{value}</strong><small>{context}</small></div>;
}

export function BenchmarkView({ source, metrics, metricsLoading, metricsError, currentRunId, baselineEvents, healingEvents, sampleHistory }: {
  source: DataSource; metrics: SystemMetrics | null; metricsLoading: boolean; metricsError: string | null;
  currentRunId: string | null; baselineEvents: RunEvent[]; healingEvents: RunEvent[];
  sampleHistory: { scenario: string; baseline: string; healing: string }[];
}) {
  const live = source === 'live' && !!metrics;
  const current = live ? currentRunMetric(metrics, currentRunId) : null;
  const baseline = live ? metrics.run_metrics.find((run) => run.execution_type === 'model_driven_baseline') : null;
  const ordinary = live ? metrics.run_metrics.find((run) => run.mode === 'normal' && run.status === 'completed') : null;
  const diagnosis = current?.mode === 'healing' ? measuredTokens(current.diagnosis_token_usage) : null;
  const session = current?.mode === 'healing' ? measuredTokens(current.session_token_usage) : null;
  const baselineTokens = baseline ? measuredTokens(baseline.session_token_usage) : null;
  const ordinaryTokens = ordinary ? measuredTokens(ordinary.session_token_usage) : null;
  return <div className="tab-content"><div className="page-title"><div><span className="eyebrow">Measured evidence</span><h1>Benchmark</h1><p>Run measurements and operational counts are shown by their actual scope. Evaluation rates await a published benchmark result.</p></div><SampleLabel source={source} /></div>
    {metricsLoading && source === 'live' && <p className="notice-line" role="status">Loading backend metrics…</p>}
    {metricsError && source === 'live' && <p className="error-line" role="alert">Metrics unavailable: {metricsError}</p>}
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Controlled evaluation</span><h2>Adaptation and safety</h2></div><span className="pill pill-amber">Benchmark result unavailable</span></div>
      <div className="metric-grid">
        <Metric label="Adaptation rate" value="Unavailable" context="Verified recoverable trials / recoverable healing trials: Unavailable / Unavailable" />
        <Metric label="Verified successes" value="Unavailable" context="Controlled recoverable healing trials" />
        <Metric label="Safe refusals" value="Unavailable" context="Deliberately unsafe trials; excluded from adaptation" />
        <Metric label="Failed recoveries" value="Unavailable" context="Recoverable trials that did not verify" />
        <Metric label="Recovery sample count" value="Unavailable" context="Number of measured verified trial durations" />
        <Metric label="Median recovery time" value="Unavailable" context="No benchmark median in GET /metrics" />
        <Metric label="p95 recovery time" value="Unavailable" context="Show only with sufficient measured sample context" />
      </div>
      <p className="subtle-note">GET /metrics does not publish evaluated recoverable and unsafe trial totals. Its repair-attempt count and healing-run count are different scopes, so no adaptation rate is calculated here.</p>
    </section>
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">GET /metrics · persisted operations</span><h2>Operational measurements</h2></div><span className="pill pill-blue">Backend counters</span></div>
      <div className="metric-grid">
        <Metric label="Persisted runs" value={live ? available(metrics.total_runs_count ?? metrics.total_runs) : 'Unavailable'} context="All run records; operational sample count" />
        <Metric label="Normal completed runs" value={live ? available(metrics.normal_success_count) : 'Unavailable'} context="Within backend's first 1,000-run scan" />
        <Metric label="Healing runs" value={live ? available(metrics.healing_trials) : 'Unavailable'} context="Within backend's first 1,000-run scan" />
        <Metric label="Verified repair attempts" value={live ? available(metrics.verified_recoveries) : 'Unavailable'} context="Repair records; may differ from run count" />
        <Metric label="Mean verified recovery time" value={live ? available(metrics.average_recovery_time_ms, ' ms') : 'Unavailable'} context="Backend-measured repair durations; timed sample count unavailable" />
      </div>
      {live && <p className="quiet-copy">Run page: {metrics.run_metrics.length} of {available(metrics.total_runs_count)} records (offset {available(metrics.offset)}, limit {available(metrics.limit)}). Aggregate counters scan at most 1,000 runs and 1,000 repair attempts. Backend status: {metrics.status}.</p>}
      {source === 'sample' && <p className="quiet-copy">Sample data — backend not connected. No playback duration or sample outcome is a measured result.</p>}
      {source === 'live' && !metrics && !metricsLoading && !metricsError && <div className="empty-panel">No metrics response has been loaded.</div>}
    </section>
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Per-run measurements</span><h2>Token usage and recovery</h2></div><span className="pill pill-muted">No cross-run totals</span></div>
      {currentRunId && <p className="run-reference">Selected run <strong>{shortId(currentRunId)}</strong><details><summary>Full run ID</summary><code>{currentRunId}</code></details></p>}
      <div className="metric-grid">
        <Metric label="Current run diagnosis tokens" value={available(diagnosis)} context={current?.mode === 'healing' ? 'Complete measured diagnosis usage only' : 'Select a healing run'} />
        <Metric label="Current healing-session tokens" value={available(session)} context="Agent + diagnosis tokens; complete usage only" />
        <Metric label="Current verified recovery" value={available(current?.recovery_duration_ms, ' ms')} context="Backend measured; verified repairs in selected run" />
        <Metric label="Baseline tokens" value={available(baselineTokens)} context={baseline ? `Latest baseline run on page: ${shortId(baseline.run_id)}` : 'No baseline run on loaded page'} />
        <Metric label="Ordinary-success tokens" value={available(ordinaryTokens)} context={ordinary ? `Latest completed normal run on page: ${shortId(ordinary.run_id)}` : 'No completed normal run on loaded page'} />
      </div>
      {baseline && <details className="json-details"><summary>Full baseline run ID</summary><code>{baseline.run_id}</code></details>}
      {ordinary && <details className="json-details"><summary>Full ordinary-success run ID</summary><code>{ordinary.run_id}</code></details>}
      {current && <p className="quiet-copy">Selected run status: {current.status}. Verified repairs: {current.verified_recoveries_count}. Rejected repairs: {current.repair_rejections_count}. These values describe this run only.</p>}
      {source === 'live' && currentRunId && !current && <p className="notice-line">Selected run is absent from the loaded metrics page. Its measurements are unavailable here.</p>}
      {source === 'sample' && <p className="quiet-copy">Sample data — backend not connected. Token cards remain unavailable.</p>}
    </section>
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Current demonstration</span><h2>Outcome lanes</h2></div><span className="pill pill-muted">{source === 'sample' ? 'Sample illustration' : 'Single live runs'}</span></div>
      {source === 'live' ? <div className="empty-panel">The backend does not expose a controlled paired comparison operation. See the Demo tab for the selected single run.</div> : baselineEvents.length === 0 && healingEvents.length === 0 ? <div className="empty-panel">Run Compare both on the Demo tab to view sample outcomes.</div> : <div className="benchmark-rows"><div><span>Baseline</span><strong>{runOutcome(baselineEvents).replace(/_/g, ' ')}</strong></div><div><span>DriftGuard healing</span><strong>{runOutcome(healingEvents).replace(/_/g, ' ')}</strong></div></div>}
      {source === 'sample' && <p className="subtle-note">These outcomes illustrate the workflow. They are not benchmark trials.</p>}
      {source === 'sample' && sampleHistory.length > 0 && <details className="json-details"><summary>Previous sample comparisons ({sampleHistory.length})</summary><ul className="sample-history">{sampleHistory.map((item, index) => <li key={`${index}-${item.scenario}`}>{item.scenario}: baseline {item.baseline.replace(/_/g, ' ')}, healing {item.healing.replace(/_/g, ' ')}</li>)}</ul></details>}
    </section>
  </div>;
}
