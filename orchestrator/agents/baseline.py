"""Genuine fixed-registry LangChain model-driven baseline agent.

Adheres strictly to Member 2 safety invariants:
- Genuine LangChain tool-calling driven purely by model choices (no deterministic substitute).
- Operates strictly against the canonical immutable frozen v1 catalogue (1.0.0).
- Active registry commits or candidate stagings NEVER alter baseline schemas or snapshots.
- Pre-HTTP validation, credential check, and safe mapping via ToolExecutor.
- Stable logical operation identity and Idempotency-Key for write operations.
- Rejects unknown or invalid tool calls without HTTP dispatch.
- Enforces configurable finite execution bounds (turns, tool calls, duration deadline).
- Independent read verification: checks get_ticket downstream state before declaring success.
- A final "done" response from the model without matching downstream state is a FAILURE.
- Truthful usage tracking: reports actual provider tokens, 0 diagnosis tokens, or "unavailable".
- Zero diagnosis, zero repair, zero patching, zero automatic retry loops.
"""
from typing import Dict, Any, Optional, List
import asyncio
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
)
from orchestrator.client.mock_client import MockBusinessApiClient, redact_secrets
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.execution.events import event_broadcaster
from orchestrator.agents.catalogue import get_frozen_v1_catalogue, get_langchain_tool_specs
from orchestrator.agents.model_provider import create_chat_model, ModelConfigurationError
from orchestrator.metrics.tracker import metrics_tracker
from orchestrator.audit.logger import audit_logger


