import { useEffect, useRef, useState } from 'react';
import { BenchmarkView, DemoView, RegistryAuditView, SampleLabel, type DataSource, type ExecutionScope } from './components/DashboardViews';
import { DEFAULT_TASK, FIXTURES, fixtureEvents, type MutationId } from './fixtures/scenarios';
import { fetchMetrics, fetchRegistry, resetDemo, subscribeToEvents, triggerRun } from './services/api';
import { mergeEvents } from './state/runState';
import type { RunEvent, RunSummary, SystemMetrics, ToolRegistryEntry } from './types';

type Tab = 'demo' | 'registry' | 'benchmark';

export default function App() {
  const [tab, setTab] = useState<Tab>('demo');
  const [source, setSource] = useState<DataSource>('sample');
  const [task, setTask] = useState(DEFAULT_TASK);
  const [mutation, setMutation] = useState<MutationId>('rename');
  const [scope, setScope] = useState<ExecutionScope>(null);
  const [baselineEvents, setBaselineEvents] = useState<RunEvent[]>([]);
  const [healingEvents, setHealingEvents] = useState<RunEvent[]>([]);
  const [baselineSummary, setBaselineSummary] = useState<RunSummary | null>(null);
  const [healingSummary, setHealingSummary] = useState<RunSummary | null>(null);
  const [tools, setTools] = useState<ToolRegistryEntry[]>([]);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const generationRef = useRef(0);

  const stopActivity = () => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    timerRef.current = null;
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
  };

  useEffect(() => () => stopActivity(), []);

  useEffect(() => {
    if (source !== 'live') return;
    let cancelled = false;
    Promise.allSettled([fetchRegistry(), fetchMetrics()]).then(([registry, systemMetrics]) => {
      if (cancelled) return;
      if (registry.status === 'fulfilled') setTools(registry.value.tools);
      if (systemMetrics.status === 'fulfilled') setMetrics(systemMetrics.value);
      if (registry.status === 'rejected' || systemMetrics.status === 'rejected') {
        setError('The backend is unavailable or returned an error. Live registry and metrics may be incomplete.');
      }
    });
    return () => { cancelled = true; };
  }, [source]);

  const clearView = () => {
    generationRef.current += 1;
    stopActivity();
    setBusy(false);
    setError(null);
    setScope(null);
    setBaselineEvents([]);
    setHealingEvents([]);
    setBaselineSummary(null);
    setHealingSummary(null);
  };

  const changeSource = (next: DataSource) => {
    if (next === source) return;
    clearView();
    setSource(next);
    setMutation(next === 'live' ? 'none' : 'rename');
  };

  const playSample = (nextScope: Exclude<ExecutionScope, null>) => {
    if (!FIXTURES[mutation]) {
      setError('No frontend sample exists for this mutation scenario.');
      return;
    }
    const baseline = nextScope === 'healing' ? [] : fixtureEvents(mutation, 'baseline');
    const healing = nextScope === 'baseline' ? [] : fixtureEvents(mutation, 'healing');
    const length = Math.max(baseline.length, healing.length);
    setBusy(true);
    let position = 0;
    timerRef.current = window.setInterval(() => {
      position += 1;
      setBaselineEvents(baseline.slice(0, position));
      setHealingEvents(healing.slice(0, position));
      if (position >= length) {
        if (timerRef.current !== null) window.clearInterval(timerRef.current);
        timerRef.current = null;
        setBusy(false);
      }
    }, 260);
  };

  const runLive = async (nextScope: 'baseline' | 'healing', generation: number) => {
    try {
      // Preserve the existing single-run API path. Comparison and mutation stay disabled in live mode.
      const run = await triggerRun(nextScope === 'baseline' ? 'normal' : 'healing', task);
      if (generation !== generationRef.current) return;
      if (nextScope === 'baseline') setBaselineSummary(run);
      else setHealingSummary(run);
      unsubscribeRef.current = subscribeToEvents(run.run_id, (event) => {
        if (generation !== generationRef.current) return;
        if (nextScope === 'baseline') setBaselineEvents((previous) => mergeEvents(previous, [event]));
        else setHealingEvents((previous) => mergeEvents(previous, [event]));
      }, () => {
        if (generation !== generationRef.current) return;
        setBusy(false);
        fetchMetrics().then(setMetrics).catch(() => setError('Could not refresh live metrics.'));
        fetchRegistry().then((response) => setTools(response.tools)).catch(() => setError('Could not refresh the live registry.'));
      }, (message) => {
        if (generation === generationRef.current) setError(message);
      });
    } catch (reason) {
      if (generation !== generationRef.current) return;
      setError(reason instanceof Error ? reason.message : 'The backend run failed to start.');
      setBusy(false);
    }
  };

  const handleRun = (nextScope: Exclude<ExecutionScope, null>) => {
    if (busy || !task.trim() || (source === 'sample' && task.trim() !== DEFAULT_TASK)) return;
    clearView();
    setScope(nextScope);
    if (source === 'sample') playSample(nextScope);
    else if (nextScope !== 'comparison') {
      setBusy(true);
      void runLive(nextScope, generationRef.current);
    }
  };

  const handleReset = async () => {
    if (source === 'sample') {
      clearView();
      setTask(DEFAULT_TASK);
      setMutation('rename');
      return;
    }
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await resetDemo();
      clearView();
      if (result.mock_api !== 'reset_successful') setError('Orchestrator reset returned, but the mock API reset was not confirmed.');
      const [registry, systemMetrics] = await Promise.all([fetchRegistry(), fetchMetrics()]);
      setTools(registry.tools);
      setMetrics(systemMetrics);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Backend reset failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleTaskChange = (value: string) => {
    clearView();
    setTask(value);
  };

  const handleMutationChange = (value: MutationId) => {
    clearView();
    setMutation(value);
  };

  return <div className="app-shell">
    <div className="source-bar"><div className="source-inner"><SampleLabel source={source} /><span>Phase 1 · fixture demonstration</span></div></div>
    <header className="dashboard-header"><div className="header-inner"><div className="brand"><div className="brand-mark">D<span>G</span></div><div><strong>DriftGuard</strong><small>Adaptive tool registry</small></div></div>
      <nav className="top-nav" aria-label="Dashboard tabs"><button className={tab === 'demo' ? 'active' : ''} onClick={() => setTab('demo')}>Demo</button><button className={tab === 'registry' ? 'active' : ''} onClick={() => setTab('registry')}>Registry & Audit</button><button className={tab === 'benchmark' ? 'active' : ''} onClick={() => setTab('benchmark')}>Benchmark</button></nav>
      <div className="source-switch" aria-label="Data source"><button className={source === 'sample' ? 'active' : ''} onClick={() => changeSource('sample')}>Sample</button><button className={source === 'live' ? 'active' : ''} onClick={() => changeSource('live')}>Live</button></div>
    </div></header>
    <main className="dashboard-main">
      {tab === 'demo' && <DemoView source={source} task={task} mutation={mutation} busy={busy} error={error} baselineEvents={baselineEvents} healingEvents={healingEvents} scope={scope} baselineSummary={baselineSummary} healingSummary={healingSummary} onTask={handleTaskChange} onMutation={handleMutationChange} onRun={handleRun} onReset={() => { void handleReset(); }} />}
      {tab === 'registry' && <RegistryAuditView source={source} tools={tools} events={healingEvents} mutation={mutation} error={error} />}
      {tab === 'benchmark' && <BenchmarkView source={source} metrics={metrics} mutation={mutation} baselineEvents={baselineEvents} healingEvents={healingEvents} error={error} />}
    </main>
    <footer className="dashboard-footer"><span>DriftGuard / GENESIS 2.0</span><span>Evidence before commitment · independent verification</span></footer>
  </div>;
}
