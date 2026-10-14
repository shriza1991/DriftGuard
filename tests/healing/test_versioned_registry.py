"""Targeted tests for Member 2 Task 2 — Versioned Tool Registry.

Verifies:
- Correct v1 entries and valid schemas.
- Mutation isolation for inputs and returned snapshots.
- Stage and discard leave active state unchanged.
- Commit advances version and preserves history.
- Stale proposals cannot overwrite newer definitions.
- Rejected operations do not partially modify state.
- Fingerprints are deterministic and sensitive to contract changes.
- Same-tool concurrent commits cannot both succeed from one base version.
- Lock usage does not deadlock; different-tool operations remain independent.
- GET /registry matches its contract and exposes no secrets.
- Existing lifecycle and client behavior remains intact.
"""
import pytest
import asyncio
import httpx

from orchestrator.models import (
    ToolDefinition,
    ToolArgumentSchema,
    RegistryResponse,
)
from orchestrator.registry import (
    VersionedToolRegistry,
    StagedCandidate,
    AsyncRLock,
    RegistryError,
    ToolNotFoundError,
    CandidateNotFoundError,
    InvalidCandidateError,
    StaleVersionError,
)
from orchestrator.app import app


def test_default_v1_entries_and_schemas():
    """Verify registry initializes with canonical v1 tools and valid JSON schemas."""
    registry = VersionedToolRegistry.create_default()
    active_tools = registry.list_active()
    assert len(active_tools) == 3

    tool_names = [t.name for t in active_tools]
    assert tool_names == ["assign_ticket", "find_user", "get_ticket"]

    # Verify assign_ticket v1 schema
    assign_tool = registry.get("assign_ticket")
    assert assign_tool.version == "1.0.0"
    assert assign_tool.endpoint == "/tickets/assign"
    assert assign_tool.method == "POST"
    assert assign_tool.status == "active"
    assert len(assign_tool.fingerprint) == 16
    assert assign_tool.argument_schema.type == "object"
    assert "ticket_id" in assign_tool.argument_schema.properties
    assert "user_id" in assign_tool.argument_schema.properties
    assert assign_tool.argument_schema.required == ["ticket_id", "user_id"]

    # Verify find_user v1 schema
    find_tool = registry.get("find_user")
    assert find_tool.version == "1.0.0"
    assert find_tool.endpoint == "/users"
    assert find_tool.method == "GET"
    assert "name" in find_tool.argument_schema.properties

    # Verify get_ticket v1 schema
    get_tool = registry.get("get_ticket")
    assert get_tool.version == "1.0.0"
    assert get_tool.endpoint == "/tickets/{ticket_id}"
    assert get_tool.method == "GET"


def test_mutation_isolation_defensive_copies():
    """Verify returned snapshots and input candidates cannot mutate registry state."""
    registry = VersionedToolRegistry.create_default()

    # 1. Modifying returned snapshot
    tool = registry.get("assign_ticket")
    tool.endpoint = "/hacked/endpoint"
    tool.argument_schema.properties["injected_field"] = {"type": "string"}

    fresh_tool = registry.get("assign_ticket")
    assert fresh_tool.endpoint == "/tickets/assign"
    assert "injected_field" not in fresh_tool.argument_schema.properties

    # 2. Modifying list_active() output
    tool_list = registry.list_active()
    tool_list.clear()
    assert len(registry.list_active()) == 3


@pytest.mark.asyncio
async def test_mutation_isolation_on_staging():
    """Verify modifying input candidate object after staging does not affect the staged candidate."""
    registry = VersionedToolRegistry.create_default()

    candidate_def = registry.get("assign_ticket")
    candidate_def.description = "Pre-stage description"

    staged = await registry.stage_candidate(
        tool_name="assign_ticket",
        candidate_definition=candidate_def,
        base_version="1.0.0"
    )

    # Mutate caller object after staging
    candidate_def.description = "Post-stage mutation"
    candidate_def.endpoint = "/unauthorized"

    inspected = registry.get_candidate(staged.candidate_id)
    assert inspected is not None
    assert inspected.candidate_definition.description == "Pre-stage description"
    assert inspected.candidate_definition.endpoint == "/tickets/assign"


