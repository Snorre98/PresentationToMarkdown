"""Tests for the CLI entry points (cli_common, cli)."""
from pathlib import Path

import pytest

import cli
import start
from cli_common import ai_env_vars, apply_ai_env

TESTS_DIR = Path(__file__).parent
PPTX = TESTS_DIR / "test_deck.pptx"


def parse(argv):
    return start.build_parser().parse_args(argv)


# --- flag -> env mapping ----------------------------------------------------


def test_no_flags_maps_to_nothing():
    assert ai_env_vars(parse([])) == {}


def test_vision_flag():
    assert ai_env_vars(parse(["--vision"])) == {"VISION_ENABLED": "1"}


def test_classify_implies_vision():
    env = ai_env_vars(parse(["--classify"]))
    assert env == {"VISION_CLASSIFY_ENABLED": "1", "VISION_ENABLED": "1"}


def test_individual_flags():
    assert ai_env_vars(parse(["--format"])) == {"FORMAT_ENABLED": "1"}
    assert ai_env_vars(parse(["--summary"])) == {"SUMMARY_ENABLED": "1"}
    assert ai_env_vars(parse(["--interpret"])) == {"INTERPRET_ENABLED": "1"}
    assert ai_env_vars(parse(["--structure"])) == {"STRUCTURE_ENABLED": "1"}


def test_structure_not_part_of_all():
    env = ai_env_vars(parse(["--all"]))
    assert "STRUCTURE_ENABLED" not in env


def test_all_enables_every_pass():
    env = ai_env_vars(parse(["--all"]))
    assert env == {
        "VISION_ENABLED": "1",
        "VISION_CLASSIFY_ENABLED": "1",
        "INTERPRET_ENABLED": "1",
        "FORMAT_ENABLED": "1",
        "SUMMARY_ENABLED": "1",
    }


def test_audio_flags_removed():
    with pytest.raises(SystemExit):
        parse(["--audio"])
    with pytest.raises(SystemExit):
        parse(["--diarize"])
    assert "audio" not in ai_env_vars(parse([]))


def test_env_passthrough():
    env = ai_env_vars(parse(["--vision", "--env", "VISION_MODEL=foo", "--env", "VISION_LOG_DB=/tmp/x.sqlite"]))
    assert env["VISION_ENABLED"] == "1"
    assert env["VISION_MODEL"] == "foo"
    assert env["VISION_LOG_DB"] == "/tmp/x.sqlite"


def test_env_passthrough_invalid_raises():
    with pytest.raises(SystemExit):
        ai_env_vars(parse(["--env", "nokey"]))


def test_apply_ai_env_sets_environment(monkeypatch):
    import os

    for var in ("VISION_ENABLED", "VISION_CLASSIFY_ENABLED"):
        monkeypatch.setenv(var, "")
    applied = apply_ai_env(parse(["--classify"]))

    assert os.environ["VISION_ENABLED"] == "1"
    assert os.environ["VISION_CLASSIFY_ENABLED"] == "1"
    assert applied == {"VISION_ENABLED": "1", "VISION_CLASSIFY_ENABLED": "1"}


# --- file collection --------------------------------------------------------


def test_collect_files_from_folder_recursive(tmp_path):
    sub = tmp_path / "nested"
    sub.mkdir()
    (tmp_path / "a.pptx").write_bytes(b"x")
    (sub / "b.pdf").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("skip me")
    files, exts = cli.collect_files([str(tmp_path)], recursive=True)
    names = sorted(f.name for f in files)
    assert names == ["a.pptx", "b.pdf"]
    assert {".pptx", ".pdf"} <= exts


def test_collect_files_no_recursive(tmp_path):
    sub = tmp_path / "nested"
    sub.mkdir()
    (tmp_path / "a.pptx").write_bytes(b"x")
    (sub / "b.pdf").write_bytes(b"x")
    files, _ = cli.collect_files([str(tmp_path)], recursive=False)
    assert [f.name for f in files] == ["a.pptx"]


def test_collect_files_deduplicates(tmp_path):
    (tmp_path / "a.pptx").write_bytes(b"x")
    files, _ = cli.collect_files([str(tmp_path / "a.pptx"), str(tmp_path / "a.pptx")], recursive=True)
    assert len(files) == 1


# --- end-to-end main() ------------------------------------------------------


def test_main_converts_and_exits_zero(tmp_path, capsys):
    code = cli.main([str(PPTX), "-o", str(tmp_path), "--no-recent"])
    out = capsys.readouterr().out
    assert code == 0
    assert "[OK]" in out
    assert "Done: 1 of 1 converted." in out
    assert (tmp_path / "test_deck.md").exists()


def test_main_exit_one_on_failure(tmp_path, capsys):
    bogus = tmp_path / "broken.pptx"
    bogus.write_bytes(b"not a real pptx")
    code = cli.main([str(bogus), "-o", str(tmp_path), "--no-recent"])
    out = capsys.readouterr().out
    assert code == 1
    assert "[ERR]" in out


def test_main_exit_two_when_no_supported_files(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = cli.main([str(empty), "--no-recent"])
    assert code == 2


def test_main_records_recent(monkeypatch, tmp_path, capsys):
    from converter import settings

    recorded = []
    monkeypatch.setattr(settings, "record_recent", lambda p: recorded.append(p))
    code = cli.main([str(PPTX), "-o", str(tmp_path)])
    assert code == 0
    assert recorded and recorded[0].endswith("test_deck.pptx")


def test_main_quiet_suppresses_progress(tmp_path, capsys):
    code = cli.main([str(PPTX), "-o", str(tmp_path), "--no-recent", "--quiet"])
    out = capsys.readouterr().out
    assert code == 0
    assert "[1/1]" not in out
    assert "[OK]" in out
