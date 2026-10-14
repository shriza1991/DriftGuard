"""HTTP tool caller adapting ToolDefinition to downstream execution.
Uses injectable MockBusinessApiClient and normalizes failures.
"""
from typing import Dict, Any, Optional
import uuid
from orchestrator.models import ToolDefinition
from orchestrator.client.mock_client import MockBusinessApiClient, MockApiClientError
from orchestrator.config import settings


class ToolExecutionError(Exception):
    def __init__(self, message: str, status_code: int, response_data: Any):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class HttpToolCaller:
    """Executes downstream HTTP requests defined by ToolDefinition using MockBusinessApiClient."""
    def __init__(self, client: Optional[MockBusinessApiClient] = None, base_url: Optional[str] = None):
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = MockBusinessApiClient(base_url=base_url or settings.mock_api_url)
            self._owns_client = True

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    async def call_tool(
        self,
        tool: ToolDefinition,
        arguments: Dict[str, Any],
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Executes tool via MockBusinessApiClient and returns raw response body dict."""
        try:
            result = await self._client.execute_tool(
                tool=tool,
                arguments=arguments,
                idempotency_key=idempotency_key
            )
            return result.outcome if result.outcome is not None else {}
        except MockApiClientError as err:
            raise ToolExecutionError(
                message=err.failure.message,
                status_code=err.failure.http_status,
                response_data=err.failure.raw_details
            )


http_tool_caller = HttpToolCaller()
