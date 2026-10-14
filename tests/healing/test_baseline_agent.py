"""Tests for Member 2 Task 4: Genuine Fixed-Registry Baseline Agent."""
import asyncio
import json
import time
from typing import Any, Dict, List, Optional
import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from orchestrator.agents.catalogue import (
    get_frozen_v1_catalogue,
    get_langchain_tool_specs,
)
from orchestrator.agents.model_provider import (
    create_chat_model,
    ModelConfigurationError,
)
from orchestrator.agents.baseline import BaselineAgent
from orchestrator.client.mock_client import MockBusinessApiClient, redact_secrets
from orchestrator.execution.mapping import extract_json_path, MappingError
from orchestrator.models import FailureCategory
from orchestrator.registry import VersionedToolRegistry


class ScriptedChatModel(BaseChatModel):
    """Fake tool-calling chat model providing scripted responses for testing."""
    responses: List[AIMessage]
    calls: List[List[BaseMessage]] = []
    bound_tools: Optional[List[Dict[str, Any]]] = None

    def __init__(self, responses: List[AIMessage], **kwargs):
        super().__init__(responses=responses, **kwargs)
        self.calls = []

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling-chat-model"

    def bind_tools(self, tools: List[Dict[str, Any]], **kwargs):
        new_model = ScriptedChatModel(responses=list(self.responses))
        new_model.bound_tools = tools
        new_model.calls = self.calls
        return new_model

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if not self.responses:
            # Fallback final message if script exhausted
            msg = AIMessage(content="I have no more actions to take.")
        else:
            msg = self.responses.pop(0)

        generation = ChatGeneration(message=msg)
        return ChatResult(generations=[generation])


# Helper mock transport builder for downstream mock API
def make_mock_transport(
    user_response: Optional[Any] = None,
    assign_response: Optional[Any] = None,
    ticket_response: Optional[Any] = None,
    assign_status: int = 200,
    ticket_status: int = 200,
    user_status: int = 200,
):
    user_data = user_response if user_response is not None else [{"id": "123", "name": "Maya", "email": "maya@example.com"}]
    assign_data = assign_response if assign_response is not None else {"ticket_id": 81, "assignee_id": "123", "status": "assigned"}
    ticket_data = ticket_response if ticket_response is not None else {
        "id": 81,
        "assignee_id": "123",
        "status": "assigned",
        "history": [{"action": "assigned", "assignee_id": "123"}],
    }

    assign_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method

        if "GET" in method and "/users" in url:
            return httpx.Response(user_status, json=user_data)
        elif "POST" in method and "/tickets/assign" in url:
            assign_calls.append(request)
            return httpx.Response(assign_status, json=assign_data)
        elif "GET" in method and "/tickets/81" in url:
            return httpx.Response(ticket_status, json=ticket_data)

        return httpx.Response(404, json={"detail": "Not found"})

    transport = httpx.MockTransport(handler)
    return transport, assign_calls


@pytest.mark.asyncio
async def test_baseline_agent_normal_flow_independent_verification():
    """Verify standard model-selected calls execute with tool-call-ids and verify via get_ticket."""
    transport, assign_calls = make_mock_transport()
    client = MockBusinessApiClient(
        base_url="http://testmock",
        transport=transport,
    )

    # Model flow:
    # Turn 1: call find_user(name="Maya")
    # Turn 2: call assign_ticket(ticket_id=81, user_id="123")
    # Turn 3: call get_ticket(ticket_id=81)
    # Turn 4: Final message confirming task completed
    call1 = AIMessage(
        content="",
        tool_calls=[{"name": "find_user", "args": {"name": "Maya"}, "id": "call_1"}],
        usage_metadata={"input_tokens": 50, "output_tokens": 15, "total_tokens": 65},
    )
    call2 = AIMessage(
        content="",
        tool_calls=[{"name": "assign_ticket", "args": {"ticket_id": 81, "user_id": "123"}, "id": "call_2"}],
        usage_metadata={"input_tokens": 70, "output_tokens": 20, "total_tokens": 90},
    )
    call3 = AIMessage(
        content="",
        tool_calls=[{"name": "get_ticket", "args": {"ticket_id": 81}, "id": "call_3"}],
        usage_metadata={"input_tokens": 90, "output_tokens": 15, "total_tokens": 105},
    )
    call4 = AIMessage(
        content="I have verified ticket 81 is now assigned to Maya (ID 123).",
        tool_calls=[],
        usage_metadata={"input_tokens": 110, "output_tokens": 25, "total_tokens": 135},
    )

    fake_model = ScriptedChatModel(responses=[call1, call2, call3, call4])
    agent = BaselineAgent(client=client, model=fake_model)

    result = await agent.execute_workflow(run_id="test-run-1")

    assert result["success"] is True
    assert result["termination_reason"] == "completed_and_verified"
    assert result["turns"] == 4
    assert len(result["tool_calls"]) == 3

    # Check paired tool call IDs
    assert result["tool_calls"][0]["tool_call_id"] == "call_1"
    assert result["tool_calls"][0]["tool_name"] == "find_user"
    assert result["tool_calls"][1]["tool_call_id"] == "call_2"
    assert result["tool_calls"][1]["tool_name"] == "assign_ticket"
    assert result["tool_calls"][2]["tool_call_id"] == "call_3"
    assert result["tool_calls"][2]["tool_name"] == "get_ticket"

    # Token usage aggregation: 50+70+90+110 = 320 input, 15+20+15+25 = 75 output, total 395
    assert result["usage"]["input_tokens"] == 320
    assert result["usage"]["output_tokens"] == 75
    assert result["usage"]["total_tokens"] == 395
    assert result["usage"]["diagnosis_tokens"] == 0

    # Independent verification confirmed
    assert result["verification"]["verified"] is True
    assert result["verification"]["resolved_user_id"] == "123"
    assert result["verification"]["actual_assignee"] == "123"


