"""Injectable async mock API client for business endpoints.

Adheres strictly to Member 2 safety and architectural boundaries:
- Pure business interface: exposes NO admin methods (reset, mutation, state).
- Dependency injection for transport, client, and credential resolution.
- Normalizes failures into SanitizedFailure preserving structured validation evidence.
- Redacts all secrets from representations, logs, and failure objects.
- Explicit lifecycle and async cleanup.
- Zero retries or schema repair inside this client.
"""
from typing import Optional, Dict, Any, List, Callable
import time
import uuid
import httpx
from datetime import datetime, timezone

from orchestrator.config import settings
from orchestrator.models import (
    ToolDefinition,
    ExecutionResult,
    SanitizedFailure,
    TimingInfo,
    OperationIdentity,
)


CredentialResolver = Callable[[str], Optional[str]]


LEGITIMATE_KEY_ALLOWLIST = {
    "idempotency_key",
    "idempotency-key",
    "ticket_id",
    "user_id",
    "assignee_id",
    "key_id",
}

SENSITIVE_KEY_SUBSTRINGS = [
    "auth",
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "apikey",
]


def redact_secrets(data: Any, known_secrets: Optional[List[str]] = None) -> Any:
    """Recursively redacts sensitive keys and known/embedded secret strings while preserving legitimate business fields."""
    import re

    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            # Preserve legitimate business identity fields
            if k_lower in LEGITIMATE_KEY_ALLOWLIST:
                redacted[k] = redact_secrets(v, known_secrets=known_secrets)
            elif any(s in k_lower for s in SENSITIVE_KEY_SUBSTRINGS):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_secrets(v, known_secrets=known_secrets)
        return redacted
    elif isinstance(data, list):
        return [redact_secrets(item, known_secrets=known_secrets) for item in data]
    elif isinstance(data, str):
        val = data
        # Redact specific known secret values embedded in strings
        if known_secrets:
            for s in known_secrets:
                if s and len(s) >= 4 and s in val:
                    val = val.replace(s, "[REDACTED]")
        # Redact standard bearer tokens and credential headers embedded in strings
        val = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", r"\1[REDACTED]", val)
        val = re.sub(r"((?:api[_-]?key|secret|token|password)\s*[:=]\s*)[A-Za-z0-9_\-\.]+", r"\1[REDACTED]", val, flags=re.IGNORECASE)
        return val
    return data


class MockApiClientError(Exception):
    """Exception raised on downstream HTTP failures containing SanitizedFailure."""
    def __init__(self, failure: SanitizedFailure):
        super().__init__(f"Downstream tool failure [{failure.http_status} {failure.error_code}]: {failure.message}")
        self.failure = failure

    def __repr__(self) -> str:
        clean = self.failure.model_dump()
        redacted = redact_secrets(clean)
        return f"MockApiClientError({redacted})"

    def __str__(self) -> str:
        return f"MockApiClientError({self.failure.error_code} at {self.failure.endpoint}: {self.failure.message})"


