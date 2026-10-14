"""Fixtures module containing valid example payloads for development, tests, and contracts verification.

CRITICAL:
These examples are static test fixtures only. They must NEVER be emitted as real runtime activity.
"""
from typing import List, Dict, Any
import json
from pathlib import Path


def load_fixture_json(filename: str) -> List[Dict[str, Any]]:
    path = Path(__file__).resolve().parent.parent.parent / "contracts" / "examples" / filename
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_normal_run_example() -> List[Dict[str, Any]]:
    return load_fixture_json("run_events_normal.json")


def get_tool_failure_example() -> List[Dict[str, Any]]:
    return load_fixture_json("run_events_tool_failure.json")


def get_patch_rejection_example() -> List[Dict[str, Any]]:
    return load_fixture_json("run_events_patch_rejection.json")


def get_terminal_status_example() -> List[Dict[str, Any]]:
    return load_fixture_json("run_events_terminal_status.json")
