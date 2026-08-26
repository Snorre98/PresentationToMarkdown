"""Speaker-diarization service (pyannote) for the audio pass.

PyTorch is deliberately kept out of ``converter`` (ADR-0006), so speaker
labelling runs here, in its own venv. The converter talks to it via
``converter.audio.diarize``. One-time setup (see ``docs/runbook.md``):

- accept the gated terms for ``pyannote/speaker-diarization-3.1`` and
  ``pyannote/segmentation-3.0``, then create a read token at
  https://huggingface.co/settings/tokens.

Run::

    python3.12 -m venv ~/tools/diarize-env
    ~/tools/diarize-env/bin/pip install pyannote.audio torch torchaudio
    HF_TOKEN=hf_... ~/tools/diarize-env/bin/python scripts/diarize_server.py --port 8083

Contract (matches ``converter.audio.diarize``)::

    POST /v1/diarize
    {"path": "/abs/lecture.mp3", "min_speakers": 1, "max_speakers": 4}
    -> [{"start": 0.0, "end": 9.2, "speaker": "SPEAKER_00"}, ...]
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("PYANNOTE_MODEL", "pyannote/speaker-diarization-3.1")
HF_TOKEN = os.environ.get("HF_TOKEN")

_pipeline = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from pyannote.audio import Pipeline

        try:
            _pipeline = Pipeline.from_pretrained(MODEL, token=HF_TOKEN)
        except TypeError:
            # Older pyannote used `use_auth_token` instead of `token`.
            _pipeline = Pipeline.from_pretrained(MODEL, use_auth_token=HF_TOKEN)
    return _pipeline


def _run_diarization(path: str, min_speakers, max_speakers) -> list[dict]:
    pipeline = _get_pipeline()
    kwargs: dict = {}
    if min_speakers is not None:
        kwargs["min_speakers"] = int(min_speakers)
    if max_speakers is not None:
        kwargs["max_speakers"] = int(max_speakers)
    diarization = pipeline(path, **kwargs)
    return [
        {"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)}
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - stdlib handler method name
        if self.path.rstrip("/") != "/v1/diarize":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return
        path = req.get("path")
        if not path:
            self._send(400, {"error": "missing 'path'"})
            return
        try:
            turns = _run_diarization(
                path, req.get("min_speakers"), req.get("max_speakers")
            )
        except Exception as exc:  # noqa: BLE001 - report a clean 500
            self._send(500, {"error": str(exc)})
            return
        self._send(200, turns)

    def log_message(self, fmt, *args):  # noqa: N802 - quiet the default logging
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="pyannote speaker-diarization service.")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not HF_TOKEN:
        print("HF_TOKEN is not set; gated model download will fail.", flush=True)

    # Load the pipeline eagerly so misconfiguration fails fast at startup.
    _get_pipeline()

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"diarization server on http://{args.host}:{args.port}/v1/diarize")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
