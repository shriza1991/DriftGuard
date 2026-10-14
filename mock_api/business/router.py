from fastapi import APIRouter, Header, Query, HTTPException, Request, status
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from mock_api.fixtures.initial_data import get_initial_users, get_initial_tickets
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


@router.get("/users", response_model=List[User])
async def search_users(name: Optional[str] = Query(None, description="Search by user name")):
    """Find user by name (e.g. Maya -> '123')"""
    if not name:
        return [User(**u) for u in _users]
    
    query = name.strip().lower()
    matches = [u for u in _users if query in u["name"].lower()]
    return [User(**u) for u in matches]


@router.post("/tickets/assign", responses={
    200: {"model": AssignTicketResponse},
    422: {"model": ValidationErrorResponse},
    400: {"model": ValidationErrorResponse},
    404: {"description": "Ticket or user not found"}
})
async def assign_ticket(
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    api_version: Optional[str] = Header(None, alias="X-API-Version")
):
    """Assign ticket 81 to Maya (user '123').
    Supports structured pre-write validation, idempotency caching, and drift mutations.
    """
    try:
        raw_body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "ValidationFailed",
                "code": "INVALID_JSON",
                "message": "Malformed JSON payload in request body",
                "details": None,
                "hint": None
            }
        )

    # 1. Check idempotency cache first if key provided
    if idempotency_key and idempotency_key in _idempotency_store:
        cached = _idempotency_store[idempotency_key]
        return AssignTicketResponse(
            ticket_id=cached["ticket_id"],
            status=cached["status"],
            assignee_id=cached["assignee_id"],
            updated_at=cached["updated_at"],
            idempotent_replayed=True
        )

    # 2. Check active drift mutation
    mut_config = mutation_manager.get_config()
    if mut_config.enabled and mut_config.scenario != DriftScenario.NONE:
        # Validate against mutated expectations
        scen = mut_config.scenario
        hint = mut_config.migration_hint

        if scen == DriftScenario.RENAME:
            if "assignee_id" not in raw_body:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "error": "ValidationFailed",
                        "code": "MISSING_REQUIRED_FIELD",
                        "message": "Field 'assignee_id' is required. Received unexpected field(s): " + ", ".join(raw_body.keys()),
                        "details": {
                            "expected_schema": {"ticket_id": "integer", "assignee_id": "string"},
                            "received_payload": raw_body
                        },
                        "hint": hint
                    }
                )
            target_user_id = str(raw_body["assignee_id"])

        elif scen == DriftScenario.TYPE_CHANGE:
            if "user_id" not in raw_body or not isinstance(raw_body["user_id"], int):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "error": "ValidationFailed",
                        "code": "TYPE_MISMATCH",
                        "message": f"Field 'user_id' must be of type integer, received {type(raw_body.get('user_id')).__name__}",
                        "details": {
                            "expected_schema": {"ticket_id": "integer", "user_id": "integer"},
                            "received_payload": raw_body
                        },
                        "hint": hint
                    }
                )
            target_user_id = str(raw_body["user_id"])

        elif scen == DriftScenario.NESTING:
            target_obj = raw_body.get("target")
            if not isinstance(target_obj, dict) or "id" not in target_obj:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "error": "ValidationFailed",
                        "code": "SCHEMA_MISMATCH",
                        "message": "Expected object 'target' containing 'id'",
                        "details": {
                            "expected_schema": {"ticket_id": "integer", "target": {"id": "string"}},
                            "received_payload": raw_body
                        },
                        "hint": hint
                    }
                )
            target_user_id = str(target_obj["id"])

        elif scen == DriftScenario.REQUIRED_HEADER:
            if not authorization or "Bearer" not in authorization:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": "ValidationFailed",
                        "code": "MISSING_AUTHORIZATION_HEADER",
                        "message": "Endpoint requires valid 'Authorization: Bearer <token>' header",
                        "details": {
                            "expected_headers": ["Authorization"],
                            "received_headers": [k for k in request.headers.keys() if "auth" in k.lower()]
                        },
                        "hint": hint
                    }
                )
            target_user_id = str(raw_body.get("user_id", ""))

        elif scen == DriftScenario.COMBINED:
            # Requires nesting: target.id (as int) and authorization header
            if not authorization:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": "ValidationFailed",
                        "code": "MISSING_AUTHORIZATION_HEADER",
                        "message": "Combined drift: missing required 'Authorization' header",
                        "details": {},
                        "hint": hint
                    }
                )
            target_obj = raw_body.get("target")
            if not isinstance(target_obj, dict) or not isinstance(target_obj.get("id"), int):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "error": "ValidationFailed",
                        "code": "COMBINED_SCHEMA_MISMATCH",
                        "message": "Combined drift: expected target.id as integer",
                        "details": {
                            "expected_schema": {"ticket_id": "integer", "target": {"id": "integer"}},
                            "received_payload": raw_body
                        },
                        "hint": hint
                    }
                )
            target_user_id = str(target_obj["id"])

        elif scen == DriftScenario.MISSING_BUSINESS_VALUE:
            if "priority_tier" not in raw_body:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "error": "ValidationFailed",
                        "code": "MISSING_BUSINESS_VALUE",
                        "message": "Required business context 'priority_tier' missing from payload",
                        "details": {
                            "expected_schema": {"ticket_id": "integer", "user_id": "string", "priority_tier": "string"},
                            "received_payload": raw_body
                        },
                        "hint": hint
                    }
                )
            target_user_id = str(raw_body.get("user_id", ""))

        elif scen == DriftScenario.UNAVAILABLE_CREDENTIAL:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "ValidationFailed",
                    "code": "UNAVAILABLE_CREDENTIAL",
                    "message": "Action requires elevated permission role 'secops-admin' not granted to standard token",
                    "details": {},
                    "hint": hint
                }
            )

        elif scen == DriftScenario.AMBIGUOUS_MAPPING:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": "ValidationFailed",
                    "code": "AMBIGUOUS_TARGET",
                    "message": "Multiple candidate user fields detected: provide 'assignee_user_id' or 'escalation_user_id'",
                    "details": {
                        "candidates": ["assignee_user_id", "escalation_user_id"]
                    },
                    "hint": hint
                }
            )
        else:
            target_user_id = str(raw_body.get("user_id", ""))
    else:
        # Standard v1 validation
        if not isinstance(raw_body, dict):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": "ValidationFailed",
                    "code": "INVALID_ROOT_OBJECT",
                    "message": "Expected JSON object at root",
                    "details": None,
                    "hint": None
                }
            )

        if "ticket_id" not in raw_body or "user_id" not in raw_body:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": "ValidationFailed",
                    "code": "SCHEMA_MISMATCH",
                    "message": "Missing required fields: 'ticket_id' and 'user_id'",
                    "details": {
                        "expected_schema": {"ticket_id": "integer", "user_id": "string"},
                        "received_payload": raw_body
                    },
                    "hint": None
                }
            )

        if not isinstance(raw_body["ticket_id"], int) or not isinstance(raw_body["user_id"], str):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": "ValidationFailed",
                    "code": "TYPE_MISMATCH",
                    "message": f"Expected ticket_id:int and user_id:str, got ticket_id:{type(raw_body['ticket_id']).__name__} and user_id:{type(raw_body['user_id']).__name__}",
                    "details": {
                        "expected_schema": {"ticket_id": "integer", "user_id": "string"},
                        "received_payload": raw_body
                    },
                    "hint": None
                }
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
        _idempotency_store[idempotency_key] = response_data

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
