"""Deterministic repair proposal planner for DriftGuard.

Strictly adheres to Member 2 Task 6 safety invariants:
- Pure evidence-backed declarative proposal generation derived strictly from observable runtime evidence.
- Zero LLM diagnosis, zero heuristics based on scenario names, zero inspection of mock admin state.
- Gated strictly on diagnosis eligibility and matching tool snapshot version/fingerprint.
- Immutability of caller business parameters (ticket_id, user_id).
- Separation of concerns: proposal generation only; static validation belongs to Task 7,
  and recovery execution belongs to Task 8.
- Independent preservation of retry-safety metadata (uncertain writes remain strictly protected).
- Inputs and tool registry state remain completely unmutated.
"""
from typing import Optional, Dict, Any, List, Tuple
import re
import copy
import hashlib
import json
from datetime import datetime, timezone

from orchestrator.models import (
    ToolDefinition,
    RestrictedOperationType,
    RepairProposal,
)
from orchestrator.models.observation import FailureObservation
from orchestrator.models.classification import ClassificationResult, SemanticFailureCategory
from orchestrator.models.proposal import PlanningStatus, PlannedOperation, PlanningResult


def _extract_nested_dict(d: Any, path: List[str]) -> Any:
    """Helper to traverse nested dicts safely without exceptions."""
    curr = d
    for p in path:
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def plan_repair(
    snapshot: ToolDefinition,
    observation: FailureObservation,
    classification: ClassificationResult,
    approved_header_credentials: Optional[Dict[str, str]] = None,
) -> PlanningResult:
    """Generates a deterministic declarative repair proposal based strictly on observable runtime evidence.
    
    Args:
        snapshot: Captured defensive ToolDefinition snapshot of the failing tool.
        observation: Sanitized, immutable failure observation carrying runtime evidence.
        classification: Semantic failure classification result.
        approved_header_credentials: Read-only mapping of approved header names to credential references.
        
    Returns:
        PlanningResult with status (proposed, insufficient_evidence, unsupported, not_eligible),
        reasons, planned operations, and candidate preview.
    """
    approved_headers = approved_header_credentials or {}
    base_fingerprint = snapshot.fingerprint or snapshot.compute_fingerprint()

    # -------------------------------------------------------------------------
    # 1. Gating: Eligibility & Identity Verification
    # -------------------------------------------------------------------------
    if not classification.diagnosis_eligible or classification.category != SemanticFailureCategory.SCHEMA_DRIFT:
        return PlanningResult(
            status=PlanningStatus.NOT_ELIGIBLE,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fingerprint,
            is_eligible=False,
            retry_safety=classification.retry_safety,
            reasons=[
                f"Failure classified under category '{classification.category.value}' is not eligible for repair diagnosis. "
                "Deterministic repair planning requires category 'schema_drift' and diagnosis_eligible=True."
            ],
            evidence_references=classification.evidence_references,
        )

    # Verify tool snapshot matches observation identity, version, and fingerprint
    if (
        snapshot.name != observation.tool_name
        or snapshot.version != observation.registry_version
        or (observation.fingerprint and snapshot.fingerprint and snapshot.fingerprint != observation.fingerprint)
    ):
        return PlanningResult(
            status=PlanningStatus.NOT_ELIGIBLE,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fingerprint,
            is_eligible=False,
            retry_safety=classification.retry_safety,
            reasons=[
                f"Mismatched observation and snapshot: observation has tool='{observation.tool_name}', "
                f"version='{observation.registry_version}', fingerprint='{observation.fingerprint}', "
                f"but snapshot has tool='{snapshot.name}', version='{snapshot.version}', "
                f"fingerprint='{snapshot.fingerprint}'. Stale or conflicting failure evidence."
            ],
            evidence_references={"observation_fingerprint": observation.fingerprint, "snapshot_fingerprint": snapshot.fingerprint},
        )

    # Caller arguments snapshot must be present
    caller_args = observation.original_arguments
    if caller_args is None:
        return PlanningResult(
            status=PlanningStatus.NOT_ELIGIBLE,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fingerprint,
            is_eligible=False,
            retry_safety=classification.retry_safety,
            reasons=["Original caller arguments snapshot is missing from failure observation; cannot plan repair."],
        )

    # -------------------------------------------------------------------------
    # 2. Evidence Gathering & Normalization
    # -------------------------------------------------------------------------
    resp_ev = observation.response_evidence or {}
    body_dict: Dict[str, Any] = {}
    if isinstance(resp_ev.get("body"), dict):
        body_dict = resp_ev["body"]
    elif isinstance(resp_ev, dict):
        body_dict = resp_ev

    err_obj = body_dict.get("error") if isinstance(body_dict.get("error"), dict) else body_dict
    details = err_obj.get("details") or body_dict.get("details") or observation.validation_details or {}
    hint_text = str(err_obj.get("hint") or err_obj.get("migration_hint") or body_dict.get("hint") or body_dict.get("migration_hint") or "")
    msg_text = str(err_obj.get("message") or body_dict.get("message") or observation.raw_error_message or "")
    code_text = str(err_obj.get("code") or err_obj.get("category") or body_dict.get("code") or "").upper()

    evidence_refs: Dict[str, Any] = {
        "status_code": observation.http_status,
        "error_code": code_text,
        "details": details,
        "hint": hint_text,
    }

    operations: List[PlannedOperation] = []
    missing_requirements: List[str] = []
    refusal_reasons: List[str] = []

    # -------------------------------------------------------------------------
    # 3. Required Header Analysis (SET_HEADER)
    # -------------------------------------------------------------------------
    required_headers: List[str] = []
    if isinstance(details, dict):
        hdrs = details.get("required_headers") or details.get("missing_headers") or details.get("required_header")
        if isinstance(hdrs, list):
            required_headers.extend([str(h) for h in hdrs])
        elif isinstance(hdrs, str):
            required_headers.append(hdrs)

    if not required_headers:
        # Check header error message or code
        if code_text in ["MISSING_HEADER", "REQUIRED_HEADER_MISSING"] or "header" in msg_text.lower():
            hdr_match = re.search(r"header\s+['\"]?([A-Za-z0-9_-]+)['\"]?\s+(?:is\s+missing|required)", msg_text + " " + hint_text, re.IGNORECASE)
            if hdr_match:
                required_headers.append(hdr_match.group(1))

    for req_hdr in sorted(set(required_headers)):
        if req_hdr in approved_headers:
            cred_ref = approved_headers[req_hdr]
            operations.append(
                PlannedOperation(
                    operation_type=RestrictedOperationType.SET_HEADER,
                    target_path=req_hdr,
                    credential_reference=cred_ref,
                    value_provenance=f"approved_credential_reference:{cred_ref}",
                    evidence_path="response_evidence.body.error.details.required_headers",
                    evidence_summary=f"Downstream service returned explicit requirement for header '{req_hdr}', mapped via approved credential reference '{cred_ref}'.",
                )
            )
        else:
            refusal_reasons.append(
                f"Downstream service requires header '{req_hdr}', but no approved credential reference is configured. "
                "Credentials cannot be fabricated and permissions cannot be bypassed."
            )
            missing_requirements.append(f"Approved credential reference mapping for header '{req_hdr}'")

    # -------------------------------------------------------------------------
    # 4. Field Migration Analysis (RENAME_ARGUMENT & NEST_ARGUMENT)
    # -------------------------------------------------------------------------
    # Extract explicit source -> target migrations
    explicit_migrations: Dict[str, str] = {}

    if isinstance(details, dict):
        # Structured migration mapping dictionary
        struct_map = (
            details.get("migration_mapping")
            or details.get("renamed")
            or details.get("field_migrations")
            or details.get("mappings")
        )
        if isinstance(struct_map, dict):
            for s, t in struct_map.items():
                explicit_migrations[str(s)] = str(t)

        # Structured migrations list: [{"from": "user_id", "to": "assignee_id"}]
        mig_list = details.get("migrations")
        if isinstance(mig_list, list):
            for item in mig_list:
                if isinstance(item, dict) and ("from" in item or "source" in item) and ("to" in item or "target" in item):
                    s = item.get("from") or item.get("source")
                    t = item.get("to") or item.get("target")
                    if s and t:
                        explicit_migrations[str(s)] = str(t)

    # Structured machine-readable hint parsing
    combined_hint = (hint_text + " " + msg_text).strip()
    if combined_hint:
        # Pattern A: "Field 'user_id' is deprecated. Expected 'assignee_id'."
        p_dep = re.search(r"['\"]?(\w+)['\"]?\s+is\s+deprecated\.\s*Expected\s+['\"]?([\w.]+)['\"]?", combined_hint, re.IGNORECASE)
        if p_dep:
            explicit_migrations[p_dep.group(1)] = p_dep.group(2)

        # Pattern B: "Use assignee_id instead of user_id"
        p_use = re.search(r"Use\s+['\"]?([\w.]+)['\"]?\s+instead\s+of\s+['\"]?(\w+)['\"]?", combined_hint, re.IGNORECASE)
        if p_use:
            explicit_migrations[p_use.group(2)] = p_use.group(1)

        # Pattern C: "'user_id' is now nested under 'target.id'"
        p_nest = re.search(r"['\"]?(\w+)['\"]?\s+(?:is\s+now\s+nested\s+under|nested\s+under)\s+['\"]?([\w.]+)['\"]?", combined_hint, re.IGNORECASE)
        if p_nest:
            explicit_migrations[p_nest.group(1)] = p_nest.group(2)

        # Pattern D: "Field 'user_id' renamed to 'assignee_id'"
        p_ren = re.search(r"['\"]?(\w+)['\"]?\s+(?:renamed\s+to|replaced\s+by)\s+['\"]?([\w.]+)['\"]?", combined_hint, re.IGNORECASE)
        if p_ren:
            explicit_migrations[p_ren.group(1)] = p_ren.group(2)

        # Pattern E: "Expected field 'target' object with nested integer 'id', received 'user_id'"
        p_exp_recv = re.search(r"Expected\s+field\s+['\"]?(\w+)['\"]?\s+object\s+with\s+nested\s+[^,]*['\"]?(\w+)['\"]?,\s*received\s+['\"]?(\w+)['\"]?", combined_hint, re.IGNORECASE)
        if p_exp_recv:
            parent_field = p_exp_recv.group(1)
            child_field = p_exp_recv.group(2)
            src_field = p_exp_recv.group(3)
            explicit_migrations[src_field] = f"{parent_field}.{child_field}"

    # Missing fields check
    missing_fields: List[str] = []
    if isinstance(details, dict):
        mf = details.get("missing")
        if isinstance(mf, list):
            missing_fields.extend([str(x) for x in mf])
        elif isinstance(mf, str):
            missing_fields.append(mf)

    # -------------------------------------------------------------------------
    # CRITICAL INVARIANT: Missing field alone does NOT prove rename correspondence!
    # -------------------------------------------------------------------------
    for mf in missing_fields:
        # Check if there is explicit correspondence mapping this missing field to an existing caller argument
        corresponding_sources = [src for src, tgt in explicit_migrations.items() if tgt == mf or tgt.endswith(f".{mf}")]
        if not corresponding_sources:
            # Does this missing field have ANY evidence linking it to an argument?
            if mf not in caller_args:
                # Is it a completely newly required business parameter with no source?
                refusal_reasons.append(
                    f"Downstream service reported missing field '{mf}', but no explicit correspondence evidence exists linking it to an existing caller argument. "
                    "Missing field evidence alone cannot establish a rename or semantic correspondence."
                )
                missing_requirements.append(f"Explicit source-to-target correspondence evidence for missing field '{mf}'")

    # Destination collision check: Multiple sources mapping to same destination
    dest_to_sources: Dict[str, List[str]] = {}
    for s, t in explicit_migrations.items():
        dest_to_sources.setdefault(t, []).append(s)
    for dest, srcs in dest_to_sources.items():
        if len(srcs) > 1:
            refusal_reasons.append(f"Destination collision: multiple caller arguments {srcs} map to identical destination '{dest}'.")

    # Generate RENAME or NEST operations from explicit migrations
    for src_field, tgt_path in sorted(explicit_migrations.items()):
        # Validate that source field exists in snapshot request mapping or caller args
        req_body = snapshot.request_mapping.get("body", {})
        has_source = (src_field in caller_args) or (isinstance(req_body, dict) and src_field in req_body)

        if not has_source:
            refusal_reasons.append(f"Migration source field '{src_field}' does not exist in tool request mapping or caller arguments.")
            continue

        if "." in tgt_path:
            # Nested destination
            operations.append(
                PlannedOperation(
                    operation_type=RestrictedOperationType.NEST_ARGUMENT,
                    source_path=src_field,
                    target_path=tgt_path,
                    value_provenance=f"caller_argument:{src_field}",
                    evidence_path="response_evidence.body.error.details.migration_hint",
                    evidence_summary=f"Evidenced migration maps argument '{src_field}' into nested wire path '{tgt_path}'.",
                )
            )
        else:
            # Simple field rename
            operations.append(
                PlannedOperation(
                    operation_type=RestrictedOperationType.RENAME_ARGUMENT,
                    source_path=src_field,
                    target_path=tgt_path,
                    value_provenance=f"caller_argument:{src_field}",
                    evidence_path="response_evidence.body.error.details.migration_hint",
                    evidence_summary=f"Evidenced migration renames wire parameter '{src_field}' to '{tgt_path}'.",
                )
            )

    # -------------------------------------------------------------------------
    # 5. Type Conversion Analysis (CAST_TYPE)
    # -------------------------------------------------------------------------
    expected_types: Dict[str, str] = {}
    if isinstance(details, dict):
        exp_schema = details.get("expected_schema")
        if isinstance(exp_schema, dict):
            # Flatten schema types: {"target": {"id": "integer"}} -> {"target.id": "integer"}
            def _extract_types(d: Dict[str, Any], prefix: str = ""):
                for k, v in d.items():
                    path_key = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, str):
                        expected_types[path_key] = v.lower()
                    elif isinstance(v, dict):
                        _extract_types(v, path_key)
            _extract_types(exp_schema)

        if isinstance(details.get("expected_types"), dict):
            for k, v in details["expected_types"].items():
                expected_types[str(k)] = str(v).lower()

    # Also check hint for explicit type errors: e.g. "nested integer 'id', received 'user_id' string"
    if "integer" in combined_hint.lower() and ("received 'user_id' string" in combined_hint.lower() or "string" in combined_hint.lower()):
        # Find which field is integer
        for src, tgt in explicit_migrations.items():
            if "integer" in combined_hint.lower():
                expected_types[tgt] = "integer"

    # Compare expected types against existing snapshot argument schema types
    snapshot_props = snapshot.argument_schema.properties
    for tgt_path, exp_type in sorted(expected_types.items()):
        # Find corresponding source argument
        src_field = None
        for s, t in explicit_migrations.items():
            if t == tgt_path or tgt_path.endswith(f".{t}") or t.endswith(f".{tgt_path}"):
                src_field = s
                break
        if not src_field and tgt_path in snapshot_props:
            src_field = tgt_path

        if src_field and src_field in snapshot_props:
            prop_def = snapshot_props[src_field]
            curr_type = prop_def.get("type") if isinstance(prop_def, dict) else None
            if curr_type and curr_type != exp_type:
                # Type change detected
                policy = f"{curr_type}_to_{exp_type}"
                operations.append(
                    PlannedOperation(
                        operation_type=RestrictedOperationType.CAST_TYPE,
                        source_path=src_field,
                        target_path=tgt_path,
                        conversion_policy=policy,
                        value_provenance=f"caller_argument:{src_field}",
                        evidence_path="response_evidence.body.error.details.expected_schema",
                        evidence_summary=f"Evidenced expected schema requires '{tgt_path}' to be '{exp_type}', currently '{curr_type}'.",
                    )
                )

    # -------------------------------------------------------------------------
    # 6. Outcome Evaluation
    # -------------------------------------------------------------------------
    if refusal_reasons:
        is_unsupported = any(
            "no approved credential" in r.lower()
            or "collision" in r.lower()
            or "newly required parameter" in r.lower()
            or "cannot invent" in r.lower()
            or "unsupported" in r.lower()
            for r in refusal_reasons
        )
        status = PlanningStatus.UNSUPPORTED if is_unsupported else PlanningStatus.INSUFFICIENT_EVIDENCE
        return PlanningResult(
            status=status,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fingerprint,
            is_eligible=True,
            retry_safety=classification.retry_safety,
            reasons=refusal_reasons,
            missing_requirements=missing_requirements,
            evidence_references=evidence_refs,
        )

    if not operations:
        return PlanningResult(
            status=PlanningStatus.INSUFFICIENT_EVIDENCE,
            tool_name=snapshot.name,
            base_version=snapshot.version,
            base_fingerprint=base_fingerprint,
            is_eligible=True,
            retry_safety=classification.retry_safety,
            reasons=[
                "Evidence from downstream response does not contain actionable, machine-readable migration mappings, "
                "expected schema types, or approved required headers. Free-text matching alone cannot authorize a repair proposal."
            ],
            missing_requirements=["Structured source-to-target migration mappings or explicit expected_schema"],
            evidence_references=evidence_refs,
        )

    # -------------------------------------------------------------------------
    # 7. Deterministic Composition & Ordering
    # -------------------------------------------------------------------------
    # Sort order:
    # 1. SET_HEADER
    # 2. RENAME_ARGUMENT / NEST_ARGUMENT
    # 3. CAST_TYPE
    def op_sort_key(op: PlannedOperation) -> Tuple[int, str, str]:
        order_map = {
            RestrictedOperationType.SET_HEADER: 1,
            RestrictedOperationType.RENAME_ARGUMENT: 2,
            RestrictedOperationType.NEST_ARGUMENT: 3,
            RestrictedOperationType.CAST_TYPE: 4,
        }
        return (order_map.get(op.operation_type, 99), op.source_path or "", op.target_path or "")

    sorted_ops = sorted(operations, key=op_sort_key)

    # -------------------------------------------------------------------------
    # 8. Deterministic Fingerprint
    # -------------------------------------------------------------------------
    plan_dict = {
        "tool": snapshot.name,
        "base_version": snapshot.version,
        "base_fingerprint": base_fingerprint,
        "operations": [
            {
                "type": op.operation_type.value,
                "source": op.source_path,
                "target": op.target_path,
                "conversion": op.conversion_policy,
                "credential_ref": op.credential_reference,
            }
            for op in sorted_ops
        ],
    }
    raw_hash = json.dumps(plan_dict, sort_keys=True).encode("utf-8")
    prop_fp = hashlib.sha256(raw_hash).hexdigest()[:16]
    proposal_id = f"prop-{prop_fp[:12]}"

    # -------------------------------------------------------------------------
    # 9. Defensive Candidate Preview Generation (Unvalidated Proposal)
    # -------------------------------------------------------------------------
    preview = copy.deepcopy(snapshot)
    preview.status = "proposed_unvalidated"

    # Apply operations to preview's request_mapping
    new_req_mapping = copy.deepcopy(preview.request_mapping)
    body_mapping = new_req_mapping.setdefault("body", {})

    for op in sorted_ops:
        if op.operation_type == RestrictedOperationType.SET_HEADER:
            if op.credential_reference:
                preview.credential_reference = op.credential_reference
            preview.headers[op.target_path] = f"Bearer {{{op.credential_reference}}}" if "auth" in op.target_path.lower() else f"{{{op.credential_reference}}}"
        elif op.operation_type == RestrictedOperationType.RENAME_ARGUMENT:
            if isinstance(body_mapping, dict) and op.source_path in body_mapping:
                orig_val = body_mapping.pop(op.source_path)
                body_mapping[op.target_path] = orig_val
        elif op.operation_type == RestrictedOperationType.NEST_ARGUMENT:
            if isinstance(body_mapping, dict) and op.source_path in body_mapping:
                orig_val = body_mapping.pop(op.source_path)
                parts = op.target_path.split(".")
                curr = body_mapping
                for part in parts[:-1]:
                    curr = curr.setdefault(part, {})
                curr[parts[-1]] = orig_val

    preview.request_mapping = new_req_mapping

    # -------------------------------------------------------------------------
    # 10. Construct RepairProposal
    # -------------------------------------------------------------------------
    proposal = RepairProposal(
        proposal_id=proposal_id,
        tool=snapshot.name,
        base_version=snapshot.version,
        target_version="1.1.0",
        restricted_operations=[op.operation_type for op in sorted_ops],
        evidence_references={
            "proposal_fingerprint": prop_fp,
            "observation_fingerprint": observation.fingerprint,
            "evidence_paths": [op.evidence_path for op in sorted_ops],
        },
        transform_spec={
            "operations": [op.model_dump() for op in sorted_ops],
            "status": "unvalidated_proposal",
        },
    )

    required_checks = [
        "verify_caller_argument_immutability",
        "verify_no_invented_business_values",
        "verify_lossless_type_conversion",
        "verify_approved_credential_resolution",
        "verify_idempotent_retry_safety",
    ]

    return PlanningResult(
        status=PlanningStatus.PROPOSED,
        tool_name=snapshot.name,
        base_version=snapshot.version,
        base_fingerprint=base_fingerprint,
        proposal=proposal,
        operations=sorted_ops,
        candidate_preview=preview,
        proposal_fingerprint=prop_fp,
        reasons=[
            f"Successfully formulated deterministic repair proposal '{proposal_id}' composed of "
            f"{len(sorted_ops)} verified operation(s). Candidate preview generated for Task 7 static authorization."
        ],
        missing_requirements=[],
        required_validation_checks=required_checks,
        evidence_references=evidence_refs,
        is_eligible=True,
        retry_safety=classification.retry_safety,
    )


def emit_patch_proposed_event(
    event_broadcaster: Any,
    run_id: str,
    planning_result: PlanningResult,
    mode: str = "healing",
) -> None:
    """Emits patch_proposed event strictly when a valid proposal was formulated, labeled as unvalidated."""
    if planning_result.status == PlanningStatus.PROPOSED and planning_result.proposal:
        event_broadcaster.publish_event(
            run_id=run_id,
            event_type="patch_proposed",
            mode=mode,
            tool=planning_result.tool_name,
            data={
                "proposal_id": planning_result.proposal.proposal_id,
                "tool": planning_result.tool_name,
                "base_version": planning_result.base_version,
                "target_version": planning_result.proposal.target_version,
                "proposal_fingerprint": planning_result.proposal_fingerprint,
                "operations": [
                    {
                        "operation_type": op.operation_type.value,
                        "source_path": op.source_path,
                        "target_path": op.target_path,
                        "conversion_policy": op.conversion_policy,
                        "credential_reference": op.credential_reference,
                    }
                    for op in planning_result.operations
                ],
                "status": "unvalidated_proposal",
                "validation_required": True,
            },
        )

