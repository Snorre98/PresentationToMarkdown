"""Unit tests for the grounded diagram-interpretation pass (converter.interpret)."""
import pytest

from converter import interpret


@pytest.fixture(autouse=True)
def _clear_cache():
    interpret._readability_cache.clear()
    yield


def _noop_record(**kw):
    return None


LABELS = [
    "Goal 3.5",
    "Decrease the recruitment of temporary workers at peak times",
    "Constraint 1",
    "The existence of statutory regulations for maximum overtime",
    "hinders",
    "Goal 3.1",
    "Reduce the maintenance costs of machines",
    "Opportunity 10",
    "Outsource maintenance to external supplier",
    "supports",
]


def test_disabled(monkeypatch):
    monkeypatch.setattr(interpret, "INTERPRET_ENABLED", False)
    assert interpret.interpret_diagram(b"\x89PNG\r\n\x1a\nfake", LABELS) is None


def test_no_labels(monkeypatch):
    monkeypatch.setattr(interpret, "INTERPRET_ENABLED", True)
    assert interpret.interpret_diagram(b"\x89PNG\r\n\x1a\nfake", []) is None


def test_happy_path(monkeypatch):
    monkeypatch.setattr(interpret, "INTERPRET_ENABLED", True)
    monkeypatch.setattr(interpret, "image_readable", lambda *a, **kw: None)
    monkeypatch.setattr(interpret, "record", _noop_record)
    monkeypatch.setattr(
        interpret,
        "_chat_completion",
        lambda messages, **kw: (
            "Constraint 1 | hinders | Goal 3.5\n"
            "Opportunity 10 | supports | Goal 3.1\n"
            "\n"
            "Meaning: Overtime rules block cutting peak-time temps."
        ),
    )
    out = interpret.interpret_diagram(b"\x89PNG\r\n\x1a\nfake", LABELS, width=1000, height=1000)
    assert "`Constraint 1` —`hinders`→ `Goal 3.5`" in out
    assert "`Opportunity 10` —`supports`→ `Goal 3.1`" in out
    assert "Overtime rules block cutting peak-time temps." in out


def test_ungrounded_statements_rejected(monkeypatch):
    monkeypatch.setattr(interpret, "INTERPRET_ENABLED", True)
    monkeypatch.setattr(interpret, "image_readable", lambda *a, **kw: None)
    monkeypatch.setattr(interpret, "record", _noop_record)
    monkeypatch.setattr(
        interpret,
        "_chat_completion",
        lambda messages, **kw: "Fabricated Node | supports | Goal 3.5\n\nMeaning: something.",
    )
    warnings: list[str] = []
    out = interpret.interpret_diagram(b"\x89PNG\r\n\x1a\nfake", LABELS, warnings=warnings, width=1000, height=1000)
    assert out is None
    assert any("no grounded" in w for w in warnings)


def test_parse_grounds_and_extracts_meaning():
    reply = (
        "Constraint 1 | hinders | Goal 3.5\n"
        "MADE UP | supports | Goal 3.1\n"
        "\n"
        "Meaning: Overtime rules block cutting peak-time temps.\n"
        "Second sentence continues."
    )
    statements, meaning = interpret._parse(reply, LABELS)
    assert statements == [("Constraint 1", "hinders", "Goal 3.5")]
    assert meaning == "Overtime rules block cutting peak-time temps. Second sentence continues."


def test_matches_normalizes_and_substring():
    assert interpret._matches("  Goal 3.5 ", "Goal 3.5")
    assert interpret._matches("Decrease the recruitment of temporary workers", "Decrease the recruitment of temporary workers at peak times")
    assert not interpret._matches("Completely unrelated", "Goal 3.5")


def test_quality_gate_rejects_repetitive(monkeypatch):
    monkeypatch.setattr(interpret, "INTERPRET_ENABLED", True)
    monkeypatch.setattr(interpret, "image_readable", lambda *a, **kw: None)
    monkeypatch.setattr(interpret, "record", _noop_record)
    monkeypatch.setattr(
        interpret,
        "_chat_completion",
        lambda messages, **kw: "\n".join(["Goal 3.5 | hinders | Goal 3.5"] * 20),
    )
    warnings: list[str] = []
    out = interpret.interpret_diagram(b"\x89PNG\r\n\x1a\nfake", LABELS, warnings=warnings, width=1000, height=1000)
    assert out is None
    assert any("low-value" in w for w in warnings)
