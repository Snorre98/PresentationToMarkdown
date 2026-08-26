"""Stub audio-model service (diarization + enhancement + dereverb + isolate) for testing.

Implements the same ``POST /v1/diarize``, ``POST /v1/enhance``,
``POST /v1/dereverb`` and ``POST /v1/isolate`` contracts as the real service
(``scripts/audio_server.py``) without PyTorch, so all paths can be exercised
end-to-end. Diarization fabricates alternating speaker turns spanning the audio
duration (via ``ffprobe``); enhancement/dereverb/isolation copy the input to the
output.

Usage::

    ./.venv/bin/python scripts/stub_audio_server.py --port 8083
"""
from __future__ import annotations

import argparse
import json
import shutil
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

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return None

    def do_POST(self):  # noqa: N802 - stdlib handler method name
        route = self.path.rstrip("/")
        if route == "/v1/diarize":
            self._handle_diarize()
        elif route == "/v1/enhance":
            self._handle_enhance()
        elif route == "/v1/dereverb":
            self._handle_copy()
        elif route == "/v1/isolate":
            self._handle_copy()
        else:
            self._send(404, {"error": "not found"})

    def _handle_diarize(self):
        req = self._read_json()
        if req is None:
            return
        path = req.get("path")
        if not path:
            self._send(400, {"error": "missing 'path'"})
            return
        turns = _speaker_turns(
            _audio_duration(path), req.get("min_speakers"), req.get("max_speakers")
        )
        self._send(200, turns)

    def _handle_enhance(self):
        req = self._read_json()
        if req is None:
            return
        path = req.get("path")
        output = req.get("output")
        if not path or not output:
            self._send(400, {"error": "missing 'path' or 'output'"})
            return
        try:
            shutil.copyfile(path, output)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})
            return
        self._send(200, {"ok": True})

    def _handle_copy(self):
        # ``/v1/dereverb`` and ``/v1/isolate`` share the same copy semantics.
        req = self._read_json()
        if req is None:
            return
        path = req.get("path")
        output = req.get("output")
        if not path or not output:
            self._send(400, {"error": "missing 'path' or 'output'"})
            return
        try:
            shutil.copyfile(path, output)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})
            return
        self._send(200, {"ok": True})

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
    parser = argparse.ArgumentParser(description="Stub audio-model server for testing.")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"stub audio server on http://{args.host}:{args.port}/v1/{{diarize,enhance,dereverb,isolate}}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
