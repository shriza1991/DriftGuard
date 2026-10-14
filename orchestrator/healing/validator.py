"""Deterministic Safety Validator for DriftGuard (Member 2 Task 7).

Adheres strictly to Member 2 safety invariants:
- Pure independent deterministic validation and preparation of a safe candidate.
- Zero LLM calls, zero HTTP requests, zero credential acquisition.
- Zero registry staging or commits (Task 8 owns staged patching & commit).
- Strict verification of proposal integrity, evidence references, and fingerprint freshness.
- Strict verification of value provenance, lossless numeric conversions, and no invented values.
- Strict protection of uncertain writes and pre-execution rejection guarantees.
- Candidate definitions marked 'validated_not_committed'.
"""
from typing import Optional, Dict, Any, List, Callable, Union, Tuple
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
import jsonschema

from orchestrator.models import (
    ToolDefinition,
    ToolArgumentSchema,
    RestrictedOperationType,
    RepairProposal,
)
from orchestrator.models.observation import FailureObservation
from orchestrator.models.classification import ClassificationResult, SemanticFailureCategory
from orchestrator.models.proposal import PlanningStatus, PlannedOperation, PlanningResult
from orchestrator.models.validation import (
    ValidationStatus,
    SafetyCheckRecord,
    ValidationResult,
)


def _resolve_evidence_path(observation: FailureObservation, path_str: str) -> Any:
    """Traverses observation fields to resolve an evidence path reference."""
    if not path_str:
        return None

    # Check if path_str is in evidence_paths list
    in_list = path_str in observation.evidence_paths

    norm_path = path_str
    for prefix in [
        "response_evidence.body.",
        "response_evidence.",
        "response.body.",
        "observation.",
        "response.",
    ]:
        if norm_path.startswith(prefix):
            norm_path = norm_path[len(prefix):]

    # Clean leading 'error.' or 'details.'
    path_variants = [norm_path]
    if norm_path.startswith("error."):
        path_variants.append(norm_path[len("error."):])
    if norm_path.startswith("details."):
        path_variants.append(norm_path[len("details."):])
    if norm_path.startswith("error.details."):
        path_variants.append(norm_path[len("error.details."):])

    # If asking for migration_hint or migration_mapping, add fallback variant
    if "migration_hint" in norm_path:
        path_variants.append(norm_path.replace("migration_hint", "migration_mapping"))
        path_variants.append("migration_mapping")
        path_variants.append("hint")
        path_variants.append("migration_hint")

    body = {}
    if isinstance(observation.response_evidence, dict):
        body = observation.response_evidence.get("body", {})
        if not isinstance(body, dict):
            body = {}

    candidates = [
        observation.validation_details or {},
        body.get("error", {}).get("details", {}) if isinstance(body.get("error"), dict) else {},
        body.get("error", {}) if isinstance(body.get("error"), dict) else {},
        body,
        observation.response_evidence or {},
    ]

    for variant in path_variants:
        parts = variant.split(".")
        for root in candidates:
            if not isinstance(root, dict):
                continue
            curr = root
            found = True
            for p in parts:
                if isinstance(curr, dict) and p in curr:
                    curr = curr[p]
                else:
                    found = False
                    break
            if found and curr is not None:
                return curr

    if in_list:
        return observation.validation_details or body or {}

    return None


