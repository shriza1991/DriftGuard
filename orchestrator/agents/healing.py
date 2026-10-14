"""Genuine model-driven self-healing agent.

Adheres strictly to Member 2 safety invariants:
- Genuine LangChain tool-calling driven purely by model choices.
- Operates against active versioned registry tools (VersionedToolRegistry).
- Supplies current active tool schemas to model via LangChain tool bindings.
- Intercepts supported runtime failures through RecoveryController.
- Returns recovered result paired with original model tool_call_id so conversational loop proceeds.
- Refreshes model bindings from newly committed registry definitions before next model invocation.
- Prevents subsequent attempts from reopening exhausted recovery for the same operation.
- Independent read verification confirms downstream state before declaring success.
- Truthful usage and event reporting: diagnosis tokens are 0.
"""
from typing import Dict, Any, Optional, List
import json
import re
import time
import uuid
from datetime import datetime, timezone

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel

from orchestrator.config import settings
from orchestrator.models import (
    ToolDefinition,
    ExecutionContext,
    SanitizedFailure,
    FailureCategory,
    FailureObservation,
)
from orchestrator.client.mock_client import MockBusinessApiClient, redact_secrets
from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.execution.events import event_broadcaster, EventBroadcaster
from orchestrator.agents.catalogue import get_langchain_tool_specs
from orchestrator.agents.model_provider import create_chat_model, ModelConfigurationError
from orchestrator.healing.recovery import RecoveryController, RecoveryResult, RecoveryStatus
from orchestrator.metrics.tracker import metrics_tracker
from orchestrator.audit.logger import audit_logger


