"""Safe mapping and JSONPath extraction utilities for registry-driven execution.

Strictly adheres to Member 2 safety invariants:
- Zero executable code evaluation (no eval, exec, or arbitrary code).
- Strict type preservation for arguments (no implicit string coercion of integers/objects).
- Deterministic tokenized JSONPath extraction.
- Clear rejection of unsupported mapping operations.
"""
from typing import Any, Dict, List
import re


SUPPORTED_REQUEST_MAPPING_KEYS = {"path_params", "query_params", "headers", "body"}


class MappingError(Exception):
    """Raised when request or response mapping fails."""
    pass


class UnsupportedMappingError(MappingError):
    """Raised when a request mapping contains unsupported keys or operations."""
    pass


def validate_request_mapping(request_mapping: Dict[str, Any]) -> None:
    """Validates that request_mapping only uses supported keys."""
    if not isinstance(request_mapping, dict):
        raise UnsupportedMappingError("request_mapping must be a dictionary")
    
    unsupported = set(request_mapping.keys()) - SUPPORTED_REQUEST_MAPPING_KEYS
    if unsupported:
        raise UnsupportedMappingError(
            f"Unsupported request mapping keys: {sorted(unsupported)}. "
            f"Allowed keys are: {sorted(SUPPORTED_REQUEST_MAPPING_KEYS)}"
        )


def expand_mapping(template_obj: Any, arguments: Dict[str, Any]) -> Any:
    """Recursively expands template placeholders from arguments without eval.
    
    If template_obj is exactly '{field_name}', the native python type of arguments[field_name]
    is preserved (e.g. int stays int, boolean stays boolean, dict stays dict).
    If template_obj is a composite string like 'prefix_{field}_suffix', string substitution is performed.
    """
    if isinstance(template_obj, dict):
        return {k: expand_mapping(v, arguments) for k, v in template_obj.items()}
    elif isinstance(template_obj, list):
        return [expand_mapping(item, arguments) for item in template_obj]
    elif isinstance(template_obj, str):
        # Exact single placeholder: "{arg_name}"
        if template_obj.startswith("{") and template_obj.endswith("}") and template_obj.count("{") == 1 and template_obj.count("}") == 1:
            var_name = template_obj[1:-1]
            if var_name not in arguments:
                raise MappingError(f"Missing required argument '{var_name}' for template '{template_obj}'")
            return arguments[var_name]
        elif "{" in template_obj and "}" in template_obj:
            # Composite template string
            matches = re.findall(r"\{([a-zA-Z0-9_]+)\}", template_obj)
            res_str = template_obj
            for m in matches:
                if m not in arguments:
                    raise MappingError(f"Missing argument '{m}' for composite template '{template_obj}'")
                res_str = res_str.replace(f"{{{m}}}", str(arguments[m]))
            return res_str
        else:
            return template_obj
    else:
        return template_obj


def extract_json_path(path: str, data: Any, default: Any = None) -> Any:
    """Safely extracts a nested value from JSON data using dot/index path notation without eval.
    
    Supports:
    - Root: '$'
    - Object keys: '.key' or "['key']" or '["key"]'
    - Array indices: '[0]', '[-1]', etc.
    Examples:
    - '$[0].id' -> data[0]['id']
    - '$.ticket_id' -> data['ticket_id']
    - '$.target.id' -> data['target']['id']
    """
    if not isinstance(path, str) or not path.startswith("$"):
        raise MappingError(f"JSONPath expression must start with '$', got: '{path}'")
    
    rem = path[1:]
    curr = data
    
    token_pattern = re.compile(r"(\.([a-zA-Z0-9_-]+)|\[(-?\d+)\]|\[['\"]([^'\"]+)['\"]\])")
    
    pos = 0
    while pos < len(rem):
        m = token_pattern.match(rem, pos)
        if not m:
            raise MappingError(f"Invalid JSONPath token at index {pos+1} in expression '{path}'")
        
        dot_key = m.group(2)
        idx_str = m.group(3)
        bracket_key = m.group(4)
        
        if dot_key is not None:
            if not isinstance(curr, dict):
                raise MappingError(f"Expected dict for key '.{dot_key}' in '{path}', got {type(curr).__name__}")
            if dot_key not in curr:
                return default
            curr = curr[dot_key]
        elif idx_str is not None:
            idx = int(idx_str)
            if not isinstance(curr, (list, tuple)):
                raise MappingError(f"Expected list/tuple for index [{idx}] in '{path}', got {type(curr).__name__}")
            if idx >= len(curr) or idx < -len(curr):
                raise MappingError(f"Index [{idx}] out of range (length {len(curr)}) in '{path}'")
            curr = curr[idx]
        elif bracket_key is not None:
            if not isinstance(curr, dict):
                raise MappingError(f"Expected dict for key ['{bracket_key}'] in '{path}', got {type(curr).__name__}")
            if bracket_key not in curr:
                return default
            curr = curr[bracket_key]
        
        pos = m.end()
        
    return curr