class MockBusinessApiClient:
    """Async HTTP client exclusively for business tool execution.
    Exposes NO admin methods.
    """
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
        credential_resolver: Optional[CredentialResolver] = None,
    ):
        self.base_url = (base_url or settings.mock_api_url).rstrip("/")
        self.timeout = timeout
        self.credential_resolver = credential_resolver or (lambda ref: None)

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                transport=transport,
                timeout=httpx.Timeout(self.timeout)
            )
            self._owns_client = True

    async def aclose(self) -> None:
        """Async cleanup of the underlying client if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    def _normalize_failure(
        self,
        status_code: int,
        response_data: Any,
        tool_name: str,
        endpoint: str,
        registry_version: str = "1.0.0",
        known_secrets: Optional[List[str]] = None,
    ) -> SanitizedFailure:
        """Normalizes downstream failure response while preserving structured evidence."""
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_raw = redact_secrets(
            response_data if isinstance(response_data, dict) else {"raw_response": str(response_data)},
            known_secrets=known_secrets,
        )

        code = "HTTP_ERROR"
        message = f"Endpoint returned HTTP {status_code}"
        details: Dict[str, Any] = {}
        hint: Optional[str] = None

        if isinstance(response_data, dict):
            err_field = response_data.get("error")
            if isinstance(err_field, dict):
                code = str(err_field.get("code") or err_field.get("category") or code)
                message = str(err_field.get("message") or err_field.get("detail") or message)
                if isinstance(err_field.get("details"), dict):
                    details = err_field.get("details")
                hint = err_field.get("hint") or hint
            elif isinstance(err_field, str):
                code = err_field

            code = str(response_data.get("code") or code)
            message = str(response_data.get("message") or response_data.get("detail") or message)
            if isinstance(response_data.get("details"), dict):
                details = response_data.get("details")
            hint = response_data.get("hint") or hint

        clean_message = redact_secrets(message, known_secrets=known_secrets)

        is_drift = status_code in [400, 422] or code in [
            "SCHEMA_MISMATCH",
            "MISSING_REQUIRED_FIELD",
            "TYPE_MISMATCH",
            "COMBINED_SCHEMA_MISMATCH",
            "MISSING_BUSINESS_VALUE",
            "AMBIGUOUS_TARGET",
        ]

        return SanitizedFailure(
            is_drift=is_drift,
            error_code=code,
            message=clean_message,
            http_status=status_code,
            normalized_errors=redact_secrets(details, known_secrets=known_secrets),
            raw_details=clean_raw,
            migration_hint=hint,
            tool_name=tool_name,
            endpoint=endpoint,
            timestamp=now_iso,
            registry_version=registry_version,
        )

    async def find_user(self, name: str) -> ExecutionResult:
        """Business tool call: search for user by name (e.g. 'Maya')."""
        started_at = datetime.now(timezone.utc).isoformat()
        start_t = time.perf_counter()

        endpoint = "/users"
        params = {"name": name}

        try:
            response = await self._client.get(endpoint, params=params)
        except httpx.RequestError as exc:
            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            completed_at = datetime.now(timezone.utc).isoformat()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="NETWORK_ERROR",
                message=f"Request to {endpoint} failed: {str(exc)}",
                http_status=503,
                normalized_errors={"network_error": str(exc)},
                raw_details={"error": str(exc)},
                migration_hint=None,
                tool_name="find_user",
                endpoint=endpoint,
                timestamp=completed_at,
                registry_version="1.0.0"
            )
            raise MockApiClientError(failure)

        duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
        completed_at = datetime.now(timezone.utc).isoformat()
        timing = TimingInfo(started_at=started_at, completed_at=completed_at, duration_ms=duration_ms)
        identity = OperationIdentity(tool_name="find_user", endpoint=endpoint, method="GET")

        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        if response.status_code >= 400:
            failure = self._normalize_failure(response.status_code, body, "find_user", endpoint)
            raise MockApiClientError(failure)

        return ExecutionResult(
            is_success=True,
            outcome={"users": body},
            http_status=response.status_code,
            normalized_result={"users": body},
            normalized_errors=None,
            failure=None,
            registry_version="1.0.0",
            timing=timing,
            operation_identity=identity
        )

    async def assign_ticket(
        self,
        ticket_id: int,
        user_id: str,
        idempotency_key: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        credential_ref: Optional[str] = None
    ) -> ExecutionResult:
        """Business tool call: assign ticket 81 to user ID (e.g. '123')."""
        started_at = datetime.now(timezone.utc).isoformat()
        start_t = time.perf_counter()

        endpoint = "/tickets/assign"
        payload = {"ticket_id": ticket_id, "user_id": user_id}

        key = idempotency_key or f"dg-idemp-{uuid.uuid4().hex[:12]}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": key
        }
        if custom_headers:
            headers.update(custom_headers)

        if credential_ref:
            resolved = self.credential_resolver(credential_ref)
            if resolved:
                headers["Authorization"] = f"Bearer {resolved}"

        try:
            response = await self._client.post(endpoint, json=payload, headers=headers)
        except httpx.RequestError as exc:
            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            completed_at = datetime.now(timezone.utc).isoformat()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="NETWORK_ERROR",
                message=f"Request to {endpoint} failed: {str(exc)}",
                http_status=503,
                normalized_errors={"network_error": str(exc)},
                raw_details={"error": str(exc)},
                migration_hint=None,
                tool_name="assign_ticket",
                endpoint=endpoint,
                timestamp=completed_at,
                registry_version="1.0.0"
            )
            raise MockApiClientError(failure)

        duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
        completed_at = datetime.now(timezone.utc).isoformat()
        timing = TimingInfo(started_at=started_at, completed_at=completed_at, duration_ms=duration_ms)
        identity = OperationIdentity(
            tool_name="assign_ticket",
            endpoint=endpoint,
            method="POST",
            idempotency_key=key
        )

        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        if response.status_code >= 400:
            failure = self._normalize_failure(response.status_code, body, "assign_ticket", endpoint)
            raise MockApiClientError(failure)

        return ExecutionResult(
            is_success=True,
            outcome=body,
            http_status=response.status_code,
            normalized_result=body,
            normalized_errors=None,
            failure=None,
            registry_version="1.0.0",
            timing=timing,
            operation_identity=identity
        )

    async def get_ticket(self, ticket_id: int) -> ExecutionResult:
        """Business tool call: independent read verification for ticket assignee."""
        started_at = datetime.now(timezone.utc).isoformat()
        start_t = time.perf_counter()

        endpoint = f"/tickets/{ticket_id}"

        try:
            response = await self._client.get(endpoint)
        except httpx.RequestError as exc:
            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            completed_at = datetime.now(timezone.utc).isoformat()
            failure = SanitizedFailure(
                is_drift=False,
                error_code="NETWORK_ERROR",
                message=f"Request to {endpoint} failed: {str(exc)}",
                http_status=503,
                normalized_errors={"network_error": str(exc)},
                raw_details={"error": str(exc)},
                migration_hint=None,
                tool_name="get_ticket",
                endpoint=endpoint,
                timestamp=completed_at,
                registry_version="1.0.0"
            )
            raise MockApiClientError(failure)

        duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
        completed_at = datetime.now(timezone.utc).isoformat()
        timing = TimingInfo(started_at=started_at, completed_at=completed_at, duration_ms=duration_ms)
        identity = OperationIdentity(tool_name="get_ticket", endpoint=endpoint, method="GET")

        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        if response.status_code >= 400:
            failure = self._normalize_failure(response.status_code, body, "get_ticket", endpoint)
            raise MockApiClientError(failure)

        return ExecutionResult(
            is_success=True,
            outcome=body,
            http_status=response.status_code,
            normalized_result=body,
            normalized_errors=None,
            failure=None,
            registry_version="1.0.0",
            timing=timing,
            operation_identity=identity
        )

    async def execute_tool(
        self,
        tool: ToolDefinition,
        arguments: Dict[str, Any],
        idempotency_key: Optional[str] = None
    ) -> ExecutionResult:
        """Generic execution adhering to ToolDefinition contract and registry mappings."""
        from orchestrator.execution.tool_executor import ToolExecutor
        from orchestrator.models import ExecutionContext

        executor = ToolExecutor(client=self)
        context = ExecutionContext(
            run_id=f"run-{uuid.uuid4().hex[:8]}",
            idempotency_key=idempotency_key
        )
        result = await executor.execute(
            tool_name_or_def=tool,
            arguments=arguments,
            context=context
        )
        if not result.is_success and result.failure:
            raise MockApiClientError(result.failure)
        return result


class MockAdminApiClient:
    """Separate client dedicated ONLY to admin endpoints (reset, mutation, state).
    MUST NEVER be referenced or used by the healer diagnosis engine.
    """
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 5.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None
    ):
        self.base_url = (base_url or settings.mock_api_url).rstrip("/")
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                transport=transport,
                timeout=httpx.Timeout(timeout)
            )
            self._owns_client = True

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def reset(self) -> Dict[str, Any]:
        """POST /admin/reset"""
        res = await self._client.post("/admin/reset")
        res.raise_for_status()
        return res.json()

    async def set_mutation(self, scenario: str, enabled: bool = True, evidence_level: str = "ordinary") -> Dict[str, Any]:
        """POST /admin/mutation"""
        res = await self._client.post("/admin/mutation", json={
            "scenario": scenario,
            "enabled": enabled,
            "evidence_level": evidence_level
        })
        res.raise_for_status()
        return res.json()

    async def get_state(self) -> Dict[str, Any]:
        """GET /admin/state"""
        res = await self._client.get("/admin/state")
        res.raise_for_status()
        return res.json()