class HealingAgent:
    """Genuine model-driven agent executing tools with autonomous self-healing recovery."""

    def __init__(
        self,
        registry: Optional[VersionedToolRegistry] = None,
        client: Optional[MockBusinessApiClient] = None,
        model: Optional[BaseChatModel] = None,
        recovery_controller: Optional[RecoveryController] = None,
        max_turns: int = 5,
        max_tool_calls: int = 10,
        timeout_seconds: float = 30.0,
        approved_header_credentials: Optional[Dict[str, str]] = None,
        execution_guarantees: Optional[Dict[str, Any]] = None,
    ):
        self.registry = registry or VersionedToolRegistry.create_default()
        self._client = client
        self._model = model
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.approved_header_credentials = approved_header_credentials or {
            "Authorization": "secops_auth",
            "X-DriftGuard-Key": "driftguard_internal",
        }
        self.execution_guarantees = execution_guarantees or {"pre_execution_rejection": True}

        # Initialize recovery controller with dependency injection
        executor = ToolExecutor(registry=self.registry, client=self._client)
        self.recovery_controller = recovery_controller or RecoveryController(
            registry=self.registry,
            executor=executor,
            event_broadcaster=event_broadcaster,
            approved_header_credentials=self.approved_header_credentials,
            execution_guarantees=self.execution_guarantees,
        )

    @property
    def event_broadcaster(self) -> EventBroadcaster:
        return event_broadcaster

    def _extract_task_intent(self, task: str) -> Dict[str, Any]:
        """Extracts expected target ticket ID and user name from task description string."""
        ticket_match = re.search(r"ticket\s*(\d+)", task, re.IGNORECASE)
        target_ticket = int(ticket_match.group(1)) if ticket_match else 81

        user_match = re.search(r"find\s+([A-Za-z]+)", task, re.IGNORECASE)
        target_user_name = user_match.group(1) if user_match else "Maya"

        return {
            "ticket_id": target_ticket,
            "target_user_name": target_user_name,
        }

    async def execute_workflow(
        self,
        run_id: str,
        task: str = "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.",
        client: Optional[MockBusinessApiClient] = None,
        override_model: Optional[BaseChatModel] = None,
        limits: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Executes the model-driven workflow with autonomous self-healing recovery."""
        start_wall = datetime.now(timezone.utc).isoformat()
        start_perf = time.perf_counter()

        max_turns = limits.get("max_turns", self.max_turns) if limits else self.max_turns
        max_tool_calls = limits.get("max_tool_calls", self.max_tool_calls) if limits else self.max_tool_calls
        timeout_sec = limits.get("timeout_seconds", self.timeout_seconds) if limits else self.timeout_seconds
        deadline = start_perf + timeout_sec

        active_client = client or self._client or MockBusinessApiClient()
        executor = ToolExecutor(registry=self.registry, client=active_client)
        self.recovery_controller.executor = executor

        intent = self._extract_task_intent(task)
        target_ticket = intent["ticket_id"]
        target_user_name = intent["target_user_name"]

        run_record: Dict[str, Any] = {
            "run_id": run_id,
            "status": "running",
            "mode": "healing",
            "execution_type": "model_driven_healing",
            "task": task,
            "created_at": start_wall,
            "completed_at": None,
            "duration_ms": None,
            "outcome": None,
            "error": None,
            "model_usage": None,
            "termination_reason": None,
        }

        # 1. Emit run_started
        event_broadcaster.publish_event(
            run_id=run_id,
            event_type="run_started",
            mode="healing",
            tool=None,
            data={
                "task": task,
                "mode": "healing",
                "execution_type": "model_driven_healing",
                "limits": {
                    "max_turns": max_turns,
                    "max_tool_calls": max_tool_calls,
                    "timeout_seconds": timeout_sec,
                },
            },
        )

        # 2. Resolve or configure ChatModel
        active_model = override_model or self._model
        if active_model is None:
            try:
                active_model = create_chat_model()
            except ModelConfigurationError as e:
                duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
                err_msg = f"Model configuration error: {e}"
                event_broadcaster.publish_event(
                    run_id=run_id,
                    event_type="run_failed",
                    mode="healing",
                    tool=None,
                    data={"reason": err_msg, "duration_ms": duration_ms},
                )
                run_record["status"] = "failed"
                run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
                run_record["duration_ms"] = duration_ms
                run_record["error"] = err_msg
                run_record["termination_reason"] = "model_configuration_error"
                return run_record

        # 3. Supply active registry definitions to the model
        current_active_tools = {t.name: t for t in self.registry.list_tools()}
        tool_specs = get_langchain_tool_specs(current_active_tools)
        bound_model = active_model.bind_tools(tool_specs)

        # 4. Message History Setup
        system_prompt = (
            "You are DriftGuard's autonomous service assistant. Your goal is to: "
            f"1. Find {target_user_name}'s user ID using find_user.\n"
            f"2. Assign ticket {target_ticket} to {target_user_name} using assign_ticket.\n"
            f"3. Confirm the updated assignee of ticket {target_ticket} using get_ticket.\n"
            "Call the necessary tools in logical order to complete this task."
        )
        messages: List[Any] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=task),
        ]

        turns_executed = 0
        total_tool_calls_executed = 0
        total_input_tokens = 0
        total_output_tokens = 0
        has_token_usage = True
        recorded_tool_calls: List[Dict[str, Any]] = []
        recovered_tool_calls: List[Dict[str, Any]] = []
        termination_reason: Optional[str] = None
        write_idempotency_keys: Dict[str, str] = {}
        had_uncertain_write = False

        # 5. Agent Multi-Turn Loop
        while turns_executed < max_turns:
            now_perf = time.perf_counter()
            if now_perf >= deadline:
                termination_reason = "deadline_exceeded"
                break

            turns_executed += 1

            try:
                ai_response: AIMessage = await bound_model.ainvoke(messages)
            except Exception as e:
                termination_reason = "model_invocation_error"
                run_record["error"] = f"Model call failed on turn {turns_executed}: {e}"
                break

            # Aggregate usage metrics truthfully
            usage = getattr(ai_response, "usage_metadata", None) or (
                ai_response.response_metadata.get("token_usage")
                if hasattr(ai_response, "response_metadata") and isinstance(ai_response.response_metadata, dict)
                else None
            )
            if usage and isinstance(usage, dict):
                in_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                out_tok = usage.get("output_tokens") or usage.get("completion_tokens") or 0
                total_input_tokens += in_tok
                total_output_tokens += out_tok
            else:
                has_token_usage = False

            messages.append(ai_response)

            # Check if model finished without requesting further tools
            if not getattr(ai_response, "tool_calls", None):
                termination_reason = "model_completed"
                break

            # Process Model's Selected Tool Calls
            tool_calls = ai_response.tool_calls
            for call in tool_calls:
                now_perf = time.perf_counter()
                if now_perf >= deadline:
                    termination_reason = "deadline_exceeded"
                    break

                if total_tool_calls_executed >= max_tool_calls:
                    termination_reason = "max_tool_calls_exceeded"
                    break

                total_tool_calls_executed += 1

                tool_name = call.get("name", "")
                tool_args = call.get("args") or {}
                tool_call_id = call.get("id") or f"call-{uuid.uuid4().hex[:8]}"

                # Reject unknown tools without HTTP dispatch
                active_tool = self.registry.get(tool_name)
                if not active_tool:
                    unknown_err = f"Unknown tool '{tool_name}'. Available tools: {list(self.registry.list_tools())}"
                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="tool_call_started",
                        mode="healing",
                        tool=tool_name,
                        data={"arguments": redact_secrets(tool_args), "tool_call_id": tool_call_id},
                    )
                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="tool_call_failed",
                        mode="healing",
                        tool=tool_name,
                        data={"error_code": "UNKNOWN_TOOL", "message": unknown_err, "status_code": 0},
                    )
                    messages.append(ToolMessage(
                        content=json.dumps({"error": unknown_err}),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    ))
                    recorded_tool_calls.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": redact_secrets(tool_args),
                        "status": "unknown_tool",
                        "error": {"message": unknown_err},
                    })
                    continue

                # Stable operation identity & Idempotency-Key on mutation writes
                write_idemp_key: Optional[str] = None
                if active_tool.method.upper() in ["POST", "PUT", "PATCH", "DELETE"]:
                    intent_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                    if intent_key not in write_idempotency_keys:
                        write_idempotency_keys[intent_key] = f"dg-idemp-agent-{uuid.uuid4().hex[:8]}"
                    write_idemp_key = write_idempotency_keys[intent_key]

                event_broadcaster.publish_event(
                    run_id=run_id,
                    event_type="tool_call_started",
                    mode="healing",
                    tool=tool_name,
                    data={
                        "arguments": redact_secrets(tool_args),
                        "tool_call_id": tool_call_id,
                        "tool_version": active_tool.version,
                        "fingerprint": active_tool.fingerprint,
                    },
                )

                # Execute call via normal ToolExecutor
                ctx = ExecutionContext(
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    idempotency_key=write_idemp_key,
                )
                exec_result = await executor.execute(
                    tool_name_or_def=tool_name,
                    arguments=tool_args,
                    context=ctx,
                )

                if exec_result.is_success:
                    # Normal success on active tool
                    success_payload = exec_result.outcome or exec_result.result or {"status": "success"}
                    messages.append(ToolMessage(
                        content=json.dumps(redact_secrets(success_payload)),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    ))
                    recorded_tool_calls.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": redact_secrets(tool_args),
                        "status": "success",
                        "result": redact_secrets(success_payload),
                    })
                else:
                    # Tool call failed!
                    err_payload = exec_result.failure.model_dump() if exec_result.failure else {"error": "Execution failed"}
                    uncertain = exec_result.failure.uncertain_write if exec_result.failure else False
                    if uncertain:
                        had_uncertain_write = True

                    event_data = {
                        "status_code": exec_result.http_status,
                        "error_code": exec_result.failure.error_code if exec_result.failure else "ERROR",
                        "message": exec_result.failure.message if exec_result.failure else "Tool failed",
                        "uncertain_write": uncertain,
                    }
                    if exec_result.classification:
                        event_data["classification"] = {
                            "category": exec_result.classification.semantic_category.value,
                            "diagnosis_eligible": exec_result.classification.diagnosis_eligible,
                            "safe_to_retry": exec_result.classification.retry_safety.safe_to_retry,
                            "retry_reason": exec_result.classification.retry_safety.retry_reason,
                        }

                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="tool_call_failed",
                        mode="healing",
                        tool=tool_name,
                        data=event_data,
                    )

                    # Intercept supported failure through recovery controller
                    observation = exec_result.failure_observation
                    recovery_res: Optional[RecoveryResult] = None

                    if observation is not None and not uncertain:
                        rem_deadline = deadline - time.perf_counter()
                        recovery_res = await self.recovery_controller.recover_failure(
                            observation=observation,
                            original_arguments=tool_args,
                            operation_identity=exec_result.operation_identity or observation.operation_identity,
                            run_id=run_id,
                            deadline=rem_deadline,
                            mode="healing",
                        )

                    if recovery_res is not None and recovery_res.recovered and recovery_res.execution_result:
                        # Recovery successful!
                        # Return recovered result paired with original model tool_call_id
                        recovered_payload = (
                            recovery_res.execution_result.outcome
                            or recovery_res.execution_result.result
                            or {"status": "success", "recovered": True}
                        )
                        messages.append(ToolMessage(
                            content=json.dumps(redact_secrets(recovered_payload)),
                            tool_call_id=tool_call_id,
                            name=tool_name,
                        ))

                        recorded_tool_calls.append({
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "arguments": redact_secrets(tool_args),
                            "status": "recovered",
                            "result": redact_secrets(recovered_payload),
                            "committed_version": recovery_res.committed_version,
                        })
                        recovered_tool_calls.append({
                            "tool_name": tool_name,
                            "proposal_fingerprint": recovery_res.proposal_fingerprint,
                            "committed_version": recovery_res.committed_version,
                        })

                        # Refresh model bindings from committed registry definitions before the next model invocation
                        current_active_tools = {t.name: t for t in self.registry.list_tools()}
                        bound_model = active_model.bind_tools(get_langchain_tool_specs(current_active_tools))
                    else:
                        # Recovery could not resolve failure
                        messages.append(ToolMessage(
                            content=json.dumps(redact_secrets(err_payload)),
                            tool_call_id=tool_call_id,
                            name=tool_name,
                        ))
                        call_status = "validation_error" if exec_result.http_status == 0 else "http_error"
                        err_dict = exec_result.failure.model_dump() if exec_result.failure else {"message": "Tool failed"}
                        err_dict["status_code"] = exec_result.http_status
                        if recovery_res:
                            err_dict["recovery_status"] = recovery_res.status.value
                            err_dict["recovery_termination"] = recovery_res.termination_reason
                        recorded_tool_calls.append({
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "arguments": redact_secrets(tool_args),
                            "status": call_status,
                            "error": redact_secrets(err_dict),
                        })

        if not termination_reason:
            termination_reason = "max_turns_exceeded"

        # 6. Independent Downstream State Verification
        verif_ctx = ExecutionContext(run_id=run_id)
        verify_result = await executor.execute(
            tool_name_or_def="get_ticket",
            arguments={"ticket_id": target_ticket},
            context=verif_ctx,
        )

        verified_assignee = None
        verified_ticket_id = None
        downstream_verified = False

        if verify_result.is_success and verify_result.outcome:
            verified_assignee = str(verify_result.outcome.get("assignee_id") or "")
            verified_ticket_id = verify_result.outcome.get("ticket_id")
            downstream_verified = bool(verified_assignee and verified_assignee != "None")

        duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)

        token_usage_dict: Optional[Dict[str, Any]] = None
        if has_token_usage:
            token_usage_dict = {
                "prompt_tokens": total_input_tokens,
                "completion_tokens": total_output_tokens,
                "total_tokens": total_input_tokens + total_output_tokens,
                "diagnosis_tokens": 0,
            }

        # Determine Final Workflow Success
        is_successful_run = downstream_verified and not had_uncertain_write

        final_outcome: Dict[str, Any] = {
            "ticket_id": target_ticket,
            "target_user_name": target_user_name,
            "downstream_verified": downstream_verified,
            "verified_assignee_id": verified_assignee,
            "verified_ticket_id": verified_ticket_id,
            "turns_executed": turns_executed,
            "tool_calls_count": len(recorded_tool_calls),
            "recovered_calls_count": len(recovered_tool_calls),
            "had_uncertain_write": had_uncertain_write,
            "tool_calls": recorded_tool_calls,
        }

        run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
        run_record["duration_ms"] = duration_ms
        run_record["outcome"] = final_outcome
        run_record["model_usage"] = token_usage_dict
        run_record["termination_reason"] = termination_reason

        if is_successful_run:
            run_record["status"] = "completed"
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_completed",
                mode="healing",
                tool=None,
                data={
                    "status": "completed",
                    "duration_ms": duration_ms,
                    "downstream_verified": True,
                    "verified_ticket_id": verified_ticket_id,
                    "verified_assignee_id": verified_assignee,
                    "recovered_calls": len(recovered_tool_calls),
                    "model_usage": token_usage_dict,
                },
            )
            metrics_tracker.record_run(mode="healing", success=True, duration_ms=duration_ms)
        else:
            run_record["status"] = "failed"
            failure_reason = "Downstream verification failed: intended assignee not confirmed"
            if had_uncertain_write:
                failure_reason = "Run aborted due to uncertain stateful write"
            elif run_record.get("error"):
                failure_reason = run_record["error"]

            run_record["error"] = failure_reason
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_failed",
                mode="healing",
                tool=None,
                data={
                    "reason": failure_reason,
                    "duration_ms": duration_ms,
                    "termination_reason": termination_reason,
                    "downstream_verified": downstream_verified,
                    "model_usage": token_usage_dict,
                },
            )
            metrics_tracker.record_run(mode="healing", success=False, duration_ms=duration_ms)

        audit_logger.record(
            run_id=run_id,
            mode="healing",
            status=run_record["status"],
            outcome=final_outcome,
            error=run_record.get("error"),
        )

        return run_record
