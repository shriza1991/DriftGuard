"""Versioned Tool Registry for AI Agents.

Manages tool lifecycle:
- In-memory versioned storage for active and historical contracts.
- Defensive immutable snapshots.
- Candidate staging with validation and unique candidate IDs.
- Candidate discarding.
- Atomic version-checked commit.
- Per-tool re-entrant async locking preventing deadlocks.
- Deterministic canonical contract fingerprinting.
"""
from typing import Dict, List, Optional, Literal
import asyncio
import copy
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from orchestrator.models import ToolDefinition
from orchestrator.registry.default_tools import create_default_registry


class RegistryError(Exception):
    """Base error for registry operations."""
    pass


class ToolNotFoundError(RegistryError):
    """Raised when a requested tool is not found in the registry."""
    pass


class CandidateNotFoundError(RegistryError):
    """Raised when a staged candidate ID is missing or already finalized."""
    pass


class InvalidCandidateError(RegistryError):
    """Raised when a candidate definition violates schema or identity constraints."""
    pass


class StaleVersionError(RegistryError):
    """Raised when committing a candidate whose base version does not match active version."""
    pass


class AsyncRLock:
    """Async Re-entrant Lock keyed to asyncio.current_task().
    Allows callers holding the tool lock to safely invoke nested registry operations
    (such as commit_candidate) without deadlocking.
    """
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._count: int = 0

    async def acquire(self):
        me = asyncio.current_task()
        if self._owner == me:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = me
        self._count = 1

    def release(self):
        me = asyncio.current_task()
        if self._owner != me:
            raise RuntimeError("Cannot release a lock not owned by current task")
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


class StagedCandidate(BaseModel):
    """Represents a proposed contract modification staged for validation."""
    candidate_id: str
    tool_name: str
    base_version: str
    target_version: str
    candidate_definition: ToolDefinition
    created_at: str
    status: Literal["staged", "committed", "discarded"] = "staged"


def derive_next_version(current: str) -> str:
    """Derives monotonic next version string."""
    parts = current.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{major}.{minor + 1}.0"
    elif len(parts) == 2 and all(p.isdigit() for p in parts):
        major, minor = int(parts[0]), int(parts[1])
        return f"{major}.{minor + 1}"
    elif current.isdigit():
        return str(int(current) + 1)
    return f"{current}.1"


