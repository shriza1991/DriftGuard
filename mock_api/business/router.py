from fastapi import APIRouter, Header, Query, HTTPException, Request, status
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import hashlib
import json

from mock_api.fixtures.initial_data import get_initial_users, get_initial_tickets
from mock_api.config import settings
from mock_api.mutations.manager import mutation_manager
from mock_api.mutations.scenarios import DriftScenario, EvidenceLevel
from mock_api.business.models import (
    User,
    Ticket,
    AssignTicketResponse,
    ValidationErrorResponse,
    AdminMutationRequest,
    AdminStateResponse
)

router = APIRouter()

# In-memory runtime state for Mock API
_users: List[Dict[str, Any]] = get_initial_users()
_tickets: Dict[int, Dict[str, Any]] = get_initial_tickets()
_idempotency_store: Dict[str, Dict[str, Any]] = {}


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_fingerprint(body: Dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validation_error(
    status_code: int,
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    hint: Optional[str] = None,
) -> JSONResponse:
    evidence = dict(details or {})
    evidence["pre_execution_rejection"] = True
    return JSONResponse(
        status_code=status_code,
        content={
            "error": "ValidationFailed",
            "code": code,
            "message": message,
            "details": evidence,
            "hint": hint,
        },
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_bearer(value: Optional[str]) -> bool:
    return value == f"Bearer {settings.assignment_bearer_token}"


@router.get("/users", response_model=List[User])
async def search_users(name: Optional[str] = Query(None, description="Search by user name")):
    """Find user by name (e.g. Maya -> '123')"""
    if not name:
        return [User(**u) for u in _users]
    
    query = name.strip().lower()
    matches = [u for u in _users if query in u["name"].lower()]
    return [User(**u) for u in matches]


@router.post(
    "/tickets/assign",
    responses={
        200: {"model": AssignTicketResponse},
        422: {"model": ValidationErrorResponse},
        400: {"model": ValidationErrorResponse},
        401: {"model": ValidationErrorResponse},
        403: {"model": ValidationErrorResponse},
        409: {"model": ValidationErrorResponse},
        404: {"description": "Ticket or user not found"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Assignment payload. Its shape changes with the active drift scenario.",
                    },
                    "examples": {
                        "normal_v1": {
                            "summary": "Normal v1 / required-header body",
                            "value": {"ticket_id": 81, "user_id": "123"},
                        },
                        "rename": {
                            "summary": "Rename mutation",
                            "value": {"ticket_id": 81, "assignee_id": "123"},
                        },
                        "type_change": {
                            "summary": "Type-change mutation",
                            "value": {"ticket_id": 81, "user_id": 123},
                        },
                        "nesting": {
                            "summary": "Nesting mutation",
                            "value": {"ticket_id": 81, "target": {"id": "123"}},
                        },
                        "combined": {
                            "summary": "Combined mutation (also requires Authorization)",
                            "value": {"ticket_id": 81, "target": {"id": 123}},
                        },
                    },
                }
            },
        }
    },
)
async def assign_ticket(
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """Assign ticket 81 to Maya (user '123').
    Supports structured pre-write validation, idempotency caching, and drift mutations.
    """
    try:
        raw_body = await request.json()
    except Exception:
        return _validation_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_JSON",
            "Malformed JSON payload in request body",
        )

    if not isinstance(raw_body, dict):
        return _validation_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_ROOT_OBJECT",
            "Expected JSON object at root",
            {"expected_schema": {"type": "object"}, "received_type": type(raw_body).__name__},
        )

    # 1. Check idempotency cache first if key provided
    if idempotency_key and idempotency_key in _idempotency_store:
        cached = _idempotency_store[idempotency_key]
        incoming_fingerprint = _request_fingerprint(raw_body)
        if cached["request_fingerprint"] != incoming_fingerprint:
            return _validation_error(
                status.HTTP_409_CONFLICT,
                "IDEMPOTENCY_KEY_REUSE_MISMATCH",
                "Idempotency-Key was already used for a different request payload",
                {"idempotency_key_reused": True},
            )
        response = cached["response"]
        return AssignTicketResponse(
            ticket_id=response["ticket_id"],
            status=response["status"],
            assignee_id=response["assignee_id"],
            updated_at=response["updated_at"],
            idempotent_replayed=True
        )

    # 2. Check active drift mutation
    mut_config = mutation_manager.get_config()
    if mut_config.enabled and mut_config.scenario != DriftScenario.NONE:
        # Validate against mutated expectations
        scen = mut_config.scenario
        hint = mut_config.migration_hint

        if scen == DriftScenario.RENAME:
            if not _is_int(raw_body.get("ticket_id")) or not isinstance(raw_body.get("assignee_id"), str):
                details = {
                    "expected_schema": {"ticket_id": "integer", "assignee_id": "string"},
                    "received_payload": raw_body,
                }
                if mut_config.evidence_level == EvidenceLevel.EXPLICIT_HINT:
                    details["migration_mapping"] = {"user_id": "assignee_id"}
                return _validation_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "SCHEMA_MISMATCH",
                    "Expected ticket_id as integer and assignee_id as string",
                    details,
                    hint,
                )
            target_user_id = raw_body["assignee_id"]

        elif scen == DriftScenario.TYPE_CHANGE:
            if not _is_int(raw_body.get("ticket_id")) or not _is_int(raw_body.get("user_id")):
                return _validation_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "TYPE_MISMATCH",
                    f"Field 'user_id' must be of type integer, received {type(raw_body.get('user_id')).__name__}",
                    {
                        "expected_schema": {"ticket_id": "integer", "user_id": "integer"},
                        "received_payload": raw_body,
                    },
                    hint,
                )
            target_user_id = str(raw_body["user_id"])

        elif scen == DriftScenario.NESTING:
            target_obj = raw_body.get("target")
            if not _is_int(raw_body.get("ticket_id")) or not isinstance(target_obj, dict) or not isinstance(target_obj.get("id"), str):
                return _validation_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "SCHEMA_MISMATCH",
                    "Expected ticket_id as integer and target.id as string",
                    {
                        "expected_schema": {"ticket_id": "integer", "target": {"id": "string"}},
                        "received_payload": raw_body,
                    },
                    hint,
                )
            target_user_id = target_obj["id"]

        elif scen == DriftScenario.REQUIRED_HEADER:
            if not _valid_bearer(authorization):
                return _validation_error(
                    status.HTTP_401_UNAUTHORIZED,
                    "MISSING_OR_INVALID_AUTHORIZATION_HEADER",
                    "Endpoint requires a valid Bearer credential in the Authorization header",
                    {
                        "required_headers": ["Authorization"],
                        "received_headers": [k for k in request.headers.keys() if "auth" in k.lower()],
                    },
                    hint,
                )
            if not _is_int(raw_body.get("ticket_id")) or not isinstance(raw_body.get("user_id"), str):
                return _validation_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "SCHEMA_MISMATCH",
                    "Expected ticket_id as integer and user_id as string",
                    {"expected_schema": {"ticket_id": "integer", "user_id": "string"}, "received_payload": raw_body},
                    hint,
                )
            target_user_id = raw_body["user_id"]

        elif scen == DriftScenario.COMBINED:
            target_obj = raw_body.get("target")
            combined_valid = (
                _valid_bearer(authorization)
                and _is_int(raw_body.get("ticket_id"))
                and isinstance(target_obj, dict)
                and _is_int(target_obj.get("id"))
            )
            if not combined_valid:
                return _validation_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "COMBINED_SCHEMA_MISMATCH",
                    "Combined drift requires Authorization plus ticket_id and target.id as integers",
                    {
                        "expected_schema": {"ticket_id": "integer", "target": {"id": "integer"}},
                        "required_headers": ["Authorization"],
                        "received_payload": raw_body,
                        "authorization_present": authorization is not None,
                    },
                    hint,
                )
            target_user_id = str(target_obj["id"])

        elif scen == DriftScenario.MISSING_BUSINESS_VALUE:
            if (
                not _is_int(raw_body.get("ticket_id"))
                or not isinstance(raw_body.get("user_id"), str)
                or not isinstance(raw_body.get("priority_tier"), str)
                or not raw_body.get("priority_tier", "").strip()
            ):
                return _validation_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "MISSING_BUSINESS_VALUE",
                    "Required business context 'priority_tier' is missing or invalid",
                    {
                        "expected_schema": {"ticket_id": "integer", "user_id": "string", "priority_tier": "string"},
                        "received_payload": raw_body,
                        "missing_business_values": ["priority_tier"] if "priority_tier" not in raw_body else [],
                    },
                    hint,
                )
            target_user_id = raw_body["user_id"]

        elif scen == DriftScenario.UNAVAILABLE_CREDENTIAL:
            return _validation_error(
                status.HTTP_403_FORBIDDEN,
                "UNAVAILABLE_CREDENTIAL",
                "Action requires elevated permission role 'secops-admin' which is unavailable",
                {"required_credential_reference": "secops-admin"},
                hint,
            )

        elif scen == DriftScenario.AMBIGUOUS_MAPPING:
            return _validation_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "AMBIGUOUS_TARGET",
                "Multiple candidate user fields detected: provide 'assignee_user_id' or 'escalation_user_id'",
                {"candidates": ["assignee_user_id", "escalation_user_id"]},
                hint,
            )
        else:
            target_user_id = str(raw_body.get("user_id", ""))
    else:
        # Standard v1 validation
        if "ticket_id" not in raw_body or "user_id" not in raw_body:
            return _validation_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "SCHEMA_MISMATCH",
                "Missing required fields: 'ticket_id' and/or 'user_id'",
                {"expected_schema": {"ticket_id": "integer", "user_id": "string"}, "received_payload": raw_body},
            )

        if not _is_int(raw_body["ticket_id"]) or not isinstance(raw_body["user_id"], str):
            return _validation_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "TYPE_MISMATCH",
                f"Expected ticket_id:int and user_id:str, got ticket_id:{type(raw_body['ticket_id']).__name__} and user_id:{type(raw_body['user_id']).__name__}",
                {"expected_schema": {"ticket_id": "integer", "user_id": "string"}, "received_payload": raw_body},
            )
        target_user_id = raw_body["user_id"]

    # 3. Check entity existence (Pre-write validation)
    ticket_id = raw_body.get("ticket_id")
    if ticket_id not in _tickets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket {ticket_id} not found"
        )

    user_exists = any(u["id"] == str(target_user_id) for u in _users)
    if not user_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{target_user_id}' not found in user directory"
        )

    # 4. Perform write safely
    now_iso = _get_utc_now_iso()
    ticket = _tickets[ticket_id]
    ticket["status"] = "assigned"
    ticket["assignee_id"] = str(target_user_id)
    ticket["history"].append({
        "action": "assigned",
        "assignee_id": str(target_user_id),
        "timestamp": now_iso,
        "idempotency_key": idempotency_key
    })

    response_data = {
        "ticket_id": ticket_id,
        "status": "assigned",
        "assignee_id": str(target_user_id),
        "updated_at": now_iso,
        "idempotent_replayed": False
    }

    # 5. Record idempotency if key present
    if idempotency_key:
        _idempotency_store[idempotency_key] = {
            "request_fingerprint": _request_fingerprint(raw_body),
            "response": response_data,
        }

    return AssignTicketResponse(**response_data)


