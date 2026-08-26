"""Unit tests for the audio transcription pass (no model/binary required)."""
import json
from pathlib import Path

import pytest

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


def test_find_audio_for(tmp_path):
    src = tmp_path / "lecture.pdf"
    src.write_text("x")
    (tmp_path / "lecture.mp3").write_text("a")
    (tmp_path / "lecture.wav").write_text("b")
    (tmp_path / "other.m4a").write_text("c")
    assert t.find_audio_for(src) == tmp_path / "lecture.wav"


def test_find_audio_for_markdown(tmp_path):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n")
    (tmp_path / "deck.mp3").write_text("a")
    assert t.find_audio_for(md) == tmp_path / "deck.mp3"


def test_find_audio_for_none(tmp_path):
    src = tmp_path / "lecture.pdf"
    src.write_text("x")
    assert t.find_audio_for(src) is None


def test_transcribe_audio(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3600.0):
        calls.append(list(cmd))
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        input_stem = Path(cmd[1]).stem
        (outdir / (input_stem + ".json")).write_text(
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
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", False)
    clean = tmp_path / "x.clean.flac"
    segs = t.transcribe_audio(tmp_path / "x.mp3", clean)

    assert segs == [{"start": 0.0, "end": 2.5, "text": "hello world"}]
    ffmpeg = calls[0]
    assert ffmpeg[0] == t.AUDIO_FFMPEG_BIN
    assert "-af" in ffmpeg and ffmpeg[ffmpeg.index("-af") + 1] == t._ENHANCE_FILTER
    assert "-c:a" in ffmpeg and "flac" in ffmpeg
    assert "-ar" in ffmpeg and "16000" in ffmpeg
    # ffmpeg targets a temp sibling that is atomically moved into place.
    assert Path(ffmpeg[-1]).parent == clean.parent
    assert ffmpeg[-1] != str(clean)
    assert clean.exists()
    whisper = calls[1]
    assert whisper[0] == t.AUDIO_MLX_WHISPER_BIN
    assert whisper[1] == str(clean)
    assert whisper[whisper.index("--model") + 1] == t.AUDIO_MODEL
    # Repetition-loop hallucination is disabled by default (see ADR-0008 caveat).
    assert "--condition-on-previous-text" in whisper
    assert whisper[whisper.index("--condition-on-previous-text") + 1] == "False"


def test_transcribe_audio_condition_on_previous_text_opt_in(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3600.0):
        calls.append(list(cmd))
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        (outdir / (Path(cmd[1]).stem + ".json")).write_text(
            json.dumps({"segments": []}), encoding="utf-8"
        )
        return ""

    monkeypatch.setattr(t, "_run", fake_run)
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", False)
    monkeypatch.setattr(t, "AUDIO_CONDITION_ON_PREVIOUS_TEXT", True)
    t.transcribe_audio(tmp_path / "x.mp3", tmp_path / "x.clean.flac")
    assert "--condition-on-previous-text" not in calls[1]


def test_transcribe_audio_silent_failure_surfaces_output(monkeypatch, tmp_path):
    def fake_run(cmd, timeout=3600.0):
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        return "Skipping x.clean.flac due to SomeError: boom\n"

    monkeypatch.setattr(t, "_run", fake_run)
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", False)
    with pytest.raises(RuntimeError, match="Skipping"):
        t.transcribe_audio(tmp_path / "x.mp3", tmp_path / "x.clean.flac")


def test_transcribe_audio_preprocess_disabled(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3600.0):
        calls.append(list(cmd))
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        (outdir / (Path(cmd[1]).stem + ".json")).write_text(
            json.dumps({"segments": []}), encoding="utf-8"
        )
        return ""

    monkeypatch.setattr(t, "_run", fake_run)
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", False)
    monkeypatch.setattr(t, "AUDIO_PREPROCESS", False)
    t.transcribe_audio(tmp_path / "x.mp3", tmp_path / "x.clean.flac")
    assert "-af" not in calls[0]


def test_transcribe_audio_calls_enhance_when_enabled(monkeypatch, tmp_path):
    enhanced: list[tuple[str, str]] = []

    def fake_run(cmd, timeout=3600.0):
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        (outdir / (Path(cmd[1]).stem + ".json")).write_text(
            json.dumps({"segments": []}), encoding="utf-8"
        )
        return ""

    monkeypatch.setattr(t, "_run", fake_run)
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", True)
    monkeypatch.setattr(t, "enhance", lambda p, o, **kw: enhanced.append((p, o)))
    clean = tmp_path / "x.clean.flac"
    t.transcribe_audio(tmp_path / "x.mp3", clean)
    assert enhanced == [(str(clean), str(clean))]


def test_transcribe_audio_enhance_failure_warns(monkeypatch, tmp_path):
    def fake_run(cmd, timeout=3600.0):
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        (outdir / (Path(cmd[1]).stem + ".json")).write_text(
            json.dumps({"segments": []}), encoding="utf-8"
        )
        return ""

    monkeypatch.setattr(t, "_run", fake_run)
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", True)
    monkeypatch.setattr(t, "enhance", lambda p, o, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    warnings: list[str] = []
    segs = t.transcribe_audio(tmp_path / "x.mp3", tmp_path / "x.clean.flac", warnings=warnings)
    assert segs == []
    assert any("Audio enhancement failed" in w for w in warnings)


def test_attach_transcript_disabled_noop(tmp_path, monkeypatch):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n", encoding="utf-8")
    monkeypatch.setattr(t, "AUDIO_ENABLED", False)
    result = t.attach_transcript(md, [])
    assert result is None
    assert md.read_text(encoding="utf-8") == "# Deck\n"
    assert not (tmp_path / "deck.transcript.srt").exists()


def test_attach_transcript_appends(tmp_path, monkeypatch):
    md = tmp_path / "deck.md"
    md.write_text("# Deck — Slide 1\n\ncontent\n", encoding="utf-8")
    monkeypatch.setattr(t, "AUDIO_ENABLED", True)
    monkeypatch.setattr(t, "AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr(
        t,
        "transcribe_audio",
        lambda p, cp, **kw: [{"start": 0.0, "end": 5.0, "text": "hello"}],
    )
    recorded: list[dict] = []
    monkeypatch.setattr(t, "record_segment", lambda **kw: recorded.append(kw))

    (tmp_path / "deck.mp3").write_bytes(b"fake audio")
    segments = t.attach_transcript(md, [], audio_path=tmp_path / "deck.mp3")

    text = md.read_text(encoding="utf-8")
    assert segments == [{"start": 0.0, "end": 5.0, "text": "hello"}]
    assert text.startswith("# Deck — Slide 1")
    assert "# Transcript" in text
    assert "[00:00:00] hello" in text
    assert (tmp_path / "deck.transcript.srt").exists()
    assert recorded and recorded[0]["text"] == "hello"
    assert recorded[0]["source"] == str(md)


def test_attach_transcript_idempotent(tmp_path, monkeypatch):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n\ncontent\n", encoding="utf-8")
    monkeypatch.setattr(t, "AUDIO_ENABLED", True)
    monkeypatch.setattr(t, "AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr(
        t,
        "transcribe_audio",
        lambda p, cp, **kw: [{"start": 0.0, "end": 5.0, "text": "hello"}],
    )
    monkeypatch.setattr(t, "record_segment", lambda **kw: None)

    (tmp_path / "deck.mp3").write_bytes(b"fake audio")
    t.attach_transcript(md, [], audio_path=tmp_path / "deck.mp3")
    t.attach_transcript(md, [], audio_path=tmp_path / "deck.mp3")

    text = md.read_text(encoding="utf-8")
    assert text.count("# Transcript") == 1
    assert text.rstrip("\n").endswith("</details>")
    assert text.strip().startswith("# Deck")


def test_attach_transcript_missing_audio_noop(tmp_path, monkeypatch):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n", encoding="utf-8")
    monkeypatch.setattr(t, "AUDIO_ENABLED", True)
    monkeypatch.setattr(t, "transcribe_audio", lambda *a, **kw: (_ for _ in ()).throw(AssertionError()))
    warnings: list[str] = []
    result = t.attach_transcript(md, warnings)
    assert result is None
    assert "# Transcript" not in md.read_text(encoding="utf-8")


def test_transcribe_to_markdown(tmp_path, monkeypatch):
    audio = tmp_path / "week-2.mp3"
    audio.write_bytes(b"fake audio")
    monkeypatch.setattr(t, "AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr(
        t,
        "transcribe_audio",
        lambda p, cp, **kw: [{"start": 0.0, "end": 5.0, "text": "hello"}],
    )
    recorded: list[dict] = []
    monkeypatch.setattr(t, "record_segment", lambda **kw: recorded.append(kw))

    out = t.transcribe_to_markdown(audio, [])

    assert out == tmp_path / "week-2.transcript.md"
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("# Transcript")
    assert (tmp_path / "week-2.transcript.srt").exists()
    assert recorded and recorded[0]["source"] == str(out)


def test_transcribe_to_markdown_missing_audio(tmp_path):
    warnings: list[str] = []
    out = t.transcribe_to_markdown(tmp_path / "nope.mp3", warnings)
    assert out is None
    assert any("Audio file not found" in w for w in warnings)


def test_next_free_version(tmp_path):
    base = tmp_path / "week-2.transcript.md"
    assert t._next_free_version(base) == base
    base.write_text("x", encoding="utf-8")
    assert t._next_free_version(base) == tmp_path / "week-2.transcript.1.md"
    (tmp_path / "week-2.transcript.1.md").write_text("x", encoding="utf-8")
    assert t._next_free_version(base) == tmp_path / "week-2.transcript.2.md"


def test_transcribe_to_markdown_versions(tmp_path, monkeypatch):
    audio = tmp_path / "week-2.mp3"
    audio.write_bytes(b"fake audio")
    monkeypatch.setattr(t, "AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr(t, "record_segment", lambda **kw: None)
    monkeypatch.setattr(
        t,
        "transcribe_audio",
        lambda p, cp, **kw: [{"start": 0.0, "end": 5.0, "text": "hello"}],
    )

    first = t.transcribe_to_markdown(audio, [])
    second = t.transcribe_to_markdown(audio, [])
    third = t.transcribe_to_markdown(audio, [])

    assert first == tmp_path / "week-2.transcript.md"
    assert second == tmp_path / "week-2.transcript.1.md"
    assert third == tmp_path / "week-2.transcript.2.md"
    assert (tmp_path / "week-2.transcript.srt").exists()
    assert (tmp_path / "week-2.transcript.1.srt").exists()
    assert (tmp_path / "week-2.transcript.2.srt").exists()
    assert first.read_text(encoding="utf-8").startswith("# Transcript")


def test_transcribe_to_markdown_overwrite(tmp_path, monkeypatch):
    audio = tmp_path / "week-2.mp3"
    audio.write_bytes(b"fake audio")
    monkeypatch.setattr(t, "AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr(t, "record_segment", lambda **kw: None)
    monkeypatch.setattr(
        t,
        "transcribe_audio",
        lambda p, cp, **kw: [{"start": 0.0, "end": 5.0, "text": "hello"}],
    )

    first = t.transcribe_to_markdown(audio, [])
    second = t.transcribe_to_markdown(audio, [], overwrite=True)

    assert first == tmp_path / "week-2.transcript.md"
    assert second == tmp_path / "week-2.transcript.md"
    assert not (tmp_path / "week-2.transcript.1.md").exists()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
