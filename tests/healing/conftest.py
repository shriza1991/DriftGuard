"""Pytest configuration and fixtures for orchestrator healing tests."""
import pytest
from orchestrator.execution.gate import admission_gate
from orchestrator.execution.tasks import app_task_manager
from orchestrator.execution.events import event_broadcaster


@pytest.fixture(autouse=True)
def reset_orchestrator_state():
    """Autouse fixture to reset gate, task manager, and event broadcaster state around each test."""
    admission_gate.reset_state()
    app_task_manager.reset()
    yield
    admission_gate.reset_state()
    app_task_manager.reset()
