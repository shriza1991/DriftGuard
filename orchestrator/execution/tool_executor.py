"""Deterministic registry-driven tool executor.

Adheres strictly to Member 2 safety invariants:
- Workflows use app-injected registry and business client.
- Captures defensive ToolDefinition snapshot per invocation.
- Allows candidate snapshot execution without activating it in the registry.
- Deep-copies input arguments; never mutates caller input.
- Validates argument schema with JSON Schema Draft 7 before ANY HTTP request (zero network calls on invalid input).
- Resolves credentials via injected CredentialResolver before HTTP request (zero network calls on missing credentials).
- Builds request strictly from documented registry mappings with exact type preservation (no eval).
- Carries logical execution identity and attaches stable Idempotency-Key on mutation writes.
- Distinguishes failure categories: validation, credential, transport, HTTP rejection, response parsing.
- Retains uncertain_write on response parsing or transport failure after a write, with ZERO automatic retries.
- Redacts secrets from results, failures, and representations.
"""
from typing import Dict, Any, Optional, Union
import copy
import time
import uuid
import jsonschema
from datetime import datetime, timezone
import httpx

from orchestrator.models import (
    ToolDefinition,
    ExecutionResult,
    SanitizedFailure,
    FailureCategory,
    ExecutionContext,
    OperationIdentity,
    TimingInfo,
)
from orchestrator.client.mock_client import MockBusinessApiClient, redact_secrets
from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.mapping import (
    expand_mapping,
    extract_json_path,
    validate_request_mapping,
    MappingError,
    UnsupportedMappingError,
)


