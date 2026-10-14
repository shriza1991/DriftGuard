from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
import hashlib
import json


class ToolArgumentSchema(BaseModel):
    type: str = "object"
    properties: Dict[str, Any]
    required: List[str] = []


class ToolRegistryEntry(BaseModel):
    name: str
    version: str = "1.0.0"
    fingerprint: str = ""
    description: str
    endpoint: str
    method: str = "GET"
    headers: Dict[str, str] = {}
    argument_schema: ToolArgumentSchema
    request_mapping: Dict[str, Any] = {}
    response_mapping: Dict[str, Any] = {}
    credential_reference: Optional[str] = None
    status: str = "active"

    def compute_fingerprint(self) -> str:
        """Compute deterministic SHA256 fingerprint for schema and endpoint."""
        payload = {
            "name": self.name,
            "version": self.version,
            "endpoint": self.endpoint,
            "method": self.method,
            "argument_schema": self.argument_schema.model_dump(),
            "request_mapping": self.request_mapping,
            "credential_reference": self.credential_reference
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class RegistryResponse(BaseModel):
    tools: List[ToolRegistryEntry]
    version: str = "v1"
    updated_at: str
