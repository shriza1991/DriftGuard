"""Initial fixture data for the Mock API service.

Deterministic state:
- Maya: ID "123", email "maya@example.com"
- Ticket 81: Unassigned open ticket
"""
from typing import Dict, Any, List
import copy

INITIAL_USERS: List[Dict[str, Any]] = [
    {
        "id": "123",
        "name": "Maya",
        "email": "maya@example.com",
        "department": "Security Operations"
    },
    {
        "id": "456",
        "name": "Alex",
        "email": "alex@example.com",
        "department": "Platform Engineering"
    }
]

INITIAL_TICKETS: Dict[int, Dict[str, Any]] = {
    81: {
        "id": 81,
        "title": "System access drift alert",
        "status": "open",
        "assignee_id": None,
        "history": []
    },
    82: {
        "id": 82,
        "title": "Unmonitored tool endpoint detected",
        "status": "open",
        "assignee_id": None,
        "history": []
    }
}


def get_initial_users() -> List[Dict[str, Any]]:
    return copy.deepcopy(INITIAL_USERS)


def get_initial_tickets() -> Dict[int, Dict[str, Any]]:
    return copy.deepcopy(INITIAL_TICKETS)


INITIAL_NOTIFICATION_PREFERENCES: Dict[str, Dict[str, Any]] = {
    "123": {"user_id": "123", "promo": True, "updated_at": None, "history": []},
    "456": {"user_id": "456", "promo": True, "updated_at": None, "history": []},
}


def get_initial_notification_preferences() -> Dict[str, Dict[str, Any]]:
    return copy.deepcopy(INITIAL_NOTIFICATION_PREFERENCES)
