"""One-request model plan, followed by deterministic, validated tool execution.

The model chooses tools and arguments. References are resolved only from observed
earlier tool results; a literal assignment ID is never accepted as a substitute.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import ValidationError, validate
from langchain_core.messages import AIMessage, ToolMessage

from orchestrator.agents.model_gateway import ModelGateway


class InvalidWorkflowPlan(ValueError):
    pass


PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_workflow_plan",
        "description": "Submit one complete ordered business-tool plan; this does not execute a tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array", "minItems": 3, "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string", "enum": ["find_user", "assign_ticket", "get_ticket"]},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool_name", "arguments"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
    },
}


def _references(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        if "$ref" in value:
            if set(value) not in ({"$ref"}, {"$ref", "$cast"}):
                raise InvalidWorkflowPlan("A result reference has unsupported fields")
            if value["$ref"] != "steps.0.user_id" or value.get("$cast") not in (None, "integer"):
                raise InvalidWorkflowPlan("Assignment must refer to the earlier find_user.user_id result")
            return [value]
        result = []
        for child in value.values():
            result.extend(_references(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_references(child))
        return result
    return []


def validate_workflow_plan(raw: Any, tool_specs: list[dict[str, Any]], target_name: str, target_ticket: int) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise InvalidWorkflowPlan("Plan is not an object")
    try:
        validate(raw, PLAN_TOOL["function"]["parameters"])
    except ValidationError as exc:
        raise InvalidWorkflowPlan("Plan does not match the required structure") from exc
    steps = raw["steps"]
    if [step["tool_name"] for step in steps] != ["find_user", "assign_ticket", "get_ticket"]:
        raise InvalidWorkflowPlan("Plan must resolve the user, assign the ticket, then verify the ticket")
    definitions = {item["function"]["name"]: item["function"]["parameters"] for item in tool_specs}
    if set(definitions) != {"find_user", "assign_ticket", "get_ticket"}:
        raise InvalidWorkflowPlan("The active business tool catalogue is incomplete")
    for step in steps:
        name, args = step["tool_name"], step["arguments"]
        schema = definitions[name]
        properties = schema.get("properties", {})
        if not isinstance(args, dict) or set(args) - set(properties) or not set(schema.get("required", [])).issubset(args):
            raise InvalidWorkflowPlan(f"{name} arguments do not match the active tool schema")
    if steps[0]["arguments"] != {"name": target_name}:
        raise InvalidWorkflowPlan("find_user must use the name from the requested task")
    if steps[2]["arguments"] != {"ticket_id": target_ticket}:
        raise InvalidWorkflowPlan("get_ticket must read the requested ticket")
    assignment = steps[1]["arguments"]
    if assignment.get("ticket_id") != target_ticket:
        raise InvalidWorkflowPlan("assign_ticket must use the requested ticket")
    refs = _references(assignment)
    if len(refs) != 1:
        raise InvalidWorkflowPlan("assign_ticket must use exactly one prior find_user result reference")
    if set(assignment) not in ({"ticket_id", "user_id"}, {"ticket_id", "assignee_id"}, {"ticket_id", "target"}):
        raise InvalidWorkflowPlan("assign_ticket contains an unsupported business argument")
    if "target" in assignment and (not isinstance(assignment["target"], dict) or set(assignment["target"]) != {"id"}):
        raise InvalidWorkflowPlan("Nested assignment target must contain only id")
    for index in (0, 2):
        try:
            validate(steps[index]["arguments"], definitions[steps[index]["tool_name"]])
        except ValidationError as exc:
            raise InvalidWorkflowPlan("Plan arguments fail the active tool schema") from exc
    return steps


def resolve_plan_arguments(value: Any, prior_result: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if "$ref" in value:
            if value["$ref"] != "steps.0.user_id":
                raise InvalidWorkflowPlan("Unknown result reference")
            user_id = prior_result.get("user_id")
            if not isinstance(user_id, (str, int)) or not str(user_id):
                raise InvalidWorkflowPlan("The prior find_user result has no usable user_id")
            if value.get("$cast") == "integer":
                if not str(user_id).isascii() or not str(user_id).isdigit() or (len(str(user_id)) > 1 and str(user_id).startswith("0")):
                    raise InvalidWorkflowPlan("User ID cannot be converted losslessly to integer")
                return int(user_id)
            return user_id
        return {key: resolve_plan_arguments(child, prior_result) for key, child in value.items()}
    if isinstance(value, list):
        return [resolve_plan_arguments(child, prior_result) for child in value]
    return value


class PlanReplayModel:
    """Presents one planned step per agent turn without another provider request."""

    def __init__(self, provider_model: Any, gateway: ModelGateway, target_name: str, target_ticket: int):
        self.provider_model = provider_model
        self.gateway = gateway
        self.target_name = target_name
        self.target_ticket = target_ticket
        self.tool_specs: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] | None = None
        self.next_step = 0

    def bind_tools(self, tool_specs: list[dict[str, Any]]):
        self.tool_specs = tool_specs
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        usage = None
        metadata = {}
        if self.steps is None:
            task = next((str(message.content) for message in reversed(messages) if message.type == "human"), "")
            definitions = json.dumps(self.tool_specs, separators=(",", ":"))
            prompt = (
                "Create a safe complete workflow plan for the user task. Select the available tools and their arguments. "
                "Do not execute tools, invent identifiers, or call admin endpoints. "
                "The assignment identifier MUST be a reference object like "
                '{"$ref":"steps.0.user_id"}; if the active schema requires an integer, use '
                '{"$ref":"steps.0.user_id","$cast":"integer"}. '
                "The reference resolves only after find_user actually returns. "
                "Follow the requested name and ticket exactly. Available tool contracts: " + definitions + "\nTask: " + task
            )
            bound = self.provider_model.bind_tools([PLAN_TOOL], tool_choice="submit_workflow_plan")
            response = await self.gateway.invoke(bound, prompt, purpose="planning")
            calls = getattr(response, "tool_calls", None) or []
            if len(calls) != 1 or calls[0].get("name") != "submit_workflow_plan":
                raise InvalidWorkflowPlan("Model did not return one structured workflow plan")
            self.steps = validate_workflow_plan(calls[0].get("args"), self.tool_specs, self.target_name, self.target_ticket)
            usage = getattr(response, "usage_metadata", None)
            metadata = getattr(response, "response_metadata", {}) or {}
        if self.next_step >= len(self.steps):
            return AIMessage(content="Plan execution finished; business result is verified independently.", additional_kwargs={"local_plan_replay": True})
        step = self.steps[self.next_step]
        arguments = step["arguments"]
        if self.next_step:
            previous_id = f"plan-step-{self.next_step - 1}"
            previous = next((message for message in reversed(messages) if isinstance(message, ToolMessage) and message.tool_call_id == previous_id), None)
            if previous is None:
                raise InvalidWorkflowPlan("Previous plan step has no tool result")
            try:
                result = json.loads(str(previous.content))
            except (TypeError, ValueError) as exc:
                raise InvalidWorkflowPlan("Previous tool result is not structured JSON") from exc
            if not isinstance(result, dict) or "error" in result:
                raise InvalidWorkflowPlan("Previous plan step did not succeed")
            first_result = next((message for message in messages if isinstance(message, ToolMessage) and message.tool_call_id == "plan-step-0"), None)
            if first_result is None:
                raise InvalidWorkflowPlan("find_user result is unavailable")
            try:
                arguments = resolve_plan_arguments(arguments, json.loads(str(first_result.content)))
            except (TypeError, ValueError) as exc:
                raise InvalidWorkflowPlan("find_user result cannot resolve the planned reference") from exc
        schema = next(item["function"]["parameters"] for item in self.tool_specs if item["function"]["name"] == step["tool_name"])
        try:
            validate(arguments, schema)
        except ValidationError as exc:
            raise InvalidWorkflowPlan("Resolved plan arguments fail the active tool schema") from exc
        call = {"name": step["tool_name"], "args": arguments, "id": f"plan-step-{self.next_step}"}
        self.next_step += 1
        return AIMessage(content="", tool_calls=[call], usage_metadata=usage, response_metadata=metadata,
                         additional_kwargs={"local_plan_replay": usage is None})