@pytest.mark.asyncio
async def test_stage_and_discard_leave_active_state_unchanged():
    """Verify staging and discarding candidates leave active tools and history unchanged."""
    registry = VersionedToolRegistry.create_default()

    # Create modified candidate (renaming user_id to assignee_id)
    candidate_def = registry.get("assign_ticket")
    candidate_def.argument_schema.properties = {
        "ticket_id": {"type": "integer"},
        "assignee_id": {"type": "string"}
    }
    candidate_def.argument_schema.required = ["ticket_id", "assignee_id"]

    staged = await registry.stage_candidate("assign_ticket", candidate_def)
    assert staged.candidate_id.startswith("cand-")
    assert staged.status == "staged"
    assert staged.base_version == "1.0.0"
    assert staged.target_version == "1.1.0"

    # Active definition must still be 1.0.0 with user_id
    active = registry.get("assign_ticket")
    assert active.version == "1.0.0"
    assert "user_id" in active.argument_schema.properties
    assert len(registry.get_history("assign_ticket")) == 0

    # Discard candidate
    discarded = registry.discard_candidate(staged.candidate_id)
    assert discarded is True

    # Check candidate status
    inspected = registry.get_candidate(staged.candidate_id)
    assert inspected.status == "discarded"

    # State still pristine
    assert registry.get("assign_ticket").version == "1.0.0"
    assert len(registry.get_history("assign_ticket")) == 0


@pytest.mark.asyncio
async def test_commit_advances_version_and_preserves_history():
    """Verify commit activates target version, updates active entry, and archives previous version."""
    registry = VersionedToolRegistry.create_default()

    candidate_def = registry.get("assign_ticket")
    candidate_def.argument_schema.properties = {
        "ticket_id": {"type": "integer"},
        "assignee_id": {"type": "string"}
    }
    candidate_def.argument_schema.required = ["ticket_id", "assignee_id"]

    staged = await registry.stage_candidate("assign_ticket", candidate_def, base_version="1.0.0")

    # Commit candidate
    committed = await registry.commit_candidate(staged.candidate_id, expected_base_version="1.0.0")
    assert committed.version == "1.1.0"
    assert committed.status == "active"
    assert "assignee_id" in committed.argument_schema.properties

    # Active lookup returns 1.1.0
    active = registry.get("assign_ticket")
    assert active.version == "1.1.0"
    assert "assignee_id" in active.argument_schema.properties

    # History lookup returns 1.0.0
    history = registry.get_history("assign_ticket")
    assert len(history) == 1
    assert history[0].version == "1.0.0"
    assert history[0].status == "historical"
    assert "user_id" in history[0].argument_schema.properties

    # Version-specific lookups
    v1_lookup = registry.get_version("assign_ticket", "1.0.0")
    assert v1_lookup is not None
    assert v1_lookup.version == "1.0.0"

    v1_1_lookup = registry.get_version("assign_ticket", "1.1.0")
    assert v1_1_lookup is not None
    assert v1_1_lookup.version == "1.1.0"


@pytest.mark.asyncio
async def test_stale_proposals_cannot_overwrite_newer_definitions():
    """Verify that a candidate staged against a base version cannot be committed if active version has advanced."""
    registry = VersionedToolRegistry.create_default()

    # Stage candidate A
    cand_a_def = registry.get("assign_ticket")
    cand_a_def.description = "Candidate A"
    cand_a = await registry.stage_candidate("assign_ticket", cand_a_def, base_version="1.0.0")

    # Stage candidate B from same base 1.0.0
    cand_b_def = registry.get("assign_ticket")
    cand_b_def.description = "Candidate B"
    cand_b = await registry.stage_candidate("assign_ticket", cand_b_def, base_version="1.0.0")

    # Commit Candidate A -> advances active version to 1.1.0
    await registry.commit_candidate(cand_a.candidate_id, expected_base_version="1.0.0")
    assert registry.get("assign_ticket").version == "1.1.0"

    # Attempt to commit Candidate B with old base version 1.0.0 -> Rejected
    with pytest.raises(StaleVersionError) as exc_info:
        await registry.commit_candidate(cand_b.candidate_id, expected_base_version="1.0.0")
    assert "active version '1.1.0' != expected base '1.0.0'" in str(exc_info.value)

    # Attempt to commit Candidate B specifying current version 1.1.0 -> Also rejected because candidate was staged on 1.0.0
    with pytest.raises(StaleVersionError):
        await registry.commit_candidate(cand_b.candidate_id, expected_base_version="1.1.0")

    # Active definition is still Candidate A (1.1.0)
    assert registry.get("assign_ticket").description == "Candidate A"
    assert len(registry.get_history("assign_ticket")) == 1


