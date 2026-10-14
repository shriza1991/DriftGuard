from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import json
import asyncio
import jsonschema
from pathlib import Path
from orchestrator.models import RunEvent, RunEventType


from orchestrator.client.mock_client import redact_secrets


class EventSubscriber:
    """Subscriber wrapper for an SSE client queue with overflow tracking."""
    def __init__(self, maxsize: int = 100):
        self.queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=maxsize)
        self.overflowed: bool = False


class EventBroadcaster:
    """In-memory event hub managing event history and real-time SSE listener queues."""
    def __init__(self, audit_store: Optional[Any] = None):
        self._events_by_run: Dict[str, List[RunEvent]] = {}
        self._listeners_by_run: Dict[str, List[asyncio.Queue]] = {}
        self._subscribers_by_run: Dict[str, List[EventSubscriber]] = {}
        self._schema: Optional[Dict[str, Any]] = None
        self._audit_store = audit_store
        self._persistence_tasks = {}
        self._persistence_errors = {}
        self._load_schema()

    def set_audit_store(self, audit_store: Any):
        self._audit_store = audit_store

    def _load_schema(self):
        schema_path = Path(__file__).resolve().parent.parent.parent / "contracts" / "events.schema.json"
        if schema_path.exists():
            try:
                with open(schema_path, "r", encoding="utf-8") as f:
                    self._schema = json.load(f)
            except Exception:
                self._schema = None

    def publish_event(
        self,
        run_id: str,
        event_type: str,
        mode: str,
        tool: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None
    ) -> RunEvent:
        if mode == "baseline":
            mode = "normal"
        if run_id not in self._events_by_run:
            self._events_by_run[run_id] = []
        
        sequence = len(self._events_by_run[run_id]) + 1
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_data = redact_secrets(data or {})

        event = RunEvent(
            run_id=run_id,
            sequence=sequence,
            timestamp=now_iso,
            mode=mode,
            type=event_type,
            tool=tool,
            data=clean_data
        )

        # Validate against schema if available
        if self._schema:
            try:
                jsonschema.validate(instance=event.model_dump(), schema=self._schema)
            except jsonschema.ValidationError as err:
                # Log schema validation failure but keep system operating
                pass

        self._events_by_run[run_id].append(event)

        if self._audit_store:
            previous = self._persistence_tasks.get(run_id)
            store = self._audit_store
            async def persist_then_deliver():
                try:
                    if previous is not None:
                        await previous
                    if run_id in self._persistence_errors:
                        raise RuntimeError("Earlier event failed persistence")
                    await store.record_event(run_id=run_id, sequence=sequence, timestamp=now_iso,
                        mode=mode, event_type=event_type, tool=tool, data=clean_data)
                    self._deliver(event)
                except Exception as exc:
                    self._persistence_errors[run_id] = exc
                    try:
                        await store.mark_audit_incomplete(run_id)
                    except Exception:
                        pass
            try:
                self._persistence_tasks[run_id] = asyncio.create_task(persist_then_deliver())
            except RuntimeError:
                self._persistence_errors[run_id] = RuntimeError("No event loop for durable event persistence")
        else:
            self._deliver(event)

        return event

    def _deliver(self, event: RunEvent) -> None:
        for q in self._listeners_by_run.get(event.run_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        for sub in list(self._subscribers_by_run.get(event.run_id, [])):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.overflowed = True

    async def flush(self, run_id: Optional[str] = None) -> None:
        """Wait for serialized durable publication; surface storage failures."""
        keys = [run_id] if run_id is not None else list(self._persistence_tasks)
        for key in keys:
            task = self._persistence_tasks.get(key)
            if task is not None:
                await asyncio.shield(task)
            if key in self._persistence_errors:
                raise RuntimeError("Event audit persistence failed") from self._persistence_errors[key]

    def get_events(self, run_id: str) -> List[RunEvent]:
        return list(self._events_by_run.get(run_id, []))

    def register_subscriber(self, run_id: str, maxsize: int = 100) -> EventSubscriber:
        """Registers a bounded SSE subscriber with overflow tracking."""
        if run_id not in self._subscribers_by_run:
            self._subscribers_by_run[run_id] = []
        sub = EventSubscriber(maxsize=maxsize)
        self._subscribers_by_run[run_id].append(sub)
        return sub

    def unregister_subscriber(self, run_id: str, sub: EventSubscriber):
        """Unregisters an SSE subscriber."""
        if run_id in self._subscribers_by_run:
            if sub in self._subscribers_by_run[run_id]:
                self._subscribers_by_run[run_id].remove(sub)
            if not self._subscribers_by_run[run_id]:
                self._subscribers_by_run.pop(run_id, None)

    def register_listener(self, run_id: str) -> asyncio.Queue:
        """Legacy registration returning an asyncio.Queue."""
        if run_id not in self._listeners_by_run:
            self._listeners_by_run[run_id] = []
        q = asyncio.Queue(maxsize=100)
        self._listeners_by_run[run_id].append(q)
        return q

    def unregister_listener(self, run_id: str, q: asyncio.Queue):
        """Legacy unregistration."""
        if run_id in self._listeners_by_run:
            if q in self._listeners_by_run[run_id]:
                self._listeners_by_run[run_id].remove(q)
            if not self._listeners_by_run[run_id]:
                self._listeners_by_run.pop(run_id, None)

    def reset(self):
        """Resets all in-memory events and listeners."""
        self._events_by_run.clear()
        self._persistence_tasks.clear()
        self._persistence_errors.clear()
        self._listeners_by_run.clear()
        self._subscribers_by_run.clear()


event_broadcaster = EventBroadcaster()
