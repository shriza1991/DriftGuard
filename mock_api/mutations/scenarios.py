"""Scenarios definition for API drift mutations.

Member 1 Domain:
Supports 8 canonical drift scenarios:
1. rename: 'user_id' renamed to 'assignee_id'
2. type_change: 'ticket_id' or 'user_id' expects integer vs string
3. nesting: 'user_id' moved inside nested 'target': {'id': ...}
4. required_header: configured 'Authorization' credential required
5. combined: nesting + type_change + required_header
6. missing_business_value: requires a field not present in initial agent prompt (e.g. 'priority_tier')
7. unavailable_credential: requires a credential token not available in vault
8. ambiguous_mapping: multiple ambiguous candidates (e.g. 'assignee_user_id' vs 'owner_user_id')

Distinguishes two evidence levels:
- ORDINARY: Standard HTTP 400/422 validation response with field error
- EXPLICIT_HINT: Includes migration notice or deprecation warning in body/headers
"""
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel


class DriftScenario(str, Enum):
    NONE = "none"
    RENAME = "rename"
    TYPE_CHANGE = "type_change"
    NESTING = "nesting"
    REQUIRED_HEADER = "required_header"
    COMBINED = "combined"
    MISSING_BUSINESS_VALUE = "missing_business_value"
    UNAVAILABLE_CREDENTIAL = "unavailable_credential"
    AMBIGUOUS_MAPPING = "ambiguous_mapping"


class EvidenceLevel(str, Enum):
    ORDINARY = "ordinary"          # Standard validation error only
    EXPLICIT_HINT = "explicit_hint"  # Includes migration guidance/deprecation hint


class ScenarioConfig(BaseModel):
    scenario: DriftScenario = DriftScenario.NONE
    enabled: bool = False
    evidence_level: EvidenceLevel = EvidenceLevel.ORDINARY
    description: str = "Standard v1 API contracts active"
    migration_hint: Optional[str] = None
