import { useEffect, useRef, useState } from 'react';
import { BenchmarkView, DemoView, RegistryAuditView, SampleLabel, type DataSource, type ExecutionScope } from './components/DashboardViews';
import { DEFAULT_TASK, FIXTURES, fixtureEvents, type MutationId } from './fixtures/scenarios';
import { api } from './services/api';
import { RunEventStream, type StreamStatus } from './services/eventStream';
import { CommandGate } from './state/commandGate';
import { mergeEvents } from './state/runState';
import type { CreateRunRequest, RepairAttempt, RunEvent, RunSummary, SystemMetrics, ToolRegistryEntry } from './types';

type Tab = 'demo' | 'registry' | 'benchmark';
type LiveAction = 'normal' | 'baseline' | 'healing';
const isTerminal = (summary: RunSummary) => summary.status !== 'running';

export default function App() {
  const [tab, setTab] = useState<Tab>('demo');
  const [source, setSource] = useState<DataSource>('sample');
  const [task, setTask] = useState(DEFAULT_TASK);
  const [mutation, setMutation] = useState<MutationId>('rename');
  const [appliedMutation, setAppliedMutation] = useState<MutationId | null>(null);
  const [scope, setScope] = useState<ExecutionScope>(null);
  const [baselineEvents, setBaselineEvents] = useState<RunEvent[]>([]);
  const [healingEvents, setHealingEvents] = useState<RunEvent[]>([]);
  const [baselineSummary, setBaselineSummary] = useState<RunSummary | null>(null);
  const [healingSummary, setHealingSummary] = useState<RunSummary | null>(null);
  const [tools, setTools] = useState<ToolRegistryEntry[]>([]);
  const [patches, setPatches] = useState<RepairAttempt[]>([]);
  const [patchError, setPatchError] = useState<string | null>(null);
  const [patchLoading, setPatchLoading] = useState(false);
  const [patchHasMore, setPatchHasMore] = useState(false);
  const [patchOffset, setPatchOffset] = useState(0);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streamWarning, setStreamWarning] = useState<string | null>(null);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('disconnected');
  const [commandStatus, setCommandStatus] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);
  const graceRef = useRef<number | null>(null);
  const streamRef = useRef<RunEventStream | null>(null);
  const generationRef = useRef(0);
  const gateRef = useRef(new CommandGate());

  const stopActivity = () => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    if (pollRef.current !== null) window.clearInterval(pollRef.current);
    if (graceRef.current !== null) window.clearTimeout(graceRef.current);
    timerRef.current = pollRef.current = graceRef.current = null;
    streamRef.current?.close();
    streamRef.current = null;
  };

  useEffect(() => () => stopActivity(), []);

  useEffect(() => {
    if (source !== 'live') return;
    let cancelled = false;
    setPatchLoading(true);
    Promise.allSettled([api.fetchRegistry(), api.fetchMetrics(), api.fetchPatches()]).then(([registry, systemMetrics, history]) => {
      if (cancelled) return;
      if (registry.status === 'fulfilled') setTools(registry.value.tools); else setTools([]);
      if (systemMetrics.status === 'fulfilled') setMetrics(systemMetrics.value);
      if (history.status === 'fulfilled') {
        setPatches(history.value.repair_history);
        setPatchHasMore(history.value.count === history.value.limit);
        setPatchOffset(history.value.offset + history.value.count);
        setPatchError(history.value.invalid_count ? `${history.value.invalid_count} incomplete patch records were omitted.` : null);
      } else { setPatches([]); setPatchHasMore(false); setPatchOffset(0); setPatchError(history.reason instanceof Error ? history.reason.message : 'Patch history unavailable.'); }
      setPatchLoading(false);
      if (registry.status === 'rejected' || systemMetrics.status === 'rejected')
        setError('Cannot load live registry or metrics. Check the orchestrator connection or response format.');
    });
    return () => { cancelled = true; };
  }, [source]);

  const clearView = () => {
    generationRef.current += 1;
    stopActivity();
    gateRef.current.release();
    setBusy(false);
    setError(null);
    setStreamWarning(null);
    setStreamStatus('disconnected');
    setCommandStatus(null);
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
    setAppliedMutation(null);
    setTools([]);
    setPatches([]); setPatchError(null); setPatchHasMore(false); setPatchOffset(0); setPatchLoading(false);
    setMetrics(null);
  };

  const playSample = (nextScope: Exclude<ExecutionScope, null>) => {
    if (!FIXTURES[mutation]) { setError('No frontend sample exists for this scenario.'); gateRef.current.release(); setBusy(false); return; }
    const baseline = nextScope === 'healing' ? [] : fixtureEvents(mutation, 'baseline');
    const healing = nextScope === 'baseline' ? [] : fixtureEvents(mutation, 'healing');
    const length = Math.max(baseline.length, healing.length);
    let position = 0;
    timerRef.current = window.setInterval(() => {
      position += 1;
      setBaselineEvents(baseline.slice(0, position));
      setHealingEvents(healing.slice(0, position));
      if (position >= length) {
        if (timerRef.current !== null) window.clearInterval(timerRef.current);
        timerRef.current = null;
        gateRef.current.release();
        setBusy(false);
      }
    }, 260);
  };

  const refreshData = async (generation: number) => {
    setPatchLoading(true);
    const [registry, systemMetrics, history] = await Promise.allSettled([api.fetchRegistry(), api.fetchMetrics(), api.fetchPatches()]);
    if (generation !== generationRef.current) return;
    if (registry.status === 'fulfilled') setTools(registry.value.tools); else setTools([]);
    if (systemMetrics.status === 'fulfilled') setMetrics(systemMetrics.value);
    if (history.status === 'fulfilled') {
      setPatches(history.value.repair_history);
      setPatchHasMore(history.value.count === history.value.limit);
      setPatchOffset(history.value.offset + history.value.count);
      setPatchError(history.value.invalid_count ? `${history.value.invalid_count} incomplete patch records were omitted.` : null);
    } else { setPatches([]); setPatchHasMore(false); setPatchOffset(0); setPatchError(history.reason instanceof Error ? history.reason.message : 'Patch history unavailable.'); }
    setPatchLoading(false);
    if (registry.status === 'rejected' || systemMetrics.status === 'rejected')
      setStreamWarning('Live registry or metrics could not be refreshed.');
  };

  const loadMorePatches = async () => {
    if (source !== 'live' || patchLoading || !patchHasMore) return;
    const generation = generationRef.current;
    setPatchLoading(true);
    try {
      const page = await api.fetchPatches(100, patchOffset);
      if (generation !== generationRef.current) return;
      setPatches((previous) => [...previous, ...page.repair_history]);
      setPatchHasMore(page.count === page.limit);
      setPatchOffset(page.offset + page.count);
      setPatchError(page.invalid_count ? `${page.invalid_count} incomplete patch records were omitted from this page.` : null);
    } catch (reason) {
      if (generation === generationRef.current) setPatchError(reason instanceof Error ? reason.message : 'More patch history could not be loaded.');
    } finally { if (generation === generationRef.current) setPatchLoading(false); }
  };

  const runLive = async (action: LiveAction, generation: number) => {
    const side = action === 'healing' ? 'healing' : 'baseline';
    const payload: CreateRunRequest = {
      mode: action === 'normal' ? 'normal' : action,
      execution_type: action === 'normal' ? 'deterministic' : action === 'baseline' ? 'model_driven_baseline' : 'model_driven_healing',
      task: task.trim(), wait: false,
    };
    try {
      const created = await api.createRun(payload);
      if (generation !== generationRef.current) return;
      const updateSummary = side === 'baseline' ? setBaselineSummary : setHealingSummary;
      const updateEvents = side === 'baseline' ? setBaselineEvents : setHealingEvents;
      updateSummary(created);
      let finishing = false;
      let terminalReceived = false;
      const finish = async (eventComplete: boolean) => {
        if (finishing || generation !== generationRef.current) return;
        finishing = true;
        terminalReceived ||= eventComplete;
        if (pollRef.current !== null) window.clearInterval(pollRef.current);
        if (graceRef.current !== null) window.clearTimeout(graceRef.current);
        pollRef.current = graceRef.current = null;
        let closeRun = true;
        try {
          let final = await api.getRun(created.run_id);
          for (let attempt = 0; final.status === 'running' && attempt < 8; attempt += 1) {
            await new Promise((resolve) => window.setTimeout(resolve, 250));
            final = await api.getRun(created.run_id);
          }
          if (generation !== generationRef.current) return;
          updateSummary(final);
          if (final.status === 'running') {
            closeRun = false;
            finishing = false;
            setStreamWarning('Terminal event arrived, but GET /runs still reports running. Continuing status checks.');
            pollRef.current = window.setInterval(() => { void checkStatus(); }, 1000);
            return;
          }
          if (!terminalReceived) setStreamWarning('Final status loaded, but event replay did not reach a complete terminal sequence. Timeline may be incomplete.');
        } catch (reason) {
          if (generation === generationRef.current) setError(reason instanceof Error ? `Final run lookup failed: ${reason.message}` : 'Final run lookup failed.');
        } finally {
          if (generation === generationRef.current && closeRun) {
            streamRef.current?.close(); streamRef.current = null;
            gateRef.current.release(); setBusy(false);
            void refreshData(generation);
          }
        }
      };
      const stream = new RunEventStream(created.run_id, {
        onEvent: (event) => {
          if (generation !== generationRef.current) return;
          updateEvents((previous) => mergeEvents(previous, [event]));
        },
        onStatus: (status) => { if (generation === generationRef.current) setStreamStatus(status); },
        onMalformed: (message) => { if (generation === generationRef.current) setStreamWarning(message); },
        onTerminal: () => { if (generation === generationRef.current) void finish(true); },
      });
      streamRef.current = stream;
      stream.start();
      let checking = false;
      const checkStatus = async () => {
        if (checking || finishing || generation !== generationRef.current) return;
        checking = true;
        try {
          const summary = await api.getRun(created.run_id);
          if (generation !== generationRef.current) return;
          updateSummary(summary);
          if (isTerminal(summary) && graceRef.current === null) {
            graceRef.current = window.setTimeout(() => { void finish(terminalReceived); }, terminalReceived ? 0 : 3000);
          }
        } catch (reason) {
          if (generation === generationRef.current) setStreamWarning(reason instanceof Error ? `Run status check failed: ${reason.message}` : 'Run status check failed.');
        } finally { checking = false; }
      };
      pollRef.current = window.setInterval(() => { void checkStatus(); }, 1000);
      void checkStatus();
    } catch (reason) {
      if (generation !== generationRef.current) return;
      streamRef.current?.close(); streamRef.current = null;
      setError(reason instanceof Error ? reason.message : 'The backend run could not start.');
      gateRef.current.release(); setBusy(false);
      setStreamStatus('disconnected');
    }
  };

  const handleRun = (action: Exclude<ExecutionScope, null> | 'normal') => {
    if (gateRef.current.locked || !task.trim() || (source === 'sample' && (action === 'normal' || task.trim() !== DEFAULT_TASK))) return;
    if (source === 'live' && (action === 'comparison' || appliedMutation !== mutation)) return;
    clearView();
    if (!gateRef.current.acquire()) return;
    setBusy(true);
    setScope(action === 'normal' ? 'baseline' : action);
    if (source === 'sample') playSample(action as Exclude<ExecutionScope, null>);
    else void runLive(action as LiveAction, generationRef.current);
  };

  const handleReset = async () => {
    if (gateRef.current.locked) return;
    if (source === 'sample') { clearView(); setTask(DEFAULT_TASK); setMutation('rename'); return; }
    if (!gateRef.current.acquire()) return;
    setBusy(true); setError(null); setCommandStatus(null);
    const generation = generationRef.current;
    try {
      const result = await api.resetDemo();
      if (generation !== generationRef.current) return;
      if (result.status !== 'reset_completed' || result.mock_api !== 'reset_successful') throw new Error('Backend did not confirm the downstream reset.');
      clearView();
      setMutation('none'); setAppliedMutation('none'); setCommandStatus('Backend and mock reset confirmed. Persistent audit history remains.');
      await refreshData(generationRef.current);
    } catch (reason) {
      if (generation === generationRef.current) { setAppliedMutation(null); setError(reason instanceof Error ? reason.message : 'Backend reset failed.'); }
    } finally { if (generation === generationRef.current) { gateRef.current.release(); setBusy(false); } }
  };

  const handleApplyMutation = async () => {
    if (source !== 'live' || gateRef.current.locked || !gateRef.current.acquire()) return;
    setBusy(true); setError(null); setCommandStatus(null);
    const generation = generationRef.current;
    try {
      const result = await api.setMutation(mutation, mutation !== 'none');
      if (generation !== generationRef.current) return;
      if (result.active_scenario !== mutation || result.enabled !== (mutation !== 'none')) throw new Error('Backend mutation response did not confirm the selected scenario.');
      setAppliedMutation(mutation);
      setCommandStatus(`Backend confirmed ${mutation} scenario ${result.enabled ? 'enabled' : 'disabled'}.`);
    } catch (reason) {
      if (generation === generationRef.current) { setAppliedMutation(null); setError(reason instanceof Error ? reason.message : 'Backend mutation failed. Reset the demo before running.'); }
    } finally { if (generation === generationRef.current) { gateRef.current.release(); setBusy(false); } }
  };

  const handleTaskChange = (value: string) => { if (!gateRef.current.locked) { clearView(); setTask(value); } };
  const handleMutationChange = (value: MutationId) => { if (!gateRef.current.locked) { clearView(); setMutation(value); } };
  const visibleEvents = healingEvents.length ? healingEvents : baselineEvents;
  const currentRunId = healingSummary?.run_id ?? baselineSummary?.run_id ?? visibleEvents[0]?.run_id ?? null;

  return <div className="app-shell">
    <div className="source-bar"><div className="source-inner"><SampleLabel source={source} /><span>{source === 'sample' ? 'Fixture demonstration' : 'Orchestrator data · single runs'}</span></div></div>
    <header className="dashboard-header"><div className="header-inner"><div className="brand"><div className="brand-mark">D<span>G</span></div><div><strong>DriftGuard</strong><small>Adaptive tool registry</small></div></div>
      <nav className="top-nav" aria-label="Dashboard tabs"><button className={tab === 'demo' ? 'active' : ''} onClick={() => setTab('demo')}>Demo</button><button className={tab === 'registry' ? 'active' : ''} onClick={() => setTab('registry')}>Registry & Audit</button><button className={tab === 'benchmark' ? 'active' : ''} onClick={() => setTab('benchmark')}>Benchmark</button></nav>
      <div className="source-switch" aria-label="Data source"><button className={source === 'sample' ? 'active' : ''} onClick={() => changeSource('sample')}>Sample</button><button className={source === 'live' ? 'active' : ''} onClick={() => changeSource('live')}>Live</button></div>
    </div></header>
    <main className="dashboard-main">
      {tab === 'demo' && <DemoView source={source} task={task} mutation={mutation} appliedMutation={appliedMutation} busy={busy} error={error} streamWarning={streamWarning} streamStatus={streamStatus} commandStatus={commandStatus} baselineEvents={baselineEvents} healingEvents={healingEvents} scope={scope} baselineSummary={baselineSummary} healingSummary={healingSummary} patches={patches} tools={tools} patchError={patchError} patchLoading={patchLoading} onTask={handleTaskChange} onMutation={handleMutationChange} onRun={handleRun} onReset={() => { void handleReset(); }} onApplyMutation={() => { void handleApplyMutation(); }} />}
      {tab === 'registry' && <RegistryAuditView source={source} tools={tools} events={visibleEvents} mutation={mutation} error={error} patches={patches} patchError={patchError} patchLoading={patchLoading} patchHasMore={patchHasMore} currentRunId={currentRunId} onLoadMore={() => { void loadMorePatches(); }} />}
      {tab === 'benchmark' && <BenchmarkView source={source} metrics={metrics} mutation={mutation} baselineEvents={baselineEvents} healingEvents={healingEvents} error={error} />}
    </main>
    <footer className="dashboard-footer"><span>DriftGuard / GENESIS 2.0</span><span>Evidence before commitment · independent verification</span></footer>
  </div>;
}