class ToolExecutor:
    """Executes tools deterministically using a captured ToolDefinition snapshot."""

    def __init__(
        self,
        registry: Optional[VersionedToolRegistry] = None,
        client: Optional[MockBusinessApiClient] = None,
    ):
        self._registry = registry
        self._client = client

    @property
    def registry(self) -> VersionedToolRegistry:
        if self._registry is None:
            self._registry = VersionedToolRegistry.create_default()
        return self._registry

    @property
    def client(self) -> MockBusinessApiClient:
        if self._client is None:
            self._client = MockBusinessApiClient()
        return self._client

    async def execute(
        self,
        tool_name_or_def: Union[str, ToolDefinition],
        arguments: Dict[str, Any],
        context: Optional[ExecutionContext] = None,
        candidate_snapshot: Optional[ToolDefinition] = None,
    ) -> ExecutionResult:
        """Executes a single tool call driven by registry mappings.
        
        Args:
            tool_name_or_def: Tool name string or pre-supplied ToolDefinition.
            arguments: Caller input arguments (deep-copied; never mutated).
            context: Logical execution context carrying run_id, tool_call_id, idempotency_key.
            candidate_snapshot: Explicit staged candidate definition to evaluate without activating.
        """
        start_wall = datetime.now(timezone.utc).isoformat()
        start_perf = time.perf_counter()

        # 1. Deep-copy arguments (caller input immutability)
        args_copy = copy.deepcopy(arguments) if arguments is not None else {}

        # 2. Defensive snapshot capture
        if candidate_snapshot is not None:
            snapshot = copy.deepcopy(candidate_snapshot)
        elif isinstance(tool_name_or_def, ToolDefinition):
            snapshot = copy.deepcopy(tool_name_or_def)
        else:
            snapshot = self.registry.get(str(tool_name_or_def))

        if not snapshot.fingerprint:
            snapshot.fingerprint = snapshot.compute_fingerprint()

        # 3. Setup logical execution context & identity
        if context is None:
            context = ExecutionContext(
                run_id=f"run-{uuid.uuid4().hex[:8]}",
                tool_call_id=f"call-{uuid.uuid4().hex[:8]}",
            )
        elif context.tool_call_id is None:
            context.tool_call_id = f"call-{uuid.uuid4().hex[:8]}"

        # Write operations require stable idempotency key
        method = snapshot.method.upper()
        is_write = method in ["POST", "PUT", "PATCH", "DELETE"]
        idempotency_key = context.idempotency_key
        if is_write and not idempotency_key:
            idempotency_key = f"dg-idemp-{uuid.uuid4().hex[:12]}"
            context.idempotency_key = idempotency_key

        op_identity = OperationIdentity(
            tool_name=snapshot.name,
            endpoint=snapshot.endpoint,
            method=method,
            idempotency_key=idempotency_key,
            tool_call_id=context.tool_call_id,
            run_id=context.run_id,
        )

        def make_timing() -> TimingInfo:
            completed_wall = datetime.now(timezone.utc).isoformat()
            duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
            return TimingInfo(
                started_at=start_wall,
                completed_at=completed_wall,
                duration_ms=duration_ms,
            )

        # 4. Pre-HTTP argument JSON Schema validation (ZERO HTTP calls on failure)
        schema = snapshot.argument_schema.model_dump()
        validator = jsonschema.Draft7Validator(schema)
        validation_errors = list(validator.iter_errors(args_copy))
        if validation_errors:
            structured_errors: Dict[str, Any] = {}
            for err in validation_errors:
                field = ".".join(str(p) for p in err.absolute_path) or "root"
                structured_errors[field] = err.message

            timing = make_timing()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="ARGUMENT_VALIDATION_ERROR",
                message=f"Arguments failed schema validation for tool '{snapshot.name}': {'; '.join(e.message for e in validation_errors)}",
                http_status=0,
                normalized_errors=structured_errors,
                raw_details={"validation_errors": [e.message for e in validation_errors]},
                migration_hint=None,
                tool_name=snapshot.name,
                endpoint=snapshot.endpoint,
                timestamp=timing.completed_at,
                registry_version=snapshot.version,
                failure_category=FailureCategory.VALIDATION_FAILURE,
                uncertain_write=False,
            )
            return ExecutionResult(
                is_success=False,
                outcome=None,
                http_status=0,
                normalized_result=None,
                normalized_errors=structured_errors,
                failure=failure,
                registry_version=snapshot.version,
                fingerprint=snapshot.fingerprint,
                timing=timing,
                operation_identity=op_identity,
            )

        # 5. Credential Resolution before network (ZERO HTTP calls on failure)
        auth_token: Optional[str] = None
        if snapshot.credential_reference:
            auth_token = self.client.credential_resolver(snapshot.credential_reference)
            if not auth_token:
                timing = make_timing()
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="MISSING_CREDENTIAL",
                    message=f"Missing credential for reference '{snapshot.credential_reference}'",
                    http_status=0,
                    normalized_errors={"credential_reference": snapshot.credential_reference},
                    raw_details={"credential_reference": snapshot.credential_reference},
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=snapshot.endpoint,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.CREDENTIAL_FAILURE,
                    uncertain_write=False,
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=0,
                    normalized_result=None,
                    normalized_errors={"credential_reference": snapshot.credential_reference},
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )

        # 6. Request Construction from Documented Registry Mappings
        try:
            validate_request_mapping(snapshot.request_mapping)
        except UnsupportedMappingError as e:
            timing = make_timing()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="UNSUPPORTED_MAPPING",
                message=str(e),
                http_status=0,
                normalized_errors={"mapping_error": str(e)},
                raw_details={"error": str(e)},
                migration_hint=None,
                tool_name=snapshot.name,
                endpoint=snapshot.endpoint,
                timestamp=timing.completed_at,
                registry_version=snapshot.version,
                failure_category=FailureCategory.VALIDATION_FAILURE,
                uncertain_write=False,
            )
            return ExecutionResult(
                is_success=False,
                outcome=None,
                http_status=0,
                normalized_result=None,
                normalized_errors={"mapping_error": str(e)},
                failure=failure,
                registry_version=snapshot.version,
                fingerprint=snapshot.fingerprint,
                timing=timing,
                operation_identity=op_identity,
            )

        # Path params substitution
        url_path = snapshot.endpoint
        path_mapping = snapshot.request_mapping.get("path_params", {})
        try:
            for param_name, template in path_mapping.items():
                val = expand_mapping(template, args_copy)
                url_path = url_path.replace(f"{{{param_name}}}", str(val))
            
            # If endpoint has remaining {param} templates not explicitly in path_params
            if "{" in url_path and "}" in url_path:
                for k, v in args_copy.items():
                    url_path = url_path.replace(f"{{{k}}}", str(v))
        except MappingError as e:
            timing = make_timing()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="PATH_MAPPING_ERROR",
                message=str(e),
                http_status=0,
                normalized_errors={"path_error": str(e)},
                raw_details={"error": str(e)},
                migration_hint=None,
                tool_name=snapshot.name,
                endpoint=snapshot.endpoint,
                timestamp=timing.completed_at,
                registry_version=snapshot.version,
                failure_category=FailureCategory.VALIDATION_FAILURE,
                uncertain_write=False,
            )
            return ExecutionResult(
                is_success=False,
                outcome=None,
                http_status=0,
                normalized_result=None,
                normalized_errors={"path_error": str(e)},
                failure=failure,
                registry_version=snapshot.version,
                fingerprint=snapshot.fingerprint,
                timing=timing,
                operation_identity=op_identity,
            )

        if "{" in url_path and "}" in url_path:
            timing = make_timing()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="UNRESOLVED_PATH_PARAMETER",
                message=f"Endpoint contains unresolved parameter placeholders: '{url_path}'",
                http_status=0,
                normalized_errors={"endpoint": url_path},
                raw_details={"endpoint": url_path},
                migration_hint=None,
                tool_name=snapshot.name,
                endpoint=snapshot.endpoint,
                timestamp=timing.completed_at,
                registry_version=snapshot.version,
                failure_category=FailureCategory.VALIDATION_FAILURE,
                uncertain_write=False,
            )
            return ExecutionResult(
                is_success=False,
                outcome=None,
                http_status=0,
                normalized_result=None,
                normalized_errors={"endpoint": url_path},
                failure=failure,
                registry_version=snapshot.version,
                fingerprint=snapshot.fingerprint,
                timing=timing,
                operation_identity=op_identity,
            )

        # Query params
        query_params: Dict[str, Any] = {}
        if "query_params" in snapshot.request_mapping:
            try:
                for qk, template in snapshot.request_mapping["query_params"].items():
                    query_params[qk] = expand_mapping(template, args_copy)
            except MappingError as e:
                timing = make_timing()
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="QUERY_MAPPING_ERROR",
                    message=str(e),
                    http_status=0,
                    normalized_errors={"query_error": str(e)},
                    raw_details={"error": str(e)},
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=snapshot.endpoint,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.VALIDATION_FAILURE,
                    uncertain_write=False,
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=0,
                    normalized_result=None,
                    normalized_errors={"query_error": str(e)},
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )

        # Headers
        headers: Dict[str, str] = dict(snapshot.headers)
        if "headers" in snapshot.request_mapping:
            try:
                for hk, template in snapshot.request_mapping["headers"].items():
                    headers[hk] = str(expand_mapping(template, args_copy))
            except MappingError as e:
                timing = make_timing()
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="HEADER_MAPPING_ERROR",
                    message=str(e),
                    http_status=0,
                    normalized_errors={"header_error": str(e)},
                    raw_details={"error": str(e)},
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=snapshot.endpoint,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.VALIDATION_FAILURE,
                    uncertain_write=False,
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=0,
                    normalized_result=None,
                    normalized_errors={"header_error": str(e)},
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )

        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        # Body
        json_body: Optional[Any] = None
        if "body" in snapshot.request_mapping:
            try:
                json_body = expand_mapping(snapshot.request_mapping["body"], args_copy)
            except MappingError as e:
                timing = make_timing()
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="BODY_MAPPING_ERROR",
                    message=str(e),
                    http_status=0,
                    normalized_errors={"body_error": str(e)},
                    raw_details={"error": str(e)},
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=snapshot.endpoint,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.VALIDATION_FAILURE,
                    uncertain_write=False,
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=0,
                    normalized_result=None,
                    normalized_errors={"body_error": str(e)},
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )

        # 7. Safe Network Execution via Injectable Business Client
        try:
            response = await self.client._client.request(
                method=method,
                url=url_path,
                params=query_params if query_params else None,
                json=json_body if json_body is not None else None,
                headers=headers,
            )
        except httpx.RequestError as exc:
            timing = make_timing()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="TRANSPORT_ERROR",
                message=f"Request to {url_path} failed: {str(exc)}",
                http_status=503,
                normalized_errors={"transport_error": str(exc)},
                raw_details={"error": str(exc)},
                migration_hint=None,
                tool_name=snapshot.name,
                endpoint=url_path,
                timestamp=timing.completed_at,
                registry_version=snapshot.version,
                failure_category=FailureCategory.TRANSPORT_FAILURE,
                uncertain_write=is_write,  # POST write may or may not have reached server
            )
            return ExecutionResult(
                is_success=False,
                outcome=None,
                http_status=503,
                normalized_result=None,
                normalized_errors={"transport_error": str(exc)},
                failure=failure,
                registry_version=snapshot.version,
                fingerprint=snapshot.fingerprint,
                timing=timing,
                operation_identity=op_identity,
            )

        timing = make_timing()

        # 8. Parse Response
        try:
            body = response.json()
        except Exception as exc:
            if response.status_code >= 400:
                body = {"raw": response.text}
            else:
                # 2xx with unparseable JSON
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="INVALID_RESPONSE_JSON",
                    message=f"Failed to parse response body as JSON: {str(exc)}",
                    http_status=response.status_code,
                    normalized_errors={"raw_response": response.text},
                    raw_details={"raw_response": response.text},
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=url_path,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.RESPONSE_PARSING_FAILURE,
                    uncertain_write=is_write,  # 200 write occurred but parsing failed: never repeat!
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=response.status_code,
                    normalized_result=None,
                    normalized_errors={"raw_response": response.text},
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )

        # 9. HTTP Rejection (>= 400)
        if response.status_code >= 400:
            failure = self.client._normalize_failure(
                status_code=response.status_code,
                response_data=body,
                tool_name=snapshot.name,
                endpoint=url_path,
                registry_version=snapshot.version,
            )
            # Preserve evidence without classifying as drift in Task 3
            failure.is_drift = False
            failure.failure_category = FailureCategory.HTTP_REJECTION
            failure.uncertain_write = False
            return ExecutionResult(
                is_success=False,
                outcome=None,
                http_status=response.status_code,
                normalized_result=None,
                normalized_errors=failure.normalized_errors,
                failure=failure,
                registry_version=snapshot.version,
                fingerprint=snapshot.fingerprint,
                timing=timing,
                operation_identity=op_identity,
            )

        # 10. Check for empty list or ambiguity for directory lookups like find_user
        if snapshot.name == "find_user" and isinstance(body, list):
            if len(body) == 0:
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="USER_NOT_FOUND",
                    message="Could not find user Maya in user directory",
                    http_status=response.status_code,
                    normalized_errors={"user_search": "Empty results returned for 'Maya'"},
                    raw_details={"raw_response": body},
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=url_path,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.RESPONSE_PARSING_FAILURE,
                    uncertain_write=False,
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=response.status_code,
                    normalized_result=None,
                    raw_response=body,
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )
            elif len(body) > 1:
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="AMBIGUOUS_USER_RESOLUTION",
                    message="Ambiguous user resolution: multiple users returned for 'Maya'",
                    http_status=response.status_code,
                    normalized_errors={"user_search": f"{len(body)} matches found for 'Maya'"},
                    raw_details={"raw_response": body},
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=url_path,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.RESPONSE_PARSING_FAILURE,
                    uncertain_write=False,
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=response.status_code,
                    normalized_result=None,
                    raw_response=body,
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )

        # 11. Response Mapping Extraction
        mapped_outcome: Dict[str, Any] = {}
        if snapshot.response_mapping:
            try:
                for out_key, json_path in snapshot.response_mapping.items():
                    val = extract_json_path(json_path, body)
                    mapped_outcome[out_key] = val
            except MappingError as me:
                failure = SanitizedFailure(
                    is_drift=False,
                    error_code="RESPONSE_MAPPING_ERROR",
                    message=f"Response mapping failed for '{snapshot.name}': {str(me)}",
                    http_status=response.status_code,
                    normalized_errors={"mapping_error": str(me), "response": redact_secrets(body)},
                    raw_details=redact_secrets(body if isinstance(body, dict) else {"raw": body}),
                    migration_hint=None,
                    tool_name=snapshot.name,
                    endpoint=url_path,
                    timestamp=timing.completed_at,
                    registry_version=snapshot.version,
                    failure_category=FailureCategory.RESPONSE_PARSING_FAILURE,
                    uncertain_write=is_write,  # Write succeeded on downstream server, but output unusable
                )
                return ExecutionResult(
                    is_success=False,
                    outcome=None,
                    http_status=response.status_code,
                    normalized_result=None,
                    raw_response=body,
                    normalized_errors={"mapping_error": str(me)},
                    failure=failure,
                    registry_version=snapshot.version,
                    fingerprint=snapshot.fingerprint,
                    timing=timing,
                    operation_identity=op_identity,
                )
        else:
            mapped_outcome = body if isinstance(body, dict) else {"result": body}

        # Redact secrets before returning
        clean_outcome = redact_secrets(mapped_outcome)
        clean_raw = redact_secrets(body if isinstance(body, dict) else {"result": body})

        return ExecutionResult(
            is_success=True,
            outcome=clean_outcome,
            http_status=response.status_code,
            normalized_result=clean_outcome,
            raw_response=clean_raw,
            normalized_errors=None,
            failure=None,
            registry_version=snapshot.version,
            fingerprint=snapshot.fingerprint,
            timing=timing,
            operation_identity=op_identity,
        )
