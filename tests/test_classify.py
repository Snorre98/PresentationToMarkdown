"""Unit tests for the complex-page vision transcription gate (converter.classify)."""
import pytest

from converter import classify, config


@pytest.fixture(autouse=True)
def _clear_caches():
    config.reset()
    for name in (
        "_readability_cache",
        "_category_cache",
        "_transcribe_cache",
        "_meta_transcribe_cache",
        "_page_transcribe_cache",
    ):
        getattr(classify, name).clear()
    yield
    config.reset()


def _fake_png() -> bytes:
    return b"\x89PNG\r\n\x1a\nfake"


def _noop_record(**kw):
    return None


def test_transcribe_complex_page_disabled(monkeypatch):
    config.set_enabled("vision", False)
    assert classify.transcribe_complex_page(_fake_png()) is None


def test_transcribe_complex_page_text_without_classifier(monkeypatch):
    config.set_enabled("vision", True)
    config.set_enabled("classify", False)
    monkeypatch.setattr(classify, "image_readable", lambda *a, **kw: None)
    monkeypatch.setattr(classify, "record", _noop_record)
    monkeypatch.setattr(
        classify,
        "transcribe_page_cached",
        lambda png_bytes, **kw: ("# Page Title\n\n- bullet one\n- bullet two", None),
    )
    out = classify.transcribe_complex_page(_fake_png(), width=1000, height=1000)
    assert out == "# Page Title\n\n- bullet one\n- bullet two"


def test_transcribe_complex_page_diagram_blockquote(monkeypatch):
    config.set_enabled("vision", True)
    config.set_enabled("classify", True)
    monkeypatch.setattr(classify, "image_readable", lambda *a, **kw: None)
    monkeypatch.setattr(classify, "record", _noop_record)
    monkeypatch.setattr(classify, "classify_image_with_log", lambda *a, **kw: "diagram")
    monkeypatch.setattr(
        classify,
        "transcribe_image_meta_cached",
        lambda blob, mime="image/png", **kw: ("A conceptual diagram of the 4EM framework.", None),
    )
    out = classify.transcribe_complex_page(_fake_png(), width=1000, height=1000)
    assert out == "> A conceptual diagram of the 4EM framework."


def test_transcribe_complex_page_rejects_low_quality(monkeypatch):
    config.set_enabled("vision", True)
    config.set_enabled("classify", False)
    monkeypatch.setattr(classify, "image_readable", lambda *a, **kw: None)
    monkeypatch.setattr(classify, "record", _noop_record)
    monkeypatch.setattr(
        classify,
        "transcribe_page_cached",
        lambda png_bytes, **kw: ("", None),
    )
    warnings: list[str] = []
    out = classify.transcribe_complex_page(_fake_png(), warnings=warnings, width=1000, height=1000)
    assert out is None
    assert any("low-value" in w for w in warnings)


def test_transcribe_complex_page_skips_unreadable(monkeypatch):
    config.set_enabled("vision", True)
    config.set_enabled("classify", False)
    monkeypatch.setattr(classify, "image_readable", lambda *a, **kw: "low resolution")
    monkeypatch.setattr(classify, "record", _noop_record)
    warnings: list[str] = []
    out = classify.transcribe_complex_page(_fake_png(), warnings=warnings, width=10, height=10)
    assert out is None
    assert any("unreadable" in w for w in warnings)
