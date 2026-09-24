"""Interview-guide alignment post-pass (opt-in, deterministic).

An interview transcript's speaker labels are only as good as pyannote's clusters,
which flip under crosstalk and can collapse to a single speaker. The interview
*guide* is a deterministic prior that fixes both problems without re-transcription:
its questions are (nearly) verbatim in the audio, so aligning them to the
transcript lets us force question spans to the interviewer, everything between to
the participant, and insert guide questions the transcript dropped.

This module is deliberately small and offline: alignment is fuzzy **token
overlap** over normalised Norwegian tokens (no embeddings, no LLM, no model
server). It parses the guide by its ``##`` headings — the guide's auto-numbering
is broken (5→7, 1→3→5), so numbers are ignored and the heading is the only
reliable section marker.

Example::

    import converter.guide as guide

    questions = guide.parse_guide(Path("interviews/questions/norwegian-questions.md").read_text())
    fixed = guide.apply_guide(segments, questions)
"""
from __future__ import annotations

import re
from pathlib import Path

INTERVIEWER = "Interviewer"
PARTICIPANT = "Participant"

_QUESTION_RE = re.compile(r"^(\d+\.|-)\s+(.+)$")
_WORD_RE = re.compile(r"[a-zæøå0-9]+")

# Below this token-overlap score a guide question is considered absent from the
# transcript and slated for insertion.
_MIN_ALIGN_OVERLAP = 0.3


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def parse_guide(text: str) -> list[dict]:
    """Parse an interview guide into ``[{section, question}]`` by heading.

    Sections come from ``## Heading`` lines; questions are the numbered items
    and the bullet follow-ups that read as questions (ending with ``?``). Role
    options and other non-question bullets are ignored. Guide order is preserved.
    """
    questions: list[dict] = []
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            continue
        m = _QUESTION_RE.match(stripped)
        if m is None or section is None:
            continue
        kind, body = m.group(1), m.group(2).strip()
        if kind == "-" and not body.rstrip().endswith("?"):
            continue
        if body:
            questions.append({"section": section, "question": body})
    return questions


def align_questions(
    questions: list[dict],
    segments: list[dict],
    min_overlap: float = _MIN_ALIGN_OVERLAP,
) -> list[dict]:
    """Fuzzy-align each guide question to a transcript segment.

    Returns ``[{question, section, segment_index, score}]`` in guide order; a
    question with no match above ``min_overlap`` has ``segment_index = None``.
    The score is Jaccard similarity of normalised tokens, which is robust to the
    interviewer paraphrasing the scripted question.
    """
    seg_tokens = [_tokens(s["text"]) for s in segments]
    aligned: list[dict] = []
    for q in questions:
        q_tokens = _tokens(q["question"])
        best_idx: int | None = None
        best_score = 0.0
        for i, st in enumerate(seg_tokens):
            if not st:
                continue
            union = q_tokens | st
            if not union:
                continue
            score = len(q_tokens & st) / len(union)
            if score > best_score:
                best_idx, best_score = i, score
        aligned.append(
            {
                "question": q["question"],
                "section": q["section"],
                "segment_index": best_idx if best_score >= min_overlap else None,
                "score": best_score,
            }
        )
    return aligned


def apply_guide(segments: list[dict], questions: list[dict]) -> list[dict]:
    """Re-attribute segments by the guide and insert missing questions.

    Returns a new list (input not mutated). Segments matched to a guide question
    are labelled :data:`INTERVIEWER`; everything between is :data:`PARTICIPANT`.
    Guide questions with no match are inserted verbatim (as interviewer segments)
    before the next matched question's segment — or appended if they trail the
    last match — so a question the ASR folded away is recovered in guide order.
    """
    aligned = align_questions(questions, segments)
    matched = {a["segment_index"] for a in aligned if a["segment_index"] is not None}

    result: list[dict] = []
    for i, seg in enumerate(segments):
        result.append({**seg, "speaker": INTERVIEWER if i in matched else PARTICIPANT})

    # Walk guide order: collect missing questions, flush them before the next
    # matched question's segment.
    pending: list[str] = []
    inserts: list[tuple[int, list[str]]] = []
    for a in aligned:
        if a["segment_index"] is None:
            pending.append(a["question"])
        elif pending:
            inserts.append((a["segment_index"], pending))
            pending = []

    for idx, qs in sorted(inserts, key=lambda x: -x[0]):
        for q in reversed(qs):
            result.insert(
                idx,
                {"start": segments[idx]["start"], "end": segments[idx]["start"],
                 "text": q, "speaker": INTERVIEWER},
            )

    if pending:
        end = result[-1]["end"] if result else 0.0
        for q in pending:
            result.append({"start": end, "end": end, "text": q, "speaker": INTERVIEWER})
    return result


def load_guide(path: str | Path) -> list[dict]:
    """Parse the guide at ``path`` into questions (see :func:`parse_guide`)."""
    return parse_guide(Path(path).read_text(encoding="utf-8"))
