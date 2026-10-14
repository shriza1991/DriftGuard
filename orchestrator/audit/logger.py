from datetime import datetime, timezone
from typing import Dict, Any, List
from pydantic import BaseModel


class AuditEntry(BaseModel):
    timestamp: str
    action: str
    actor: str
    details: Dict[str, Any]


class AuditLogger:
    def __init__(self):
        self._entries: List[AuditEntry] = []

    def log(self, action: str, actor: str, details: Dict[str, Any]):
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            actor=actor,
            details=details
        )
        self._entries.append(entry)

    def get_entries(self) -> List[AuditEntry]:
        return self._entries

    def reset(self):
        self._entries.clear()


audit_logger = AuditLogger()
