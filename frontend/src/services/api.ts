import { RunEvent, ToolRegistryEntry, SystemMetrics, RunSummary } from '../types';

const API_BASE = import.meta.env.VITE_ORCHESTRATOR_URL || 'http://127.0.0.1:8000';

export async function fetchRegistry(): Promise<{ tools: ToolRegistryEntry[]; version: string }> {
  const res = await fetch(`${API_BASE}/registry`);
  if (!res.ok) throw new Error(`Failed to load registry: ${res.statusText}`);
  return res.json();
}

export async function fetchMetrics(): Promise<SystemMetrics> {
  const res = await fetch(`${API_BASE}/metrics`);
  if (!res.ok) throw new Error(`Failed to load metrics: ${res.statusText}`);
  return res.json();
}

export async function fetchPatches(): Promise<any> {
  const res = await fetch(`${API_BASE}/patches`);
  if (!res.ok) throw new Error(`Failed to load patches: ${res.statusText}`);
  return res.json();
}

export async function triggerRun(mode: 'normal' | 'healing', task: string): Promise<RunSummary> {
  const res = await fetch(`${API_BASE}/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, task }),
  });
  if (!res.ok) throw new Error(`Failed to trigger run: ${res.statusText}`);
  return res.json();
}

export async function resetDemo(): Promise<any> {
  const res = await fetch(`${API_BASE}/demo/reset`, { method: 'POST' });
  if (!res.ok) throw new Error(`Failed to reset demo: ${res.statusText}`);
  return res.json();
}

export function subscribeToEvents(
  runId: string,
  onEvent: (event: RunEvent) => void,
  onDone?: () => void
): () => void {
  const eventSource = new EventSource(`${API_BASE}/runs/${runId}/events`);

  eventSource.onmessage = (e) => {
    try {
      const event: RunEvent = JSON.parse(e.data);
      onEvent(event);
      if (event.type === 'run_completed' || event.type === 'run_failed') {
        eventSource.close();
        onDone?.();
      }
    } catch (err) {
      console.error('Error parsing SSE event data:', err);
    }
  };

  eventSource.onerror = (err) => {
    console.warn('SSE event stream closed or interrupted:', err);
    eventSource.close();
    onDone?.();
  };

  return () => {
    eventSource.close();
  };
}
