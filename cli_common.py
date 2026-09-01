"""Shared command-line helpers for the ``ptm`` and ``ptm-start`` entry points.

Both commands expose the AI capabilities (vision, classifier gate, markdown
restructure, and the RAG summary) as simple flags, mapped onto the same
environment variables the converter already reads. This module deliberately
does **not** import ``converter`` (or ``gui``): the converter reads its
configuration at import time, so callers must apply the environment variables
*first*, then import the converter.
"""
from __future__ import annotations

import argparse
import os

# Map a flag -> (env var, value). ``--classify`` implies ``--vision``, so
# ``apply_ai_env`` sets the prerequisite env var whenever the dependent flag is
# given.
AI_FLAGS: dict[str, tuple[str, str]] = {
    "vision": ("VISION_ENABLED", "1"),
    "classify": ("VISION_CLASSIFY_ENABLED", "1"),
    "interpret": ("INTERPRET_ENABLED", "1"),
    "format": ("FORMAT_ENABLED", "1"),
    "summary": ("SUMMARY_ENABLED", "1"),
}

# ``--all`` enables the core slide passes only — not audio transcription, which
# is a separate command (``ptm-transcribe``) and needs an audio file to exist
# plus the ffmpeg/mlx-whisper toolchain.
_ALL_FLAGS = ("vision", "classify", "interpret", "format", "summary")


def add_ai_flags(parser: argparse.ArgumentParser) -> None:
    """Add the AI-capability flags to ``parser`` (a shared argument group)."""
    group = parser.add_argument_group("AI capabilities (default: all off)")
    group.add_argument(
        "--vision",
        action="store_true",
        help="enable the vision transcription post-pass",
    )
    group.add_argument(
        "--classify",
        action="store_true",
        help="enable the classifier gate (implies --vision)",
    )
    group.add_argument(
        "--interpret",
        action="store_true",
        help="enable the grounded diagram-interpretation pass",
    )
    group.add_argument(
        "--format",
        action="store_true",
        help="enable the LLM markdown-restructure pass",
    )
    group.add_argument(
        "--summary",
        action="store_true",
        help="enable the per-presentation RAG summary pass",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="enable every slide AI pass (vision + classify + interpret + format + summary)",
    )
    group.add_argument(
        "--env",
        action="append",
        metavar="KEY=VALUE",
        help="set an arbitrary environment variable (repeatable); e.g. "
        "--env VISION_MODEL=... or --env VISION_LOG_DB=/tmp/ptm.sqlite",
    )
    pdf = parser.add_argument_group("PDF layout")
    pdf.add_argument(
        "--paper",
        action="store_true",
        help="treat PDFs as multi-column paper documents (whitepapers, academic papers)",
    )
    pdf.add_argument(
        "--slide",
        action="store_true",
        help="treat PDFs as slide decks (default)",
    )


def ai_env_vars(args: argparse.Namespace) -> dict[str, str]:
    """Return the ``{env var: value}`` mapping implied by ``args``.

    Does not mutate the environment; :func:`apply_ai_env` applies the result.
    """
    env: dict[str, str] = {}

    enabled: set[str] = set()
    if getattr(args, "all", False):
        enabled.update(_ALL_FLAGS)
    for flag in AI_FLAGS:
        if getattr(args, flag, False):
            enabled.add(flag)

    if "classify" in enabled:
        enabled.add("vision")

    for flag in enabled:
        key, value = AI_FLAGS[flag]
        env[key] = value

    paper = getattr(args, "paper", False)
    slide = getattr(args, "slide", False)
    if paper and slide:
        raise SystemExit("--paper and --slide are mutually exclusive")
    if paper:
        env["PDF_MODE"] = "paper"
    elif slide:
        env["PDF_MODE"] = "slide"

    for item in getattr(args, "env", None) or []:
        if "=" not in item:
            raise SystemExit(f"invalid --env value {item!r} (expected KEY=VALUE)")
        key, value = item.split("=", 1)
        env[key.strip()] = value

    return env


def apply_ai_env(args: argparse.Namespace) -> dict[str, str]:
    """Set the mapped environment variables and return the applied mapping.

    Must be called before importing ``converter`` or ``gui``.
    """
    env = ai_env_vars(args)
    os.environ.update(env)
    return env