class VersionedToolRegistry:
    """Thread-safe / task-safe in-memory versioned tool registry."""

    def __init__(self, initial_tools: Optional[Dict[str, ToolDefinition]] = None):
        self._active: Dict[str, ToolDefinition] = {}
        self._history: Dict[str, List[ToolDefinition]] = {}
        self._staged: Dict[str, StagedCandidate] = {}
        self._tool_locks: Dict[str, AsyncRLock] = {}
        self._global_lock = asyncio.Lock()

        if initial_tools:
            for name, tool in initial_tools.items():
                snap = tool.model_copy(deep=True)
                snap.fingerprint = snap.compute_fingerprint()
                self._active[name] = snap
                self._history[name] = []
                self._tool_locks[name] = AsyncRLock()

    @classmethod
    def create_default(cls) -> "VersionedToolRegistry":
        """Factory initializing registry with canonical v1 tools (find_user, assign_ticket, get_ticket)."""
        tools = create_default_registry()
        return cls(initial_tools=tools)

    def tool_lock(self, tool_name: str) -> AsyncRLock:
        """Returns the per-tool re-entrant async lock for concurrency coordination."""
        if tool_name not in self._tool_locks:
            self._tool_locks[tool_name] = AsyncRLock()
        return self._tool_locks[tool_name]

    def __getitem__(self, tool_name: str) -> ToolDefinition:
        """Enables dict-like indexing returning defensive snapshot."""
        return self.get(tool_name)

    def __contains__(self, tool_name: str) -> bool:
        """Enables 'in' membership check against active tools."""
        return tool_name in self._active

    def __iter__(self):
        """Iterates over active tool names."""
        return iter(self._active)

    def values(self) -> List[ToolDefinition]:
        """Enables .values() returning active defensive snapshots."""
        return self.list_active()

    def get(self, tool_name: str) -> ToolDefinition:
        """Returns a defensive copy of the active definition for tool_name."""
        if tool_name not in self._active:
            raise ToolNotFoundError(f"Tool '{tool_name}' not found in registry")
        return self._active[tool_name].model_copy(deep=True)

    def list_active(self) -> List[ToolDefinition]:
        """Returns defensive copies of all active tool definitions sorted by name."""
        return [
            tool.model_copy(deep=True)
            for _, tool in sorted(self._active.items())
        ]

    def list_tools(self) -> List[ToolDefinition]:
        """Alias for list_active() returning defensive copies of all active tool definitions."""
        return self.list_active()

    def get_version(self, tool_name: str, version: str) -> Optional[ToolDefinition]:
        """Looks up a specific active or historical version of a tool."""
        if tool_name not in self._active:
            raise ToolNotFoundError(f"Tool '{tool_name}' not found in registry")

        active = self._active[tool_name]
        if active.version == version:
            return active.model_copy(deep=True)

        for hist in self._history.get(tool_name, []):
            if hist.version == version:
                return hist.model_copy(deep=True)

        return None

    def get_history(self, tool_name: str) -> List[ToolDefinition]:
        """Returns defensive copies of historical versions for a tool in chronological order."""
        if tool_name not in self._active:
            raise ToolNotFoundError(f"Tool '{tool_name}' not found in registry")
        return [t.model_copy(deep=True) for t in self._history.get(tool_name, [])]

    async def stage_candidate(
        self,
        tool_name: str,
        candidate_definition: ToolDefinition,
        base_version: Optional[str] = None
    ) -> StagedCandidate:
        """Stages a proposed contract candidate after structural validation.
        Leaves active entries and history completely untouched.
        Derives target version and canonical fingerprint internally.
        """
        async with self.tool_lock(tool_name):
            if tool_name not in self._active:
                raise ToolNotFoundError(f"Tool '{tool_name}' not found in registry")

            active = self._active[tool_name]
            expected_base = base_version if base_version is not None else active.version

            # Check base version matches currently active version
            if expected_base != active.version:
                raise StaleVersionError(
                    f"Candidate base version '{expected_base}' does not match active version '{active.version}'"
                )

            # Defensive copy of input: caller mutations cannot affect candidate
            candidate_copy = candidate_definition.model_copy(deep=True)

            # Reject identity changes
            if candidate_copy.name != tool_name:
                raise InvalidCandidateError(
                    f"Candidate identity mismatch: expected name '{tool_name}', got '{candidate_copy.name}'"
                )

            # Reject unapproved endpoint or method changes
            if candidate_copy.endpoint != active.endpoint:
                raise InvalidCandidateError(
                    f"Candidate endpoint alteration rejected: '{candidate_copy.endpoint}' != '{active.endpoint}'"
                )
            if candidate_copy.method.upper() != active.method.upper():
                raise InvalidCandidateError(
                    f"Candidate method alteration rejected: '{candidate_copy.method}' != '{active.method}'"
                )

            # Validate argument schema JSON Schema structure
            schema = candidate_copy.argument_schema
            if schema.type != "object":
                raise InvalidCandidateError("Argument schema type must be 'object'")
            if not isinstance(schema.properties, dict):
                raise InvalidCandidateError("Argument schema properties must be a dictionary")
            if not isinstance(schema.required, list):
                raise InvalidCandidateError("Argument schema required fields must be a list")

            import jsonschema
            try:
                jsonschema.Draft7Validator.check_schema(schema.model_dump())
            except jsonschema.SchemaError as exc:
                raise InvalidCandidateError("Invalid argument JSON Schema") from exc

            # Internally derive monotonic version and canonical fingerprint
            target_version = derive_next_version(active.version)
            candidate_copy.version = target_version
            candidate_copy.status = "staged"
            candidate_copy.fingerprint = candidate_copy.compute_fingerprint()

            candidate_id = f"cand-{uuid.uuid4().hex[:12]}"
            staged = StagedCandidate(
                candidate_id=candidate_id,
                tool_name=tool_name,
                base_version=active.version,
                target_version=target_version,
                candidate_definition=candidate_copy,
                created_at=datetime.now(timezone.utc).isoformat(),
                status="staged"
            )

            self._staged[candidate_id] = staged
            return staged.model_copy(deep=True)

    def get_candidate(self, candidate_id: str) -> Optional[StagedCandidate]:
        """Returns a defensive copy of a staged candidate."""
        if candidate_id not in self._staged:
            return None
        return self._staged[candidate_id].model_copy(deep=True)

    def list_staged(self, tool_name: Optional[str] = None) -> List[StagedCandidate]:
        """Returns defensive copies of all currently staged candidates."""
        candidates = [
            c.model_copy(deep=True)
            for c in self._staged.values()
            if c.status == "staged" and (tool_name is None or c.tool_name == tool_name)
        ]
        return candidates

    def discard_candidate(self, candidate_id: str) -> bool:
        """Discards a staged candidate. Leaves active and historical states intact."""
        if candidate_id not in self._staged:
            return False

        candidate = self._staged[candidate_id]
        if candidate.status != "staged":
            return False

        candidate.status = "discarded"
        return True

    async def commit_candidate(
        self,
        candidate_id: str,
        expected_base_version: str
    ) -> ToolDefinition:
        """Atomically commits a staged candidate if base version still matches active version.
        Archives previous version into history and activates candidate definition.
        Leaves all state unchanged on failure.
        """
        if candidate_id not in self._staged:
            raise CandidateNotFoundError(f"Candidate '{candidate_id}' not found")

        candidate = self._staged[candidate_id]
        if candidate.status != "staged":
            raise CandidateNotFoundError(
                f"Candidate '{candidate_id}' cannot be committed; current status: '{candidate.status}'"
            )

        tool_name = candidate.tool_name

        async with self.tool_lock(tool_name):
            # Double check status inside tool lock
            if candidate.status != "staged":
                raise CandidateNotFoundError(
                    f"Candidate '{candidate_id}' cannot be committed; current status: '{candidate.status}'"
                )

            active = self._active[tool_name]

            # Atomic version-check
            if active.version != expected_base_version or active.version != candidate.base_version:
                raise StaleVersionError(
                    f"Atomic commit rejected: active version '{active.version}' != expected base '{expected_base_version}'"
                )

            # Preserve previous active snapshot in history
            prev_snapshot = active.model_copy(deep=True)
            prev_snapshot.status = "historical"
            self._history[tool_name].append(prev_snapshot)

            # Activate candidate
            new_active = candidate.candidate_definition.model_copy(deep=True)
            new_active.status = "active"
            new_active.version = candidate.target_version
            new_active.fingerprint = new_active.compute_fingerprint()

            self._active[tool_name] = new_active
            candidate.status = "committed"

            return new_active.model_copy(deep=True)
