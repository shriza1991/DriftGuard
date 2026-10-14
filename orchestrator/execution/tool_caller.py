import uuid
from typing import Dict, Any, Tuple, Optional
import httpx
from orchestrator.registry.models import ToolRegistryEntry
from orchestrator.config import settings


class ToolExecutionError(Exception):
    def __init__(self, message: str, status_code: int, response_data: Any):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class HttpToolCaller:
    """Executes downstream HTTP requests defined by ToolRegistryEntry."""
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.mock_api_url).rstrip("/")

    async def call_tool(
        self,
        tool: ToolRegistryEntry,
        arguments: Dict[str, Any],
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{tool.endpoint}"
        method = tool.method.upper()
        headers = dict(tool.headers)

        if method == "POST" and not idempotency_key:
            idempotency_key = f"driftguard-{uuid.uuid4().hex[:12]}"
        
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        params = {}
        json_body = None

        if method == "GET":
            # Apply query params or path params
            if "{" in url:
                for k, v in arguments.items():
                    url = url.replace(f"{{{k}}}", str(v))
            else:
                params = arguments
        elif method == "POST":
            # Standard POST body from arguments
            json_body = arguments

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    params=params if method == "GET" else None,
                    json=json_body if method == "POST" else None,
                    headers=headers
                )
            except httpx.RequestError as exc:
                raise ToolExecutionError(
                    message=f"Network error connecting to downstream service: {str(exc)}",
                    status_code=503,
                    response_data={"error": "NetworkError", "details": str(exc)}
                )

            try:
                data = response.json()
            except Exception:
                data = {"raw_text": response.text}

            if response.status_code >= 400:
                raise ToolExecutionError(
                    message=f"Downstream service returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    response_data=data
                )

            return data


http_tool_caller = HttpToolCaller()
