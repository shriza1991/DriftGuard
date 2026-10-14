import React, { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { Controls } from './components/Controls';
import { Timeline } from './components/Timeline';
import { RegistryView } from './components/RegistryView';
import { ContractDiffView } from './components/ContractDiffView';
import { MetricsView } from './components/MetricsView';
import { fetchRegistry, fetchMetrics, triggerRun, resetDemo, subscribeToEvents } from './services/api';
import { ToolRegistryEntry, SystemMetrics, RunEvent, RunSummary } from './types';

export const App: React.FC = () => {
  const [mode, setMode] = useState<'normal' | 'healing'>('normal');
  const [task, setTask] = useState<string>(
    "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."
  );
  const [tools, setTools] = useState<ToolRegistryEntry[]>([]);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [currentRun, setCurrentRun] = useState<RunSummary | null>(null);
  const [isRunning, setIsRunning] = useState<boolean>(false);
  const [isResetting, setIsResetting] = useState<boolean>(false);

  const loadInitialData = async () => {
    try {
      const regData = await fetchRegistry();
      setTools(regData.tools);
    } catch (err) {
      console.warn('Could not fetch registry (orchestrator may still be starting):', err);
    }

    try {
      const metData = await fetchMetrics();
      setMetrics(metData);
    } catch (err) {
      console.warn('Could not fetch metrics:', err);
    }
  };

  useEffect(() => {
    loadInitialData();
  }, []);

  const handleExecute = async () => {
    if (isRunning) return;
    setIsRunning(true);
    setEvents([]);

    try {
      const run = await triggerRun(mode, task);
      setCurrentRun(run);

      // Subscribe to real-time SSE event stream
      subscribeToEvents(
        run.run_id,
        (event) => {
          setEvents((prev) => {
            if (prev.some((e) => e.sequence === event.sequence && e.run_id === event.run_id)) {
              return prev;
            }
            return [...prev, event];
          });
        },
        () => {
          setIsRunning(false);
          fetchMetrics().then(setMetrics).catch(console.error);
        }
      );
    } catch (err) {
      console.error('Execution failed:', err);
      setIsRunning(false);
    }
  };

  const handleReset = async () => {
    if (isResetting || isRunning) return;
    setIsResetting(true);
    try {
      await resetDemo();
      setEvents([]);
      setCurrentRun(null);
      await loadInitialData();
    } catch (err) {
      console.error('Reset failed:', err);
    } finally {
      setIsResetting(false);
    }
  };

  return (
    <>
      <Header onReset={handleReset} isResetting={isResetting} />

      <main className="main-content">
        <Controls
          mode={mode}
          setMode={setMode}
          task={task}
          setTask={setTask}
          onExecute={handleExecute}
          isRunning={isRunning}
        />

        <MetricsView metrics={metrics} />

        <div className="grid-two-col">
          <Timeline events={events} currentRun={currentRun} />
          <ContractDiffView />
        </div>

        <RegistryView tools={tools} />
      </main>

      <footer className="app-footer">
        <p>
          DriftGuard &bull; Genesis Hackathon PS01 Selection Prototype &bull; Autonomous Tool Registry & Healing Pipeline
        </p>
      </footer>
    </>
  );
};

export default App;