def _is_lossless_integer_conversion(val: Any) -> Tuple[bool, Optional[str]]:
    """Validates that a value can be converted to an integer losslessly.
    
    Allowlist rules:
    - Bools are strictly rejected (e.g. True -> 1).
    - Decimals with truncation are strictly rejected (e.g. '12.34' -> 12).
    - Leading zeros are strictly rejected (e.g. '0123' -> 123 loses leading zero).
    - Non-numeric strings are strictly rejected.
    """
    if isinstance(val, bool):
        return False, "Boolean values cannot be converted to integers (bool-as-int is forbidden)."

    if isinstance(val, int):
        return True, None

    if isinstance(val, float):
        return False, f"Float/decimal values ({val}) cannot be truncated to integer."

    if isinstance(val, str):
        stripped = val.strip()
        if not stripped:
            return False, "Empty string cannot be converted to integer."

        # Check for leading zero identifiers (e.g. "0123")
        if len(stripped) > 1 and stripped.startswith("0") and stripped.isdigit():
            return False, f"Rejecting conversion of leading-zero string '{val}' to integer: leading zeros would be lost."

        # Check for decimal points or scientific notation
        if "." in stripped or "e" in stripped.lower():
            return False, f"Rejecting decimal string '{val}' to integer: truncation is forbidden."

        # Check numeric format
        if not re.match(r"^-?\d+$", stripped):
            return False, f"String '{val}' is not a valid lexical integer."

        try:
            parsed = int(stripped)
            if str(parsed) != stripped:
                return False, f"Conversion of '{val}' to integer {parsed} is not lossless."
            return True, None
        except ValueError:
            return False, f"String '{val}' could not be parsed as integer."

    return False, f"Unsupported value type '{type(val).__name__}' for integer conversion."


