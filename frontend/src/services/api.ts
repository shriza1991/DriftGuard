import type { CreateRunRequest, EvidenceLevel, MutationResponse, PatchPage, ResetResponse, RunSummary, SystemMetrics, ToolRegistryEntry } from '../types';
import { parsePatchPage } from '../state/patchState.ts';
import { parseSystemMetrics } from '../state/metricsState.ts';

const configuredBase = import.meta.env?.VITE_ORCHESTRATOR_URL;
const DEFAULT_BASE = configuredBase ? configuredBase.replace(/\/$/, '') : '/api';

export class ApiError extends Error {
  constructor(public status: number | null, message: string) { super(message); }
}

export class DriftGuardApiClient {
  // Wrap the browser function so invoking this.request does not rebind native fetch to the client instance.
  constructor(public readonly base = DEFAULT_BASE, private readonly request: typeof fetch = (input, init) => fetch(input, init)) {}

  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try { response = await this.request(`${this.base}${path}`, init); }
    catch { throw new ApiError(null, 'Cannot connect to the orchestrator. Start the backend and check the frontend proxy or VITE_ORCHESTRATOR_URL.'); }
    let body: unknown;
    try { body = await response.json(); } catch { body = null; }
    if (!response.ok) {
      const detail = body && typeof body === 'object' && 'detail' in body ? String(body.detail) : response.statusText;
      const message = response.status === 409 ? 'Another backend operation is active.'
        : response.status === 502 ? 'Downstream service unavailable or demo state unknown; reset the backend.'
        : response.status >= 500 ? 'Backend request failed.' : detail || 'Backend request failed.';
      throw new ApiError(response.status, `${response.status} ${message}`);
    }
    if (!body || typeof body !== 'object') throw new ApiError(response.status, 'Backend returned an invalid JSON response.');
    return body as T;
  }

  async createRun(payload: CreateRunRequest): Promise<RunSummary> {
    const run = await this.json<RunSummary>('/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (typeof run.run_id !== 'string' || !run.run_id || !['running', 'completed', 'failed', 'interrupted'].includes(run.status))
      throw new ApiError(null, 'Backend returned an invalid run identifier or status.');
    return run;
  }
  async getRun(runId: string): Promise<RunSummary> {
    const run = await this.json<RunSummary>(`/runs/${encodeURIComponent(runId)}`);
    if (run.run_id !== runId || !['running', 'completed', 'failed', 'interrupted'].includes(run.status))
      throw new ApiError(null, 'Backend returned a mismatched or invalid run summary.');
    return run;
  }
  eventUrl(runId: string, cursor?: number): string { return `${this.base}/runs/${encodeURIComponent(runId)}/events${cursor === undefined ? '' : `?cursor=${cursor}`}`; }
  resetDemo(): Promise<ResetResponse> { return this.json('/demo/reset', { method: 'POST' }); }
  setMutation(scenario: string, enabled = true, evidenceLevel: EvidenceLevel = 'ordinary'): Promise<MutationResponse> {
    return this.json('/demo/mutation', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenario, enabled, evidence_level: evidenceLevel }) });
  }
  async fetchRegistry(): Promise<{ tools: ToolRegistryEntry[]; version: string }> {
    const response = await this.json<{ tools: ToolRegistryEntry[]; version: string }>('/registry');
    if (!Array.isArray(response.tools) || typeof response.version !== 'string' || response.tools.some((tool) =>
      !tool || typeof tool.name !== 'string' || typeof tool.version !== 'string' || typeof tool.fingerprint !== 'string' ||
      typeof tool.method !== 'string' || typeof tool.endpoint !== 'string' || !tool.argument_schema ||
      !tool.argument_schema.properties || typeof tool.argument_schema.properties !== 'object'))
      throw new ApiError(null, 'Backend returned an invalid active registry response.');
    return response;
  }
  async fetchPatches(limit = 100, offset = 0): Promise<PatchPage> {
    return parsePatchPage(await this.json(`/patches?limit=${limit}&offset=${offset}`));
  }
  async fetchMetrics(): Promise<SystemMetrics> { return parseSystemMetrics(await this.json('/metrics')); }
}

export const api = new DriftGuardApiClient();