@router.get("/tickets/{ticket_id}", response_model=Ticket)
async def get_ticket(ticket_id: int):
    """Independent read verification endpoint for ticket status and assignee."""
    if ticket_id not in _tickets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket {ticket_id} not found"
        )
    return Ticket(**_tickets[ticket_id])


# Admin Endpoints (Separated from business endpoints)
@router.post("/admin/reset")
async def admin_reset():
    """Resets mock API database to pristine fixtures and clears active mutations."""
    global _users, _tickets, _idempotency_store
    _users = get_initial_users()
    _tickets = get_initial_tickets()
    _idempotency_store.clear()
    mutation_manager.reset()
    return {
        "status": "reset_successful",
        "timestamp": _get_utc_now_iso(),
        "tickets_reset": len(_tickets),
        "users_reset": len(_users)
    }


@router.post("/admin/mutation")
async def admin_set_mutation(req: AdminMutationRequest):
    """Configure drift mutation scenario on Mock API (Member 1 scope)."""
    try:
        scenario_enum = DriftScenario(req.scenario)
        evidence_enum = EvidenceLevel(req.evidence_level)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scenario or evidence level: {str(e)}"
        )
    
    config = mutation_manager.set_scenario(
        scenario=scenario_enum,
        enabled=req.enabled,
        evidence_level=evidence_enum
    )
    return {
        "active_scenario": config.scenario.value,
        "enabled": config.enabled,
        "evidence_level": config.evidence_level.value,
        "description": config.description,
        "migration_hint": config.migration_hint
    }


@router.get("/admin/state", response_model=AdminStateResponse)
async def admin_get_state():
    """Returns internal mock state.
    WARNING: The orchestrator diagnosis engine MUST NEVER call this endpoint.
    """
    config = mutation_manager.get_config()
    return AdminStateResponse(
        users_count=len(_users),
        tickets={tid: Ticket(**t) for tid, t in _tickets.items()},
        active_mutation={
            "scenario": config.scenario.value,
            "enabled": config.enabled,
            "evidence_level": config.evidence_level.value,
            "description": config.description
        },
        idempotency_records_count=len(_idempotency_store)
    )
