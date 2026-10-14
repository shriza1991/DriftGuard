import type { RunEvent, RunEventType } from '../types';
import { api, type DriftGuardApiClient } from './api.ts';

export type StreamStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected';
const eventTypes = new Set<RunEventType>([
  'run_started', 'tool_call_started', 'tool_call_failed', 'diagnosis_started',
  'patch_proposed', 'patch_validated', 'patch_rejected', 'retry_started',
  'recovery_verified', 'registry_committed', 'run_completed', 'run_failed',
]);

export function parseRunEvent(raw: string, runId: string): RunEvent | null {
  let value: unknown;
  try { value = JSON.parse(raw); } catch { return null; }
  if (!value || typeof value !== 'object') return null;
  const event = value as Partial<RunEvent>;
  if (event.run_id !== runId || !Number.isSafeInteger(event.sequence) || (event.sequence ?? 0) < 1 ||
      !eventTypes.has(event.type as RunEventType) || typeof event.timestamp !== 'string' ||
      (event.mode !== 'normal' && event.mode !== 'healing') ||
      (event.tool !== null && typeof event.tool !== 'string') ||
      !event.data || typeof event.data !== 'object' || Array.isArray(event.data)) return null;
  return event as RunEvent;
}

export interface StreamHandlers {
  onEvent: (event: RunEvent) => void;
  onStatus: (status: StreamStatus) => void;
  onMalformed: (message: string) => void;
  onTerminal: () => void;
}

export class RunEventStream {
  private source: EventSource | null = null;
  private closed = false;
  private seen = new Set<number>();
  private highest = 0;
  private replayAttempts = 0;
  private replayTimer: ReturnType<typeof setTimeout> | null = null;
  private terminal = false;
  private terminalSequence: number | null = null;

  constructor(private readonly runId: string, private readonly handlers: StreamHandlers,
    private readonly client: DriftGuardApiClient = api,
    private readonly sourceFactory: (url: string) => EventSource = (url) => new EventSource(url)) {}

  start() { this.open(false); }

  private open(reconnecting: boolean) {
    if (this.closed) return;
    this.handlers.onStatus(reconnecting ? 'reconnecting' : 'connecting');
    // No query cursor: native EventSource can send Last-Event-ID without conflicting with a query cursor.
    const source = this.sourceFactory(this.client.eventUrl(this.runId));
    this.source = source;
    source.onopen = () => { if (!this.closed && this.source === source) this.handlers.onStatus('connected'); };
    source.onmessage = (message) => {
      if (this.closed || this.source !== source) return;
      const event = parseRunEvent(message.data, this.runId);
      if (!event || (message.lastEventId && Number(message.lastEventId) !== event.sequence)) {
        this.handlers.onMalformed('A malformed or mismatched backend event was ignored.');
        return;
      }
      if (this.seen.has(event.sequence)) return;
      if (event.sequence > this.highest + 1 && this.replayAttempts < 2) this.scheduleReplay();
      this.seen.add(event.sequence);
      this.highest = Math.max(this.highest, event.sequence);
      this.handlers.onEvent(event);
      if (event.type === 'run_completed' || event.type === 'run_failed') this.terminalSequence = event.sequence;
      if (!this.terminal && this.terminalSequence !== null &&
          Array.from({ length: this.terminalSequence }, (_, index) => index + 1).every((number) => this.seen.has(number))) {
        this.terminal = true;
        this.handlers.onTerminal();
      }
    };
    source.addEventListener('error', (message) => {
      if (this.closed || this.source !== source) return;
      if (message instanceof MessageEvent && message.data) {
        this.handlers.onMalformed(`Backend event stream error: ${String(message.data)}`);
        this.scheduleReplay();
      } else if (!this.terminal) {
        this.handlers.onStatus(source.readyState === 2 ? 'disconnected' : 'reconnecting');
        if (source.readyState === 2) this.scheduleReplay();
      }
    });
  }

  private scheduleReplay() {
    if (this.closed || this.replayTimer !== null || this.replayAttempts >= 2) return;
    this.replayAttempts += 1;
    this.handlers.onStatus('reconnecting');
    this.replayTimer = setTimeout(() => {
      this.replayTimer = null;
      this.source?.close();
      // Full durable replay can fill a gap; (run_id, sequence) dedupes existing events.
      this.open(true);
    }, 250);
  }

  close() {
    this.closed = true;
    if (this.replayTimer !== null) clearTimeout(this.replayTimer);
    this.source?.close();
    this.source = null;
    this.handlers.onStatus('disconnected');
  }
}