@pytest.mark.asyncio
async def test_baseline_agent_schemas_and_executor_stay_frozen_after_registry_mutation():
    """Verify that committing a v2 tool to the mutable registry does NOT affect the baseline catalogue."""
    # Start mutable registry and commit a breaking change to assign_ticket
    registry = VersionedToolRegistry.create_default()
    candidate_def = registry.get("assign_ticket")
    candidate_def.argument_schema.properties = {
        "ticket_id": {"type": "integer"},
        "assignee_id": {"type": "string"},
    }
    candidate_def.argument_schema.required = ["ticket_id", "assignee_id"]
    candidate_def.version = "2.0.0"

    staged = await registry.stage_candidate("assign_ticket", candidate_def)
    await registry.commit_candidate(staged.candidate_id, expected_base_version="1.0.0")

    # Baseline catalogue must remain strictly v1.0.0
    baseline_agent = BaselineAgent()
    assign_tool = baseline_agent.catalogue["assign_ticket"]
    assert assign_tool.version == "1.0.0"
    assert "user_id" in assign_tool.argument_schema.properties
    assert "assignee_id" not in assign_tool.argument_schema.properties

    # Tool specs passed to model must also remain v1.0.0
    specs = get_langchain_tool_specs(baseline_agent.catalogue)
    assign_spec = next(s for s in specs if s["function"]["name"] == "assign_ticket")
    assert "user_id" in assign_spec["function"]["parameters"]["properties"]
    assert "assignee_id" not in assign_spec["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_baseline_rejects_unknown_and_invalid_tool_calls_without_http():
    """Verify unknown or schema-violating tool calls are rejected safely with zero downstream HTTP calls."""
    transport, assign_calls = make_mock_transport()
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    call1 = AIMessage(
        content="",
        tool_calls=[{"name": "hack_system", "args": {"root": True}, "id": "call_bad_1"}],
    )
    call2 = AIMessage(
        content="",
        tool_calls=[{"name": "assign_ticket", "args": {"invalid_arg": 123}, "id": "call_bad_2"}],
    )
    call3 = AIMessage(
        content="I give up.",
        tool_calls=[],
    )

    fake_model = ScriptedChatModel(responses=[call1, call2, call3])
    agent = BaselineAgent(client=client, model=fake_model)

    result = await agent.execute_workflow(run_id="test-unknown-tools")

    assert result["success"] is False
    assert len(assign_calls) == 0  # Zero HTTP assignment calls made
    assert len(result["tool_calls"]) == 2
    assert result["tool_calls"][0]["status"] == "unknown_tool"
    assert result["tool_calls"][1]["status"] == "intent_rejected"


@pytest.mark.asyncio
async def test_false_model_success_fails_independent_verification():
    """Verify that a model claiming 'done' fails if downstream state shows assignee mismatch."""
    # Ticket state in mock returns assignee_id == "999" (not Maya's "123")
    transport, _ = make_mock_transport(
        ticket_response={
            "id": 81,
            "assignee_id": "999",
            "status": "assigned",
            "history": [{"action": "assigned", "assignee_id": "999"}],
        }
    )
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    call1 = AIMessage(
        content="",
        tool_calls=[{"name": "find_user", "args": {"name": "Maya"}, "id": "c1"}],
    )
    call2 = AIMessage(
        content="",
        tool_calls=[{"name": "assign_ticket", "args": {"ticket_id": 81, "user_id": "123"}, "id": "c2"}],
    )
    call3 = AIMessage(
        content="Everything is done and ticket 81 is successfully assigned to Maya!",
        tool_calls=[],
    )

    fake_model = ScriptedChatModel(responses=[call1, call2, call3])
    agent = BaselineAgent(client=client, model=fake_model)

    result = await agent.execute_workflow(run_id="test-false-claim")

    assert result["success"] is False
    assert result["termination_reason"] == "verification_failed"
    assert result["verification"]["verified"] is False
    assert result["verification"]["actual_assignee"] == "999"


@pytest.mark.asyncio
async def test_drift_produces_real_bounded_failure_without_patching():
    """Verify that downstream drift (e.g. 422 Unprocessable Entity) causes genuine failure with no patching."""
    # Mock returns 422 for assign_ticket due to schema drift
    drift_response = {
        "error": {
            "category": "schema_mismatch",
            "message": "Field 'user_id' is deprecated. Expected 'assignee_id'.",
            "details": {"missing": ["assignee_id"], "unexpected": ["user_id"]},
        }
    }
    transport, _ = make_mock_transport(
        assign_response=drift_response,
        assign_status=422,
        ticket_response={"id": 81, "assignee_id": None, "status": "open", "history": []},
    )
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    call1 = AIMessage(
        content="",
        tool_calls=[{"name": "find_user", "args": {"name": "Maya"}, "id": "c1"}],
    )
    call2 = AIMessage(
        content="",
        tool_calls=[{"name": "assign_ticket", "args": {"ticket_id": 81, "user_id": "123"}, "id": "c2"}],
    )
    call3 = AIMessage(
        content="I encountered an API error and cannot proceed.",
        tool_calls=[],
    )

    fake_model = ScriptedChatModel(responses=[call1, call2, call3])
    agent = BaselineAgent(client=client, model=fake_model)

    result = await agent.execute_workflow(run_id="test-drift-failure")

    assert result["success"] is False
    # Verified: Tool call recorded actual failure
    assign_tool_call = next(c for c in result["tool_calls"] if c["tool_name"] == "assign_ticket")
    assert assign_tool_call["status"] == "http_error"
    assert "error" in assign_tool_call
    assert assign_tool_call["error"]["status_code"] == 422
    # Baseline does zero diagnosis or patching: catalogue remains strictly v1.0.0
    assert agent.catalogue["assign_ticket"].version == "1.0.0"


@pytest.mark.asyncio
async def test_repeated_write_intent_preserves_idempotency_identity():
    """Verify that repeated identical assignment intents within one task use the same Idempotency-Key."""
    transport, assign_calls = make_mock_transport()
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    # Model calls assign_ticket twice with identical arguments in separate turns
    call1 = AIMessage(
        content="",
        tool_calls=[{"name": "assign_ticket", "args": {"ticket_id": 81, "user_id": "123"}, "id": "c1"}],
    )
    call2 = AIMessage(
        content="",
        tool_calls=[{"name": "assign_ticket", "args": {"ticket_id": 81, "user_id": "123"}, "id": "c2"}],
    )
    call3 = AIMessage(content="Done.", tool_calls=[])

    fake_model = ScriptedChatModel(responses=[AIMessage(content="", tool_calls=[{"name":"find_user","args":{"name":"Maya"},"id":"resolve"}]), call1, call2, call3])
    agent = BaselineAgent(client=client, model=fake_model)

    await agent.execute_workflow(run_id="test-idempotency-key")

    assert len(assign_calls) == 2
    key1 = assign_calls[0].headers.get("Idempotency-Key")
    key2 = assign_calls[1].headers.get("Idempotency-Key")
    assert key1 is not None
    assert key1 == key2  # Same stable idempotency key for identical write intent!


@pytest.mark.asyncio
async def test_uncertain_writes_not_blindly_retried():
    """Verify that an uncertain write outcome (HTTP 500) is flagged uncertain and not blindly repeated."""
    transport, assign_calls = make_mock_transport(
        assign_response={"error": {"message": "Internal server error"}},
        assign_status=500,
    )
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    call1 = AIMessage(
        content="",
        tool_calls=[{"name": "assign_ticket", "args": {"ticket_id": 81, "user_id": "123"}, "id": "c1"}],
    )
    call2 = AIMessage(content="Assign failed.", tool_calls=[])

    fake_model = ScriptedChatModel(responses=[AIMessage(content="", tool_calls=[{"name":"find_user","args":{"name":"Maya"},"id":"resolve"}]), call1, call2])
    agent = BaselineAgent(client=client, model=fake_model)

    result = await agent.execute_workflow(run_id="test-uncertain-write")

    assert result["success"] is False
    assert len(assign_calls) == 1
    assign_rec = next(c for c in result["tool_calls"] if c["tool_name"] == "assign_ticket")
    assert assign_rec["status"] == "http_error"
    assert assign_rec["error"]["status_code"] == 500


@pytest.mark.asyncio
async def test_turn_and_call_limits_terminate_safely():
    """Verify that configured finite limits (max_turns, max_tool_calls) terminate execution cleanly."""
    transport, _ = make_mock_transport()
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    # Infinitely looping model
    looping_call = AIMessage(
        content="",
        tool_calls=[{"name": "find_user", "args": {"name": "Maya"}, "id": "loop_call"}],
    )
    # Provide plenty of calls, but limit max_turns to 2
    fake_model = ScriptedChatModel(responses=[looping_call, looping_call, looping_call, looping_call])
    agent = BaselineAgent(client=client, model=fake_model, max_turns=2)

    result = await agent.execute_workflow(run_id="test-turn-limits")

    assert result["success"] is False
    assert result["termination_reason"] == "max_turns_exceeded"
    assert result["turns"] == 2


@pytest.mark.asyncio
async def test_deadline_timeout_terminates_safely():
    """Verify that deadline timeout terminates execution cleanly and sets explicit termination reason."""
    transport, _ = make_mock_transport()
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    call1 = AIMessage(
        content="",
        tool_calls=[{"name": "find_user", "args": {"name": "Maya"}, "id": "c1"}],
    )
    fake_model = ScriptedChatModel(responses=[call1])
    agent = BaselineAgent(client=client, model=fake_model, timeout_seconds=0.001)

    # Slight sleep before invoke in workflow will trigger deadline
    await asyncio.sleep(0.01)
    result = await agent.execute_workflow(run_id="test-timeout")

    assert result["success"] is False
    assert result["termination_reason"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_truthful_token_usage_reporting():
    """Verify token usage aggregates accurately without double counting, or marks unavailable."""
    transport, _ = make_mock_transport()
    client = MockBusinessApiClient(base_url="http://testmock", transport=transport)

    # Model call with no usage_metadata (unavailable)
    call1 = AIMessage(
        content="Done.",
        tool_calls=[],
    )
    fake_model = ScriptedChatModel(responses=[call1])
    agent = BaselineAgent(client=client, model=fake_model)

    result = await agent.execute_workflow(run_id="test-no-usage")

    assert result["usage"]["input_tokens"] == "unavailable"
    assert result["usage"]["output_tokens"] == "unavailable"
    assert result["usage"]["total_tokens"] == "unavailable"
    assert result["usage"]["diagnosis_tokens"] == 0


def test_missing_provider_config_fails_clearly():
    """Verify lazy model factory raises ModelConfigurationError on missing credentials."""
    import os
    orig_key = os.environ.get("GEMINI_API_KEY")
    orig_google = os.environ.get("GOOGLE_API_KEY")
    orig_openai = os.environ.get("OPENAI_API_KEY")
    try:
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)

        with pytest.raises(ModelConfigurationError) as exc_info:
            create_chat_model(provider="google_genai")
        assert "Missing API key" in str(exc_info.value)
    finally:
        if orig_key:
            os.environ["GEMINI_API_KEY"] = orig_key
        if orig_google:
            os.environ["GOOGLE_API_KEY"] = orig_google
        if orig_openai:
            os.environ["OPENAI_API_KEY"] = orig_openai


def test_prerequisite_missing_required_response_mapping_fails():
    """Verify extract_json_path fails with MappingError when a required path is missing."""
    data = {"users": []}
    with pytest.raises(MappingError) as exc_info:
        extract_json_path("$.users[0].id", data)
    assert "out of range" in str(exc_info.value)


def test_prerequisite_secret_redaction_preserves_legitimate_keys_and_scrubs_strings():
    """Verify redact_secrets scrubs embedded tokens in strings while preserving legitimate business fields."""
    sample = {
        "idempotency_key": "ticket-81-user-123-assign",
        "ticket_id": 81,
        "error_message": "Failed with Bearer my_top_secret_token_12345678 in header",
        "api_key": "super_secret_api_key",
    }
    redacted = redact_secrets(sample, known_secrets={"my_top_secret_token_12345678"})

    # Legitimate business keys must be preserved
    assert redacted["idempotency_key"] == "ticket-81-user-123-assign"
    assert redacted["ticket_id"] == 81

    # Secret keys must be redacted
    assert redacted["api_key"] == "[REDACTED]"

    # Embedded secrets inside ordinary strings must be scrubbed
    assert "my_top_secret_token" not in redacted["error_message"]
    assert "[REDACTED]" in redacted["error_message"]
