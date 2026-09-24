"""Tests for the opt-in LLM transcript edit pass (``converter.transcript_edit``).

Model-free: the chat client is monkeypatched, so nothing here hits a server. The
word-preservation gate is the property under test — an edit is accepted only if
it drops no content word and invents none.
"""
from __future__ import annotations

import pytest

import converter.transcript_edit as te


def test_safe_edit_rejects_dropped_content_word():
    assert te._safe_edit("Det var kjempekleit dag", "Det var dag") is False


def test_safe_edit_rejects_invented_words():
    assert te._safe_edit("det var kleint", "det var veldig kleint og morsomt") is False


def test_safe_edit_accepts_punctuation_only():
    assert te._safe_edit("det var kleit", "det var kleit.") is True


def test_edit_transcript_applies_safe_edit(monkeypatch):
    monkeypatch.setattr(te, "_chat_edit", lambda text: "Hva føler du ikke funker bra?")
    segs = [{"start": 0.0, "end": 1.0, "text": "Hva føler du ikke funker bra ?"}]
    out = te.edit_transcript(segs)
    assert out[0]["text"] == "Hva føler du ikke funker bra?"


def test_edit_transcript_rejects_dropping_edit(monkeypatch):
    monkeypatch.setattr(te, "_chat_edit", lambda text: "Hva føler du?")
    segs = [{"start": 0.0, "end": 1.0, "text": "Hva føler du ikke funker bra?"}]
    out = te.edit_transcript(segs)
    assert out[0]["text"] == "Hva føler du ikke funker bra?"  # verbatim


def test_edit_transcript_noop_when_server_down(monkeypatch):
    def _boom(text):
        raise RuntimeError("down")

    monkeypatch.setattr(te, "_chat_edit", _boom)
    segs = [{"start": 0.0, "end": 1.0, "text": "Hva føler du ikke funker bra?"}]
    out = te.edit_transcript(segs)
    assert out[0]["text"] == "Hva føler du ikke funker bra?"


def test_edit_transcript_skips_empty_segments(monkeypatch):
    calls = []
    monkeypatch.setattr(te, "_chat_edit", lambda text: calls.append(text) or text)
    segs = [{"start": 0.0, "end": 1.0, "text": "   "}]
    out = te.edit_transcript(segs)
    assert out == segs and calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