class BaselineAgent:
    """Genuine model-driven baseline agent executing tools via LangChain against frozen v1 contracts."""

    def __init__(
        self,
        client: Optional[MockBusinessApiClient] = None,
        model: Optional[BaseChatModel] = None,
        max_turns: int = 5,
        max_tool_calls: int = 10,
        timeout_seconds: float = 30.0,
        audit_store: Optional[Any] = None,
    ):
        self._client = client
        self._model = model
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.catalogue = get_frozen_v1_catalogue()
        self.audit_store = audit_store

    @property
    def event_broadcaster(self):
        """Uniform access to event broadcaster instance."""
        return event_broadcaster

    def _extract_task_intent(self, task: str) -> Dict[str, Any]:
        """Extracts expected target ticket ID and user name from task description string."""
        ticket_match = re.search(r"ticket\s*(\d+)", task, re.IGNORECASE)
        if ticket_match is None:
            raise ValueError("Task must explicitly identify a ticket")
        target_ticket = int(ticket_match.group(1))

        user_match = re.search(r"find\s+([A-Za-z]+)", task, re.IGNORECASE)
        if user_match is None:
            raise ValueError("Prototype task must explicitly say Find <name>")
        target_user_name = user_match.group(1)

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
        """Executes the genuine LangChain tool-calling baseline agent."""
        start_wall = datetime.now(timezone.utc).isoformat()
        start_perf = time.perf_counter()

        # Configurable limits
        max_turns = limits.get("max_turns", self.max_turns) if limits else self.max_turns
        max_tool_calls = limits.get("max_tool_calls", self.max_tool_calls) if limits else self.max_tool_calls
        timeout_sec = limits.get("timeout_seconds", self.timeout_seconds) if limits else self.timeout_seconds
        deadline = start_perf + timeout_sec

        active_client = client or self._client or MockBusinessApiClient()
        executor = ToolExecutor(client=active_client)

        intent = self._extract_task_intent(task)
        target_ticket = intent["ticket_id"]
        target_user_name = intent["target_user_name"]

        run_record: Dict[str, Any] = {
            "run_id": run_id,
            "status": "running",
            "mode": "normal",
            "execution_type": "model_driven_baseline",
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
            mode="normal",
            tool=None,
            data={
                "task": task,
                "mode": "normal",
                "execution_type": "model_driven_baseline",
            },
        )
        if self.audit_store:
            await self.audit_store.record_run_start(
                run_id=run_id,
                mode="normal",
                task=task,
                execution_type="model_driven_baseline",
                created_at=start_wall,
            )

        # 2. Initialize Model (lazy instantiation with explicit error on missing credentials)
        try:
            chat_model = create_chat_model(override_model=override_model or self._model)
            model_with_tools = chat_model.bind_tools(get_langchain_tool_specs())
        except ModelConfigurationError as mce:
            duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
            completed_wall = datetime.now(timezone.utc).isoformat()
            run_record["status"] = "failed"
            run_record["completed_at"] = completed_wall
            run_record["duration_ms"] = duration_ms
            run_record["error"] = str(mce)
            run_record["termination_reason"] = "configuration_error"
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_failed",
                mode="normal",
                tool=None,
                data={"reason": str(mce), "duration_ms": duration_ms},
            )
            if self.audit_store:
                await self.audit_store.record_run_completed(
                    run_id=run_id,
                    status="failed",
                    duration_ms=duration_ms,
                    error=str(mce),
                    termination_reason="configuration_error",
                    completed_at=completed_wall,
                )
            return run_record

        # 3. Conversation & Tool Execution Loop
        system_prompt = (
            "You are an autonomous ticketing assistant. Your task is to resolve user requests by calling available tools.\n"
            "Rules:\n"
            "1. First find the user's ID by name using find_user.\n"
            "2. Assign the requested ticket to the resolved user ID using assign_ticket.\n"
            "3. Verify the assignee on the ticket using get_ticket.\n"
            "4. Only declare completion once you have confirmed the assignment state."
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=task),
        ]

        write_idempotency_keys: Dict[str, str] = {}
        resolved_user_id: Optional[str] = None
        had_uncertain_write = False
        termination_reason = "completed"
        recorded_tool_calls: List[Dict[str, Any]] = []

        total_input_tokens = 0
        total_output_tokens = 0
        has_token_usage = True

        turn = 0
        total_tool_calls_executed = 0

        while turn < max_turns and total_tool_calls_executed < max_tool_calls:
            turn += 1
            now_perf = time.perf_counter()
            rem_time = deadline - now_perf
            if rem_time <= 0:
                termination_reason = "deadline_exceeded"
                break

            # Model Turn with timeout deadline
            try:
                ai_response: AIMessage = await asyncio.wait_for(
                    model_with_tools.ainvoke(messages),
                    timeout=rem_time,
                )
            except asyncio.TimeoutError:
                termination_reason = "deadline_exceeded"
                break
            except Exception as e:
                termination_reason = f"model_error: {str(e)}"
                break

            # Aggregate usage metrics truthfully without double-counting
            usage = getattr(ai_response, "usage_metadata", None) or (
                ai_response.response_metadata.get("token_usage")
                if hasattr(ai_response, "response_metadata") and isinstance(ai_response.response_metadata, dict)
                else None
            )
            if usage and isinstance(usage, dict):
                raw_in = usage.get("input_tokens", usage.get("prompt_tokens"))
                raw_out = usage.get("output_tokens", usage.get("completion_tokens"))
                complete_usage = isinstance(raw_in, int) and isinstance(raw_out, int)
                has_token_usage = has_token_usage and complete_usage
                in_tok = raw_in if isinstance(raw_in, int) else 0
                out_tok = raw_out if isinstance(raw_out, int) else 0
                total_input_tokens += in_tok
                total_output_tokens += out_tok
                if self.audit_store:
                    await self.audit_store.record_model_usage(
                        run_id=run_id,
                        call_id=f"turn-{turn}",
                        purpose="agent_turn",
                        prompt_tokens=raw_in,
                        completion_tokens=raw_out,
                        total_tokens=in_tok + out_tok if complete_usage else None,
                        is_complete=complete_usage,
                    )
            else:
                has_token_usage = False
                if self.audit_store:
                    await self.audit_store.record_model_usage(
                        run_id=run_id,
                        call_id=f"turn-{turn}",
                        purpose="agent_turn",
                        prompt_tokens=None,
                        completion_tokens=None,
                        total_tokens=None,
                        is_complete=False,
                    )

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
                if tool_name not in self.catalogue:
                    unknown_err = f"Unknown tool '{tool_name}'. Available tools: {list(self.catalogue.keys())}"
                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="tool_call_started",
                        mode="normal",
                        tool=tool_name,
                        data={"arguments": redact_secrets(tool_args), "tool_call_id": tool_call_id},
                    )
                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="tool_call_failed",
                        mode="normal",
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

                # Retrieve frozen v1 snapshot
                frozen_tool = self.catalogue[tool_name]

                if tool_name == "assign_ticket":
                    supplied_user = tool_args.get("user_id", tool_args.get("assignee_id", tool_args.get("target", {}).get("id")))
                    if resolved_user_id is None or str(supplied_user) != str(resolved_user_id) or tool_args.get("ticket_id") != target_ticket:
                        message = "Assignment does not match independently resolved task intent; resolve the requested user first"
                        messages.append(ToolMessage(content=json.dumps({"error": message}), tool_call_id=tool_call_id, name=tool_name))
                        recorded_tool_calls.append({"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": redact_secrets(tool_args), "status": "intent_rejected"})
                        continue

                # Stable operation identity & Idempotency-Key on mutation writes
                write_idemp_key: Optional[str] = None
                if frozen_tool.method.upper() in ["POST", "PUT", "PATCH", "DELETE"]:
                    intent_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                    if intent_key not in write_idempotency_keys:
                        write_idempotency_keys[intent_key] = f"dg-idemp-agent-{uuid.uuid4().hex[:8]}"
                    write_idemp_key = write_idempotency_keys[intent_key]

                event_broadcaster.publish_event(
                    run_id=run_id,
                    event_type="tool_call_started",
                    mode="normal",
                    tool=tool_name,
                    data={
                        "arguments": redact_secrets(tool_args),
                        "tool_call_id": tool_call_id,
                        "tool_version": frozen_tool.version,
                        "fingerprint": frozen_tool.fingerprint,
                    },
                )

                # Execute call via ToolExecutor using frozen v1 snapshot
                ctx = ExecutionContext(
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    idempotency_key=write_idemp_key,
                )
                exec_result = await asyncio.wait_for(executor.execute(
                    tool_name_or_def=tool_name,
                    arguments=tool_args,
                    context=ctx,
                    candidate_snapshot=frozen_tool,
                ), timeout=max(0.0, deadline - time.perf_counter()))

                if self.audit_store:
                    await self.audit_store.record_tool_execution(
                        run_id=run_id,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        tool_version=frozen_tool.version,
                        fingerprint=frozen_tool.fingerprint or "",
                        attempt_number=1,
                        is_repaired=False,
                        arguments=tool_args,
                        status="success" if exec_result.is_success else "failed",
                        result=exec_result.outcome or exec_result.result,
                        failure=exec_result.failure.model_dump() if exec_result.failure else None,
                        timing={"duration_ms": exec_result.timing.duration_ms} if exec_result else None,
                        operation_identity=exec_result.operation_identity.key if exec_result.operation_identity else None,
                    )

                if not exec_result.is_success:
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
                        mode="normal",
                        tool=tool_name,
                        data=event_data,
                    )
                    messages.append(ToolMessage(
                        content=json.dumps(redact_secrets(err_payload)),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    ))

                    call_status = "validation_error" if exec_result.http_status == 0 else "http_error"
                    err_dict = exec_result.failure.model_dump() if exec_result.failure else {"message": "Tool failed"}
                    err_dict["status_code"] = exec_result.http_status
                    recorded_tool_calls.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": redact_secrets(tool_args),
                        "status": call_status,
                        "error": redact_secrets(err_dict),
                        "uncertain_write": uncertain,
                    })
                    if had_uncertain_write:
                        break
                else:
                    outcome_data = exec_result.outcome or {}
                    if tool_name == "find_user" and "user_id" in outcome_data and str(tool_args.get("name", "")).casefold() == target_user_name.casefold():
                        resolved_user_id = str(outcome_data["user_id"])

                    messages.append(ToolMessage(
                        content=json.dumps(redact_secrets(outcome_data)),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    ))
                    recorded_tool_calls.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": redact_secrets(tool_args),
                        "status": "success",
                        "outcome": redact_secrets(outcome_data),
                    })

            if had_uncertain_write:
                termination_reason = "uncertain_write"
                break

            if termination_reason in ["deadline_exceeded", "max_tool_calls_exceeded"]:
                break

        if turn >= max_turns and termination_reason not in ["model_completed", "deadline_exceeded", "max_tool_calls_exceeded"]:
            termination_reason = "max_turns_exceeded"

        # 4. Independent Read Verification
        # Derive expectations from genuine tool evidence, never from the model's claim alone
        verified_assignee_id: Optional[str] = None
        verified_ticket_id: Optional[int] = None
        ticket_status: Optional[str] = None
        is_verified = False
        verification_error: Optional[str] = None

        # Execute independent read verification via get_ticket
        get_ticket_tool = self.catalogue["get_ticket"]
        verify_ctx = ExecutionContext(
            run_id=run_id,
            tool_call_id=f"call-verify-{uuid.uuid4().hex[:6]}",
        )
        try:
            read_result = await asyncio.wait_for(executor.execute(
                "get_ticket",
                {"ticket_id": target_ticket},
                context=verify_ctx,
                candidate_snapshot=get_ticket_tool,
            ), timeout=max(0.0, deadline - time.perf_counter()))
        except asyncio.TimeoutError:
            read_result = None
            termination_reason = "deadline_exceeded"

        if read_result is not None and read_result.is_success and read_result.outcome:
            verified_assignee_id = read_result.outcome.get("assignee_id")
            verified_ticket_id = read_result.outcome.get("ticket_id")
            ticket_status = read_result.outcome.get("status")

            if resolved_user_id is None:
                verification_error = f"Cannot verify assignment: target user '{target_user_name}' was never resolved in directory."
            elif str(verified_assignee_id) != str(resolved_user_id):
                verification_error = (
                    f"Verification mismatch: expected ticket {target_ticket} assigned to '{resolved_user_id}', "
                    f"but downstream state shows assignee '{verified_assignee_id}'."
                )
            elif str(verified_ticket_id) != str(target_ticket):
                verification_error = (
                    f"Verification mismatch: expected ticket {target_ticket}, but read returned '{verified_ticket_id}'."
                )
            else:
                is_verified = True
        else:
            verification_error = (
                read_result.failure.message
                if read_result is not None and read_result.failure
                else "Independent read verification call to get_ticket failed."
            )

        duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
        completed_wall = datetime.now(timezone.utc).isoformat()

        # Build usage metadata
        if has_token_usage and (total_input_tokens > 0 or total_output_tokens > 0):
            usage_dict: Dict[str, Any] = {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_input_tokens + total_output_tokens,
                "diagnosis_tokens": 0,
                "model_name": settings.model_name,
                "provider": settings.model_provider,
                "is_complete": True,
            }
        elif not has_token_usage and (total_input_tokens > 0 or total_output_tokens > 0):
            usage_dict = {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_input_tokens + total_output_tokens,
                "subtotal_status": "incomplete_subtotal",
                "is_complete": False,
                "diagnosis_tokens": 0,
                "model_name": settings.model_name,
                "provider": settings.model_provider,
            }
        else:
            usage_dict = {
                "input_tokens": "unavailable",
                "output_tokens": "unavailable",
                "total_tokens": "unavailable",
                "token_usage": "unavailable",
                "is_complete": False,
                "diagnosis_tokens": 0,
                "model_name": settings.model_name,
                "provider": settings.model_provider,
            }

        outcome = {
            "ticket_id": target_ticket,
            "target_user_name": target_user_name,
            "resolved_user_id": resolved_user_id,
            "verified_assignee_id": verified_assignee_id,
            "verified": is_verified,
            "execution_type": "model_driven_baseline",
            "status": ticket_status or "unverified",
            "uncertain_write": had_uncertain_write,
        }

        if is_verified and termination_reason in ["completed", "model_completed"]:
            termination_reason = "completed_and_verified"
        elif not is_verified and termination_reason in ["completed", "model_completed"]:
            termination_reason = "verification_failed"

        is_success = is_verified and termination_reason == "completed_and_verified"

        run_record["completed_at"] = completed_wall
        run_record["duration_ms"] = duration_ms
        run_record["outcome"] = outcome
        run_record["model_usage"] = usage_dict
        run_record["usage"] = usage_dict
        run_record["token_usage"] = usage_dict
        run_record["termination_reason"] = termination_reason
        run_record["success"] = is_success
        run_record["status"] = "completed" if is_success else "failed"
        run_record["turns"] = turn
        run_record["tool_calls"] = recorded_tool_calls
        run_record["verification"] = {
            "verified": is_verified,
            "resolved_user_id": resolved_user_id,
            "actual_assignee": verified_assignee_id,
            "target_ticket": target_ticket,
        }

        if is_success:
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_completed",
                mode="normal",
                tool=None,
                data={
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "model_usage": usage_dict,
                    "termination_reason": termination_reason,
                },
            )
            metrics_tracker.record_run(mode="normal", success=True, duration_ms=duration_ms)
            audit_logger.log(action="baseline_completed", actor="baseline_agent", details=outcome)
        else:
            failure_reason = verification_error or f"Baseline execution terminated with reason: {termination_reason}"
            run_record["error"] = failure_reason
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_failed",
                mode="normal",
                tool=None,
                data={
                    "reason": failure_reason,
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "model_usage": usage_dict,
                    "termination_reason": termination_reason,
                },
            )
            metrics_tracker.record_run(mode="normal", success=False, duration_ms=duration_ms)
            audit_logger.log(action="baseline_failed", actor="baseline_agent", details=outcome)

        if self.audit_store:
            await self.audit_store.record_run_completed(
                run_id=run_id,
                status=run_record["status"],
                duration_ms=duration_ms,
                verified_outcome=outcome,
                error=run_record.get("error"),
                termination_reason=termination_reason,
                completed_at=completed_wall,
            )

        return run_record
