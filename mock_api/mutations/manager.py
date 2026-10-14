"""Mutation manager for Mock API.
Manages active drift scenarios for evaluation and testing.
"""
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
            DriftScenario.RENAME: "Field 'user_id' renamed to 'assignee_id'.",
            DriftScenario.TYPE_CHANGE: "Field 'user_id' expects integer, received string.",
            DriftScenario.NESTING: "Field 'user_id' is now nested under 'target.id'.",
            DriftScenario.REQUIRED_HEADER: "Missing required header 'Authorization' using the configured credential reference.",
            DriftScenario.COMBINED: "Field 'user_id' is now nested under 'target.id' as an integer and header 'Authorization' is required.",
            DriftScenario.MISSING_BUSINESS_VALUE: "Field 'priority_tier' is mandatory and missing.",
            DriftScenario.UNAVAILABLE_CREDENTIAL: "Requires unavailable role 'secops-admin'.",
            DriftScenario.AMBIGUOUS_MAPPING: "Ambiguous assignee: specify 'assignee_user_id' or 'escalation_user_id'."
            ,DriftScenario.AUTHENTICATION_FAILURE: "Authentication failed for the supplied credential."
            ,DriftScenario.PERMISSION_FAILURE: "The caller lacks permission for this operation."
            ,DriftScenario.RATE_LIMIT: "Provider rate limit exceeded."
            ,DriftScenario.PROVIDER_OUTAGE: "Provider is temporarily unavailable."
            ,DriftScenario.MALFORMED_ERROR: "Provider returned adversarial malformed error content."
            ,DriftScenario.VERIFICATION_MISMATCH: "Provider acknowledges the write but exposes mismatched state on verification."
        }

        descriptions = {
            DriftScenario.NONE: "Standard v1 API contracts active",
            DriftScenario.RENAME: "Renames parameter 'user_id' to 'assignee_id'",
            DriftScenario.TYPE_CHANGE: "Changes 'user_id' type from string to integer",
            DriftScenario.NESTING: "Moves 'user_id' to nested 'target': {'id': ...}",
            DriftScenario.REQUIRED_HEADER: "Requires the configured Authorization header",
            DriftScenario.COMBINED: "Combined nesting, integer conversion, and authorization header",
            DriftScenario.MISSING_BUSINESS_VALUE: "Requires unknown 'priority_tier' parameter",
            DriftScenario.UNAVAILABLE_CREDENTIAL: "Requires unavailable secret credential",
            DriftScenario.AMBIGUOUS_MAPPING: "Presents ambiguous matching targets in error message"
            ,DriftScenario.AUTHENTICATION_FAILURE: "Returns a non-drift authentication rejection"
            ,DriftScenario.PERMISSION_FAILURE: "Returns a non-drift permission rejection"
            ,DriftScenario.RATE_LIMIT: "Returns a non-drift rate-limit response"
            ,DriftScenario.PROVIDER_OUTAGE: "Returns a non-drift provider outage"
            ,DriftScenario.MALFORMED_ERROR: "Returns untrusted adversarial error content"
            ,DriftScenario.VERIFICATION_MISMATCH: "Acknowledges a write without applying the intended state"
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
