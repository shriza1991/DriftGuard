"""Mutation manager for Mock API.
Manages active drift scenarios for evaluation and testing.
"""
from typing import Optional, Dict, Any, Tuple
from mock_api.mutations.scenarios import DriftScenario, EvidenceLevel, ScenarioConfig


class MutationManager:
    def __init__(self):
        self._active_config: ScenarioConfig = ScenarioConfig()

    def get_config(self) -> ScenarioConfig:
        return self._active_config

    def set_scenario(
        self,
        scenario: DriftScenario,
        enabled: bool = True,
        evidence_level: EvidenceLevel = EvidenceLevel.ORDINARY
    ) -> ScenarioConfig:
        hints = {
            DriftScenario.RENAME: "Field 'user_id' has been renamed to 'assignee_id'.",
            DriftScenario.TYPE_CHANGE: "Field 'user_id' expects integer, received string.",
            DriftScenario.NESTING: "Field 'user_id' is now nested under 'target.id'.",
            DriftScenario.REQUIRED_HEADER: "Missing required header 'X-Agent-Auth: Bearer secret-agent-token'.",
            DriftScenario.COMBINED: "v2 schema: nested 'target.id' as integer and header 'X-Agent-Auth' required.",
            DriftScenario.MISSING_BUSINESS_VALUE: "Field 'priority_tier' is mandatory and missing.",
            DriftScenario.UNAVAILABLE_CREDENTIAL: "Requires role 'admin-super' token.",
            DriftScenario.AMBIGUOUS_MAPPING: "Ambiguous assignee: specify 'assignee_user_id' or 'supervisor_id'."
        }

        descriptions = {
            DriftScenario.NONE: "Standard v1 API contracts active",
            DriftScenario.RENAME: "Renames parameter 'user_id' to 'assignee_id'",
            DriftScenario.TYPE_CHANGE: "Changes 'user_id' type from string to integer",
            DriftScenario.NESTING: "Moves 'user_id' to nested 'target': {'id': ...}",
            DriftScenario.REQUIRED_HEADER: "Requires 'X-Agent-Auth' authorization header",
            DriftScenario.COMBINED: "Combined nesting, integer conversion, and authorization header",
            DriftScenario.MISSING_BUSINESS_VALUE: "Requires unknown 'priority_tier' parameter",
            DriftScenario.UNAVAILABLE_CREDENTIAL: "Requires unavailable secret credential",
            DriftScenario.AMBIGUOUS_MAPPING: "Presents ambiguous matching targets in error message"
        }

        self._active_config = ScenarioConfig(
            scenario=scenario,
            enabled=enabled,
            evidence_level=evidence_level,
            description=descriptions.get(scenario, "Drift scenario active"),
            migration_hint=hints.get(scenario) if evidence_level == EvidenceLevel.EXPLICIT_HINT else None
        )
        return self._active_config

    def reset(self):
        self._active_config = ScenarioConfig()


mutation_manager = MutationManager()
