"""Tests for the ``ptm-transcribe`` entry point (no model/binary required)."""
from pathlib import Path

import pytest

import cli_transcribe as ct


def parse(argv):
    return ct.build_parser().parse_args(argv)


# --- flag -> env mapping ----------------------------------------------------


def test_apply_env_defaults_enables_audio():
    env = ct._apply_env(parse([]))
    assert env["AUDIO_ENABLED"] == "1"


def test_apply_env_diarize_and_language():
    env = ct._apply_env(parse(["--diarize", "--language", "no"]))
    assert env["AUDIO_ENABLED"] == "1"
    assert env["AUDIO_DIARIZE_ENABLED"] == "1"
    assert env["AUDIO_LANGUAGE"] == "no"


def test_apply_env_passthrough_overrides():
    env = ct._apply_env(parse(["--env", "AUDIO_MODEL=foo", "--env", "AUDIO_ENABLED=0"]))
    assert env["AUDIO_MODEL"] == "foo"
    assert env["AUDIO_ENABLED"] == "0"


def test_apply_env_invalid_env_raises():
    with pytest.raises(SystemExit):
        ct._apply_env(parse(["--env", "nokey"]))


# --- collection -------------------------------------------------------------


def test_collect_targets(tmp_path):
    (tmp_path / "deck.md").write_text("# x")
    (tmp_path / "deck.mp3").write_bytes(b"a")
    (tmp_path / "notes.txt").write_text("skip")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.m4a").write_bytes(b"b")
    md_files, audio_files = ct.collect_targets([str(tmp_path)])
    assert sorted(p.name for p in md_files) == ["deck.md"]
    assert sorted(p.name for p in audio_files) == ["b.m4a", "deck.mp3"]


def test_collect_targets_dedupes(tmp_path):
    (tmp_path / "deck.md").write_text("# x")
    md_files, audio_files = ct.collect_targets(
        [str(tmp_path / "deck.md"), str(tmp_path / "deck.md")]
    )
    assert len(md_files) == 1
    assert audio_files == []


# --- end-to-end main() with faked transcription -----------------------------


def _fake_attach(calls):
    def attach(md, warnings, audio_path=None):
        calls.append(("attach", md, audio_path))
        return [{"start": 0.0, "end": 1.0, "text": "hi"}]

    return attach


def test_main_attaches_discovered_audio(tmp_path, monkeypatch, capsys):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n")
    (tmp_path / "deck.mp3").write_bytes(b"audio")

    calls = []
    monkeypatch.setattr("converter.transcribe.attach_transcript", _fake_attach(calls))

    code = ct.main([str(md)])
    out = capsys.readouterr().out
    assert code == 0
    assert calls == [("attach", md, tmp_path / "deck.mp3")]
    assert "[OK]" in out


def test_main_standalone(tmp_path, monkeypatch, capsys):
    audio = tmp_path / "week-2.mp3"
    audio.write_bytes(b"audio")

    calls = []
    monkeypatch.setattr(
        "converter.transcribe.transcribe_to_markdown",
        lambda a, w=None: calls.append(("standalone", a)) or (tmp_path / "week-2.transcript.md"),
    )

    code = ct.main([str(audio)])
    out = capsys.readouterr().out
    assert code == 0
    assert calls == [("standalone", audio)]
    assert "[OK]" in out


def test_main_to_flag_pairs_audio(tmp_path, monkeypatch, capsys):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n")
    audio = tmp_path / "lecture.mp3"
    audio.write_bytes(b"audio")

    calls = []
    monkeypatch.setattr("converter.transcribe.attach_transcript", _fake_attach(calls))

    code = ct.main([str(audio), "--to", str(md)])
    assert code == 0
    assert calls == [("attach", md, audio)]


def test_main_audio_file_sole_target(tmp_path, monkeypatch, capsys):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n")
    audio = tmp_path / "other.m4a"
    audio.write_bytes(b"audio")

    calls = []
    monkeypatch.setattr("converter.transcribe.attach_transcript", _fake_attach(calls))

    code = ct.main([str(md), "--audio-file", str(audio)])
    assert code == 0
    assert calls == [("attach", md, audio)]


def test_main_prompts_when_ambiguous(tmp_path, monkeypatch, capsys):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n")
    audio = tmp_path / "lecture.mp3"
    audio.write_bytes(b"audio")

    calls = []
    monkeypatch.setattr("converter.transcribe.attach_transcript", _fake_attach(calls))
    monkeypatch.setattr(ct, "_pick_lecture", lambda a, cands: md)

    code = ct.main([str(audio), str(md)])
    assert code == 0
    assert calls == [("attach", md, audio)]


def test_main_prompt_standalone_when_none(tmp_path, monkeypatch, capsys):
    audio = tmp_path / "lecture.mp3"
    audio.write_bytes(b"audio")

    standalone_calls = []
    monkeypatch.setattr(
        "converter.transcribe.transcribe_to_markdown",
        lambda a, w=None: standalone_calls.append(a) or (tmp_path / "lecture.transcript.md"),
    )
    monkeypatch.setattr(ct, "_pick_lecture", lambda a, cands: None)

    code = ct.main([str(audio)])
    assert code == 0
    assert standalone_calls == [audio]


def test_main_no_targets(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ct.main([str(empty)]) == 2


# --- end-to-end main() with real attach (faked subprocess) ------------------


def test_main_end_to_end_attaches(tmp_path, monkeypatch, capsys):
    md = tmp_path / "deck.md"
    md.write_text("# Deck\n", encoding="utf-8")
    (tmp_path / "deck.mp3").write_bytes(b"audio")

    monkeypatch.setattr("converter.transcribe.AUDIO_ENABLED", True)
    monkeypatch.setattr("converter.transcribe.AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr("converter.transcribe.record_segment", lambda **kw: None)
    monkeypatch.setattr(
        "converter.transcribe.transcribe_audio",
        lambda p, cp, **kw: [{"start": 0.0, "end": 2.0, "text": "hello"}],
    )

    code = ct.main([str(md)])
    out = capsys.readouterr().out
    assert code == 0
    assert "[OK]" in out
    assert "# Transcript" in md.read_text(encoding="utf-8")


def test_main_end_to_end_standalone(tmp_path, monkeypatch, capsys):
    audio = tmp_path / "week-2.mp3"
    audio.write_bytes(b"audio")

    monkeypatch.setattr("converter.transcribe.AUDIO_ENABLED", True)
    monkeypatch.setattr("converter.transcribe.AUDIO_DIARIZE_ENABLED", False)
    monkeypatch.setattr("converter.transcribe.record_segment", lambda **kw: None)
    monkeypatch.setattr(
        "converter.transcribe.transcribe_audio",
        lambda p, cp, **kw: [{"start": 0.0, "end": 2.0, "text": "hello"}],
    )

    code = ct.main([str(audio)])
    out = capsys.readouterr().out
    assert code == 0
    assert "[OK]" in out
    assert (tmp_path / "week-2.transcript.md").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
