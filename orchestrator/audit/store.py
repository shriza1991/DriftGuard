"""SQLite-backed persistent audit store for DriftGuard.
"""
from __future__ import annotations
import sqlite3
import json
import os
import asyncio
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager

from orchestrator.config import settings
from orchestrator.client.mock_client import redact_secrets
from orchestrator.audit.models import (
    PersistedRunRecord,
    PersistedToolExecution,
    PersistedRepairAttempt,
    PersistedEvent,
    PersistedModelUsage,
)


class PersistenceError(Exception):
    """Exception raised when an audit persistence operation fails."""
    pass


class PreWriteAuditError(PersistenceError):
    """Exception raised when required pre-write audit persistence fails."""
    pass


class SQLiteAuditStore:
    """Thread-safe, async-friendly SQLite audit store.
    
    Invariants:
    - Never blocks the async event loop (runs SQLite operations in worker threads via asyncio.to_thread).
    - Uses WAL mode, foreign keys, and bounded busy timeout.
    - Idempotent schema initialization with explicit schema version.
    - Parameterized SQL queries to prevent SQL injection.
    - Strict secret sanitization before persistence (zero credentials/tokens stored).
    - On store restart, nonterminal runs (running/pending) are marked as interrupted; never resumed.
    - Archived patch candidates are never activated into in-memory registry on restart.
    - Demo reset preserves audit history; no destructive purge endpoint.
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Optional[str] = None, timeout: float = 5.0):
        self.db_path = str(db_path or settings.sqlite_db_path)
        self.timeout = timeout
        self._initialized = False

    def _get_connection(self) -> sqlite3.Connection:
        """Creates and configures a SQLite connection."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)};")
        return conn

    @contextmanager
    def _transaction(self):
        """Context manager for atomic SQLite transactions."""
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db_sync(self):
        """Synchronous database schema initialization and startup migration."""
        parent = Path(self.db_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

        with self._transaction() as conn:
            # 1. Schema migrations table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)

            cursor = conn.execute("SELECT MAX(version) as max_v FROM schema_migrations;")
            row = cursor.fetchone()
            current_version = row["max_v"] if row and row["max_v"] is not None else 0

            if current_version < 1:
                # 2. Runs table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        execution_type TEXT,
                        task TEXT NOT NULL,
                        status TEXT NOT NULL,
                        termination_reason TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        duration_ms REAL,
                        verified_outcome TEXT,
                        error TEXT,
                        audit_status TEXT NOT NULL DEFAULT 'complete'
                    );
                """)

                # 3. Tool executions table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tool_executions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        tool_call_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        tool_version TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        is_repaired BOOLEAN NOT NULL DEFAULT 0,
                        arguments TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result TEXT,
                        failure TEXT,
                        timing TEXT,
                        operation_identity TEXT,
                        UNIQUE(run_id, tool_call_id, attempt_number)
                    );
                """)

                # 4. Repair attempts table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS repair_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        tool_name TEXT NOT NULL,
                        base_version TEXT NOT NULL,
                        base_fingerprint TEXT NOT NULL,
                        proposal_fingerprint TEXT NOT NULL,
                        candidate_id TEXT,
                        candidate_version TEXT,
                        candidate_fingerprint TEXT,
                        diagnosis_source TEXT NOT NULL,
                        diagnosis_tokens INTEGER NOT NULL DEFAULT 0,
                        proposal TEXT NOT NULL,
                        candidate_definition TEXT,
                        validation_status TEXT NOT NULL,
                        validation_details TEXT,
                        recovery_status TEXT NOT NULL,
                        business_effect_occurred BOOLEAN NOT NULL DEFAULT 0,
                        verification_succeeded BOOLEAN NOT NULL DEFAULT 0,
                        registry_committed BOOLEAN NOT NULL DEFAULT 0,
                        committed_version TEXT,
                        termination_reason TEXT,
                        recovery_duration_ms REAL,
                        commit_latency_ms REAL,
                        failed_recovery_duration_ms REAL,
                        created_at TEXT NOT NULL,
                        UNIQUE(run_id, tool_name, proposal_fingerprint)
                    );
                """)

                # 5. Events table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        sequence INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        tool TEXT,
                        data TEXT NOT NULL,
                        UNIQUE(run_id, sequence)
                    );
                """)

                # 6. Model usages table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS model_usages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        call_id TEXT NOT NULL,
                        purpose TEXT NOT NULL,
                        prompt_tokens INTEGER,
                        completion_tokens INTEGER,
                        total_tokens INTEGER,
                        is_complete BOOLEAN NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        UNIQUE(run_id, call_id, purpose)
                    );
                """)

                # 7. Indexes
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, sequence);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_repair_run ON repair_attempts(run_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_exec_run ON tool_executions(run_id);")

                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (1, datetime.now(timezone.utc).isoformat())
                )

            # Cleanup nonterminal runs from prior process interruptions
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                UPDATE runs
                SET status = 'interrupted',
                    termination_reason = 'system_interrupted_restart',
                    completed_at = ?,
                    audit_status = 'incomplete'
                WHERE status IN ('running', 'pending');
            """, (now_iso,))

        self._initialized = True

    async def initialize(self):
        """Asynchronously initializes the database."""
        await asyncio.to_thread(self._init_db_sync)

    def _ensure_init(self):
        if not self._initialized:
            self._init_db_sync()

    # -------------------------------------------------------------------------
    # Runs Persistence
    # -------------------------------------------------------------------------

    def _record_run_start_sync(
        self,
        run_id: str,
        mode: str,
        task: str,
        execution_type: Optional[str] = None,
        created_at: Optional[str] = None,
    ):
        self._ensure_init()
        now_iso = created_at or datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO runs (run_id, mode, execution_type, task, status, created_at, audit_status)
                VALUES (?, ?, ?, ?, 'running', ?, 'complete');
            """, (run_id, mode, execution_type, task, now_iso))

    async def record_run_start(
        self,
        run_id: str,
        mode: str,
        task: str,
        execution_type: Optional[str] = None,
        created_at: Optional[str] = None,
    ):
        await asyncio.to_thread(
            self._record_run_start_sync, run_id, mode, task, execution_type, created_at
        )

    def _record_run_completed_sync(
        self,
        run_id: str,
        status: str,
        duration_ms: Optional[float] = None,
        verified_outcome: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        termination_reason: Optional[str] = None,
        completed_at: Optional[str] = None,
        audit_status: str = "complete",
    ):
        self._ensure_init()
        now_iso = completed_at or datetime.now(timezone.utc).isoformat()
        sanitized_outcome = redact_secrets(verified_outcome) if verified_outcome is not None else None
        outcome_json = json.dumps(sanitized_outcome) if sanitized_outcome is not None else None

        with self._transaction() as conn:
            conn.execute("""
                UPDATE runs
                SET status = ?,
                    completed_at = ?,
                    duration_ms = ?,
                    verified_outcome = ?,
                    error = ?,
                    termination_reason = ?,
                    audit_status = ?
                WHERE run_id = ?;
            """, (status, now_iso, duration_ms, outcome_json, error, termination_reason, audit_status, run_id))

    async def record_run_completed(
        self,
        run_id: str,
        status: str,
        duration_ms: Optional[float] = None,
        verified_outcome: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        termination_reason: Optional[str] = None,
        completed_at: Optional[str] = None,
        audit_status: str = "complete",
    ):
        await asyncio.to_thread(
            self._record_run_completed_sync,
            run_id,
            status,
            duration_ms,
            verified_outcome,
            error,
            termination_reason,
            completed_at,
            audit_status,
        )

    # -------------------------------------------------------------------------
    # Tool Execution Persistence
    # -------------------------------------------------------------------------

    def _record_tool_execution_sync(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        tool_version: str,
        fingerprint: str,
        attempt_number: int,
        is_repaired: bool,
        arguments: Dict[str, Any],
        status: str,
        result: Optional[Dict[str, Any]] = None,
        failure: Optional[Dict[str, Any]] = None,
        timing: Optional[Dict[str, Any]] = None,
        operation_identity: Optional[str] = None,
    ):
        self._ensure_init()
        clean_args = json.dumps(redact_secrets(arguments or {}))
        clean_res = json.dumps(redact_secrets(result)) if result is not None else None
        clean_fail = json.dumps(redact_secrets(failure)) if failure is not None else None
        clean_timing = json.dumps(timing) if timing is not None else None

        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO tool_executions (
                    run_id, tool_call_id, tool_name, tool_version, fingerprint,
                    attempt_number, is_repaired, arguments, status, result, failure, timing, operation_identity
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, tool_call_id, attempt_number) DO UPDATE SET
                    status = excluded.status,
                    result = excluded.result,
                    failure = excluded.failure,
                    timing = excluded.timing,
                    operation_identity = excluded.operation_identity;
            """, (
                run_id, tool_call_id, tool_name, tool_version, fingerprint,
                attempt_number, 1 if is_repaired else 0, clean_args, status, clean_res, clean_fail, clean_timing, operation_identity
            ))

    async def record_tool_execution(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        tool_version: str,
        fingerprint: str,
        attempt_number: int,
        is_repaired: bool,
        arguments: Dict[str, Any],
        status: str,
        result: Optional[Dict[str, Any]] = None,
        failure: Optional[Dict[str, Any]] = None,
        timing: Optional[Dict[str, Any]] = None,
        operation_identity: Optional[str] = None,
    ):
        await asyncio.to_thread(
            self._record_tool_execution_sync,
            run_id,
            tool_call_id,
            tool_name,
            tool_version,
            fingerprint,
            attempt_number,
            is_repaired,
            arguments,
            status,
            result,
            failure,
            timing,
            operation_identity,
        )

    # -------------------------------------------------------------------------
    # Repair Attempts Persistence
    # -------------------------------------------------------------------------

    def _record_repair_attempt_sync(
        self,
        run_id: str,
        tool_name: str,
        base_version: str,
        base_fingerprint: str,
        proposal_fingerprint: str,
        candidate_id: Optional[str],
        candidate_version: Optional[str],
        candidate_fingerprint: Optional[str],
        diagnosis_source: str,
        diagnosis_tokens: int,
        proposal: Dict[str, Any],
        candidate_definition: Optional[Dict[str, Any]],
        validation_status: str,
        validation_details: Optional[Dict[str, Any]],
        recovery_status: str,
        business_effect_occurred: bool,
        verification_succeeded: bool,
        registry_committed: bool,
        committed_version: Optional[str],
        termination_reason: Optional[str],
        recovery_duration_ms: Optional[float] = None,
        commit_latency_ms: Optional[float] = None,
        failed_recovery_duration_ms: Optional[float] = None,
        created_at: Optional[str] = None,
    ):
        self._ensure_init()
        clean_prop = json.dumps(redact_secrets(proposal or {}))
        clean_cand = json.dumps(redact_secrets(candidate_definition)) if candidate_definition is not None else None
        clean_val_det = json.dumps(redact_secrets(validation_details)) if validation_details is not None else None
        now_iso = created_at or datetime.now(timezone.utc).isoformat()

        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO repair_attempts (
                    run_id, tool_name, base_version, base_fingerprint, proposal_fingerprint,
                    candidate_id, candidate_version, candidate_fingerprint,
                    diagnosis_source, diagnosis_tokens, proposal, candidate_definition,
                    validation_status, validation_details, recovery_status,
                    business_effect_occurred, verification_succeeded, registry_committed,
                    committed_version, termination_reason,
                    recovery_duration_ms, commit_latency_ms, failed_recovery_duration_ms, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, tool_name, proposal_fingerprint) DO UPDATE SET
                    candidate_id = excluded.candidate_id,
                    candidate_version = excluded.candidate_version,
                    candidate_fingerprint = excluded.candidate_fingerprint,
                    diagnosis_source = excluded.diagnosis_source,
                    diagnosis_tokens = excluded.diagnosis_tokens,
                    proposal = excluded.proposal,
                    candidate_definition = excluded.candidate_definition,
                    validation_status = excluded.validation_status,
                    validation_details = excluded.validation_details,
                    recovery_status = excluded.recovery_status,
                    business_effect_occurred = excluded.business_effect_occurred,
                    verification_succeeded = excluded.verification_succeeded,
                    registry_committed = excluded.registry_committed,
                    committed_version = excluded.committed_version,
                    termination_reason = excluded.termination_reason,
                    recovery_duration_ms = excluded.recovery_duration_ms,
                    commit_latency_ms = excluded.commit_latency_ms,
                    failed_recovery_duration_ms = excluded.failed_recovery_duration_ms;
            """, (
                run_id, tool_name, base_version, base_fingerprint, proposal_fingerprint,
                candidate_id, candidate_version, candidate_fingerprint,
                diagnosis_source, diagnosis_tokens, clean_prop, clean_cand,
                validation_status, clean_val_det, recovery_status,
                1 if business_effect_occurred else 0,
                1 if verification_succeeded else 0,
                1 if registry_committed else 0,
                committed_version, termination_reason,
                recovery_duration_ms, commit_latency_ms, failed_recovery_duration_ms, now_iso
            ))

    async def record_repair_attempt(
        self,
        run_id: str,
        tool_name: str,
        base_version: str,
        base_fingerprint: str,
        proposal_fingerprint: str,
        candidate_id: Optional[str],
        candidate_version: Optional[str],
        candidate_fingerprint: Optional[str],
        diagnosis_source: str,
        diagnosis_tokens: int,
        proposal: Dict[str, Any],
        candidate_definition: Optional[Dict[str, Any]],
        validation_status: str,
        validation_details: Optional[Dict[str, Any]],
        recovery_status: str,
        business_effect_occurred: bool,
        verification_succeeded: bool,
        registry_committed: bool,
        committed_version: Optional[str],
        termination_reason: Optional[str],
        recovery_duration_ms: Optional[float] = None,
        commit_latency_ms: Optional[float] = None,
        failed_recovery_duration_ms: Optional[float] = None,
        created_at: Optional[str] = None,
    ):
        await asyncio.to_thread(
            self._record_repair_attempt_sync,
            run_id,
            tool_name,
            base_version,
            base_fingerprint,
            proposal_fingerprint,
            candidate_id,
            candidate_version,
            candidate_fingerprint,
            diagnosis_source,
            diagnosis_tokens,
            proposal,
            candidate_definition,
            validation_status,
            validation_details,
            recovery_status,
            business_effect_occurred,
            verification_succeeded,
            registry_committed,
            committed_version,
            termination_reason,
            recovery_duration_ms,
            commit_latency_ms,
            failed_recovery_duration_ms,
            created_at,
        )

    # -------------------------------------------------------------------------
    # Pre-Write Audit Hook
    # -------------------------------------------------------------------------

    def _record_pre_write_audit_sync(
        self,
        run_id: str,
        tool_name: str,
        candidate_id: str,
        candidate_version: str,
        proposal_fingerprint: str,
        operation_identity: str,
    ):
        """Pre-write audit entry in SQLite.
        
        If this fails, the mutating repaired dispatch MUST be blocked!
        """
        self._ensure_init()
        with self._transaction() as conn:
            # Check run exists
            cursor = conn.execute("SELECT run_id FROM runs WHERE run_id = ?;", (run_id,))
            if not cursor.fetchone():
                conn.execute("""
                    INSERT OR IGNORE INTO runs (run_id, mode, task, status, created_at, audit_status)
                    VALUES (?, 'healing', 'Autonomous repair execution', 'running', ?, 'complete');
                """, (run_id, datetime.now(timezone.utc).isoformat()))

            # Record pre-write audit event
            seq_cursor = conn.execute("SELECT COUNT(*) as c FROM events WHERE run_id = ?;", (run_id,))
            seq = (seq_cursor.fetchone()["c"] or 0) + 1

            now_iso = datetime.now(timezone.utc).isoformat()
            pre_write_data = json.dumps({
                "pre_write_authorization": True,
                "tool": tool_name,
                "candidate_id": candidate_id,
                "candidate_version": candidate_version,
                "proposal_fingerprint": proposal_fingerprint,
                "operation_identity": operation_identity,
            })
            conn.execute("""
                INSERT OR IGNORE INTO events (run_id, sequence, timestamp, mode, event_type, tool, data)
                VALUES (?, ?, ?, 'healing', 'pre_write_audit_authorized', ?, ?);
            """, (run_id, seq, now_iso, tool_name, pre_write_data))

    async def record_pre_write_audit(
        self,
        run_id: str,
        tool_name: str,
        candidate_id: str,
        candidate_version: str,
        proposal_fingerprint: str,
        operation_identity: str,
    ):
        try:
            await asyncio.to_thread(
                self._record_pre_write_audit_sync,
                run_id,
                tool_name,
                candidate_id,
                candidate_version,
                proposal_fingerprint,
                operation_identity,
            )
        except Exception as e:
            raise PreWriteAuditError(f"Pre-write audit persistence failed: {e}") from e

    # -------------------------------------------------------------------------
    # Events Persistence
    # -------------------------------------------------------------------------

    def _record_event_sync(
        self,
        run_id: str,
        sequence: int,
        timestamp: str,
        mode: str,
        event_type: str,
        tool: Optional[str],
        data: Dict[str, Any],
    ):
        self._ensure_init()
        clean_data = json.dumps(redact_secrets(data or {}))
        with self._transaction() as conn:
            # Ensure parent run exists
            conn.execute("""
                INSERT OR IGNORE INTO runs (run_id, mode, task, status, created_at, audit_status)
                VALUES (?, ?, 'Task execution', 'running', ?, 'complete');
            """, (run_id, mode, timestamp))

            conn.execute("""
                INSERT OR IGNORE INTO events (run_id, sequence, timestamp, mode, event_type, tool, data)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (run_id, sequence, timestamp, mode, event_type, tool, clean_data))

    async def record_event(
        self,
        run_id: str,
        sequence: int,
        timestamp: str,
        mode: str,
        event_type: str,
        tool: Optional[str],
        data: Dict[str, Any],
    ):
        await asyncio.to_thread(
            self._record_event_sync,
            run_id,
            sequence,
            timestamp,
            mode,
            event_type,
            tool,
            data,
        )

    # -------------------------------------------------------------------------
    # Model Usage Persistence
    # -------------------------------------------------------------------------

    def _record_model_usage_sync(
        self,
        run_id: str,
        call_id: str,
        purpose: str,
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        total_tokens: Optional[int],
        is_complete: bool = True,
        created_at: Optional[str] = None,
    ):
        self._ensure_init()
        now_iso = created_at or datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO runs (run_id, mode, task, status, created_at, audit_status)
                VALUES (?, 'model_driven', 'Task execution', 'running', ?, 'complete');
            """, (run_id, now_iso))

            conn.execute("""
                INSERT OR IGNORE INTO model_usages (
                    run_id, call_id, purpose, prompt_tokens, completion_tokens, total_tokens, is_complete, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, call_id, purpose, prompt_tokens, completion_tokens, total_tokens,
                1 if is_complete else 0, now_iso
            ))

    async def record_model_usage(
        self,
        run_id: str,
        call_id: str,
        purpose: str,
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        total_tokens: Optional[int],
        is_complete: bool = True,
        created_at: Optional[str] = None,
    ):
        await asyncio.to_thread(
            self._record_model_usage_sync,
            run_id,
            call_id,
            purpose,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            is_complete,
            created_at,
        )

    def _mark_audit_incomplete_sync(self, run_id: str):
        self._ensure_init()
        with self._transaction() as conn:
            conn.execute("UPDATE runs SET audit_status = 'incomplete' WHERE run_id = ?;", (run_id,))

    async def mark_audit_incomplete(self, run_id: str):
        await asyncio.to_thread(self._mark_audit_incomplete_sync, run_id)

    # -------------------------------------------------------------------------
    # Read Methods
    # -------------------------------------------------------------------------

    def _get_run_sync(self, run_id: str) -> Optional[PersistedRunRecord]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            cur = conn.execute("SELECT * FROM runs WHERE run_id = ?;", (run_id,))
            row = cur.fetchone()
            if not row:
                return None
            outcome = json.loads(row["verified_outcome"]) if row["verified_outcome"] else None
            return PersistedRunRecord(
                run_id=row["run_id"],
                mode=row["mode"],
                execution_type=row["execution_type"],
                task=row["task"],
                status=row["status"],
                termination_reason=row["termination_reason"],
                created_at=row["created_at"],
                completed_at=row["completed_at"],
                duration_ms=row["duration_ms"],
                verified_outcome=outcome,
                error=row["error"],
                audit_status=row["audit_status"],
            )
        finally:
            conn.close()

    async def get_run(self, run_id: str) -> Optional[PersistedRunRecord]:
        return await asyncio.to_thread(self._get_run_sync, run_id)

    def _get_run_tool_executions_sync(self, run_id: str) -> List[PersistedToolExecution]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM tool_executions WHERE run_id = ? ORDER BY id ASC;",
                (run_id,)
            )
            records = []
            for r in cur.fetchall():
                records.append(PersistedToolExecution(
                    run_id=r["run_id"],
                    tool_call_id=r["tool_call_id"],
                    tool_name=r["tool_name"],
                    tool_version=r["tool_version"],
                    fingerprint=r["fingerprint"],
                    attempt_number=r["attempt_number"],
                    is_repaired=bool(r["is_repaired"]),
                    arguments=json.loads(r["arguments"]) if r["arguments"] else {},
                    status=r["status"],
                    result=json.loads(r["result"]) if r["result"] else None,
                    failure=json.loads(r["failure"]) if r["failure"] else None,
                    timing=json.loads(r["timing"]) if r["timing"] else None,
                    operation_identity=r["operation_identity"],
                ))
            return records
        finally:
            conn.close()

    async def get_run_tool_executions(self, run_id: str) -> List[PersistedToolExecution]:
        return await asyncio.to_thread(self._get_run_tool_executions_sync, run_id)

    def _get_run_repair_attempts_sync(self, run_id: str) -> List[PersistedRepairAttempt]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_attempts WHERE run_id = ? ORDER BY id ASC;",
                (run_id,)
            )
            records = []
            for r in cur.fetchall():
                records.append(PersistedRepairAttempt(
                    run_id=r["run_id"],
                    tool_name=r["tool_name"],
                    base_version=r["base_version"],
                    base_fingerprint=r["base_fingerprint"],
                    proposal_fingerprint=r["proposal_fingerprint"],
                    candidate_id=r["candidate_id"],
                    candidate_version=r["candidate_version"],
                    candidate_fingerprint=r["candidate_fingerprint"],
                    diagnosis_source=r["diagnosis_source"],
                    diagnosis_tokens=r["diagnosis_tokens"],
                    proposal=json.loads(r["proposal"]) if r["proposal"] else {},
                    candidate_definition=json.loads(r["candidate_definition"]) if r["candidate_definition"] else None,
                    validation_status=r["validation_status"],
                    validation_details=json.loads(r["validation_details"]) if r["validation_details"] else None,
                    recovery_status=r["recovery_status"],
                    business_effect_occurred=bool(r["business_effect_occurred"]),
                    verification_succeeded=bool(r["verification_succeeded"]),
                    registry_committed=bool(r["registry_committed"]),
                    committed_version=r["committed_version"],
                    termination_reason=r["termination_reason"],
                    recovery_duration_ms=r["recovery_duration_ms"],
                    commit_latency_ms=r["commit_latency_ms"],
                    failed_recovery_duration_ms=r["failed_recovery_duration_ms"],
                    created_at=r["created_at"],
                ))
            return records
        finally:
            conn.close()

    async def get_run_repair_attempts(self, run_id: str) -> List[PersistedRepairAttempt]:
        return await asyncio.to_thread(self._get_run_repair_attempts_sync, run_id)

    def _get_run_events_sync(self, run_id: str) -> List[PersistedEvent]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence ASC;",
                (run_id,)
            )
            records = []
            for r in cur.fetchall():
                records.append(PersistedEvent(
                    run_id=r["run_id"],
                    sequence=r["sequence"],
                    timestamp=r["timestamp"],
                    mode=r["mode"],
                    event_type=r["event_type"],
                    tool=r["tool"],
                    data=json.loads(r["data"]) if r["data"] else {},
                ))
            return records
        finally:
            conn.close()

    async def get_run_events(self, run_id: str) -> List[PersistedEvent]:
        return await asyncio.to_thread(self._get_run_events_sync, run_id)

    def _get_run_model_usages_sync(self, run_id: str) -> List[PersistedModelUsage]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM model_usages WHERE run_id = ? ORDER BY id ASC;",
                (run_id,)
            )
            records = []
            for r in cur.fetchall():
                records.append(PersistedModelUsage(
                    run_id=r["run_id"],
                    call_id=r["call_id"],
                    purpose=r["purpose"],
                    prompt_tokens=r["prompt_tokens"],
                    completion_tokens=r["completion_tokens"],
                    total_tokens=r["total_tokens"],
                    is_complete=bool(r["is_complete"]),
                    created_at=r["created_at"],
                ))
            return records
        finally:
            conn.close()

    async def get_run_model_usages(self, run_id: str) -> List[PersistedModelUsage]:
        return await asyncio.to_thread(self._get_run_model_usages_sync, run_id)

    def _get_all_repair_attempts_sync(self, limit: int = 50, offset: int = 0) -> List[PersistedRepairAttempt]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            cur = conn.execute("""
                SELECT * FROM repair_attempts
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?;
            """, (min(limit, 100), offset))
            records = []
            for r in cur.fetchall():
                records.append(PersistedRepairAttempt(
                    run_id=r["run_id"],
                    tool_name=r["tool_name"],
                    base_version=r["base_version"],
                    base_fingerprint=r["base_fingerprint"],
                    proposal_fingerprint=r["proposal_fingerprint"],
                    candidate_id=r["candidate_id"],
                    candidate_version=r["candidate_version"],
                    candidate_fingerprint=r["candidate_fingerprint"],
                    diagnosis_source=r["diagnosis_source"],
                    diagnosis_tokens=r["diagnosis_tokens"],
                    proposal=json.loads(r["proposal"]) if r["proposal"] else {},
                    candidate_definition=json.loads(r["candidate_definition"]) if r["candidate_definition"] else None,
                    validation_status=r["validation_status"],
                    validation_details=json.loads(r["validation_details"]) if r["validation_details"] else None,
                    recovery_status=r["recovery_status"],
                    business_effect_occurred=bool(r["business_effect_occurred"]),
                    verification_succeeded=bool(r["verification_succeeded"]),
                    registry_committed=bool(r["registry_committed"]),
                    committed_version=r["committed_version"],
                    termination_reason=r["termination_reason"],
                    recovery_duration_ms=r["recovery_duration_ms"],
                    commit_latency_ms=r["commit_latency_ms"],
                    failed_recovery_duration_ms=r["failed_recovery_duration_ms"],
                    created_at=r["created_at"],
                ))
            return records
        finally:
            conn.close()

    async def get_all_repair_attempts(self, limit: int = 50, offset: int = 0) -> List[PersistedRepairAttempt]:
        return await asyncio.to_thread(self._get_all_repair_attempts_sync, limit, offset)

    def _list_runs_sync(
        self, limit: int = 50, offset: int = 0, mode: Optional[str] = None
    ) -> Tuple[List[PersistedRunRecord], int]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            query = "SELECT * FROM runs"
            count_query = "SELECT COUNT(*) as total FROM runs"
            params: List[Any] = []

            if mode:
                query += " WHERE mode = ?"
                count_query += " WHERE mode = ?"
                params.append(mode)

            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?;"
            params_with_bounds = list(params)
            params_with_bounds.extend([min(limit, 100), offset])

            cur = conn.execute(query, tuple(params_with_bounds))
            rows = cur.fetchall()

            count_cur = conn.execute(count_query, tuple(params))
            total_count = count_cur.fetchone()["total"]

            records = []
            for r in rows:
                outcome = json.loads(r["verified_outcome"]) if r["verified_outcome"] else None
                records.append(PersistedRunRecord(
                    run_id=r["run_id"],
                    mode=r["mode"],
                    execution_type=r["execution_type"],
                    task=r["task"],
                    status=r["status"],
                    termination_reason=r["termination_reason"],
                    created_at=r["created_at"],
                    completed_at=r["completed_at"],
                    duration_ms=r["duration_ms"],
                    verified_outcome=outcome,
                    error=r["error"],
                    audit_status=r["audit_status"],
                ))
            return records, total_count
        finally:
            conn.close()

    async def list_runs(
        self, limit: int = 50, offset: int = 0, mode: Optional[str] = None
    ) -> Tuple[List[PersistedRunRecord], int]:
        return await asyncio.to_thread(self._list_runs_sync, limit, offset, mode)
