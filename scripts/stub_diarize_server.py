"""Stub speaker-diarization service for testing the audio pass.

Implements the same ``POST /v1/diarize`` contract as the real pyannote service
(``scripts/diarize_server.py``) without PyTorch, so the ``--diarize`` path can be
exercised end-to-end. It fabricates alternating speaker turns spanning the audio
file's duration (detected with ``ffprobe``; defaults to 120 s when unavailable).

Usage::

    ./.venv/bin/python scripts/stub_diarize_server.py --port 8083

Request/response contract (matches ``converter.audio.diarize``)::

    POST /v1/diarize
    {"path": "/abs/lecture.mp3", "min_speakers": 1, "max_speakers": 2}
    -> [{"start": 0.0, "end": 8.0, "speaker": "SPEAKER_00"}, ...]
"""
from __future__ import annotations

import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TURN_SECONDS = 8.0
_DEFAULT_DURATION = 120.0


def _audio_duration(path: str) -> float:
    """Return the audio duration in seconds via ffprobe, or a default."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(proc.stdout.strip())
    except Exception:
        return _DEFAULT_DURATION


def _speaker_turns(duration: float, min_speakers: int | None, max_speakers: int | None) -> list[dict]:
    n = max_speakers or min_speakers or 2
    n = max(1, int(n))
    turns: list[dict] = []
    t = 0.0
    i = 0
    while t < duration:
        end = min(t + _TURN_SECONDS, duration)
        turns.append({"start": t, "end": end, "speaker": f"SPEAKER_{i % n:02d}"})
        t = end
        i += 1
    return turns


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
        turns = _speaker_turns(
            _audio_duration(path),
            req.get("min_speakers"),
            req.get("max_speakers"),
        )
        self._send(200, turns)

    def log_message(self, fmt, *args):  # noqa: N802 - quiet the default logging
        pass


def make_server(port: int = 0) -> ThreadingHTTPServer:
    """Build (and bind) a server, ready to ``serve_forever``.

    ``port=0`` picks a free ephemeral port; read it from ``server_address``.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="Stub diarization server for testing.")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"stub diarization server on http://{args.host}:{args.port}/v1/diarize")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
