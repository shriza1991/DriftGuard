import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage

from orchestrator.audit.models import PersistedLLMInteraction, PersistedRepairCacheEntry
from orchestrator.audit.store import SQLiteAuditStore
from orchestrator.client.mock_client import MockBusinessApiClient
from orchestrator.execution.events import EventBroadcaster
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.healing.classifier import classify_failure
from orchestrator.healing.llm_cache import build_repair_cache_key
from orchestrator.healing.llm_diagnosis import LLMDiagnoser
from orchestrator.healing.llm_runtime import RepairLLMCircuitOpen, RepairLLMRuntime
from orchestrator.healing.recovery import RecoveryController, RecoveryStatus
from orchestrator.models import FailureObservation, OperationIdentity
from orchestrator.models.observation import ExecutionPhase
from orchestrator.registry import VersionedToolRegistry


class CountingModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if not self.responses:
            raise RuntimeError("No scripted response")
        return self.responses.pop(0)


def _observation(*, endpoint: str = "/tickets/assign") -> FailureObservation:
    message = "Field user_id was renamed to assignee_id."
    return FailureObservation(
        tool_name="assign_ticket",
        execution_phase=ExecutionPhase.HTTP_DISPATCH,
        endpoint=endpoint,
        method="POST",
        registry_version="1.0.0",
        fingerprint="",
        original_arguments={"ticket_id": 81, "user_id": "123"},
        http_status=400,
        raw_error_message=message,
        response_evidence={"body": {"error": {"code": "SCHEMA_MISMATCH", "message": message}}},
        validation_details={"pre_execution_rejection": True},
        evidence_paths=["raw_error_message"],
        uncertain_write=False,
        operation_identity=OperationIdentity(
            tool_name="assign_ticket", tool_version="1.0.0", endpoint=endpoint,
            method="POST", idempotency_key="cache-test-key",
        ),
    )


def _envelope() -> AIMessage:
    payload = {
        "diagnosis": {
            "category": "schema_drift",
            "confidence": 0.96,
            "evidence_refs": ["raw_error_message"],
        },
        "patch": {"operations": [{
            "operation_type": "rename_argument",
            "source_path": "user_id",
            "target_path": "assignee_id",
            "conversion_policy": None,
            "credential_reference": None,
            "value_provenance": "original_arguments",
            "evidence_path": "raw_error_message",
            "evidence_quote": "user_id was renamed to assignee_id",
        }]},
        "reasoning_summary": "The provider explicitly identifies the renamed field.",
        "stop_if_invalid": True,
    }
    return AIMessage(
        content=json.dumps(payload),
        usage_metadata={"input_tokens": 40, "output_tokens": 25, "total_tokens": 65},
    )


@pytest.mark.asyncio
async def test_interaction_store_is_durable_append_only_and_redacted(tmp_path: Path):
    store = SQLiteAuditStore(str(tmp_path / "audit.sqlite3"))
    await store.initialize()
    record = PersistedLLMInteraction(
        interaction_id="interaction-1", run_id="run-1", incident_id="incident-1",
        created_at=datetime.now(timezone.utc).isoformat(), duration_ms=12.5,
        provider="test", model_name="model", prompt_template_version="prompt-v1",
        response_schema_version="schema-v1", cache_status="miss",
        sanitized_prompt_hash="store-computes-this",
        sanitized_prompt_payload={"authorization": "Bearer TOP_SECRET", "note": "token=TOP_SECRET"},
        sanitized_response_payload={"secret": "TOP_SECRET", "result": "ok"},
        input_tokens=4, output_tokens=2, total_tokens=6,
        parse_status="valid", validation_status="candidate_proposed",
        deterministic_decision="validation_required",
    )
    saved = await store.record_llm_interaction(record)
    rows = await store.get_run_llm_interactions("run-1")
    assert len(rows) == 1
    serialized = json.dumps(rows[0].model_dump())
    assert "TOP_SECRET" not in serialized
    assert "[REDACTED]" in serialized
    assert saved.sanitized_prompt_hash != "store-computes-this"
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_llm_interaction(record)


def test_cache_key_rejects_cross_scope_reuse():
    registry = VersionedToolRegistry.create_default()
    tool = registry.get("assign_ticket")
    obs = _observation()
    classification = classify_failure(obs)
    common = dict(
        provider_identity="MockBusinessApiClient", provider_origin="http://provider-a:8001/path?token=x",
        tenant_scope="tenant-a", snapshot=tool, observation=obs, classification=classification,
        prompt_version="prompt-v1", response_schema_version="schema-v1",
        model_provider="test", model_name="model-a",
    )
    base_key, base_scope, _ = build_repair_cache_key(**common)
    assert "token" not in json.dumps(base_scope)
    for changed in (
        {"provider_origin": "http://provider-b:8001"},
        {"tenant_scope": "tenant-b"},
        {"model_name": "model-b"},
        {"observation": _observation(endpoint="/other")},
    ):
        key, _, _ = build_repair_cache_key(**{**common, **changed})
        assert key != base_key


