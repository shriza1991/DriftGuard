"""Configuration compatibility and safe provider error reporting."""

from orchestrator.agents.model_provider import provider_error_summary
from orchestrator.config import OrchestratorSettings
from orchestrator.healing.llm_diagnosis import build_evidence_packet
from orchestrator.models.observation import ExecutionPhase, FailureObservation
from orchestrator.registry import VersionedToolRegistry


def test_legacy_model_env_names_load_without_exposing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_MODEL_NAME", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_MODEL_API_KEY", raising=False)
    config = tmp_path / "local.env"
    config.write_text("MODEL_PROVIDER=gemini\nMODEL_NAME=gemini-test\nMODEL_API_KEY=test-only-placeholder\n")
    settings = OrchestratorSettings(_env_file=config)
    assert settings.model_provider == "gemini"
    assert settings.model_name == "gemini-test"
    assert settings.model_api_key == "test-only-placeholder"
    assert "test-only-placeholder" not in repr(settings)


def test_documented_names_take_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_MODEL_PROVIDER", raising=False)
    config = tmp_path / "local.env"
    config.write_text("MODEL_PROVIDER=mock\nORCHESTRATOR_MODEL_PROVIDER=gemini\n")
    assert OrchestratorSettings(_env_file=config).model_provider == "gemini"


def test_provider_failure_summary_never_copies_exception_body():
    error = RuntimeError("request failed at https://provider.invalid/?key=test-only-placeholder")
    summary = provider_error_summary(error)
    assert summary == "Model provider request failed (provider error)."
    assert "test-only-placeholder" not in summary


def test_diagnosis_evidence_accepts_string_error_body():
    tool = VersionedToolRegistry.create_default().get("assign_ticket")
    observation = FailureObservation(
        tool_name="assign_ticket", execution_phase=ExecutionPhase.HTTP_DISPATCH,
        method="POST", endpoint="/tickets/assign", http_status=400,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        response_evidence={"body": {"error": "user_id was renamed to assignee_id"}},
        raw_error_message="user_id was renamed to assignee_id",
    )
    packet = build_evidence_packet(tool, observation, {})
    assert "user_id was renamed to assignee_id" in packet
