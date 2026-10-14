"""Quota-free checks of the actual transport reservation and plan replay."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from orchestrator.agents.catalogue import get_langchain_tool_specs
from orchestrator.agents.model_gateway import BudgetedGroqTransport, ModelBudgetExhausted, ModelGateway, ProviderCooldown, RunCallBudget
from orchestrator.agents.workflow_plan import InvalidWorkflowPlan, PlanReplayModel, validate_workflow_plan


def plan(name="Maya", ticket=81):
    return {"steps": [
        {"tool_name": "find_user", "arguments": {"name": name}},
        {"tool_name": "assign_ticket", "arguments": {"ticket_id": ticket, "user_id": {"$ref": "steps.0.user_id"}}},
        {"tool_name": "get_ticket", "arguments": {"ticket_id": ticket}},
    ]}


def test_concurrent_third_transport_attempt_blocked_and_budget_reopens(tmp_path):
    path = str(tmp_path / "calls.sqlite3")
    budget = RunCallBudget("one-run", path)
    def reserve(_):
        try:
            budget.reserve_attempt("planning")
            return None
        except ModelBudgetExhausted as exc:
            return exc
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(reserve, range(3)))
    assert sum(result is None for result in results) == 2
    assert sum(isinstance(result, ModelBudgetExhausted) for result in results) == 1
    assert RunCallBudget("one-run", path).snapshot()["outbound_attempts"] == 2


@pytest.mark.asyncio
async def test_429_records_failed_http_attempt_and_honors_retry_after(tmp_path):
    budget = RunCallBudget("rate-limited", str(tmp_path / "calls.sqlite3"))
    async def reject(request):
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": "rate limited"})
    transport = BudgetedGroqTransport(budget, httpx.MockTransport(reject))
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions", json={})
    response = await transport.handle_async_request(request)
    assert response.status_code == 429
    with pytest.raises(ProviderCooldown):
        await transport.handle_async_request(request)
    await transport.aclose()
    snapshot = budget.snapshot()
    assert (snapshot["outbound_attempts"], snapshot["failed_attempts"], snapshot["http_429_responses"]) == (1, 1, 1)
    assert snapshot["token_usage"] == "unavailable"


class FakePlanningModel:
    def __init__(self, response):
        self.response = response
        self.requests = 0

    def bind_tools(self, tools, tool_choice=None):
        assert tools[0]["function"]["name"] == "submit_workflow_plan"
        return self

    async def ainvoke(self, messages):
        self.requests += 1
        return self.response


class FakeGateway:
    def __init__(self):
        self.purposes = []

    async def invoke(self, model, messages, purpose):
        self.purposes.append(purpose)
        return await model.ainvoke(messages)


@pytest.mark.asyncio
async def test_one_model_plan_replays_dependent_steps_without_more_inference():
    response = AIMessage(content="", tool_calls=[{"name": "submit_workflow_plan", "args": plan(), "id": "call-plan"}],
                         usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18})
    provider = FakePlanningModel(response)
    gateway = FakeGateway()
    replay = PlanReplayModel(provider, gateway, "Maya", 81).bind_tools(get_langchain_tool_specs())
    messages = [HumanMessage(content="Find Maya, assign ticket 81, and confirm the assignee")]
    first = await replay.ainvoke(messages)
    assert first.tool_calls[0]["name"] == "find_user"
    messages += [first, ToolMessage(content=json.dumps({"user_id": "123"}), tool_call_id="plan-step-0")]
    second = await replay.ainvoke(messages)
    assert second.tool_calls[0]["args"] == {"ticket_id": 81, "user_id": "123"}
    messages += [second, ToolMessage(content=json.dumps({"status": "assigned"}), tool_call_id="plan-step-1")]
    third = await replay.ainvoke(messages)
    assert third.tool_calls[0]["name"] == "get_ticket"
    assert provider.requests == 1 and gateway.purposes == ["planning"]


def test_invalid_plan_rejects_literal_or_invented_assignment_id():
    unsafe = plan()
    unsafe["steps"][1]["arguments"]["user_id"] = "999"
    with pytest.raises(InvalidWorkflowPlan):
        validate_workflow_plan(unsafe, get_langchain_tool_specs(), "Maya", 81)


@pytest.mark.asyncio
async def test_invalid_structured_response_stops_after_one_logical_call():
    provider = FakePlanningModel(AIMessage(content="not a tool call"))
    gateway = FakeGateway()
    replay = PlanReplayModel(provider, gateway, "Maya", 81).bind_tools(get_langchain_tool_specs())
    with pytest.raises(InvalidWorkflowPlan):
        await replay.ainvoke([HumanMessage(content="Find Maya and assign ticket 81")])
    assert provider.requests == 1 and gateway.purposes == ["planning"]


@pytest.mark.asyncio
async def test_gateway_counts_actual_mocked_transport_for_plan_and_repair(tmp_path):
    sent = []
    async def respond(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": True})
    gateway = ModelGateway("two-call-run", str(tmp_path / "calls.sqlite3"), httpx.MockTransport(respond))
    class TransportModel:
        async def ainvoke(self, messages):
            response = await gateway.client.post("https://api.groq.com/openai/v1/chat/completions", json={})
            response.raise_for_status()
            return AIMessage(content="ok", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    model = TransportModel()
    try:
        await gateway.invoke(model, "plan", "planning")
        await gateway.invoke(model, "repair", "repair_diagnosis")
        with pytest.raises(ModelBudgetExhausted):
            await gateway.invoke(model, "third", "repair_diagnosis")
        snapshot = gateway.snapshot()
        assert snapshot["outbound_attempts"] == 2 and len(sent) == 2
        assert snapshot["planning_attempts"] == 1 and snapshot["repair_diagnosis_attempts"] == 1
        assert snapshot["successful_responses"] == 2 and snapshot["failed_attempts"] == 0
    finally:
        await gateway.aclose()


def test_groq_sdk_retries_are_disabled():
    from orchestrator.agents.model_provider import create_chat_model
    model = create_chat_model(provider="groq", api_key="test-only-placeholder")
    assert model.max_retries == 0
