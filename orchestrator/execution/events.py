from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import json
import asyncio
import jsonschema
from pathlib import Path
from orchestrator.models import RunEvent, RunEventType


def redact_secrets(data: Any) -> Any:
    """Recursively redacts sensitive keys such as authorization tokens or API keys."""
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            k_lower = k.lower()
            if any(s in k_lower for s in ["auth", "token", "secret", "password", "key", "credential"]):
                if k_lower.endswith("_tokens") or k_lower in ("tokens", "token_usage") or k_lower.endswith("_token_count"):
                    redacted[k] = redact_secrets(v)
                else:
                    redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_secrets(v)
        return redacted
    elif isinstance(data, list):
        return [redact_secrets(item) for item in data]
    return data


class EventBroadcaster:
    """In-memory event hub managing event history and real-time SSE listener queues."""
    def __init__(self, audit_store: Optional[Any] = None):
        self._events_by_run: Dict[str, List[RunEvent]] = {}
        self._listeners_by_run: Dict[str, List[asyncio.Queue]] = {}
        self._schema: Optional[Dict[str, Any]] = None
        self._audit_store = audit_store
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

        # Notify any active SSE listeners
        if run_id in self._listeners_by_run:
            for q in self._listeners_by_run[run_id]:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        # Persist event to audit store if available
        if self._audit_store:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._audit_store.record_event(
                    run_id=run_id,
                    sequence=sequence,
                    timestamp=now_iso,
                    mode=mode,
                    event_type=event_type,
                    tool=tool,
                    data=clean_data,
                ))
            except RuntimeError:
                pass

        return event

    def get_events(self, run_id: str) -> List[RunEvent]:
        return self._events_by_run.get(run_id, [])

    def register_listener(self, run_id: str) -> asyncio.Queue:
        if run_id not in self._listeners_by_run:
            self._listeners_by_run[run_id] = []
        q = asyncio.Queue(maxsize=100)
        self._listeners_by_run[run_id].append(q)
        return q

    def unregister_listener(self, run_id: str, q: asyncio.Queue):
        if run_id in self._listeners_by_run:
            if q in self._listeners_by_run[run_id]:
                self._listeners_by_run[run_id].remove(q)

    def reset(self):
        self._events_by_run.clear()
        self._listeners_by_run.clear()


event_broadcaster = EventBroadcaster()
