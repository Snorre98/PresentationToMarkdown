"""Unit tests for the audio transcription pass (no model/binary required)."""
import json
from pathlib import Path

from converter import transcribe as t
from converter.audio import assign_speakers


def test_format_timestamp():
    assert t.format_timestamp(0) == "00:00:00"
    assert t.format_timestamp(65) == "00:01:05"
    assert t.format_timestamp(3661) == "01:01:01"


def test_segments_to_markdown():
    segs = [
        {"start": 4.0, "end": 10.0, "text": "hello", "speaker": "Speaker A"},
        {"start": 12.0, "end": 15.0, "text": "world", "speaker": None},
    ]
    md = t.segments_to_markdown(segs)
    assert md.startswith("# Transcript")
    assert "[00:00:04] **Speaker A:** hello" in md
    assert "[00:00:12] world" in md
    assert "</details>" in md


def test_segments_to_srt():
    segs = [{"start": 1.5, "end": 3.0, "text": "hi", "speaker": "Speaker A"}]
    srt = t.segments_to_srt(segs)
    assert "1\n00:00:01,500 --> 00:00:03,000\n[Speaker A] hi" in srt


def test_assign_speakers():
    segs = [
        {"start": 0.0, "end": 10.0, "text": "a"},
        {"start": 10.0, "end": 20.0, "text": "b"},
        {"start": 20.0, "end": 30.0, "text": "c"},
    ]
    turns = [
        {"start": 0.0, "end": 9.0, "speaker": "S1"},
        {"start": 9.0, "end": 25.0, "speaker": "S2"},
    ]
    assign_speakers(segs, turns)
    assert segs[0]["speaker"] == "S1"
    assert segs[1]["speaker"] == "S2"
    assert segs[2]["speaker"] is None


def test_find_audio_for_source(tmp_path):
    src = tmp_path / "lecture.pdf"
    src.write_text("x")
    (tmp_path / "lecture.mp3").write_text("a")
    (tmp_path / "lecture.wav").write_text("b")
    (tmp_path / "other.m4a").write_text("c")
    assert t.find_audio_for_source(src) == tmp_path / "lecture.wav"


def test_find_audio_for_source_none(tmp_path):
    src = tmp_path / "lecture.pdf"
    src.write_text("x")
    assert t.find_audio_for_source(src) is None


def test_transcribe_audio(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3600.0):
        calls.append(list(cmd))
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        (outdir / "audio.json").write_text(
            json.dumps(
                {
                    "text": "hello world",
                    "segments": [{"start": 0.0, "end": 2.5, "text": " hello world "}],
                }
            ),
            encoding="utf-8",
        )
        return ""

    monkeypatch.setattr(t, "_run", fake_run)
    segs = t.transcribe_audio(tmp_path / "x.mp3")

    assert segs == [{"start": 0.0, "end": 2.5, "text": "hello world"}]
    assert calls[0][0] == t.AUDIO_FFMPEG_BIN
    assert "-ar" in calls[0] and "16000" in calls[0]
    assert calls[1][0] == t.AUDIO_MLX_WHISPER_BIN
    assert calls[1][calls[1].index("--model") + 1] == t.AUDIO_MODEL


def test_attach_transcript_disabled_noop(tmp_path, monkeypatch):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n", encoding="utf-8")
    monkeypatch.setattr(t, "AUDIO_ENABLED", False)
    t.attach_transcript(md, tmp_path / "deck.pdf", [])
    assert md.read_text(encoding="utf-8") == "# Deck\n"
    assert not (tmp_path / "deck.transcript.srt").exists()


def test_attach_transcript_appends(tmp_path, monkeypatch):
    md = tmp_path / "deck.md"
    md.write_text("# Deck — Slide 1\n\ncontent\n", encoding="utf-8")
    monkeypatch.setattr(t, "AUDIO_ENABLED", True)
    monkeypatch.setattr(t, "AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr(
        t, "transcribe_audio", lambda p: [{"start": 0.0, "end": 5.0, "text": "hello"}]
    )
    recorded: list[dict] = []
    monkeypatch.setattr(t, "record_segment", lambda **kw: recorded.append(kw))

    (tmp_path / "deck.mp3").write_bytes(b"fake audio")
    t.attach_transcript(md, tmp_path / "deck.pdf", [], audio_path=tmp_path / "deck.mp3")

    text = md.read_text(encoding="utf-8")
    assert text.startswith("# Deck — Slide 1")
    assert "# Transcript" in text
    assert "[00:00:00] hello" in text
    assert (tmp_path / "deck.transcript.srt").exists()
    assert recorded and recorded[0]["text"] == "hello"


def test_attach_transcript_missing_audio_noop(tmp_path, monkeypatch):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n", encoding="utf-8")
    monkeypatch.setattr(t, "AUDIO_ENABLED", True)
    monkeypatch.setattr(t, "transcribe_audio", lambda p: (_ for _ in ()).throw(AssertionError()))
    warnings: list[str] = []
    t.attach_transcript(md, tmp_path / "deck.pdf", warnings)
    assert "# Transcript" not in md.read_text(encoding="utf-8")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
