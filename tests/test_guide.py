"""Tests for the interview-guide alignment pass (``converter.guide``).

Deterministic and offline: parsing by heading (the guide's auto-numbering is
broken), fuzzy token-overlap alignment, and interviewer/participant
re-attribution plus verbatim insertion of dropped questions.
"""
from __future__ import annotations

from converter import guide as g

GUIDE = """# Intervjuguide

## Bakgrunnsspørsmål

1. Hvor gammel er du?

3. Har du deltatt i Fadderuka før?

## Informasjon og kommunikasjon

1. Hvordan fant du vanligvis ut av hva som skulle skje?

    - Hva fungerte bra?
    - Hva fungerte ikke bra?
    - Fadderbarn
"""


def test_parse_guide_by_heading():
    questions = g.parse_guide(GUIDE)
    texts = [q["question"] for q in questions]
    assert "Hvor gammel er du?" in texts
    assert "Har du deltatt i Fadderuka før?" in texts  # numbered 3, heading-derived
    assert "Hva fungerte bra?" in texts
    assert "Hva fungerte ikke bra?" in texts
    assert "Fadderbarn" not in texts  # non-question bullet excluded
    sections = {q["section"] for q in questions}
    assert {"Bakgrunnsspørsmål", "Informasjon og kommunikasjon"} <= sections


def test_align_questions_matches_paraphrase():
    segments = [
        {"start": 0.0, "end": 2.0, "text": "Hvor gammel er du?"},
        {"start": 2.0, "end": 4.0, "text": "Jeg er 22 år."},
        {"start": 4.0, "end": 6.0, "text": "Hva føler du ikke funker bra?"},
    ]
    aligned = g.align_questions(
        [{"section": "X", "question": "Hva fungerte ikke bra?"}], segments
    )
    assert aligned[0]["segment_index"] == 2  # "fungerte ikke bra" ~ "føler ... funker ... bra"


def test_apply_guide_reattributes_and_inserts_missing():
    segments = [
        {"start": 0.0, "end": 2.0, "text": "Hvor gammel er du?"},
        {"start": 2.0, "end": 4.0, "text": "Jeg er 22 år."},
        {"start": 4.0, "end": 6.0, "text": "Jeg er student ved NTNU."},
    ]
    questions = [
        {"section": "B", "question": "Hvor gammel er du?"},
        {"section": "B", "question": "Hva er ditt favorittfag?"},
        {"section": "B", "question": "Er du student ved NTNU?"},
    ]
    out = g.apply_guide(segments, questions)
    texts = [s["text"] for s in out]
    speakers = [s["speaker"] for s in out]
    # Question 1 matched seg 0; question 3 matched seg 2; question 2 missing ->
    # inserted verbatim between them.
    assert texts[0] == "Hvor gammel er du?" and speakers[0] == g.INTERVIEWER
    assert "Hva er ditt favorittfag?" in texts
    assert texts.index("Hva er ditt favorittfag?") < texts.index("Jeg er student ved NTNU.")
    # segment 2 matched q3 -> re-labelled interviewer, ASR text preserved.
    assert speakers[texts.index("Jeg er student ved NTNU.")] == g.INTERVIEWER
    # the participant's answer is labelled as such.
    assert texts[1] == "Jeg er 22 år." and speakers[1] == g.PARTICIPANT


def test_apply_guide_input_not_mutated():
    segments = [
        {"start": 0.0, "end": 2.0, "text": "Hvor gammel er du?", "speaker": "SPEAKER_00"},
    ]
    questions = [{"section": "B", "question": "Hvor gammel er du?"}]
    g.apply_guide(segments, questions)
    assert segments[0]["speaker"] == "SPEAKER_00"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
