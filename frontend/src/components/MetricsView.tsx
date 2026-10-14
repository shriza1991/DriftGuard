import type { BenchmarkSummary, RunEvent, SystemMetrics } from '../types';
import type { DataSource } from './DashboardViews';
import { SampleLabel } from './DashboardViews';
import { currentRunMetric, measuredTokens } from '../state/metricsState';
import { runOutcome } from '../state/runState';

const available = (value: number | null | undefined, suffix = '') => value == null ? 'Unavailable' : `${value.toLocaleString()}${suffix}`;
const shortId = (id: string) => id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id;

function Metric({ label, value, context }: { label: string; value: string; context: string }) {
  return <div className="metric-card"><span>{label}</span><strong>{value}</strong><small>{context}</small></div>;
}

export function BenchmarkView({ source, metrics, benchmarkSummary, metricsLoading, metricsError, currentRunId, baselineEvents, healingEvents, sampleHistory }: {
  source: DataSource; metrics: SystemMetrics | null; metricsLoading: boolean; metricsError: string | null;
  benchmarkSummary: BenchmarkSummary | null;
  currentRunId: string | null; baselineEvents: RunEvent[]; healingEvents: RunEvent[];
  sampleHistory: { scenario: string; baseline: string; healing: string }[];
}) {
  const live = source === 'live' && !!metrics;
  const current = live ? currentRunMetric(metrics, currentRunId) : null;
  const baseline = live ? metrics.run_metrics.find((run) => run.execution_type === 'model_driven_baseline') : null;
  const ordinary = live ? metrics.run_metrics.find((run) => run.mode === 'normal' && run.status === 'completed') : null;
  const hasObservedDiagnosis = [...baselineEvents, ...healingEvents].some((event) => event.run_id === currentRunId && event.type === 'diagnosis_started');
  const diagnosis = current?.mode === 'healing' && hasObservedDiagnosis ? measuredTokens(current.diagnosis_token_usage) : null;
  const session = current?.mode === 'healing' ? measuredTokens(current.session_token_usage) : null;
  const baselineTokens = baseline ? measuredTokens(baseline.session_token_usage) : null;
  const ordinaryTokens = ordinary ? measuredTokens(ordinary.session_token_usage) : null;
  const measured = source === 'live' && benchmarkSummary?.status === 'measured' ? benchmarkSummary.metrics : null;
  return <div className="tab-content"><div className="page-title"><div><span className="eyebrow">Measured evidence</span><h1>Benchmark</h1><p>Run measurements and operational counts are shown by their actual scope. Evaluation rates await a published benchmark result.</p></div><SampleLabel source={source} /></div>
    {metricsLoading && source === 'live' && <p className="notice-line" role="status">Loading backend metrics…</p>}
    {metricsError && source === 'live' && <p className="error-line" role="alert">Metrics unavailable: {metricsError}</p>}
    <section className="panel"><div className="section-heading"><div><span className="eyebrow">Controlled evaluation</span><h2>Adaptation and safety</h2></div><span className={`pill ${measured ? 'pill-blue' : 'pill-amber'}`}>{measured ? 'Measured' : 'Planned · unavailable'}</span></div>
      <div className="metric-grid">
        <Metric label="Adaptation rate" value={measured ? available(measured.recoverable_case_adaptation_rate_percent, '%') : 'Unavailable'} context="Measured verified recoveries / recoverable cold trials" />
        <Metric label="All-breaking resolution" value={measured ? available(measured.all_breaking_case_resolution_rate_percent, '%') : 'Unavailable'} context="Verified recovery or authoritative safe stop" />
        <Metric label="Safe-stop rate" value={measured ? available(measured.safe_stop_rate_percent, '%') : 'Unavailable'} context="Excluded from adaptation success" />
        <Metric label="Verified correctness" value={measured ? available(measured.verified_task_correctness_percent, '%') : 'Unavailable'} context="Independent provider-state read" />
        <Metric label="Median MTTR" value={measured ? available(measured.median_mttr_ms, ' ms') : 'Unavailable'} context="Cold verified recoveries" />
        <Metric label="p95 MTTR" value={measured ? available(measured.p95_mttr_ms, ' ms') : 'Unavailable'} context="Measured only with sufficient samples" />
        <Metric label="Unsafe executions" value={measured ? available(measured.unsafe_execution_count) : 'Unavailable'} context="Expected zero" />
        <Metric label="Maximum retries" value={measured ? available(measured.maximum_retry_count) : 'Unavailable'} context="Expected maximum one" />
        <Metric label="Warm diagnosis reduction" value={measured ? available(measured.warm_call_diagnosis_reduction_percent, '%') : 'Unavailable'} context="Second matching call" />
      </div>
      <p className="subtle-note">{measured ? `${benchmarkSummary?.repetitions} deterministic repetitions per scenario. Task: ${benchmarkSummary?.task}` : 'Run the documented benchmark command to publish measured results. Targets are never displayed as measurements.'}</p>
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
      {currentRunId && <div className="run-reference">Selected run <strong>{shortId(currentRunId)}</strong><details><summary>Full run ID</summary><code>{currentRunId}</code></details></div>}
      <div className="metric-grid">
        <Metric label="Current run diagnosis tokens" value={available(diagnosis)} context={hasObservedDiagnosis ? 'Backend-reported complete usage for observed diagnosis' : 'No diagnosis event in the selected run; usage unavailable'} />
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
