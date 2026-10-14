"""Bounded Recovery and Registry Commit Controller.
"""
from __future__ import annotations
from typing import Dict, Any, Optional, List, Callable, Set, Union
from enum import Enum
import time
import asyncio
import copy
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from orchestrator.models import (
    ToolDefinition,
    ExecutionResult,
    ExecutionContext,
    OperationIdentity,
    TimingInfo,
    FailureObservation,
    ClassificationResult,
    SemanticFailureCategory,
    RepairProposal,
    PlanningResult,
    PlanningStatus,
    RestrictedOperationType,
    ValidationResult,
    ValidationStatus,
)
from orchestrator.registry import (
    VersionedToolRegistry,
    StaleVersionError,
    CandidateNotFoundError,
    InvalidCandidateError,
)
from orchestrator.execution.events import EventBroadcaster, event_broadcaster
from orchestrator.healing.planner import plan_repair
from orchestrator.healing.validator import validate_repair_proposal, emit_patch_validation_event
from orchestrator.healing.classifier import classify_failure

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from orchestrator.execution.tool_executor import ToolExecutor


class RecoveryStatus(str, Enum):
    RECOVERED = "recovered"
    INELIGIBLE = "ineligible"
    VALIDATION_REJECTED = "validation_rejected"
    PLANNING_FAILED = "planning_failed"
    EXECUTION_FAILED = "execution_failed"
    VERIFICATION_FAILED = "verification_failed"
    COMMIT_FAILED = "commit_failed"
    STALE_SKIPPED = "stale_skipped"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class RecoveryResult(BaseModel):
    """Complete, truthful outcome of an autonomous recovery attempt."""
    status: RecoveryStatus
    recovered: bool
    tool_name: str
    base_version: str
    base_fingerprint: str
    proposal_fingerprint: Optional[str] = None
    candidate_id: Optional[str] = None
    committed_version: Optional[str] = None
    committed_fingerprint: Optional[str] = None
    business_effect_occurred: bool = False
    verification_succeeded: bool = False
    registry_committed: bool = False
    execution_result: Optional[ExecutionResult] = None
    verification_details: Optional[Dict[str, Any]] = None
    termination_reason: Optional[str] = None
    duration_ms: float = 0.0
    diagnosis_source: str = "deterministic"
    diagnosis_tokens: int = 0
    diagnosis_token_details: Optional[Dict[str, int]] = None
    staged_candidate_discarded: bool = False


async def default_verify_business_intent(
    executor: ToolExecutor,
    tool_name: str,
    original_arguments: Dict[str, Any],
    execution_result: ExecutionResult,
    run_id: str,
) -> Dict[str, Any]:
    """Independently verifies downstream business effect via read-only check.
    
    Adheres to safety invariants:
    - Derives expectations strictly from original caller arguments, never from repaired field names or model claims.
    - Identity equivalence allows authorized lossless string/integer representation (str(actual) == str(expected)).
    - Read-only; does not invoke recursive healing.
    - Halts safely if downstream state diverges; zero speculative retries.
    """
    if tool_name == "assign_ticket":
        expected_ticket = original_arguments.get("ticket_id")
        expected_user = original_arguments.get("user_id") or original_arguments.get("assignee_id")

        if expected_ticket is None or expected_user is None:
            return {
                "verified": False,
                "reason": "Missing expected ticket_id or assignee in original business arguments",
            }

        # Read-only verification call to get_ticket
        ctx = ExecutionContext(run_id=run_id)
        read_res = await executor.execute("get_ticket", {"ticket_id": expected_ticket}, context=ctx)

        if not read_res.is_success:
            err_msg = read_res.failure.message if read_res.failure else "Unknown read error"
            return {
                "verified": False,
                "reason": f"Independent read verification get_ticket({expected_ticket}) failed: {err_msg}",
            }

        ticket_data = read_res.result or {}
        actual_ticket = ticket_data.get("ticket_id") if "ticket_id" in ticket_data else ticket_data.get("id")
        actual_assignee = ticket_data.get("assignee_id")

        # Identity equivalence check (allows string/int equivalence e.g. "123" == 123)
        ticket_match = (str(actual_ticket) == str(expected_ticket))
        assignee_match = (str(actual_assignee) == str(expected_user))

        if ticket_match and assignee_match:
            return {
                "verified": True,
                "actual_ticket_id": actual_ticket,
                "actual_assignee_id": actual_assignee,
                "verification_method": "independent_read:get_ticket",
            }
        else:
            return {
                "verified": False,
                "reason": (
                    f"Independent verification mismatch: expected ticket={expected_ticket}, assignee={expected_user}; "
                    f"downstream returned ticket={actual_ticket}, assignee={actual_assignee}"
                ),
                "actual_ticket_id": actual_ticket,
                "actual_assignee_id": actual_assignee,
            }

    # For other tools, verify that HTTP execution succeeded
    if execution_result.is_success:
        return {"verified": True, "verification_method": "execution_status"}
    return {"verified": False, "reason": "Execution did not succeed"}


