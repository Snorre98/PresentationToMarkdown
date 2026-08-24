"""Unit tests for the transcription quality gate and classifier category parsing."""
from converter.classify import _parse_category
from converter.vision import transcription_quality


def _loop(label: str, depth: int, count: int) -> str:
    return "\n".join("  " * depth + f"- **{label}**" for _ in range(count))


def test_quality_accepts_normal_transcription():
    md = "\n".join(
        [
            "# Process overview",
            "",
            "- Identify a case to model",
            "- Build the model",
            "- Evaluate the model",
        ]
    )
    assert transcription_quality(md) is None


def test_quality_rejects_empty():
    assert transcription_quality("") == "empty"
    assert transcription_quality("   \n\n  ") == "empty"


def test_quality_rejects_excessive_nesting():
    md = _loop("End process (pink)", 50, 5)
    assert transcription_quality(md) == "excessive nesting"


def test_quality_rejects_repetition_loop():
    md = _loop("Start process (pink)", 1, 30)
    assert transcription_quality(md) == "repetitive"


def test_quality_rejects_runaway_length():
    md = "\n".join(f"- item {i}" for i in range(400))
    assert transcription_quality(md) == "runaway length"


def test_quality_rejects_low_information():
    md = " ".join(["process"] * 60)
    assert transcription_quality(md) == "low information"


def test_parse_category_decorative():
    assert _parse_category("DECORATIVE") == "decorative"
    assert _parse_category("PHOTOGRAPH of a person") == "decorative"
    assert _parse_category("a logo") == "decorative"


def test_parse_category_diagram():
    assert _parse_category("DIAGRAM") == "diagram"
    assert _parse_category("a flowchart") == "diagram"
    assert _parse_category("conceptual figure") == "diagram"


def test_parse_category_text():
    assert _parse_category("TEXT") == "text"
    assert _parse_category("a table of data") == "text"


def test_parse_category_defaults_to_decorative():
    assert _parse_category("gibberish") == "decorative"
    assert _parse_category("") == "decorative"
