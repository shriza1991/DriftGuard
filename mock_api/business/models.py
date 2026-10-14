from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field


class User(BaseModel):
    id: str
    name: str
    email: str
    department: Optional[str] = None


class AssignmentHistoryEntry(BaseModel):
    action: str = "assigned"
    assignee_id: str
    timestamp: str
    idempotency_key: Optional[str] = None


class Ticket(BaseModel):
    id: int
    title: str
    status: str
    assignee_id: Optional[str] = None
    history: List[AssignmentHistoryEntry] = Field(default_factory=list)


class AssignTicketRequestV1(BaseModel):
    ticket_id: int
    user_id: str


class TargetObject(BaseModel):
    id: int


class AssignTicketRequestV2Combined(BaseModel):
    ticket_id: int
    target: TargetObject


class AssignTicketResponse(BaseModel):
    ticket_id: int
    status: str
    assignee_id: str
    updated_at: str
    idempotent_replayed: bool = False


class ValidationErrorResponse(BaseModel):
    error: str = "ValidationFailed"
    code: str
    message: str
    # Drift failures intentionally carry different structured evidence shapes
    # (schema, headers, candidate fields). Keep the public error model honest.
    details: Optional[Dict[str, Any]] = None
    hint: Optional[str] = None


class AdminMutationRequest(BaseModel):
    scenario: str
    enabled: bool = True
    evidence_level: str = "ordinary"


class AdminStateResponse(BaseModel):
    users_count: int
    tickets: Dict[int, Ticket]
    active_mutation: Dict[str, Any]
    idempotency_records_count: int
    notification_preferences: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
