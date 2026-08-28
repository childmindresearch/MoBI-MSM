import os
import tempfile
from pathlib import Path

import pytest

# Point the app at a throwaway data dir and the fake LLM before app import.
_tmp = tempfile.mkdtemp(prefix="mobi-test-")
os.environ["MOBI_DATA_DIR"] = _tmp
os.environ["MOBI_LLM_PROVIDER"] = "fake"
# Real env vars win over .env, so this keeps the suite fast regardless of any
# rehearsal delay left configured for development.
os.environ["MOBI_FAKE_LLM_DELAY_S"] = "0"
# The pinned webfont is committed, so the suite runs against the enforced path
# rather than the development escape hatch.
os.environ.pop("MOBI_ALLOW_SYSTEM_FONT", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A fresh app with an isolated database and marker log per test."""
    from app import config, markers, store

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(config, "MARKER_LOG_DIR", tmp_path / "markers")
    monkeypatch.setattr(store, "_store", store.Store(tmp_path / "sessions.db"))
    monkeypatch.setattr(markers, "_stream", markers.MarkerStream(tmp_path / "markers"))
    main.sessions.clear()
    with TestClient(main.app) as c:
        yield c
