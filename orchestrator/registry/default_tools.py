from typing import Dict
from orchestrator.registry.models import ToolRegistryEntry, ToolArgumentSchema


def create_default_registry() -> Dict[str, ToolRegistryEntry]:
    """Generates the frozen v1 registry entries for the 3 target tools."""
    find_user = ToolRegistryEntry(
        name="find_user",
        version="1.0.0",
        description="Search for a user by name in the corporate directory to resolve their user ID.",
        endpoint="/users",
        method="GET",
        headers={"Accept": "application/json"},
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={
                "name": {
                    "type": "string",
                    "description": "Full or partial name of user to find (e.g. 'Maya')"
                }
            },
            required=["name"]
        ),
        request_mapping={
            "query_params": {"name": "{name}"}
        },
        response_mapping={
            "user_id": "$[0].id",
            "name": "$[0].name",
            "email": "$[0].email"
        },
        credential_reference=None,
        status="active"
    )
    find_user.fingerprint = find_user.compute_fingerprint()

    assign_ticket = ToolRegistryEntry(
        name="assign_ticket",
        version="1.0.0",
        description="Assigns a support/security ticket to a user ID via standard v1 contract.",
        endpoint="/tickets/assign",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={
                "ticket_id": {
                    "type": "integer",
                    "description": "Numeric identifier of ticket (e.g. 81)"
                },
                "user_id": {
                    "type": "string",
                    "description": "User identifier resolved from directory (e.g. '123')"
                }
            },
            required=["ticket_id", "user_id"]
        ),
        request_mapping={
            "body": {
                "ticket_id": "{ticket_id}",
                "user_id": "{user_id}"
            }
        },
        response_mapping={
            "ticket_id": "$.ticket_id",
            "status": "$.status",
            "assignee_id": "$.assignee_id"
        },
        credential_reference=None,
        status="active"
    )
    assign_ticket.fingerprint = assign_ticket.compute_fingerprint()

    get_ticket = ToolRegistryEntry(
        name="get_ticket",
        version="1.0.0",
        description="Retrieves ticket details and assignee for independent post-assignment verification.",
        endpoint="/tickets/{ticket_id}",
        method="GET",
        headers={"Accept": "application/json"},
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={
                "ticket_id": {
                    "type": "integer",
                    "description": "Numeric identifier of ticket to verify"
                }
            },
            required=["ticket_id"]
        ),
        request_mapping={
            "path_params": {"ticket_id": "{ticket_id}"}
        },
        response_mapping={
            "ticket_id": "$.id",
            "status": "$.status",
            "assignee_id": "$.assignee_id",
            "history": "$.history"
        },
        credential_reference=None,
        status="active"
    )
    get_ticket.fingerprint = get_ticket.compute_fingerprint()

    return {
        "find_user": find_user,
        "assign_ticket": assign_ticket,
        "get_ticket": get_ticket
    }