@pytest.mark.asyncio
async def test_cache_expiry_is_invalidated_deterministically(tmp_path: Path):
    store = SQLiteAuditStore(str(tmp_path / "expiry.sqlite3"))
    await store.initialize()
    interaction = PersistedLLMInteraction(
        interaction_id="interaction-expired", run_id="run-expired", incident_id="incident",
        created_at=datetime.now(timezone.utc).isoformat(), duration_ms=1,
        provider="test", model_name="model", prompt_template_version="p",
        response_schema_version="s", sanitized_prompt_hash="x", parse_status="valid",
        validation_status="accepted", deterministic_decision="authorized_for_single_retry",
    )
    await store.record_llm_interaction(interaction)
    now = datetime.now(timezone.utc)
    entry = PersistedRepairCacheEntry(
        cache_key="expired-key", scope_hashes={"scope": "hash"}, tool_name="assign_ticket",
        base_version="1.0.0", base_fingerprint="fp", policy_version="policy",
        prompt_version="p", response_schema_version="s", model_configuration_hash="m",
        operations=[{"operation_type": "rename_argument"}], evidence_references={},
        proposal_fingerprint="proposal", original_interaction_id="interaction-expired",
        committed_version="1.1.0", committed_fingerprint="committed",
        created_at=(now - timedelta(hours=2)).isoformat(),
        expires_at=(now - timedelta(hours=1)).isoformat(),
    )
    await store.put_validated_repair_cache(entry)
    assert await store.get_validated_repair_cache("expired-key") is None


@pytest.mark.asyncio
async def test_verified_llm_repair_is_reused_without_second_model_call(tmp_path: Path):
    store = SQLiteAuditStore(str(tmp_path / "reuse.sqlite3"))
    await store.initialize()
    model = CountingModel([_envelope()])
    broadcaster = EventBroadcaster()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets/assign":
            body = json.loads(request.content.decode())
            if body.get("assignee_id") == "123":
                return httpx.Response(200, json={"ticket_id": 81, "assignee_id": "123"})
            return httpx.Response(400, json={"error": "wrong shape"})
        if request.url.path == "/tickets/81":
            return httpx.Response(200, json={"ticket_id": 81, "assignee_id": "123"})
        return httpx.Response(404)

    client = MockBusinessApiClient(
        base_url="http://provider-a:8001", transport=httpx.MockTransport(handler)
    )
    obs1 = _observation()
    registry1 = VersionedToolRegistry.create_default()
    controller1 = RecoveryController(
        registry=registry1, executor=ToolExecutor(registry1, client),
        event_broadcaster=broadcaster,
        llm_diagnoser=LLMDiagnoser(model=model, audit_store=store),
        audit_store=store, execution_guarantees={"pre_execution_rejection": True},
    )
    first = await controller1.recover_failure(
        obs1, obs1.original_arguments, obs1.operation_identity, "run-first"
    )
    assert first.status == RecoveryStatus.RECOVERED
    assert first.repair_llm_calls == 1
    assert first.cache_status == "stored"
    assert model.calls == 1

    registry2 = VersionedToolRegistry.create_default()
    obs2 = _observation()
    obs2.operation_identity = obs2.operation_identity.model_copy(
        update={"idempotency_key": "cache-test-key-2"}
    )
    controller2 = RecoveryController(
        registry=registry2, executor=ToolExecutor(registry2, client),
        event_broadcaster=broadcaster,
        llm_diagnoser=LLMDiagnoser(model=model, audit_store=store),
        audit_store=store, execution_guarantees={"pre_execution_rejection": True},
    )
    second = await controller2.recover_failure(
        obs2, obs2.original_arguments, obs2.operation_identity, "run-second"
    )
    assert second.status == RecoveryStatus.RECOVERED
    assert second.decision_path == "cache"
    assert second.repair_llm_calls == 0
    assert second.cache_status == "hit"
    assert model.calls == 1
    assert any(e.type == "cache_hit_validated" for e in broadcaster.get_events("run-second"))


@pytest.mark.asyncio
async def test_runtime_enforces_concurrency_and_opens_breaker():
    runtime = RepairLLMRuntime(max_concurrency=2, failure_threshold=2, cooldown_seconds=10)
    active = 0
    maximum = 0

    async def bounded_call():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    await asyncio.gather(*[
        runtime.invoke(bounded_call, timeout_seconds=1) for _ in range(5)
    ])
    assert maximum == 2

    async def fail():
        raise RuntimeError("provider unavailable")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await runtime.invoke(fail, timeout_seconds=1)
    assert runtime.is_open()
    with pytest.raises(RepairLLMCircuitOpen):
        await runtime.invoke(bounded_call, timeout_seconds=1)