@pytest.mark.asyncio
async def test_rejected_operations_leave_state_completely_unmodified():
    """Verify rejections on staging and commit leave all state untouched."""
    registry = VersionedToolRegistry.create_default()

    # 1. Staging rejection: Tool not found
    candidate_def = registry.get("assign_ticket")
    with pytest.raises(ToolNotFoundError):
        await registry.stage_candidate("non_existent_tool", candidate_def)

    # 2. Staging rejection: Identity mismatch
    candidate_def.name = "renamed_tool"
    with pytest.raises(InvalidCandidateError):
        await registry.stage_candidate("assign_ticket", candidate_def)

    # 3. Staging rejection: Endpoint alteration
    candidate_def = registry.get("assign_ticket")
    candidate_def.endpoint = "/malicious/endpoint"
    with pytest.raises(InvalidCandidateError):
        await registry.stage_candidate("assign_ticket", candidate_def)

    # 4. Staging rejection: Method alteration
    candidate_def = registry.get("assign_ticket")
    candidate_def.method = "DELETE"
    with pytest.raises(InvalidCandidateError):
        await registry.stage_candidate("assign_ticket", candidate_def)

    # 5. Staging rejection: Invalid argument schema structure
    candidate_def = registry.get("assign_ticket")
    candidate_def.argument_schema.type = "array"
    with pytest.raises(InvalidCandidateError):
        await registry.stage_candidate("assign_ticket", candidate_def)

    # 6. Commit rejection: Non-existent candidate
    with pytest.raises(CandidateNotFoundError):
        await registry.commit_candidate("cand-does-not-exist", expected_base_version="1.0.0")

    # Verify active state and history remain completely untouched
    assert len(registry.list_active()) == 3
    assert registry.get("assign_ticket").version == "1.0.0"
    assert len(registry.get_history("assign_ticket")) == 0
    assert len(registry.list_staged()) == 0


def test_fingerprints_deterministic_and_sensitive_to_contract_changes():
    """Verify fingerprints are stable across dictionary ordering, exclude lifecycle metadata, and detect schema changes."""
    # 1. Dictionary key ordering does not alter fingerprint
    tool1 = ToolDefinition(
        name="assign_ticket",
        version="1.0.0",
        endpoint="/tickets/assign",
        method="POST",
        description="Assigns ticket",
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={"ticket_id": {"type": "integer"}, "user_id": {"type": "string"}},
            required=["ticket_id", "user_id"]
        ),
        request_mapping={"a": 1, "b": 2}
    )

    tool2 = ToolDefinition(
        name="assign_ticket",
        version="1.0.0",
        endpoint="/tickets/assign",
        method="POST",
        description="Assigns ticket",
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={"user_id": {"type": "string"}, "ticket_id": {"type": "integer"}},
            required=["ticket_id", "user_id"]
        ),
        request_mapping={"b": 2, "a": 1}
    )

    assert tool1.compute_fingerprint() == tool2.compute_fingerprint()

    # 2. Version alone does not alter canonical contract fingerprint
    tool_diff_version = tool1.model_copy(deep=True)
    tool_diff_version.version = "2.0.0"
    assert tool_diff_version.compute_fingerprint() == tool1.compute_fingerprint()

    # 3. Schema change alters fingerprint
    tool_diff_schema = tool1.model_copy(deep=True)
    tool_diff_schema.argument_schema.properties["assignee_id"] = {"type": "string"}
    assert tool_diff_schema.compute_fingerprint() != tool1.compute_fingerprint()

    # 4. Request mapping change alters fingerprint
    tool_diff_mapping = tool1.model_copy(deep=True)
    tool_diff_mapping.request_mapping = {"target": {"id": "{user_id}"}}
    assert tool_diff_mapping.compute_fingerprint() != tool1.compute_fingerprint()


