"""Long-file diarization: chunk planning and cross-chunk speaker identity.

These tests never touch a model or a daemon: the pure helpers in
``converter.audio`` are exercised directly, and the orchestration in
``converter.transcribe`` runs against fakes (ADR-0068).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from converter import audio as a
from converter import transcribe as t


# ── chunk planning ────────────────────────────────────────────────────────────


def test_plan_short_file_is_one_chunk():
    plan = a.plan_diarize_chunks(300.0, chunk_seconds=900.0)
    assert len(plan) == 1
    assert plan[0]["core_start"] == 0.0
    assert plan[0]["core_end"] == 300.0
    assert plan[0]["request_start"] == 0.0
    assert plan[0]["request_end"] == 300.0


def test_plan_covers_timeline_exactly_once():
    plan = a.plan_diarize_chunks(3600.0, chunk_seconds=900.0, overlap_seconds=15.0)
    assert len(plan) >= 4
    assert plan[0]["core_start"] == 0.0
    assert plan[-1]["core_end"] == 3600.0
    for previous, following in zip(plan, plan[1:]):
        assert previous["core_end"] == following["core_start"]
    for chunk in plan:
        assert chunk["request_start"] <= chunk["core_start"]
        assert chunk["request_end"] >= chunk["core_end"]
        assert chunk["request_start"] >= 0.0
        assert chunk["request_end"] <= 3600.0


def test_plan_prefers_a_nearby_silence():
    # one long silence in the middle of the second chunk's search window
    regions = [
        {"start": 0.0, "end": 880.0},
        {"start": 920.0, "end": 1800.0},
        {"start": 1800.0, "end": 3600.0},
    ]
    plan = a.plan_diarize_chunks(
        3600.0, regions, chunk_seconds=900.0, overlap_seconds=15.0, min_seconds=480.0
    )
    assert 880.0 <= plan[0]["core_end"] <= 920.0


def test_plan_zero_duration_is_empty():
    assert a.plan_diarize_chunks(0.0) == []


# ── identity clustering ───────────────────────────────────────────────────────


def test_cluster_merges_near_identical_and_keeps_distinct():
    embeddings = [
        [1.0, 0.0, 0.0],
        [0.98, 0.02, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    labels = a.cluster_speaker_embeddings(embeddings, threshold=0.65)
    assert labels[0] == labels[1]
    assert len(set(labels)) == 3


def test_cluster_honours_exact_speaker_count():
    embeddings = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7], [0.0, 0.9]]
    labels = a.cluster_speaker_embeddings(embeddings, num_speakers=2)
    assert len(set(labels)) == 2


def test_cluster_empty_input():
    assert a.cluster_speaker_embeddings([]) == []


# ── stitching ─────────────────────────────────────────────────────────────────


def _chunk(core_start, core_end, turns, embeddings):
    return {
        "core_start": core_start,
        "core_end": core_end,
        "turns": turns,
        "embeddings": embeddings,
    }


def test_stitch_keeps_identity_across_chunks():
    chunks = [
        _chunk(
            0.0,
            600.0,
            [
                {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
            ],
            {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
        ),
        _chunk(
            600.0,
            1200.0,
            [
                # local labels are renumbered by the server per request
                {"start": 605.0, "end": 610.0, "speaker": "SPEAKER_01"},
                {"start": 610.0, "end": 615.0, "speaker": "SPEAKER_00"},
            ],
            {"SPEAKER_00": [0.0, 1.0], "SPEAKER_01": [1.0, 0.0]},
        ),
    ]
    turns = a.stitch_chunk_turns(chunks, num_speakers=2)
    by_time = {turn["start"]: turn["speaker"] for turn in turns}
    # chunk 2 swapped the local numbering: 605 belongs with 0, 610 with 5
    assert by_time[0.0] == by_time[605.0]
    assert by_time[5.0] == by_time[610.0]
    assert by_time[0.0] != by_time[5.0]


def test_stitch_clamps_turns_to_their_core_span():
    chunks = [
        _chunk(
            0.0,
            600.0,
            [{"start": 590.0, "end": 620.0, "speaker": "SPEAKER_00"}],
            {"SPEAKER_00": [1.0, 0.0]},
        ),
        _chunk(
            600.0,
            1200.0,
            [{"start": 590.0, "end": 620.0, "speaker": "SPEAKER_00"}],
            {"SPEAKER_00": [1.0, 0.0]},
        ),
    ]
    turns = a.stitch_chunk_turns(chunks, num_speakers=1)
    assert [turn["start"] for turn in turns] == [590.0, 600.0]
    assert [turn["end"] for turn in turns] == [600.0, 620.0]


def test_stitch_never_merges_a_label_without_an_embedding():
    chunks = [
        _chunk(0.0, 60.0, [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}], {}),
        _chunk(
            60.0,
            120.0,
            [{"start": 60.0, "end": 65.0, "speaker": "SPEAKER_00"}],
            {"SPEAKER_00": [1.0, 0.0]},
        ),
    ]
    turns = a.stitch_chunk_turns(chunks, num_speakers=1)
    assert len({turn["speaker"] for turn in turns}) == 2


def test_stitch_names_speakers_by_first_appearance():
    chunks = [
        _chunk(
            0.0,
            60.0,
            [
                {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_07"},
                {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_03"},
            ],
            {"SPEAKER_07": [1.0, 0.0], "SPEAKER_03": [0.0, 1.0]},
        )
    ]
    turns = a.stitch_chunk_turns(chunks, num_speakers=2)
    assert turns[0]["speaker"] == "SPEAKER_00"
    assert turns[1]["speaker"] == "SPEAKER_01"


# ── client parsing ────────────────────────────────────────────────────────────


def test_diarize_detailed_reads_embeddings(monkeypatch):
    def fake_post(req, timeout, what):
        return {
            "turns": [{"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"SPEAKER_00": [0.1, 0.2]},
            "device": "mps",
        }

    monkeypatch.setattr(a, "_post_json", fake_post)
    body = a.diarize_detailed("x.flac", return_embeddings=True)
    assert body["turns"] == [{"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"}]
    assert body["embeddings"] == {"SPEAKER_00": [0.1, 0.2]}


def test_diarize_detailed_accepts_legacy_list_body(monkeypatch):
    monkeypatch.setattr(
        a, "_post_json", lambda req, timeout, what: [{"start": 0.0, "end": 1.0}]
    )
    body = a.diarize_detailed("x.flac", return_embeddings=True)
    assert body["turns"][0]["speaker"] == "SPEAKER"
    assert body["embeddings"] == {}


# ── orchestration ─────────────────────────────────────────────────────────────


@pytest.fixture
def chunk_env(monkeypatch, tmp_path):
    """Small chunk length, no real ffmpeg/ffprobe/daemon."""
    monkeypatch.setattr(t, "AUDIO_DIARIZE_CHUNK_ENABLED", True)
    monkeypatch.setattr(t, "AUDIO_DIARIZE_CHUNK_SECONDS", 600.0)
    monkeypatch.setattr(t, "AUDIO_DIARIZE_CHUNK_OVERLAP_SECONDS", 10.0)
    monkeypatch.setattr(t, "AUDIO_DIARIZE_CHUNK_MIN_SECONDS", 300.0)
    monkeypatch.setattr(t, "AUDIO_DIARIZE_PREFLIGHT", False)
    monkeypatch.setattr(t, "_audio_duration", lambda path: 1800.0)
    monkeypatch.setattr(t, "vad", lambda path: [{"start": 0.0, "end": 1800.0}])
    monkeypatch.setattr(t, "_temp_sibling", lambda path: str(tmp_path / "chunk.flac"))
    monkeypatch.setattr(t, "_run", lambda cmd, **kw: "")
    monkeypatch.setattr(t, "diarize_bounds", lambda: {"min_speakers": 2, "max_speakers": 2})
    return tmp_path


def test_diarize_long_chunks_and_stitches(monkeypatch, chunk_env):
    calls: list[dict] = []

    def fake_detailed(path, **kw):
        calls.append(kw)
        return {
            "turns": [
                {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
                {"start": 4.0, "end": 8.0, "speaker": "SPEAKER_01"},
            ],
            "embeddings": {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
        }

    monkeypatch.setattr(t, "diarize_detailed", fake_detailed)
    warnings: list[str] = []
    result = t._diarize_long(Path("x.clean.flac"), warnings)

    assert len(calls) == result["info"]["chunks"] >= 3
    assert result["info"]["strategy"] == "chunked"
    assert result["info"]["failed_chunks"] == 0
    assert warnings == []
    # per-chunk calls ask for embeddings and cap at the exact speaker count
    assert all(call["return_embeddings"] for call in calls)
    assert all(call["min_speakers"] is None and call["max_speakers"] == 2 for call in calls)
    assert len({turn["speaker"] for turn in result["turns"]}) == 2


def test_diarize_long_uses_one_request_for_short_files(monkeypatch, chunk_env):
    monkeypatch.setattr(t, "_audio_duration", lambda path: 300.0)
    calls: list[str] = []
    monkeypatch.setattr(t, "diarize", lambda path, **kw: calls.append(path) or [])
    result = t._diarize_long(Path("x.clean.flac"), [])
    assert result["info"] == {"strategy": "single", "chunks": 1}
    assert calls


def test_diarize_long_falls_back_when_server_has_no_embeddings(monkeypatch, chunk_env):
    calls: list[str] = []

    def no_embeddings(path, **kw):
        calls.append(path)
        return {
            "turns": [{"start": 0.0, "end": 1.0, "speaker": "S"}],
            "embeddings": {},
        }

    monkeypatch.setattr(t, "diarize_detailed", no_embeddings)
    single: list[str] = []
    monkeypatch.setattr(t, "diarize", lambda path, **kw: single.append(path) or [])
    warnings: list[str] = []
    result = t._diarize_long(Path("x.clean.flac"), warnings)
    assert result["info"]["strategy"] == "single"
    assert single
    # stops after the first chunk instead of diarizing the whole file twice
    assert len(calls) == 1
    assert any("no speaker embeddings" in warning for warning in warnings)


def test_diarize_long_keeps_going_when_one_chunk_fails(monkeypatch, chunk_env):
    state = {"calls": 0}

    def flaky(path, **kw):
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("boom")
        return {
            "turns": [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"SPEAKER_00": [1.0, 0.0]},
        }

    monkeypatch.setattr(t, "diarize_detailed", flaky)
    warnings: list[str] = []
    result = t._diarize_long(Path("x.clean.flac"), warnings)
    assert result["info"]["failed_chunks"] == 1
    assert result["info"]["chunks"] >= 2
    assert any("stay unlabelled" in warning for warning in warnings)
    assert result["turns"]


def test_diarize_long_raises_when_every_chunk_fails(monkeypatch, chunk_env):
    def always_boom(path, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(t, "diarize_detailed", always_boom)
    with pytest.raises(RuntimeError):
        t._diarize_long(Path("x.clean.flac"), [])


# ── transcript header ─────────────────────────────────────────────────────────


def test_diarization_note_variants():
    assert t._diarization_note(None) is None
    assert t._diarization_note({"diarize_requested": False}) == "Speaker diarization: off"
    failed = t._diarization_note({"diarize_requested": True, "error": "timed out"})
    assert "failed (timed out)" in failed and "no speaker labels" in failed
    empty = t._diarization_note({"diarize_requested": True, "speakers": 0})
    assert "no speaker labels were assigned" in empty
    note = t._diarization_note(
        {
            "diarize_requested": True,
            "speakers": 2,
            "words": 100,
            "labelled": 40,
            "chunks": 4,
            "strategy": "chunked",
            "failed_chunks": 1,
        }
    )
    assert "2 speaker(s)" in note
    assert "4 chunk(s)" in note
    assert "40/100" in note
    assert "1 chunk(s) failed" in note


def test_segments_to_markdown_states_diarization():
    segments = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}]
    md = t.segments_to_markdown(
        segments,
        info={"diarize_requested": True, "speakers": 1, "words": 1, "labelled": 1},
    )
    assert "*Speaker diarization: 1 speaker(s)" in md
    assert "**SPEAKER_00:** hi" in md


def test_transcribe_fills_info_and_survives_diarization_failure(monkeypatch):
    words = [{"start": 0.0, "end": 0.5, "text": "hei"}]
    monkeypatch.setattr(t, "transcribe_audio", lambda *a, **kw: [dict(w) for w in words])
    monkeypatch.setattr(t, "diarize_requested", lambda: True)
    monkeypatch.setattr(t, "record_segment", lambda **kw: None)
    monkeypatch.setattr(t, "_diarize_long", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("nope")))

    info: dict = {}
    warnings: list[str] = []
    segments = t._transcribe(
        Path("x.mp3"), Path("x.clean.flac"), "x", warnings, info=info
    )
    assert info["error"] == "nope"
    assert info["speakers"] == 0
    assert segments[0].get("speaker") is None
    assert any("Diarization failed" in warning for warning in warnings)


def test_diarize_long_preflight_short_circuits(monkeypatch, chunk_env):
    """A dead audio server costs one failed probe, not a whole chunk loop."""
    monkeypatch.setattr(t, "AUDIO_DIARIZE_PREFLIGHT", True)
    monkeypatch.setattr(a, "audio_server_available", lambda *args, **kw: False)
    attempted: list[str] = []
    monkeypatch.setattr(t, "diarize_detailed", lambda path, **kw: attempted.append(path) or {})
    monkeypatch.setattr(t, "diarize", lambda path, **kw: attempted.append(path) or [])

    with pytest.raises(RuntimeError, match="audio server unreachable"):
        t._diarize_long(Path("x.clean.flac"), [])
    assert attempted == []


def test_diarize_long_preflight_can_be_disabled(monkeypatch, chunk_env):
    monkeypatch.setattr(t, "AUDIO_DIARIZE_PREFLIGHT", False)
    monkeypatch.setattr(a, "audio_server_available", lambda *args, **kw: False)
    monkeypatch.setattr(
        t,
        "diarize_detailed",
        lambda path, **kw: {
            "turns": [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"SPEAKER_00": [1.0, 0.0]},
        },
    )
    result = t._diarize_long(Path("x.clean.flac"), [])
    assert result["info"]["strategy"] == "chunked"


def test_audio_server_available_counts_any_http_answer(monkeypatch):
    """A 501 from the real server means "listening", not "unreachable"."""
    import urllib.error

    def fake_urlopen(req, timeout=2.0):
        raise urllib.error.HTTPError(req.full_url, 501, "Unsupported method", {}, None)

    monkeypatch.setattr(a.urllib.request, "urlopen", fake_urlopen)
    assert a.audio_server_available() is True


def test_speech_regions_come_from_word_gaps():
    words = [
        {"start": 0.0, "end": 0.4},
        {"start": 0.45, "end": 0.9},
        {"start": 1.0, "end": 1.4},   # 0.1 s gap -> same region
        {"start": 3.0, "end": 3.4},   # 1.6 s pause -> new region
        {"start": 3.45, "end": 3.9},
    ]
    regions = t._speech_regions_from_words(words)
    assert len(regions) == 2
    assert regions[0] == {"start": 0.0, "end": 1.4}
    assert regions[1] == {"start": 3.0, "end": 3.9}


def test_diarize_long_uses_word_gaps_not_the_model_vad(monkeypatch, chunk_env):
    """Chunk planning must not run a full-file VAD: the words already say where."""
    def boom(path):
        raise AssertionError("model VAD must not be called when words are supplied")

    monkeypatch.setattr(t, "vad", boom)
    monkeypatch.setattr(
        t,
        "diarize_detailed",
        lambda path, **kw: {
            "turns": [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"SPEAKER_00": [1.0, 0.0]},
        },
    )
    words = [{"start": float(i), "end": float(i) + 0.5} for i in range(0, 1800)]
    result = t._diarize_long(Path("x.clean.flac"), [], words=words)
    assert result["info"]["strategy"] == "chunked"


def test_transcribe_info_counts_words_not_lines(monkeypatch):
    """Coverage must be word-weighted: a paragraph outweighs a stray "It's"."""
    segments = [
        {"start": 0.0, "end": 3.0, "text": "one two three", "speaker": "SPEAKER_00"},
        {"start": 3.0, "end": 4.0, "text": "okay", "speaker": None},
    ]
    monkeypatch.setattr(t, "transcribe_audio", lambda *a, **kw: [dict(s) for s in segments])
    monkeypatch.setattr(t, "diarize_requested", lambda: True)
    monkeypatch.setattr(t, "record_segment", lambda **kw: None)
    monkeypatch.setattr(
        t, "_diarize_long", lambda *a, **kw: {"turns": [], "info": {"strategy": "single", "chunks": 1}}
    )
    monkeypatch.setattr(t, "assign_speakers_by_words", lambda segs, turns: segs)

    info: dict = {}
    t._transcribe(Path("x.mp3"), Path("x.clean.flac"), "x", [], info=info)
    assert info["words"] == 4
    assert info["labelled"] == 3
    assert info["segments"] == 2
    assert info["labelled_segments"] == 1
