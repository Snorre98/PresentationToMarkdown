"""``ptm-start`` — launch the desktop GUI with AI capabilities toggled by flags.

The GUI reads the AI configuration from environment variables at import time, so
this entry point parses the flags, applies them to ``os.environ``, and only then
imports ``gui``.
"""
from __future__ import annotations

import argparse

from cli_common import add_ai_flags, apply_ai_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptm-start",
        description="Launch the Presentation-to-Markdown GUI, optionally enabling AI passes.",
    )
    add_ai_flags(parser)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    apply_ai_env(args)

    from gui import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
