"""Read-only web dashboard package for the PresentationToMarkdown conversion log.

A dependency-light Flask app that serves an auto-refreshing view of
``ptm.sqlite`` as written by :mod:`converter.logstore` (ADR-0014, superseded by
ADR-0022 which adds Flask and conversion-level run telemetry). It opens the
database READ-ONLY and never imports ``converter``, so it cannot interfere with
a running conversion.

Usage::

    ./.venv/bin/python -m dashboard            # default ptm.sqlite, port 8080
    ptm-dashboard --db /path/to/ptm.sqlite --port 9000 --host 0.0.0.0
"""
from __future__ import annotations

from dashboard.app import create_app, main

__all__ = ["create_app", "main"]
