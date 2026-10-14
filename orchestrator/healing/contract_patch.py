"""Restricted contract transformation shared by preview and independent derivation.

Evidence authorization is deliberately separate in validator.py.
"""
import copy
import re
from orchestrator.models import RestrictedOperationType as Op


def derive_contract(snapshot, operations, arguments=None):
    candidate = copy.deepcopy(snapshot)
    args = copy.deepcopy(arguments) if arguments is not None else None
    body = candidate.request_mapping.setdefault("body", {})
    moves = {op.source_path: op.target_path for op in operations if op.operation_type in (Op.RENAME_ARGUMENT, Op.NEST_ARGUMENT)}
    for op in operations:
        if op.operation_type == Op.SET_HEADER:
            candidate.credential_reference = op.credential_reference
            candidate.headers[op.target_path] = ("Bearer " if op.target_path.lower() == "authorization" else "") + "{" + op.credential_reference + "}"
        elif op.operation_type in (Op.RENAME_ARGUMENT, Op.NEST_ARGUMENT):
            src, dst = op.source_path, op.target_path
            if not src or not dst or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", dst):
                raise ValueError("Unsupported repair path")
            if src not in body or src not in candidate.argument_schema.properties:
                raise ValueError("Missing migration source")
            if dst == src or dst.split('.')[0] in candidate.argument_schema.properties:
                raise ValueError("Destination collision with existing argument")
            schema_leaf = candidate.argument_schema.properties.pop(src)
            was_required = src in candidate.argument_schema.required
            if was_required:
                candidate.argument_schema.required.remove(src)
            props = candidate.argument_schema.properties
            required = candidate.argument_schema.required
            wire = body
            value = args.pop(src) if args is not None else None
            arg_parent = args
            body.pop(src)
            parts = dst.split('.')
            for part in parts[:-1]:
                if part in wire and not isinstance(wire[part], dict):
                    raise ValueError("Wire destination collision")
                node = props.setdefault(part, {"type": "object", "properties": {}, "required": []})
                if node.get("type") != "object":
                    raise ValueError("Schema destination collision")
                if was_required and part not in required:
                    required.append(part)
                props, required = node['properties'], node['required']
                wire = wire.setdefault(part, {})
                if arg_parent is not None:
                    arg_parent = arg_parent.setdefault(part, {})
            leaf = parts[-1]
            if leaf in wire or leaf in props:
                raise ValueError("Destination collision")
            props[leaf] = schema_leaf
            if was_required:
                required.append(leaf)
            wire[leaf] = '{' + dst + '}'
            if arg_parent is not None:
                arg_parent[leaf] = value
        elif op.operation_type == Op.CAST_TYPE:
            target = moves.get(op.source_path, op.source_path or op.target_path)
            props = candidate.argument_schema.properties
            parent = args
            parts = target.split('.')
            for part in parts[:-1]:
                props = props[part]['properties']
                if parent is not None:
                    parent = parent[part]
            leaf = parts[-1]
            policy = op.conversion_policy or ''
            if policy not in ('to_integer', 'string_to_integer', 'to_string', 'integer_to_string'):
                raise ValueError("Unsupported conversion")
            props[leaf]['type'] = 'integer' if policy in ('to_integer', 'string_to_integer') else 'string'
            if parent is not None:
                parent[leaf] = int(parent[leaf]) if props[leaf]['type'] == 'integer' else str(parent[leaf])
        else:
            raise ValueError("Unsupported repair operation")
    candidate.fingerprint = candidate.compute_fingerprint()
    return candidate, args