class RecoveryController:
    """Bounded, deterministic recovery and registry commit controller."""

    def __init__(
        self,
        registry: VersionedToolRegistry,
        executor: ToolExecutor,
        planner: Optional[Callable] = None,
        validator: Optional[Callable] = None,
        verifier: Optional[Callable] = None,
        event_broadcaster: Optional[EventBroadcaster] = None,
        approved_header_credentials: Optional[Dict[str, str]] = None,
        credential_resolver: Optional[Callable[[str], bool]] = None,
        execution_guarantees: Optional[Dict[str, Any]] = None,
        llm_diagnoser: Optional[Any] = None,
        diagnosis_timeout: float = 10.0,
    ):
        self.registry = registry
        self.executor = executor
        self.planner = planner or plan_repair
        self.validator = validator or validate_repair_proposal
        self.verifier = verifier or default_verify_business_intent
        self.events = event_broadcaster or globals()["event_broadcaster"]
        self.approved_header_credentials = approved_header_credentials or {}
        self.credential_resolver = credential_resolver
        self.execution_guarantees = execution_guarantees or {}
        self.llm_diagnoser = llm_diagnoser
        self.diagnosis_timeout = diagnosis_timeout

        # Track attempted proposal fingerprints per logical operation to prevent repeated cycles
        self._attempted_proposals: Set[str] = set()
        # Track operations that have undergone LLM diagnosis (max 1 LLM diagnosis per operation)
        self._diagnosed_operations: Set[str] = set()
        # Track operations that have had a repaired dispatch (max 1 repaired dispatch per operation)
        self._dispatched_operations: Set[str] = set()

    def _emit_event(
        self,
        run_id: str,
        mode: str,
        event_type: str,
        tool: Optional[str],
        data: Dict[str, Any],
    ) -> None:
        """Publishes contract-compatible event through broadcaster."""
        if self.events is not None:
            self.events.publish_event(
                run_id=run_id,
                event_type=event_type,
                mode=mode,
                tool=tool,
                data=data,
            )

    async def recover_failure(
        self,
        observation: FailureObservation,
        original_arguments: Dict[str, Any],
        operation_identity: OperationIdentity,
        run_id: str,
        deadline: Optional[float] = None,
        mode: str = "healing",
    ) -> RecoveryResult:
        """Executes the bounded, deterministic self-healing recovery sequence."""
        start_time = time.perf_counter()
        tool_name = observation.tool_name
        base_version = observation.registry_version or "1.0.0"
        base_fp = observation.fingerprint or ""

        # Calculate remaining time from deadline
        timeout_budget: Optional[float] = None
        if deadline is not None:
            # If deadline is given as a duration (e.g. 30.0) vs timestamp
            if deadline < 1000000000.0:
                timeout_budget = deadline
            else:
                timeout_budget = max(0.0, deadline - time.time())
            if timeout_budget <= 0.0:
                return RecoveryResult(
                    status=RecoveryStatus.TIMEOUT,
                    recovered=False,
                    tool_name=tool_name,
                    base_version=base_version,
                    base_fingerprint=base_fp,
                    termination_reason="Execution deadline expired prior to recovery lock acquisition",
                    duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # ---------------------------------------------------------------------
        # 1. Eligibility Check
        # ---------------------------------------------------------------------
        op_key = operation_identity.key if operation_identity else f"{tool_name}:{base_fp}"
        if op_key in self._dispatched_operations:
            return RecoveryResult(
                status=RecoveryStatus.PLANNING_FAILED,
                recovered=False,
                tool_name=tool_name,
                base_version=base_version,
                base_fingerprint=base_fp,
                termination_reason=f"Operation '{op_key}' has already exhausted its single repaired dispatch budget; recovery loop already attempted.",
                duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        classification: ClassificationResult = (
            getattr(observation, "classification", None)
            or classify_failure(observation)
        )

        if not classification.diagnosis_eligible:
            return RecoveryResult(
                status=RecoveryStatus.INELIGIBLE,
                recovered=False,
                tool_name=tool_name,
                base_version=base_version,
                base_fingerprint=base_fp,
                termination_reason=f"Failure is not diagnosis-eligible: {classification.reason}",
                duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        # Block uncertain writes strictly
        if observation.uncertain_write:
            return RecoveryResult(
                status=RecoveryStatus.INELIGIBLE,
                recovered=False,
                tool_name=tool_name,
                base_version=base_version,
                base_fingerprint=base_fp,
                termination_reason="Automated recovery blocked: uncertain write detected. Retrying risks duplicating mutation.",
                duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        # Pre-execution rejection check for stateful writes
        is_write = observation.method.upper() in ("POST", "PUT", "PATCH", "DELETE")
        obs_status = getattr(observation, "http_status", None)
        if is_write and obs_status in (400, 401, 403, 422):
            has_trusted_guarantee = (
                self.execution_guarantees.get("pre_execution_rejection") is True
                or (
                    isinstance(observation.validation_details, dict)
                    and observation.validation_details.get("pre_execution_rejection") is True
                )
            )
            if not has_trusted_guarantee:
                return RecoveryResult(
                    status=RecoveryStatus.INELIGIBLE,
                    recovered=False,
                    tool_name=tool_name,
                    base_version=base_version,
                    base_fingerprint=base_fp,
                    termination_reason=(
                        f"Automated retry blocked: HTTP {obs_status} alone does not prove rejection "
                        "before mutation without a trusted endpoint guarantee or explicit execution evidence."
                    ),
                    duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # Emit diagnosis_started event
        self._emit_event(
            run_id=run_id,
            mode=mode,
            event_type="diagnosis_started",
            tool=tool_name,
            data={
                "tool": tool_name,
                "base_version": base_version,
                "base_fingerprint": base_fp,
                "category": classification.category.value,
                "reason": classification.reason,
            },
        )

        # ---------------------------------------------------------------------
        # 2. Acquire Tool Lock Within Deadline
        # ---------------------------------------------------------------------
        lock = self.registry.tool_lock(tool_name)
        staged_candidate_id: Optional[str] = None
        business_effect_occurred = False
        verification_succeeded = False
        registry_committed = False
        proposal_fp: Optional[str] = None
        executed_res: Optional[ExecutionResult] = None
        verif_data: Optional[Dict[str, Any]] = None

        try:
            lock_coro = lock.acquire()
            if timeout_budget is not None:
                await asyncio.wait_for(lock_coro, timeout=timeout_budget)
            else:
                await lock_coro

            try:
                # -------------------------------------------------------------
                # 3. Active Registry Freshness & Stale Recheck
                # -------------------------------------------------------------
                active_tool = self.registry.get(tool_name)
                if not active_tool:
                    return RecoveryResult(
                        status=RecoveryStatus.PLANNING_FAILED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=base_version,
                        base_fingerprint=base_fp,
                        termination_reason=f"Tool '{tool_name}' not found in active registry snapshot",
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

                active_fp = active_tool.fingerprint or active_tool.compute_fingerprint()

                # Check if state changed while waiting for lock
                if active_tool.version != base_version or (base_fp and active_fp != base_fp):
                    return RecoveryResult(
                        status=RecoveryStatus.STALE_SKIPPED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=active_tool.version,
                        base_fingerprint=active_fp,
                        termination_reason=(
                            f"Stale recovery observation: active registry tool '{tool_name}' was already updated "
                            f"to v{active_tool.version} ({active_fp[:8]}) while waiting for lock. Speculative execution avoided."
                        ),
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

                # -------------------------------------------------------------
                # 4. Pure Deterministic Proposal Formulation (with bounded LLM fallback)
                # -------------------------------------------------------------
                plan_result: PlanningResult = self.planner(
                    snapshot=active_tool,
                    observation=observation,
                    classification=classification,
                    approved_header_credentials=self.approved_header_credentials,
                )
                diagnosis_source = "deterministic"
                diagnosis_tokens = 0
                diagnosis_token_details: Optional[Dict[str, int]] = None

                # Fallback to LLM diagnoser if deterministic planning returned insufficient_evidence or unsupported
                if (
                    plan_result.status in (PlanningStatus.INSUFFICIENT_EVIDENCE, PlanningStatus.UNSUPPORTED)
                    and self.llm_diagnoser is not None
                    and classification.diagnosis_eligible
                    and classification.category == SemanticFailureCategory.SCHEMA_DRIFT
                    and not observation.uncertain_write
                    and op_key not in self._diagnosed_operations
                ):
                    self._diagnosed_operations.add(op_key)
                    rem_time = None
                    if timeout_budget is not None:
                        rem_time = max(0.0, timeout_budget - (time.perf_counter() - start_time))
                    diag_budget = min(self.diagnosis_timeout, rem_time) if rem_time is not None else self.diagnosis_timeout

                    if diag_budget > 0.05:
                        llm_result: PlanningResult = await self.llm_diagnoser.diagnose(
                            snapshot=active_tool,
                            observation=observation,
                            classification=classification,
                            timeout_budget=diag_budget,
                        )
                        if llm_result.diagnosis_tokens:
                            diagnosis_tokens = llm_result.diagnosis_tokens.get("total_tokens", 0)
                            diagnosis_token_details = llm_result.diagnosis_tokens
                        if llm_result.status == PlanningStatus.PROPOSED:
                            plan_result = llm_result
                            diagnosis_source = "llm_assisted"
                        else:
                            return RecoveryResult(
                                status=RecoveryStatus.PLANNING_FAILED,
                                recovered=False,
                                tool_name=tool_name,
                                base_version=active_tool.version,
                                base_fingerprint=active_fp,
                                termination_reason=f"LLM diagnosis could not formulate a safe proposal: {'; '.join(llm_result.reasons)}",
                                duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                                diagnosis_source="llm_assisted",
                                diagnosis_tokens=diagnosis_tokens,
                                diagnosis_token_details=diagnosis_token_details,
                            )

                if plan_result.status != PlanningStatus.PROPOSED:
                    return RecoveryResult(
                        status=RecoveryStatus.PLANNING_FAILED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=active_tool.version,
                        base_fingerprint=active_fp,
                        termination_reason=f"Planner could not formulate a safe proposal: {'; '.join(plan_result.reasons)}",
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        diagnosis_source=diagnosis_source,
                        diagnosis_tokens=diagnosis_tokens,
                        diagnosis_token_details=diagnosis_token_details,
                    )

                proposal_fp = plan_result.proposal_fingerprint

                # Check attempted proposal fingerprint to prevent loops for the same logical operation
                attempt_key = f"{operation_identity.key}:{proposal_fp}"
                if attempt_key in self._attempted_proposals:
                    return RecoveryResult(
                        status=RecoveryStatus.PLANNING_FAILED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=active_tool.version,
                        base_fingerprint=active_fp,
                        proposal_fingerprint=proposal_fp,
                        termination_reason=f"Proposal fingerprint '{proposal_fp}' was already attempted for this operation identity. Recovery loop terminated.",
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        diagnosis_source=diagnosis_source,
                        diagnosis_tokens=diagnosis_tokens,
                        diagnosis_token_details=diagnosis_token_details,
                    )
                self._attempted_proposals.add(attempt_key)

                # Emit patch_proposed event
                self._emit_event(
                    run_id=run_id,
                    mode=mode,
                    event_type="patch_proposed",
                    tool=tool_name,
                    data={
                        "tool": tool_name,
                        "base_version": active_tool.version,
                        "proposal_fingerprint": proposal_fp,
                        "operations": [op.model_dump() for op in plan_result.operations],
                        "source": diagnosis_source,
                        "diagnosis_tokens": diagnosis_tokens,
                    },
                )

                # -------------------------------------------------------------
                # 5. Independent Safety Validation
                # -------------------------------------------------------------
                val_result: ValidationResult = self.validator(
                    snapshot=active_tool,
                    original_arguments=original_arguments,
                    observation=observation,
                    classification=classification,
                    proposal=plan_result,
                    registry_snapshot=self.registry,
                    approved_header_credentials=self.approved_header_credentials,
                    credential_resolver=self.credential_resolver,
                    execution_guarantees=self.execution_guarantees,
                )

                # Emit validation event
                emit_patch_validation_event(
                    event_broadcaster=self.events,
                    run_id=run_id,
                    validation_result=val_result,
                    mode=mode,
                )

                if val_result.status != ValidationStatus.ACCEPTED or not val_result.authorized_for_recovery:
                    return RecoveryResult(
                        status=RecoveryStatus.VALIDATION_REJECTED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=active_tool.version,
                        base_fingerprint=active_fp,
                        proposal_fingerprint=proposal_fp,
                        termination_reason=f"Validator rejected proposal: {'; '.join(val_result.rejection_reasons)}",
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        diagnosis_source=diagnosis_source,
                        diagnosis_tokens=diagnosis_tokens,
                        diagnosis_token_details=diagnosis_token_details,
                    )

                # -------------------------------------------------------------
                # 6. Stage Validated Candidate
                # -------------------------------------------------------------
                staged = await self.registry.stage_candidate(
                    tool_name=tool_name,
                    candidate_definition=val_result.candidate,
                    base_version=active_tool.version,
                )
                staged_candidate_id = staged.candidate_id
                candidate_tool = staged.candidate_definition

                # -------------------------------------------------------------
                # 7. Single Repaired HTTP Execution
                # -------------------------------------------------------------
                self._dispatched_operations.add(op_key)

                self._emit_event(
                    run_id=run_id,
                    mode=mode,
                    event_type="retry_started",
                    tool=tool_name,
                    data={
                        "tool": tool_name,
                        "candidate_id": staged_candidate_id,
                        "candidate_version": staged.target_version,
                        "proposal_fingerprint": proposal_fp,
                        "attempt": 1,
                    },
                )

                exec_ctx = ExecutionContext(
                    run_id=run_id,
                    idempotency_key=operation_identity.idempotency_key,
                    candidate_snapshot=candidate_tool,
                )

                executed_res = await self.executor.execute(
                    tool_name_or_def=candidate_tool,
                    arguments=val_result.transformed_arguments or original_arguments,
                    context=exec_ctx,
                    candidate_snapshot=candidate_tool,
                )

                if not executed_res.is_success:
                    err_msg = executed_res.failure.message if executed_res.failure else "Repaired execution failed"
                    if executed_res.failure and executed_res.failure.uncertain_write:
                        uncertain_reason = "Repaired execution produced an uncertain write downstream. Halting without further retry."
                        return RecoveryResult(
                            status=RecoveryStatus.EXECUTION_FAILED,
                            recovered=False,
                            tool_name=tool_name,
                            base_version=active_tool.version,
                            base_fingerprint=active_fp,
                            proposal_fingerprint=proposal_fp,
                            candidate_id=staged_candidate_id,
                            business_effect_occurred=False,
                            verification_succeeded=False,
                            registry_committed=False,
                            execution_result=executed_res,
                            termination_reason=uncertain_reason,
                            duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            diagnosis_source=diagnosis_source,
                            diagnosis_tokens=diagnosis_tokens,
                            diagnosis_token_details=diagnosis_token_details,
                        )

                    return RecoveryResult(
                        status=RecoveryStatus.EXECUTION_FAILED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=active_tool.version,
                        base_fingerprint=active_fp,
                        proposal_fingerprint=proposal_fp,
                        candidate_id=staged_candidate_id,
                        business_effect_occurred=False,
                        verification_succeeded=False,
                        registry_committed=False,
                        execution_result=executed_res,
                        termination_reason=f"Repaired execution failed: {err_msg}",
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        diagnosis_source=diagnosis_source,
                        diagnosis_tokens=diagnosis_tokens,
                        diagnosis_token_details=diagnosis_token_details,
                    )

                # HTTP write succeeded downstream! Record separate business outcome.
                business_effect_occurred = True

                # -------------------------------------------------------------
                # 8. Independent Business Intent Verification
                # -------------------------------------------------------------
                verif_data = await self.verifier(
                    executor=self.executor,
                    tool_name=tool_name,
                    original_arguments=original_arguments,
                    execution_result=executed_res,
                    run_id=run_id,
                )

                if not verif_data.get("verified", False):
                    # Business write succeeded, but verification failed!
                    # Do NOT rollback or report that the write was rolled back; record truthful outcomes.
                    return RecoveryResult(
                        status=RecoveryStatus.VERIFICATION_FAILED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=active_tool.version,
                        base_fingerprint=active_fp,
                        proposal_fingerprint=proposal_fp,
                        candidate_id=staged_candidate_id,
                        business_effect_occurred=True,
                        verification_succeeded=False,
                        registry_committed=False,
                        execution_result=executed_res,
                        verification_details=verif_data,
                        termination_reason=f"Independent verification failed: {verif_data.get('reason')}",
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        diagnosis_source=diagnosis_source,
                        diagnosis_tokens=diagnosis_tokens,
                        diagnosis_token_details=diagnosis_token_details,
                    )

                verification_succeeded = True

                # Emit recovery_verified event ONLY after actual verification
                self._emit_event(
                    run_id=run_id,
                    mode=mode,
                    event_type="recovery_verified",
                    tool=tool_name,
                    data={
                        "tool": tool_name,
                        "candidate_id": staged_candidate_id,
                        "verified_ticket_id": verif_data.get("actual_ticket_id"),
                        "verified_assignee_id": verif_data.get("actual_assignee_id"),
                        "verification_method": verif_data.get("verification_method", "independent_read"),
                    },
                )

                # -------------------------------------------------------------
                # 9. Atomic Registry Commit
                # -------------------------------------------------------------
                try:
                    committed_tool = await self.registry.commit_candidate(
                        candidate_id=staged_candidate_id,
                        expected_base_version=active_tool.version,
                    )
                    registry_committed = True
                except StaleVersionError as e:
                    # Write succeeded and was verified, but registry commit encountered a concurrent stale conflict
                    return RecoveryResult(
                        status=RecoveryStatus.COMMIT_FAILED,
                        recovered=False,
                        tool_name=tool_name,
                        base_version=active_tool.version,
                        base_fingerprint=active_fp,
                        proposal_fingerprint=proposal_fp,
                        candidate_id=staged_candidate_id,
                        business_effect_occurred=True,
                        verification_succeeded=True,
                        registry_committed=False,
                        execution_result=executed_res,
                        verification_details=verif_data,
                        termination_reason=f"Atomic registry commit failed due to stale version conflict: {e}",
                        duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        diagnosis_source=diagnosis_source,
                        diagnosis_tokens=diagnosis_tokens,
                        diagnosis_token_details=diagnosis_token_details,
                    )

                # Emit registry_committed event ONLY after actual commit
                self._emit_event(
                    run_id=run_id,
                    mode=mode,
                    event_type="registry_committed",
                    tool=tool_name,
                    data={
                        "tool": tool_name,
                        "candidate_id": staged_candidate_id,
                        "base_version": active_tool.version,
                        "committed_version": committed_tool.version,
                        "committed_fingerprint": committed_tool.fingerprint,
                    },
                )

                return RecoveryResult(
                    status=RecoveryStatus.RECOVERED,
                    recovered=True,
                    tool_name=tool_name,
                    base_version=active_tool.version,
                    base_fingerprint=active_fp,
                    proposal_fingerprint=proposal_fp,
                    candidate_id=staged_candidate_id,
                    committed_version=committed_tool.version,
                    committed_fingerprint=committed_tool.fingerprint,
                    business_effect_occurred=True,
                    verification_succeeded=True,
                    registry_committed=True,
                    execution_result=executed_res,
                    verification_details=verif_data,
                    duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    diagnosis_source=diagnosis_source,
                    diagnosis_tokens=diagnosis_tokens,
                    diagnosis_token_details=diagnosis_token_details,
                )

            finally:
                # Discard staged candidate if not committed
                if staged_candidate_id is not None and not registry_committed:
                    cand = self.registry.get_candidate(staged_candidate_id)
                    if cand and cand.status == "staged":
                        self.registry.discard_candidate(staged_candidate_id)

                lock.release()

        except asyncio.TimeoutError:
            # Deadline exceeded while waiting for lock or executing
            if staged_candidate_id is not None and not registry_committed:
                self.registry.discard_candidate(staged_candidate_id)
            return RecoveryResult(
                status=RecoveryStatus.TIMEOUT,
                recovered=False,
                tool_name=tool_name,
                base_version=base_version,
                base_fingerprint=base_fp,
                proposal_fingerprint=proposal_fp,
                candidate_id=staged_candidate_id,
                business_effect_occurred=business_effect_occurred,
                verification_succeeded=verification_succeeded,
                registry_committed=False,
                termination_reason="Overall recovery deadline exceeded",
                duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )
        except asyncio.CancelledError:
            # Cancellation propagation while recording uncertainty if write was dispatched
            if staged_candidate_id is not None and not registry_committed:
                self.registry.discard_candidate(staged_candidate_id)
            raise
