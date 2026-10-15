"""SQLite-backed persistent audit store for DriftGuard.
"""
from __future__ import annotations
import threading
import sqlite3
import json
import os
import asyncio
import hashlib
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
    PersistedLLMInteraction,
    PersistedRepairCacheEntry,
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

    SCHEMA_VERSION = 3

    def __init__(self, db_path: Optional[str] = None, timeout: float = 5.0):
        self.db_path = str(db_path or settings.sqlite_db_path)
        self.timeout = timeout
        self._initialized = False
        self._init_lock = threading.Lock()

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

            if current_version < 2:
                conn.execute("""CREATE TABLE IF NOT EXISTS pre_write_authorizations (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    operation_identity TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (run_id, operation_identity, candidate_id)
                );""")
                conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                    (datetime.now(timezone.utc).isoformat(),))

            if current_version < 3:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS llm_interactions (
                        interaction_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        incident_id TEXT NOT NULL,
                        trial_id TEXT,
                        created_at TEXT NOT NULL,
                        duration_ms REAL NOT NULL,
                        provider TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        prompt_template_version TEXT NOT NULL,
                        response_schema_version TEXT NOT NULL,
                        cache_key TEXT,
                        cache_status TEXT NOT NULL,
                        sanitized_prompt_hash TEXT NOT NULL,
                        sanitized_prompt_payload TEXT,
                        sanitized_response_payload TEXT,
                        failure_payload TEXT,
                        response_hash TEXT,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        total_tokens INTEGER,
                        usage_unavailable_reason TEXT,
                        timed_out BOOLEAN NOT NULL DEFAULT 0,
                        provider_error TEXT,
                        parse_status TEXT NOT NULL,
                        validation_status TEXT NOT NULL,
                        deterministic_decision TEXT NOT NULL,
                        reused_later BOOLEAN NOT NULL DEFAULT 0,
                        retention_until TEXT,
                        deleted_at TEXT
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS validated_repair_cache (
                        cache_key TEXT PRIMARY KEY,
                        scope_hashes TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        base_version TEXT NOT NULL,
                        base_fingerprint TEXT NOT NULL,
                        policy_version TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        response_schema_version TEXT NOT NULL,
                        model_configuration_hash TEXT NOT NULL,
                        operations TEXT NOT NULL,
                        evidence_references TEXT NOT NULL,
                        proposal_fingerprint TEXT NOT NULL,
                        original_interaction_id TEXT NOT NULL REFERENCES llm_interactions(interaction_id),
                        committed_version TEXT NOT NULL,
                        committed_fingerprint TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        last_accessed_at TEXT,
                        hit_count INTEGER NOT NULL DEFAULT 0,
                        valid BOOLEAN NOT NULL DEFAULT 1,
                        invalidation_reason TEXT
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_interaction_incident ON llm_interactions(incident_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_interaction_cache ON llm_interactions(cache_key);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_interaction_status ON llm_interactions(validation_status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_interaction_created ON llm_interactions(created_at);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_repair_cache_expiry ON validated_repair_cache(expires_at, valid);")
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (3, ?)",
                    (datetime.now(timezone.utc).isoformat(),),
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
        await asyncio.to_thread(self._ensure_init)

    def _ensure_init(self):
        with self._init_lock:
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
                    audit_status = CASE WHEN audit_status = 'incomplete' OR ? = 'incomplete' THEN 'incomplete' ELSE 'complete' END
                WHERE run_id = ?;
            """, (status, now_iso, duration_ms, outcome_json, redact_secrets(error), termination_reason, audit_status, run_id))

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

            # Separate internal authorization records from the public SSE sequence.
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
                INSERT OR IGNORE INTO pre_write_authorizations (run_id, operation_identity, candidate_id, timestamp, data)
                VALUES (?, ?, ?, ?, ?);
            """, (run_id, operation_identity, candidate_id, now_iso, pre_write_data))

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
    # Repair-model interaction and validated-cache persistence
    # -------------------------------------------------------------------------

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _record_llm_interaction_sync(
        self, interaction: PersistedLLMInteraction
    ) -> PersistedLLMInteraction:
        """Append one redacted interaction record; duplicate IDs fail closed."""
        self._ensure_init()
        prompt = redact_secrets(interaction.sanitized_prompt_payload)
        response = redact_secrets(interaction.sanitized_response_payload)
        failure = redact_secrets(interaction.failure_payload)
        prompt_hash = hashlib.sha256(self._canonical_json(prompt).encode("utf-8")).hexdigest()
        response_hash = (
            hashlib.sha256(self._canonical_json(response).encode("utf-8")).hexdigest()
            if response is not None else None
        )
        clean = interaction.model_copy(update={
            "sanitized_prompt_payload": prompt,
            "sanitized_response_payload": response,
            "failure_payload": failure,
            "sanitized_prompt_hash": prompt_hash,
            "response_hash": response_hash,
            "provider_error": redact_secrets(interaction.provider_error),
        })
        with self._transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO runs (run_id, mode, task, status, created_at, audit_status)
                VALUES (?, 'healing', 'Repair-model diagnosis', 'running', ?, 'complete');
            """, (clean.run_id, clean.created_at))
            conn.execute("""
                INSERT INTO llm_interactions (
                    interaction_id, run_id, incident_id, trial_id, created_at, duration_ms,
                    provider, model_name, prompt_template_version, response_schema_version,
                    cache_key, cache_status, sanitized_prompt_hash, sanitized_prompt_payload,
                    sanitized_response_payload, failure_payload, response_hash,
                    input_tokens, output_tokens, total_tokens, usage_unavailable_reason,
                    timed_out, provider_error, parse_status, validation_status,
                    deterministic_decision, reused_later, retention_until, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                clean.interaction_id, clean.run_id, clean.incident_id, clean.trial_id,
                clean.created_at, clean.duration_ms, clean.provider, clean.model_name,
                clean.prompt_template_version, clean.response_schema_version,
                clean.cache_key, clean.cache_status, clean.sanitized_prompt_hash,
                self._canonical_json(prompt) if prompt is not None else None,
                self._canonical_json(response) if response is not None else None,
                self._canonical_json(failure) if failure is not None else None,
                clean.response_hash, clean.input_tokens, clean.output_tokens,
                clean.total_tokens, clean.usage_unavailable_reason,
                1 if clean.timed_out else 0, clean.provider_error, clean.parse_status,
                clean.validation_status, clean.deterministic_decision,
                1 if clean.reused_later else 0, clean.retention_until, clean.deleted_at,
            ))
        return clean

    async def record_llm_interaction(
        self, interaction: PersistedLLMInteraction
    ) -> PersistedLLMInteraction:
        return await asyncio.to_thread(self._record_llm_interaction_sync, interaction)

    @staticmethod
    def _llm_interaction_from_row(row: sqlite3.Row) -> PersistedLLMInteraction:
        return PersistedLLMInteraction(
            interaction_id=row["interaction_id"], run_id=row["run_id"],
            incident_id=row["incident_id"], trial_id=row["trial_id"],
            created_at=row["created_at"], duration_ms=row["duration_ms"],
            provider=row["provider"], model_name=row["model_name"],
            prompt_template_version=row["prompt_template_version"],
            response_schema_version=row["response_schema_version"],
            cache_key=row["cache_key"], cache_status=row["cache_status"],
            sanitized_prompt_hash=row["sanitized_prompt_hash"],
            sanitized_prompt_payload=json.loads(row["sanitized_prompt_payload"]) if row["sanitized_prompt_payload"] else None,
            sanitized_response_payload=json.loads(row["sanitized_response_payload"]) if row["sanitized_response_payload"] else None,
            failure_payload=json.loads(row["failure_payload"]) if row["failure_payload"] else None,
            response_hash=row["response_hash"], input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"], total_tokens=row["total_tokens"],
            usage_unavailable_reason=row["usage_unavailable_reason"],
            timed_out=bool(row["timed_out"]), provider_error=row["provider_error"],
            parse_status=row["parse_status"], validation_status=row["validation_status"],
            deterministic_decision=row["deterministic_decision"],
            reused_later=bool(row["reused_later"]), retention_until=row["retention_until"],
            deleted_at=row["deleted_at"],
        )

    def _get_run_llm_interactions_sync(self, run_id: str) -> List[PersistedLLMInteraction]:
        self._ensure_init()
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM llm_interactions WHERE run_id = ? ORDER BY created_at, interaction_id;",
                (run_id,),
            ).fetchall()
            return [self._llm_interaction_from_row(row) for row in rows]
        finally:
            conn.close()

    async def get_run_llm_interactions(self, run_id: str) -> List[PersistedLLMInteraction]:
        return await asyncio.to_thread(self._get_run_llm_interactions_sync, run_id)

    def _finalize_llm_interaction_sync(
        self, interaction_id: str, validation_status: str, deterministic_decision: str
    ) -> None:
        self._ensure_init()
        with self._transaction() as conn:
            cursor = conn.execute("""
                UPDATE llm_interactions
                SET validation_status = ?, deterministic_decision = ?
                WHERE interaction_id = ?;
            """, (validation_status, deterministic_decision, interaction_id))
            if cursor.rowcount != 1:
                raise PersistenceError("LLM interaction record was unavailable for finalization")

    async def finalize_llm_interaction(
        self, interaction_id: str, validation_status: str, deterministic_decision: str
    ) -> None:
        await asyncio.to_thread(
            self._finalize_llm_interaction_sync,
            interaction_id,
            validation_status,
            deterministic_decision,
        )

    @staticmethod
    def _repair_cache_from_row(row: sqlite3.Row) -> PersistedRepairCacheEntry:
        return PersistedRepairCacheEntry(
            cache_key=row["cache_key"], scope_hashes=json.loads(row["scope_hashes"]),
            tool_name=row["tool_name"], base_version=row["base_version"],
            base_fingerprint=row["base_fingerprint"], policy_version=row["policy_version"],
            prompt_version=row["prompt_version"], response_schema_version=row["response_schema_version"],
            model_configuration_hash=row["model_configuration_hash"],
            operations=json.loads(row["operations"]),
            evidence_references=json.loads(row["evidence_references"]),
            proposal_fingerprint=row["proposal_fingerprint"],
            original_interaction_id=row["original_interaction_id"],
            committed_version=row["committed_version"], committed_fingerprint=row["committed_fingerprint"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            last_accessed_at=row["last_accessed_at"], hit_count=row["hit_count"],
            valid=bool(row["valid"]), invalidation_reason=row["invalidation_reason"],
        )

    def _put_validated_repair_cache_sync(self, entry: PersistedRepairCacheEntry) -> bool:
        self._ensure_init()
        clean_ops = redact_secrets(entry.operations)
        clean_evidence = redact_secrets(entry.evidence_references)
        with self._transaction() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO validated_repair_cache (
                    cache_key, scope_hashes, tool_name, base_version, base_fingerprint,
                    policy_version, prompt_version, response_schema_version,
                    model_configuration_hash, operations, evidence_references,
                    proposal_fingerprint, original_interaction_id, committed_version,
                    committed_fingerprint, created_at, expires_at, last_accessed_at,
                    hit_count, valid, invalidation_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                entry.cache_key, self._canonical_json(entry.scope_hashes), entry.tool_name,
                entry.base_version, entry.base_fingerprint, entry.policy_version,
                entry.prompt_version, entry.response_schema_version,
                entry.model_configuration_hash, self._canonical_json(clean_ops),
                self._canonical_json(clean_evidence), entry.proposal_fingerprint,
                entry.original_interaction_id, entry.committed_version,
                entry.committed_fingerprint, entry.created_at, entry.expires_at,
                entry.last_accessed_at, entry.hit_count, 1 if entry.valid else 0,
                entry.invalidation_reason,
            ))
            inserted = cursor.rowcount == 1
            conn.execute("""
                UPDATE validated_repair_cache SET valid = 0, invalidation_reason = 'expired'
                WHERE valid = 1 AND expires_at <= ?;
            """, (datetime.now(timezone.utc).isoformat(),))
            overflow = conn.execute("""
                SELECT cache_key FROM validated_repair_cache WHERE valid = 1
                ORDER BY COALESCE(last_accessed_at, created_at) DESC, cache_key DESC
                LIMIT -1 OFFSET ?;
            """, (max(1, settings.repair_cache_max_entries),)).fetchall()
            if overflow:
                conn.executemany(
                    "UPDATE validated_repair_cache SET valid = 0, invalidation_reason = 'size_eviction' WHERE cache_key = ?;",
                    [(row["cache_key"],) for row in overflow],
                )
        return inserted

    async def put_validated_repair_cache(self, entry: PersistedRepairCacheEntry) -> bool:
        return await asyncio.to_thread(self._put_validated_repair_cache_sync, entry)

    def _get_validated_repair_cache_sync(self, cache_key: str) -> Optional[PersistedRepairCacheEntry]:
        self._ensure_init()
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM validated_repair_cache WHERE cache_key = ?;", (cache_key,)
            ).fetchone()
            if row is None or not bool(row["valid"]):
                return None
            if row["expires_at"] <= now_iso:
                conn.execute(
                    "UPDATE validated_repair_cache SET valid = 0, invalidation_reason = 'expired' WHERE cache_key = ?;",
                    (cache_key,),
                )
                return None
            conn.execute("""
                UPDATE validated_repair_cache
                SET hit_count = hit_count + 1, last_accessed_at = ? WHERE cache_key = ?;
            """, (now_iso, cache_key))
            conn.execute(
                "UPDATE llm_interactions SET reused_later = 1 WHERE interaction_id = ?;",
                (row["original_interaction_id"],),
            )
            refreshed = conn.execute(
                "SELECT * FROM validated_repair_cache WHERE cache_key = ?;", (cache_key,)
            ).fetchone()
            return self._repair_cache_from_row(refreshed)

    async def get_validated_repair_cache(self, cache_key: str) -> Optional[PersistedRepairCacheEntry]:
        return await asyncio.to_thread(self._get_validated_repair_cache_sync, cache_key)

    def _invalidate_repair_cache_sync(self, cache_key: str, reason: str) -> None:
        self._ensure_init()
        with self._transaction() as conn:
            conn.execute("""
                UPDATE validated_repair_cache SET valid = 0, invalidation_reason = ?
                WHERE cache_key = ?;
            """, (redact_secrets(reason), cache_key))

    async def invalidate_repair_cache(self, cache_key: str, reason: str) -> None:
        await asyncio.to_thread(self._invalidate_repair_cache_sync, cache_key, reason)

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
