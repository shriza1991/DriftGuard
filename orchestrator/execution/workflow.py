import time
import uuid
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.events import event_broadcaster
from orchestrator.execution.tool_caller import http_tool_caller, ToolExecutionError
from orchestrator.healing.stubs import healing_engine
from orchestrator.metrics.tracker import metrics_tracker
from orchestrator.audit.logger import audit_logger


class WorkflowExecutionManager:
    """Manages execution of the canonical DriftGuard workflow:
    'Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.'
    """
    def __init__(self, registry: Optional[VersionedToolRegistry] = None):
        self._registry = registry or VersionedToolRegistry.create_default()
        self._runs_store: Dict[str, Dict[str, Any]] = {}

    def get_registry(self) -> VersionedToolRegistry:
        return self._registry

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._runs_store.get(run_id)

    def reset(self):
        self._runs_store.clear()
        self._registry = VersionedToolRegistry.create_default()
        event_broadcaster.reset()
        metrics_tracker.reset()
        audit_logger.reset()

    async def execute_workflow(
        self,
        run_id: str,
        mode: str = "normal",
        task: str = "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."
    ) -> Dict[str, Any]:
        start_time = time.time()
        created_at = datetime.now(timezone.utc).isoformat()

        run_record = {
            "run_id": run_id,
            "status": "running",
            "mode": mode,
            "task": task,
            "created_at": created_at,
            "completed_at": None,
            "duration_ms": None,
            "outcome": None,
            "error": None
        }
        self._runs_store[run_id] = run_record

        # 1. Publish run_started
        event_broadcaster.publish_event(
            run_id=run_id,
            event_type="run_started",
            mode=mode,
            tool=None,
            data={"task": task, "mode": mode}
        )

        try:
            # Step 1: Find Maya's user ID
            find_tool = self._registry["find_user"]
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="tool_call_started",
                mode=mode,
                tool="find_user",
                data={"arguments": {"name": "Maya"}}
            )

            try:
                users_data = await http_tool_caller.call_tool(find_tool, {"name": "Maya"})
            except ToolExecutionError as e:
                event_broadcaster.publish_event(
                    run_id=run_id,
                    event_type="tool_call_failed",
                    mode=mode,
                    tool="find_user",
                    data={"status_code": e.status_code, "error": e.response_data}
                )
                raise e

            if not users_data or not isinstance(users_data, list):
                raise ValueError("Could not find user Maya in user directory")
            
            maya_id = users_data[0].get("id", "123")

            # Step 2: Assign Ticket 81
            assign_tool = self._registry["assign_ticket"]
            assign_args = {"ticket_id": 81, "user_id": maya_id}
            
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="tool_call_started",
                mode=mode,
                tool="assign_ticket",
                data={"arguments": assign_args}
            )

            try:
                assign_res = await http_tool_caller.call_tool(assign_tool, assign_args)
            except ToolExecutionError as e:
                # Tool call failed (observed drift or error)
                event_broadcaster.publish_event(
                    run_id=run_id,
                    event_type="tool_call_failed",
                    mode=mode,
                    tool="assign_ticket",
                    data={"status_code": e.status_code, "error": e.response_data}
                )

                if mode == "healing":
                    # Autonomous healing branch (Member 2 scope)
                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="diagnosis_started",
                        mode=mode,
                        tool="assign_ticket",
                        data={"observed_status": e.status_code, "message": "Analyzing runtime failure evidence..."}
                    )
                    
                    healing_result = await healing_engine.attempt_healing(
                        tool_name="assign_ticket",
                        failed_arguments=assign_args,
                        error_details=e.response_data
                    )

                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="run_failed",
                        mode=mode,
                        tool="assign_ticket",
                        data={
                            "reason": "Autonomous healing pipeline pending Member 2 implementation.",
                            "status": healing_result.status,
                            "notice": healing_result.message
                        }
                    )
                    
                    duration_ms = round((time.time() - start_time) * 1000, 2)
                    run_record["status"] = "failed"
                    run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
                    run_record["duration_ms"] = duration_ms
                    run_record["error"] = healing_result.message
                    metrics_tracker.record_run(mode=mode, success=False, duration_ms=duration_ms)
                    return run_record
                else:
                    # Normal baseline failure
                    event_broadcaster.publish_event(
                        run_id=run_id,
                        event_type="run_failed",
                        mode=mode,
                        tool="assign_ticket",
                        data={"reason": "Downstream validation failure under normal baseline mode", "error": e.response_data}
                    )
                    duration_ms = round((time.time() - start_time) * 1000, 2)
                    run_record["status"] = "failed"
                    run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
                    run_record["duration_ms"] = duration_ms
                    run_record["error"] = str(e.response_data)
                    metrics_tracker.record_run(mode=mode, success=False, duration_ms=duration_ms)
                    return run_record

            # Step 3: Independent verification via get_ticket
            get_tool = self._registry["get_ticket"]
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="tool_call_started",
                mode=mode,
                tool="get_ticket",
                data={"arguments": {"ticket_id": 81}}
            )

            ticket_data = await http_tool_caller.call_tool(get_tool, {"ticket_id": 81})
            verified_assignee = ticket_data.get("assignee_id")

            duration_ms = round((time.time() - start_time) * 1000, 2)
            outcome = {
                "ticket_id": 81,
                "target_user_name": "Maya",
                "resolved_user_id": maya_id,
                "verified_assignee_id": verified_assignee,
                "verified": verified_assignee == maya_id,
                "status": ticket_data.get("status", "assigned")
            }

            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_completed",
                mode=mode,
                tool=None,
                data={"outcome": outcome, "duration_ms": duration_ms}
            )

            run_record["status"] = "completed"
            run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
            run_record["duration_ms"] = duration_ms
            run_record["outcome"] = outcome

            metrics_tracker.record_run(mode=mode, success=True, duration_ms=duration_ms)
            audit_logger.log(action="run_completed", actor="orchestrator", details=outcome)
            return run_record

        except Exception as exc:
            duration_ms = round((time.time() - start_time) * 1000, 2)
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_failed",
                mode=mode,
                tool=None,
                data={"error": str(exc), "duration_ms": duration_ms}
            )
            run_record["status"] = "failed"
            run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
            run_record["duration_ms"] = duration_ms
            run_record["error"] = str(exc)
            metrics_tracker.record_run(mode=mode, success=False, duration_ms=duration_ms)
            return run_record


workflow_manager = WorkflowExecutionManager()
