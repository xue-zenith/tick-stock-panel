"""Test isolation for the backend suite.

The application deliberately persists data under ``DATA_DIR`` in production.
Tests must never inherit that directory from a developer shell, otherwise a
local provider preference or cache can change the test path and may be mutated.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="tick-stock-panel-pytest-"))

os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)

# Import after selecting the test directory so modules that capture the global
# settings object during collection already point at the isolated location.
from app.config import settings  # noqa: E402

settings.data_dir = _TEST_DATA_DIR


@pytest.fixture(autouse=True)
def isolate_runtime_data(monkeypatch: pytest.MonkeyPatch):
    """Prevent shell ``DATA_DIR`` from leaking into individual test cases.

    The environment variable is removed for each test so tests that construct
    ``Settings(_env_file=...)`` can exercise dotenv precedence as intended.
    The already-loaded application settings remain pinned to the temp folder.
    """
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr(settings, "data_dir", _TEST_DATA_DIR)
    yield
