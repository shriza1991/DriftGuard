import test from 'node:test';
import assert from 'node:assert/strict';
import { DriftGuardApiClient } from '../src/services/api.ts';
import { RunEventStream, parseRunEvent } from '../src/services/eventStream.ts';
import { mergeEvents, summaryOutcome } from '../src/state/runState.ts';
import { CommandGate } from '../src/state/commandGate.ts';
import type { RunEvent, RunSummary } from '../src/types/index.ts';

const runId = 'run-one';
const event = (sequence: number, type: RunEvent['type'] = 'tool_call_started'): RunEvent => ({
  run_id: runId, sequence, type, timestamp: '2026-10-14T00:00:00Z', mode: 'normal', tool: null, data: {},
});

class FakeSource {
  static sources: FakeSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((message: MessageEvent) => void) | null = null;
  error: ((message: Event) => void) | null = null;
  readyState = 1;
  closed = false;
  constructor(readonly url: string) { FakeSource.sources.push(this); }
  addEventListener(name: string, callback: EventListenerOrEventListenerObject) {
    if (name === 'error') this.error = callback as (message: Event) => void;
  }
  emit(item: RunEvent) { this.onmessage?.({ data: JSON.stringify(item), lastEventId: String(item.sequence) } as MessageEvent); }
  close() { this.closed = true; this.readyState = 2; }
}

test('API client uses prompt run creation, final lookup, mutation and proxy paths', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const request = async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return { ok: true, status: 200, json: async () => ({ run_id: runId, status: 'running', active_scenario: 'rename', enabled: true }) } as Response;
  };
  const client = new DriftGuardApiClient('/api', request);
  await client.createRun({ mode: 'healing', execution_type: 'model_driven_healing', task: 'task', wait: false });
  await client.getRun(runId);
  await client.setMutation('rename');
  await client.setMutation('rename', true, 'explicit_hint');
  await client.resetDemo();
  assert.deepEqual(calls.map(({ url }) => url), ['/api/runs', '/api/runs/run-one', '/api/demo/mutation', '/api/demo/mutation', '/api/demo/reset']);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { mode: 'healing', execution_type: 'model_driven_healing', task: 'task', wait: false });
  assert.deepEqual(JSON.parse(String(calls[2].init?.body)), { scenario: 'rename', enabled: true, evidence_level: 'ordinary' });
  assert.deepEqual(JSON.parse(String(calls[3].init?.body)), { scenario: 'rename', enabled: true, evidence_level: 'explicit_hint' });
});

test('SSE parser rejects wrong run, malformed JSON and invalid sequence', () => {
  assert.equal(parseRunEvent('{', runId), null);
  assert.equal(parseRunEvent(JSON.stringify({ ...event(1), run_id: 'other' }), runId), null);
  assert.equal(parseRunEvent(JSON.stringify(event(0)), runId), null);
  assert.equal(parseRunEvent(JSON.stringify(event(1)), runId)?.type, 'tool_call_started');
});

test('stream reconnects and deduplicates replay while terminal waits for sequence gap', async () => {
  FakeSource.sources = [];
  const received: RunEvent[] = [];
  const statuses: string[] = [];
  let terminals = 0;
  const client = new DriftGuardApiClient('/api');
  const stream = new RunEventStream(runId, {
    onEvent: (item) => received.push(item), onStatus: (status) => statuses.push(status),
    onMalformed: () => assert.fail('unexpected malformed event'), onTerminal: () => { terminals += 1; },
  }, client, (url) => new FakeSource(url) as unknown as EventSource);
  stream.start();
  const first = FakeSource.sources[0];
  assert.equal(first.url, '/api/runs/run-one/events');
  first.onopen?.();
  first.emit(event(1, 'run_started'));
  first.emit(event(3, 'run_completed'));
  assert.equal(terminals, 0);
  await new Promise((resolve) => setTimeout(resolve, 350));
  const replay = FakeSource.sources[1];
  assert.equal(first.closed, true);
  replay.emit(event(1, 'run_started'));
  replay.emit(event(2));
  replay.emit(event(3, 'run_completed'));
  assert.deepEqual(mergeEvents([], received).map((item) => item.sequence), [1, 2, 3]);
  assert.deepEqual(received.map((item) => item.sequence), [1, 3, 2]);
  assert.equal(terminals, 1);
  assert.ok(statuses.includes('reconnecting'));
  stream.close();
  assert.equal(replay.closed, true);
});

test('closed old stream ignores events after run switching', () => {
  FakeSource.sources = [];
  let received = 0;
  const stream = new RunEventStream(runId, { onEvent: () => { received += 1; }, onStatus: () => {}, onMalformed: () => {}, onTerminal: () => {} },
    new DriftGuardApiClient('/api'), (url) => new FakeSource(url) as unknown as EventSource);
  stream.start();
  const old = FakeSource.sources[0];
  stream.close();
  old.emit(event(1));
  assert.equal(received, 0);
});

test('transport errors show reconnecting without creating a business failure', async () => {
  FakeSource.sources = [];
  const statuses: string[] = [];
  const stream = new RunEventStream(runId, { onEvent: () => {}, onStatus: (status) => statuses.push(status), onMalformed: () => {}, onTerminal: () => {} },
    new DriftGuardApiClient('/api'), (url) => new FakeSource(url) as unknown as EventSource);
  stream.start();
  const first = FakeSource.sources[0];
  first.readyState = 0;
  first.error?.(new Event('error'));
  assert.equal(statuses.at(-1), 'reconnecting');
  first.onopen?.();
  assert.equal(statuses.at(-1), 'connected');
  first.readyState = 2;
  first.error?.(new Event('error'));
  await new Promise((resolve) => setTimeout(resolve, 350));
  assert.equal(first.closed, true);
  assert.equal(FakeSource.sources.length, 2);
  stream.close();
});

test('authoritative summary controls live success classification', () => {
  const summary = { status: 'completed', outcome: { downstream_verified: true } } as RunSummary;
  assert.equal(summaryOutcome(summary, [event(1, 'run_failed')]), 'verified');
  assert.equal(summaryOutcome({ ...summary, outcome: null }, [event(1, 'run_completed')]), 'completed_unverified');
  assert.equal(summaryOutcome({ ...summary, status: 'failed', outcome: null }, [event(1, 'run_completed')]), 'failed');
});

test('two immediate command attempts admit only one before release', () => {
  const gate = new CommandGate();
  assert.equal(gate.acquire(), true);
  assert.equal(gate.acquire(), false);
  assert.equal(gate.locked, true);
  gate.release();
  assert.equal(gate.acquire(), true);
});