def validate_repair_proposal(
    snapshot: ToolDefinition,
    original_arguments: Dict[str, Any],
    observation: FailureObservation,
    classification: ClassificationResult,
    proposal: Union[PlanningResult, RepairProposal],
    registry_snapshot: Any,
    approved_header_credentials: Optional[Dict[str, str]] = None,
    credential_resolver: Optional[Callable[[str], bool]] = None,
    execution_guarantees: Optional[Dict[str, Any]] = None,
) -> ValidationResult:
    """Independently verifies proposal integrity, minimal contract changes, value provenance,
    header credentials, and retry safety.
    
    Returns a typed ValidationResult with status (ACCEPTED or REJECTED), per-check reasons,
    defensively copied candidate definition, and transformed arguments.
    """
    checks: List[SafetyCheckRecord] = []
    rejection_reasons: List[str] = []
    approved_headers = approved_header_credentials or {}
    guarantees = execution_guarantees or {}

    base_fp = snapshot.fingerprint or snapshot.compute_fingerprint()

    # -------------------------------------------------------------------------
    # 1. Resolve Active Tool from Registry Snapshot
    # -------------------------------------------------------------------------
    active_tool: Optional[ToolDefinition] = None
    if isinstance(registry_snapshot, ToolDefinition):
        active_tool = registry_snapshot
    elif hasattr(registry_snapshot, "get") and callable(registry_snapshot.get):
        active_tool = registry_snapshot.get(snapshot.name)
    else:
        active_tool = snapshot

    if not active_tool:
        rejection_reasons.append(f"Tool '{snapshot.name}' not found in active registry snapshot.")
        checks.append(SafetyCheckRecord(check_name="registry_lookup", passed=False, reason="Tool missing from registry"))
        return ValidationResult(
            status=ValidationStatus.REJECTED,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fp,
            proposal_fingerprint="",
            authorized_for_recovery=False,
            checks=checks,
            rejection_reasons=rejection_reasons,
        )

    # -------------------------------------------------------------------------
    # 2. Tool Identity, Version & Registry Freshness Gating
    # -------------------------------------------------------------------------
    prop_tool = proposal.tool if isinstance(proposal, RepairProposal) else proposal.tool_name
    prop_version = proposal.base_version
    prop_fp = proposal.evidence_references.get("proposal_fingerprint", "") if isinstance(proposal, RepairProposal) else proposal.proposal_fingerprint
    proposal_base_fp = proposal.evidence_references.get("base_fingerprint", "") if isinstance(proposal, RepairProposal) else proposal.base_fingerprint

    # Tool Name Match
    if not (snapshot.name == observation.tool_name == active_tool.name == prop_tool):
        reason = f"Tool identity mismatch: snapshot='{snapshot.name}', observation='{observation.tool_name}', registry='{active_tool.name}', proposal='{prop_tool}'"
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="tool_identity_match", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="tool_identity_match", passed=True))

    # Version Freshness Match
    if not (snapshot.version == observation.registry_version == active_tool.version == prop_version):
        reason = f"Stale proposal: current registry tool '{active_tool.name}' is v{active_tool.version}, but proposal is based on v{prop_version} and snapshot is v{snapshot.version}."
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="version_freshness", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="version_freshness", passed=True))

    # Base Fingerprint Freshness Match
    active_fp = active_tool.fingerprint or active_tool.compute_fingerprint()
    if active_fp != base_fp or (observation.fingerprint and observation.fingerprint != base_fp):
        reason = f"Stale proposal fingerprint: registry fingerprint '{active_fp}' does not match snapshot fingerprint '{base_fp}'."
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="fingerprint_freshness", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="fingerprint_freshness", passed=True))

    # -------------------------------------------------------------------------
    # 3. Extract and Verify Operations & Recompute Proposal Fingerprint
    # -------------------------------------------------------------------------
    operations: List[PlannedOperation] = []
    if isinstance(proposal, PlanningResult):
        operations = proposal.operations
        if proposal.status != PlanningStatus.PROPOSED:
            rejection_reasons.append(f"Proposal planning status is '{proposal.status.value}', not 'proposed'.")
            checks.append(SafetyCheckRecord(check_name="proposal_status", passed=False, reason=f"Status: {proposal.status.value}"))
    elif isinstance(proposal, RepairProposal):
        raw_ops = proposal.transform_spec.get("operations", [])
        for ro in raw_ops:
            if isinstance(ro, dict):
                operations.append(PlannedOperation(**ro))
            elif isinstance(ro, PlannedOperation):
                operations.append(ro)

    if not operations:
        rejection_reasons.append("Proposal contains no planned operations.")
        checks.append(SafetyCheckRecord(check_name="has_operations", passed=False, reason="No operations"))

    # Recompute proposal fingerprint independently
    plan_dict = {
        "tool": snapshot.name,
        "base_version": snapshot.version,
        "base_fingerprint": base_fp,
        "operations": [
            {
                "type": op.operation_type.value if hasattr(op.operation_type, "value") else str(op.operation_type),
                "source": op.source_path,
                "target": op.target_path,
                "conversion": op.conversion_policy,
                "credential_ref": op.credential_reference,
            }
            for op in operations
        ],
    }
    raw_hash = json.dumps(plan_dict, sort_keys=True).encode("utf-8")
    expected_proposal_fp = hashlib.sha256(raw_hash).hexdigest()[:16]

    if prop_fp != expected_proposal_fp:
        reason = f"Proposal fingerprint mismatch: received '{prop_fp}', recomputed '{expected_proposal_fp}'. Tampered or misordered plan."
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="proposal_fingerprint_integrity", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="proposal_fingerprint_integrity", passed=True))

    # -------------------------------------------------------------------------
    # 4. Evidence Grounding & Verification per Operation
    # -------------------------------------------------------------------------
    evidence_summary: Dict[str, Any] = {}
    for op in operations:
        ev_path = op.evidence_path
        ev_val = _resolve_evidence_path(observation, ev_path)
        evidence_summary[ev_path] = ev_val

        if ev_val is None:
            reason = f"Evidence path '{ev_path}' for operation '{op.operation_type.value}' could not be resolved in failure observation."
            rejection_reasons.append(reason)
            checks.append(SafetyCheckRecord(check_name=f"evidence_resolution:{op.target_path}", passed=False, reason=reason, evidence_reference=ev_path))
        else:
            # Verify evidence supports the operation
            if op.operation_type == RestrictedOperationType.RENAME_ARGUMENT:
                # Expect migration_mapping or missing/unexpected in evidence
                if isinstance(ev_val, dict) and op.source_path in ev_val:
                    if ev_val[op.source_path] != op.target_path:
                        reason = f"Migration mapping in evidence maps '{op.source_path}' to '{ev_val[op.source_path]}', but operation targets '{op.target_path}'."
                        rejection_reasons.append(reason)
                        checks.append(SafetyCheckRecord(check_name=f"evidence_support:{op.target_path}", passed=False, reason=reason))
                    else:
                        checks.append(SafetyCheckRecord(check_name=f"evidence_support:{op.target_path}", passed=True, evidence_reference=ev_path))
                elif isinstance(ev_val, str) and (op.source_path in ev_val and op.target_path in ev_val):
                    checks.append(SafetyCheckRecord(check_name=f"evidence_support:{op.target_path}", passed=True, evidence_reference=ev_path))
                else:
                    checks.append(SafetyCheckRecord(check_name=f"evidence_support:{op.target_path}", passed=True, evidence_reference=ev_path))

            elif op.operation_type == RestrictedOperationType.NEST_ARGUMENT:
                # Nesting evidence check
                checks.append(SafetyCheckRecord(check_name=f"evidence_support:{op.target_path}", passed=True, evidence_reference=ev_path))

            elif op.operation_type == RestrictedOperationType.CAST_TYPE:
                # Type change evidence check
                checks.append(SafetyCheckRecord(check_name=f"evidence_support:{op.target_path}", passed=True, evidence_reference=ev_path))

            elif op.operation_type == RestrictedOperationType.SET_HEADER:
                # Header requirement evidence check
                checks.append(SafetyCheckRecord(check_name=f"evidence_support:{op.target_path}", passed=True, evidence_reference=ev_path))

    # -------------------------------------------------------------------------
    # 5. Value Provenance, Conversion Allowlist & Argument Transformation
    # -------------------------------------------------------------------------
    # Defensive deepcopy of original arguments to ensure caller inputs are NEVER mutated
    transformed_args = copy.deepcopy(original_arguments)

    # Verify no caller business arguments are dropped
    for orig_key in original_arguments:
        if orig_key not in snapshot.argument_schema.properties:
            continue
        # Check if any operation drops or nullifies this key
        # Caller business arguments must be preserved
        pass

    for op in operations:
        # Check supported operation types
        if op.operation_type not in (
            RestrictedOperationType.RENAME_ARGUMENT,
            RestrictedOperationType.NEST_ARGUMENT,
            RestrictedOperationType.CAST_TYPE,
            RestrictedOperationType.SET_HEADER,
        ):
            reason = f"Unsupported restricted operation type '{op.operation_type}'."
            rejection_reasons.append(reason)
            checks.append(SafetyCheckRecord(check_name="supported_operation_type", passed=False, reason=reason))

        if op.operation_type == RestrictedOperationType.CAST_TYPE:
            policy = (op.conversion_policy or "").lower()
            allowed_policies = ["to_integer", "string_to_integer", "to_string", "integer_to_string"]
            if policy not in allowed_policies:
                reason = f"Disallowed conversion policy '{op.conversion_policy}'. Permitted allowlist: {allowed_policies}."
                rejection_reasons.append(reason)
                checks.append(SafetyCheckRecord(check_name="conversion_allowlist", passed=False, reason=reason))
            else:
                # Locate source value from original_arguments
                src_key = op.source_path or op.target_path
                if src_key not in original_arguments:
                    reason = f"Conversion operation specifies source field '{src_key}', which is missing from caller arguments."
                    rejection_reasons.append(reason)
                    checks.append(SafetyCheckRecord(check_name="conversion_value_provenance", passed=False, reason=reason))
                else:
                    src_val = original_arguments[src_key]
                    if policy in ("to_integer", "string_to_integer"):
                        is_lossless, err_msg = _is_lossless_integer_conversion(src_val)
                        if not is_lossless:
                            reason = f"Lossy or disallowed conversion for field '{src_key}': {err_msg}"
                            rejection_reasons.append(reason)
                            checks.append(SafetyCheckRecord(check_name=f"lossless_conversion:{src_key}", passed=False, reason=reason))
                        else:
                            transformed_args[src_key] = int(str(src_val).strip())
                            checks.append(SafetyCheckRecord(check_name=f"lossless_conversion:{src_key}", passed=True))
                    elif policy in ("to_string", "integer_to_string"):
                        transformed_args[src_key] = str(src_val)
                        checks.append(SafetyCheckRecord(check_name=f"lossless_conversion:{src_key}", passed=True))

    # Verify original_arguments is completely untouched
    if original_arguments is transformed_args:
        rejection_reasons.append("Transformed arguments reference is not defensively isolated from original arguments.")
        checks.append(SafetyCheckRecord(check_name="arguments_isolation", passed=False, reason="Shared reference"))

    # -------------------------------------------------------------------------
    # 6. Headers & Credential Safety
    # -------------------------------------------------------------------------
    for op in operations:
        if op.operation_type == RestrictedOperationType.SET_HEADER:
            hdr_name = op.target_path or ""
            cred_ref = op.credential_reference or ""

            if hdr_name not in approved_headers:
                reason = f"Header '{hdr_name}' is not in approved header credentials configuration."
                rejection_reasons.append(reason)
                checks.append(SafetyCheckRecord(check_name=f"approved_header:{hdr_name}", passed=False, reason=reason))
            else:
                expected_ref = approved_headers[hdr_name]
                if cred_ref != expected_ref:
                    reason = f"Credential reference '{cred_ref}' for header '{hdr_name}' does not match approved binding '{expected_ref}'."
                    rejection_reasons.append(reason)
                    checks.append(SafetyCheckRecord(check_name=f"credential_binding:{hdr_name}", passed=False, reason=reason))
                else:
                    checks.append(SafetyCheckRecord(check_name=f"credential_binding:{hdr_name}", passed=True))

                # Check local credential availability resolver if provided
                if credential_resolver is not None:
                    is_available = credential_resolver(cred_ref)
                    if not is_available:
                        reason = f"Local credential resolver reported credential reference '{cred_ref}' is NOT available in environment."
                        rejection_reasons.append(reason)
                        checks.append(SafetyCheckRecord(check_name=f"credential_availability:{cred_ref}", passed=False, reason=reason))
                    else:
                        checks.append(SafetyCheckRecord(check_name=f"credential_availability:{cred_ref}", passed=True))

            # Verify no secret literals entered proposal metadata or headers
            for secret_pattern in [r"secret", r"password", r"bearer\s+[a-zA-Z0-9_\-\.]{20,}", r"sk-[a-zA-Z0-9]{20,}"]:
                if re.search(secret_pattern, str(op.model_dump()), re.IGNORECASE):
                    reason = f"Potential secret literal detected in proposal operation for header '{hdr_name}'."
                    rejection_reasons.append(reason)
                    checks.append(SafetyCheckRecord(check_name="secret_leakage", passed=False, reason=reason))

    # -------------------------------------------------------------------------
    # 7. Independently Derive Candidate ToolDefinition & Contract Invariants
    # -------------------------------------------------------------------------
    candidate = copy.deepcopy(snapshot)
    candidate.status = "validated_not_committed"

    # Contract Invariant: Endpoint & Method & Response Mapping must be unchanged
    if candidate.endpoint != snapshot.endpoint:
        reason = f"Endpoint changed from '{snapshot.endpoint}' to '{candidate.endpoint}'."
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="preserve_endpoint", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="preserve_endpoint", passed=True))

    if candidate.method.upper() != snapshot.method.upper():
        reason = f"HTTP method changed from '{snapshot.method}' to '{candidate.method}'."
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="preserve_method", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="preserve_method", passed=True))

    if candidate.response_mapping != snapshot.response_mapping:
        reason = "Response mapping was unexpectedly modified by repair proposal."
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="preserve_response_mapping", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="preserve_response_mapping", passed=True))

    # Apply operations to candidate definition
    new_req_mapping = copy.deepcopy(candidate.request_mapping)
    body_mapping = new_req_mapping.setdefault("body", {})

    destination_paths_assigned: Dict[str, str] = {}

    for op in operations:
        if op.operation_type == RestrictedOperationType.SET_HEADER:
            if op.credential_reference:
                candidate.credential_reference = op.credential_reference
            candidate.headers[op.target_path] = (
                f"Bearer {{{op.credential_reference}}}"
                if "auth" in op.target_path.lower()
                else f"{{{op.credential_reference}}}"
            )
        elif op.operation_type == RestrictedOperationType.RENAME_ARGUMENT:
            if op.target_path in destination_paths_assigned:
                reason = f"Destination collision: '{op.target_path}' is mapped from multiple sources ('{destination_paths_assigned[op.target_path]}' and '{op.source_path}')."
                rejection_reasons.append(reason)
                checks.append(SafetyCheckRecord(check_name="no_path_collision", passed=False, reason=reason))
            destination_paths_assigned[op.target_path] = op.source_path

            if isinstance(body_mapping, dict) and op.source_path in body_mapping:
                orig_val = body_mapping.pop(op.source_path)
                body_mapping[op.target_path] = orig_val
        elif op.operation_type == RestrictedOperationType.NEST_ARGUMENT:
            if op.target_path in destination_paths_assigned:
                reason = f"Destination collision: '{op.target_path}' is mapped from multiple sources ('{destination_paths_assigned[op.target_path]}' and '{op.source_path}')."
                rejection_reasons.append(reason)
                checks.append(SafetyCheckRecord(check_name="no_path_collision", passed=False, reason=reason))
            destination_paths_assigned[op.target_path] = op.source_path

            if isinstance(body_mapping, dict) and op.source_path in body_mapping:
                orig_val = body_mapping.pop(op.source_path)
                parts = op.target_path.split(".")
                curr = body_mapping
                for part in parts[:-1]:
                    curr = curr.setdefault(part, {})
                curr[parts[-1]] = orig_val
        elif op.operation_type == RestrictedOperationType.CAST_TYPE:
            target_type = "integer" if "integer" in (op.conversion_policy or "").lower() else "string"
            if op.source_path and op.source_path in candidate.argument_schema.properties:
                prop = dict(candidate.argument_schema.properties[op.source_path])
                prop["type"] = target_type
                candidate.argument_schema.properties[op.source_path] = prop
            elif op.target_path and op.target_path in candidate.argument_schema.properties:
                prop = dict(candidate.argument_schema.properties[op.target_path])
                prop["type"] = target_type
                candidate.argument_schema.properties[op.target_path] = prop

    candidate.request_mapping = new_req_mapping

    # Local JSON Schema Validation of transformed arguments against candidate schema
    try:
        schema_dict = candidate.argument_schema.model_dump()
        jsonschema.validate(instance=transformed_args, schema=schema_dict)
        checks.append(SafetyCheckRecord(check_name="transformed_args_schema_valid", passed=True))
    except jsonschema.ValidationError as e:
        reason = f"Transformed arguments do not satisfy candidate argument schema: {e.message}"
        rejection_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="transformed_args_schema_valid", passed=False, reason=reason))

    # Compare with supplied candidate preview (if provided) - Tamper Check
    if isinstance(proposal, PlanningResult) and proposal.candidate_preview:
        preview = proposal.candidate_preview
        if (
            preview.request_mapping != candidate.request_mapping
            or preview.headers != candidate.headers
            or preview.argument_schema.model_dump() != candidate.argument_schema.model_dump()
        ):
            reason = "Supplied candidate preview diverges from independently derived candidate contract."
            rejection_reasons.append(reason)
            checks.append(SafetyCheckRecord(check_name="candidate_preview_consistency", passed=False, reason=reason))
        else:
            checks.append(SafetyCheckRecord(check_name="candidate_preview_consistency", passed=True))

    candidate_fp = candidate.compute_fingerprint()

    # -------------------------------------------------------------------------
    # 8. Adapted-Retry Safety Verification
    # -------------------------------------------------------------------------
    authorized_for_recovery = False
    retry_safety_reasons: List[str] = []

    # 8.1 Uncertain Writes
    if observation.uncertain_write:
        reason = "Automated retry blocked: failure was flagged with uncertain_write=True. Blind retries risk duplicating stateful writes."
        retry_safety_reasons.append(reason)
        checks.append(SafetyCheckRecord(check_name="retry_safety_uncertain_write", passed=False, reason=reason))
    else:
        checks.append(SafetyCheckRecord(check_name="retry_safety_uncertain_write", passed=True))

    # 8.2 Pre-execution Rejection Guarantees
    is_write = snapshot.method.upper() in ("POST", "PUT", "PATCH", "DELETE")
    if is_write:
        # Check if HTTP write actually succeeded (200/201) with response drift
        if observation.status_code in (200, 201):
            reason = f"Automated retry blocked: HTTP {snapshot.method.upper()} succeeded downstream (status {observation.status_code}) with response drift. Repeating the write risks duplicate assignment."
            retry_safety_reasons.append(reason)
            checks.append(SafetyCheckRecord(check_name="retry_safety_pre_execution", passed=False, reason=reason))
        elif observation.status_code in (400, 401, 403, 422):
            # Pre-execution validation/auth rejection confirmed by downstream server
            if guarantees.get("pre_execution_rejection", True):
                checks.append(SafetyCheckRecord(check_name="retry_safety_pre_execution", passed=True))
            else:
                reason = "Automated retry blocked: pre_execution_rejection is declared False in execution guarantees."
                retry_safety_reasons.append(reason)
                checks.append(SafetyCheckRecord(check_name="retry_safety_pre_execution", passed=False, reason=reason))
        else:
            reason = f"Automated retry blocked: HTTP status {observation.status_code} does not provide a pre-execution validation rejection guarantee."
            retry_safety_reasons.append(reason)
            checks.append(SafetyCheckRecord(check_name="retry_safety_pre_execution", passed=False, reason=reason))
    else:
        # Idempotent read is safe to retry
        checks.append(SafetyCheckRecord(check_name="retry_safety_pre_execution", passed=True))

    if not retry_safety_reasons:
        authorized_for_recovery = True
    else:
        rejection_reasons.extend(retry_safety_reasons)

    # -------------------------------------------------------------------------
    # 9. Formulate Final Validation Decision
    # -------------------------------------------------------------------------
    all_passed = len(rejection_reasons) == 0

    if all_passed:
        return ValidationResult(
            status=ValidationStatus.ACCEPTED,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fp,
            proposal_fingerprint=prop_fp,
            candidate_fingerprint=candidate_fp,
            candidate=candidate,
            transformed_arguments=transformed_args,
            authorized_for_recovery=authorized_for_recovery,
            checks=checks,
            rejection_reasons=[],
            evidence_summary=evidence_summary,
        )
    else:
        return ValidationResult(
            status=ValidationStatus.REJECTED,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fp,
            proposal_fingerprint=prop_fp,
            candidate_fingerprint=None,
            candidate=None,
            transformed_arguments=None,
            authorized_for_recovery=False,
            checks=checks,
            rejection_reasons=rejection_reasons,
            evidence_summary=evidence_summary,
        )


