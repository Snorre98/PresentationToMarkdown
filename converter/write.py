"""Writer-role model defaults shared by the rewrite passes (ADR-0016).

The AI passes split into two roles:

- **Reader** — the OCR role: ``VISION_*`` (verbatim transcription) and
  ``VISION_CLASSIFY_*`` (the cheap gate). These read images losslessly and are
  allowed to be an OCR specialist (e.g. ``glm-ocr``).
- **Writer** — the rewrite role: ``FORMAT_*`` / ``STRUCTURE_*`` / ``INTERPRET_*``
  / ``SUMMARY_*``. These restructure, interpret, and summarize prose, and must
  stay on a capable instruction model — never an OCR specialist.

This module owns the ``WRITE_*`` default that every writer pass cascades to, so
re-pointing the reader (``VISION_MODEL``) no longer silently re-points the
rewriters. By default the writer is the vision transcriber server
(Qwen2.5-VL-7B on ``:8081``), which keeps the all-default configuration
byte-identical to before the split.

Configuration (environment variables):

- ``WRITE_BASE_URL`` — writer server base URL, default ``http://127.0.0.1:8081/v1``.
- ``WRITE_MODEL`` — writer model id, default ``mlx-community/Qwen2.5-VL-7B-Instruct-4bit``
  (a VLM, so ``structure``'s image regime needs no extra server).
- ``WRITE_API_KEY`` — optional bearer token (not needed for local servers).
"""
from __future__ import annotations

import os

WRITE_BASE_URL = os.environ.get("WRITE_BASE_URL", "http://127.0.0.1:8081/v1")
WRITE_MODEL = os.environ.get("WRITE_MODEL", "mlx-community/Qwen2.5-VL-7B-Instruct-4bit")
WRITE_API_KEY = os.environ.get("WRITE_API_KEY") or None