@pytest.mark.asyncio
async def test_same_tool_concurrent_commits_atomic():
    """Verify race condition handling: two concurrent commits from same base version cannot both succeed."""
    registry = VersionedToolRegistry.create_default()

    # Stage two candidates from base 1.0.0
    cand1_def = registry.get("assign_ticket")
    cand1_def.description = "Proposal 1"
    cand1 = await registry.stage_candidate("assign_ticket", cand1_def, base_version="1.0.0")

    cand2_def = registry.get("assign_ticket")
    cand2_def.description = "Proposal 2"
    cand2 = await registry.stage_candidate("assign_ticket", cand2_def, base_version="1.0.0")

    # Execute concurrent commit attempts
    results = await asyncio.gather(
        registry.commit_candidate(cand1.candidate_id, expected_base_version="1.0.0"),
        registry.commit_candidate(cand2.candidate_id, expected_base_version="1.0.0"),
        return_exceptions=True
    )

    successes = [r for r in results if isinstance(r, ToolDefinition)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1, "Exactly one commit must succeed"
    assert len(failures) == 1, "The competing commit must fail"
    assert isinstance(failures[0], StaleVersionError)

    # Active tool version must be 1.1.0
    assert registry.get("assign_ticket").version == "1.1.0"
    assert len(registry.get_history("assign_ticket")) == 1


@pytest.mark.asyncio
async def test_lock_usage_no_deadlock_and_different_tools_independent():
    """Verify re-entrant locking allows nested registry operations without deadlocking,
    and different tools can be locked independently without blocking each other.
    """
    registry = VersionedToolRegistry.create_default()

    # 1. Caller holds tool_lock and invokes stage and commit -> no deadlock
    async with registry.tool_lock("assign_ticket"):
        cand_def = registry.get("assign_ticket")
        cand_def.description = "Locked transaction update"
        staged = await registry.stage_candidate("assign_ticket", cand_def)
        committed = await registry.commit_candidate(staged.candidate_id, expected_base_version="1.0.0")
        assert committed.version == "1.1.0"

    # 2. Different tools operate concurrently
    order_of_events = []

    async def task_a():
        async with registry.tool_lock("find_user"):
            order_of_events.append("find_user_acquired")
            await asyncio.sleep(0.05)
            order_of_events.append("find_user_released")

    async def task_b():
        # Should be able to acquire get_ticket lock without waiting for find_user
        await asyncio.sleep(0.01)
        async with registry.tool_lock("get_ticket"):
            order_of_events.append("get_ticket_acquired")
            order_of_events.append("get_ticket_released")

    await asyncio.gather(task_a(), task_b())

    # get_ticket was acquired while find_user was still holding its lock
    assert "get_ticket_acquired" in order_of_events
    find_user_rel_idx = order_of_events.index("find_user_released")
    get_ticket_acq_idx = order_of_events.index("get_ticket_acquired")
    assert get_ticket_acq_idx < find_user_rel_idx, "Different tools must not block each other"


@pytest.mark.asyncio
async def test_get_registry_public_contract_and_no_secrets():
    """Verify GET /registry endpoint returns valid public contract and leaks no secrets or internal state."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/registry")
        assert res.status_code == 200
        body = res.json()

        assert "tools" in body
        assert "version" in body
        assert "updated_at" in body
        assert len(body["tools"]) == 3

        for tool in body["tools"]:
            assert "name" in tool
            assert "version" in tool
            assert "endpoint" in tool
            assert "method" in tool
            assert "argument_schema" in tool
            assert "fingerprint" in tool
            # Credential references can be present, but raw secrets must not exist
            assert "credential_reference" in tool
            assert "secret" not in str(tool).lower()
            assert "token" not in str(tool).lower()
            # Staged candidates must not be leaked in public /registry
            assert "staged" not in str(tool).lower()
            assert "candidate_id" not in tool