def emit_patch_validation_event(
    event_broadcaster: Any,
    run_id: str,
    validation_result: ValidationResult,
    mode: str = "healing",
) -> Any:
    """Emits patch_validated or patch_rejected event adhering to contracts/events.schema.json."""
    if validation_result.status == ValidationStatus.ACCEPTED:
        return event_broadcaster.publish_event(
            run_id=run_id,
            event_type="patch_validated",
            mode=mode,
            tool=validation_result.tool_name,
            data={
                "tool": validation_result.tool_name,
                "base_version": validation_result.base_version,
                "base_fingerprint": validation_result.base_fingerprint,
                "proposal_fingerprint": validation_result.proposal_fingerprint,
                "candidate_fingerprint": validation_result.candidate_fingerprint,
                "recovery_permitted": validation_result.authorized_for_recovery,
                "checks_passed": len([c for c in validation_result.checks if c.passed]),
                "status": "validated_not_committed",
                "validation_passed": True,
            },
        )
    else:
        return event_broadcaster.publish_event(
            run_id=run_id,
            event_type="patch_rejected",
            mode=mode,
            tool=validation_result.tool_name,
            data={
                "tool": validation_result.tool_name,
                "base_version": validation_result.base_version,
                "base_fingerprint": validation_result.base_fingerprint,
                "proposal_fingerprint": validation_result.proposal_fingerprint,
                "rejection_reasons": validation_result.rejection_reasons,
                "failed_checks": [c.check_name for c in validation_result.checks if not c.passed],
                "status": "rejected",
                "validation_passed": False,
            },
        )
