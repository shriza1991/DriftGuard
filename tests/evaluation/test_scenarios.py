import json
from pathlib import Path
import jsonschema
import pytest

from evaluation.scenarios import SCENARIO_CATALOG
from evaluation.reporting import EvaluationReporter


def test_scenario_catalog_integrity():
    assert len(SCENARIO_CATALOG) == 8
    scenario_ids = [s.id for s in SCENARIO_CATALOG]
    expected_ids = [
        "rename",
        "type_change",
        "nesting",
        "required_header",
        "combined",
        "missing_business_value",
        "unavailable_credential",
        "ambiguous_mapping"
    ]
    assert scenario_ids == expected_ids

    # Check that safety scenarios are classified as safe_refusal
    safety_negatives = [s for s in SCENARIO_CATALOG if s.category == "safety_negative"]
    assert len(safety_negatives) == 3
    for s in safety_negatives:
        assert s.expected_outcome == "safe_refusal"


def test_events_schema_validation():
    schema_path = Path("contracts/events.schema.json")
    assert schema_path.exists()
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    example_path = Path("contracts/examples/run_events_normal.json")
    assert example_path.exists()
    with open(example_path, "r", encoding="utf-8") as f:
        events = json.load(f)

    assert isinstance(events, list)
    assert len(events) >= 5

    for event in events:
        jsonschema.validate(instance=event, schema=schema)


def test_evaluation_reporter_no_fabricated_data():
    summary = EvaluationReporter.generate_summary([])
    assert summary["status"] == "no_data"
    assert summary["token_usage"] == "unavailable"
