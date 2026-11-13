"""Shared test setup."""
import pytest

from dayflow import faults, tools


@pytest.fixture(autouse=True)
def _no_real_sleeping_and_no_leftover_faults(monkeypatch):
    """Lesson 91: dispatch() backs off between retries. Tests must not actually wait,
    and a fault plan from one test must not leak into the next."""
    monkeypatch.setattr(tools, "_sleep", lambda s: None)
    monkeypatch.delenv("DAYFLOW_FAULTS", raising=False)
    faults.reset()
