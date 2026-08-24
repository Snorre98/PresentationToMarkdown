"""Unit tests for the transcription quality gate and classifier category parsing."""
from converter.base import _table_to_md
from converter.classify import _blockquote, _looks_like_content, _parse_category
from converter.vision import (
    _laplacian_variance,
    bullet_item_count,
    image_readable,
    transcription_quality,
)


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


def test_quality_rejects_enumerated_loop():
    md = "# Workflow Diagram\n\n- **Main Components:**\n" + "\n".join(
        f"  - **Data Source {i}**" for i in range(1, 112)
    )
    assert transcription_quality(md) == "repetitive"


def test_quality_rejects_placeholder_ellipsis():
    md = (
        "```markdown\n# Title\n\n"
        "- **Rules:** The rules of the game are...\n"
        "- **Objective:** The objective of the game is...\n"
        "```"
    )
    assert transcription_quality(md) == "placeholder"


def test_quality_rejects_placeholder_bracket():
    md = "- **Represents:** A process flow for [specific process]"
    assert transcription_quality(md) == "placeholder"


def test_image_readable_low_resolution():
    assert image_readable(b"", "png", width=100, height=100) == "low resolution"
    assert image_readable(b"", "png", width=249, height=300) == "low resolution"


def test_image_readable_ok_resolution_fails_open_on_decode():
    assert image_readable(b"", "png", width=400, height=300) is None


def test_laplacian_variance_sharp_vs_flat():
    w = h = 16
    sharp = [255 if (x + y) % 2 == 0 else 0 for y in range(h) for x in range(w)]
    flat = [128] * (w * h)
    assert _laplacian_variance(sharp, w, h) > _laplacian_variance(flat, w, h)
    assert _laplacian_variance(flat, w, h) == 0.0


def test_bullet_item_count():
    assert bullet_item_count("A process flow.\n- one\n  - nested\n- two") == 3
    assert bullet_item_count("plain prose, no lists") == 0


def test_blockquote_wraps_lines():
    assert _blockquote("A process flow.\n\nIt shows X to Y.") == (
        "> A process flow.\n>\n> It shows X to Y."
    )


def test_table_to_md_handles_none_cells():
    md = _table_to_md([[None, "b"], ["c", "d"]])
    assert "|  | b |" in md[0]
    assert "| c | d |" in md[2]


def test_looks_like_content():
    assert _looks_like_content(1092, 397) is True
    assert _looks_like_content(300, 336) is False  # large but portrait-ish
    assert _looks_like_content(100, 100) is False  # small
    assert _looks_like_content(None, None) is False


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
