"""Run-scoped inference gateway with a durable transport-level Groq attempt limit.

The SQLite reservation is committed before an HTTP request leaves this process.
It remains effective if execution is reconstructed with the same run ID.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import sqlite3
import time
from typing import Any

import httpx

from orchestrator.config import settings


MAX_HEALING_ATTEMPTS = 2
_purpose: contextvars.ContextVar[str] = contextvars.ContextVar("llm_purpose", default="unclassified")
_provider_semaphore = asyncio.Semaphore(1)
_run_db_paths: dict[str, str] = {}


class ModelBudgetExhausted(RuntimeError):
    pass


class ProviderCooldown(RuntimeError):
    pass


class RunCallBudget:
    """SQLite-backed counters; BEGIN IMMEDIATE serializes reservations across workers."""

    def __init__(self, run_id: str, db_path: str | None = None, limit: int = MAX_HEALING_ATTEMPTS):
        self.run_id = run_id
        self.db_path = db_path or settings.sqlite_db_path
        self.limit = limit
        _run_db_paths[run_id] = self.db_path
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS llm_call_budgets (
                run_id TEXT PRIMARY KEY, logical_invocations INTEGER NOT NULL DEFAULT 0,
                outbound_attempts INTEGER NOT NULL DEFAULT 0,
                successful_responses INTEGER NOT NULL DEFAULT 0,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                http_429_responses INTEGER NOT NULL DEFAULT 0,
                planning_invocations INTEGER NOT NULL DEFAULT 0,
                repair_diagnosis_invocations INTEGER NOT NULL DEFAULT 0,
                planning_attempts INTEGER NOT NULL DEFAULT 0,
                repair_diagnosis_attempts INTEGER NOT NULL DEFAULT 0,
                tool_api_calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                token_usage_complete INTEGER NOT NULL DEFAULT 1
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS llm_provider_cooldowns (
                provider TEXT PRIMARY KEY, until_epoch REAL NOT NULL
            )""")
            db.execute("INSERT OR IGNORE INTO llm_call_budgets(run_id) VALUES (?)", (self.run_id,))

    @staticmethod
    def record_tool_api_call_if_budgeted(run_id: str, db_path: str | None = None):
        """Count a downstream dispatch only for a run already using this gateway."""
        try:
            with closing(sqlite3.connect(db_path or _run_db_paths.get(run_id) or settings.sqlite_db_path, timeout=10)) as db, db:
                db.execute("UPDATE llm_call_budgets SET tool_api_calls=tool_api_calls+1 WHERE run_id=?", (run_id,))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise

    def _update(self, sql: str, args: tuple[Any, ...] = ()):
        with closing(self._connect()) as db, db:
            db.execute(sql, args)

    def logical_invocation(self, purpose: str):
        column = "planning_invocations" if purpose == "planning" else "repair_diagnosis_invocations"
        self._update(f"UPDATE llm_call_budgets SET logical_invocations=logical_invocations+1, {column}={column}+1 WHERE run_id=?", (self.run_id,))

    def reserve_attempt(self, purpose: str):
        column = "planning_attempts" if purpose == "planning" else "repair_diagnosis_attempts"
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            cooldown = db.execute("SELECT until_epoch FROM llm_provider_cooldowns WHERE provider='groq'").fetchone()
            if cooldown and cooldown["until_epoch"] > time.time():
                raise ProviderCooldown("Groq Retry-After cooldown remains active; no inference request was sent.")
            row = db.execute("SELECT outbound_attempts FROM llm_call_budgets WHERE run_id=?", (self.run_id,)).fetchone()
            if row is None:
                raise RuntimeError("Missing run inference budget")
            if row["outbound_attempts"] >= self.limit:
                raise ModelBudgetExhausted(f"Run inference budget exhausted ({self.limit} outbound attempts).")
            db.execute(f"UPDATE llm_call_budgets SET outbound_attempts=outbound_attempts+1, {column}={column}+1 WHERE run_id=?", (self.run_id,))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def record_response(self, status_code: int, retry_after: str | None = None):
        success = 200 <= status_code < 300
        self._update("""UPDATE llm_call_budgets SET
            successful_responses=successful_responses+?, failed_attempts=failed_attempts+?,
            http_429_responses=http_429_responses+? WHERE run_id=?""",
            (int(success), int(not success), int(status_code == 429), self.run_id))
        if status_code == 429 and retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    return
            if delay > 0:
                until = time.time() + min(delay, 3600)
                self._update("INSERT INTO llm_provider_cooldowns(provider, until_epoch) VALUES ('groq', ?) ON CONFLICT(provider) DO UPDATE SET until_epoch=MAX(until_epoch, excluded.until_epoch)", (until,))

    def record_transport_failure(self):
        self._update("UPDATE llm_call_budgets SET failed_attempts=failed_attempts+1 WHERE run_id=?", (self.run_id,))

    def record_usage(self, message: Any):
        usage = getattr(message, "usage_metadata", None) or getattr(message, "response_metadata", {}).get("token_usage") or {}
        incoming = usage.get("input_tokens", usage.get("prompt_tokens"))
        outgoing = usage.get("output_tokens", usage.get("completion_tokens"))
        complete = isinstance(incoming, int) and isinstance(outgoing, int)
        self._update("""UPDATE llm_call_budgets SET input_tokens=input_tokens+?, output_tokens=output_tokens+?,
            token_usage_complete=token_usage_complete AND ? WHERE run_id=?""",
            (incoming if isinstance(incoming, int) else 0, outgoing if isinstance(outgoing, int) else 0, int(complete), self.run_id))

    def snapshot(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = dict(db.execute("SELECT * FROM llm_call_budgets WHERE run_id=?", (self.run_id,)).fetchone())
        row.pop("run_id", None)
        complete = (bool(row.pop("token_usage_complete")) and row["logical_invocations"] > 0
                    and row["successful_responses"] == row["logical_invocations"])
        row["token_usage"] = ({"input_tokens": row.pop("input_tokens"), "output_tokens": row.pop("output_tokens"), "is_complete": True}
                              if complete else "unavailable")
        if not complete:
            row.pop("input_tokens", None)
            row.pop("output_tokens", None)
        row["limit"] = self.limit
        return row

    @classmethod
    def existing_snapshot(cls, run_id: str, db_path: str | None = None) -> dict[str, Any] | None:
        path = db_path or settings.sqlite_db_path
        try:
            with closing(sqlite3.connect(path, timeout=10)) as db:
                found = db.execute("SELECT 1 FROM llm_call_budgets WHERE run_id=?", (run_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        return cls(run_id, path).snapshot() if found else None


class BudgetedGroqTransport(httpx.AsyncBaseTransport):
    def __init__(self, budget: RunCallBudget, inner: httpx.AsyncBaseTransport | None = None):
        self.budget = budget
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            raise RuntimeError("Unexpected Groq request outside the inference endpoint")
        self.budget.reserve_attempt(_purpose.get())
        try:
            response = await self.inner.handle_async_request(request)
        except BaseException:
            self.budget.record_transport_failure()
            raise
        self.budget.record_response(response.status_code, response.headers.get("Retry-After"))
        return response

    async def aclose(self):
        await self.inner.aclose()


class ModelGateway:
    def __init__(self, run_id: str, db_path: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.budget = RunCallBudget(run_id, db_path=db_path)
        self.client = httpx.AsyncClient(transport=BudgetedGroqTransport(self.budget, transport), timeout=25)

    def create_model(self):
        from orchestrator.agents.model_provider import create_chat_model, ModelConfigurationError
        if settings.model_provider.lower() != "groq":
            raise ModelConfigurationError("Bounded live inference requires the configured Groq provider.")
        return create_chat_model(http_async_client=self.client)

    async def invoke(self, model: Any, messages: Any, purpose: str):
        self.budget.logical_invocation(purpose)
        token = _purpose.set(purpose)
        try:
            async with _provider_semaphore:
                response = await model.ainvoke(messages)
            self.budget.record_usage(response)
            return response
        finally:
            _purpose.reset(token)

    def snapshot(self) -> dict[str, Any]:
        return self.budget.snapshot()

    async def aclose(self):
        await self.client.aclose()
