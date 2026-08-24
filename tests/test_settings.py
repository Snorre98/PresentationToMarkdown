"""Tests for the persistent settings/recent-files store (converter.settings)."""
import pytest

from converter import logstore, settings


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(logstore, "VISION_LOG_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.setattr(logstore, "_conn", None)
    yield tmp_path / "test.sqlite"


def test_get_setting_default(isolated_db):
    assert settings.get_setting("missing") is None
    assert settings.get_setting("missing", "dflt") == "dflt"


def test_set_and_get_setting(isolated_db):
    settings.set_setting("foo", "bar")
    assert settings.get_setting("foo") == "bar"
    settings.set_setting("foo", "baz")
    assert settings.get_setting("foo") == "baz"


def test_settings_share_meta_table_with_logstore(isolated_db):
    settings.set_setting("k", "v")
    conn = logstore._connection()
    row = conn.execute("SELECT value FROM meta WHERE key = 'k'").fetchone()
    assert row == ("v",)


def test_recent_files_empty(isolated_db):
    assert settings.recent_files() == []


def test_recent_files_re_record_moves_to_front(isolated_db):
    settings.record_recent("a.pptx")
    settings.record_recent("b.pdf")
    settings.record_recent("a.pptx")
    assert settings.recent_files() == ["a.pptx", "b.pdf"]


def test_recent_files_limit(isolated_db):
    for i in range(15):
        settings.record_recent(f"file_{i}.pptx")
    got = settings.recent_files(limit=5)
    assert len(got) == 5
    assert got[0] == "file_14.pptx"
