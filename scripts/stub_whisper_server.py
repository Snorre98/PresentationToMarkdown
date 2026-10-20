"""Stub whisper daemon (OpenAI transcription contract) for testing.

Implements the subset of ``mlx_audio.server`` that ``converter.audio`` uses,
without MLX or model weights, so the full Multipart → verbose_json path can be
exercised hermetically and in a manual TUI smoke:

- ``GET  /v1/models``                      → the loaded model ids (health probe)
- ``POST /v1/models?model_name=<repo>``    → mark loaded (the ``warm`` verb)
- ``DELETE /v1/models?model_name=<repo>``  → unload
- ``POST /v1/audio/transcriptions``        → multipart verbose_json with
  ``segments`` and, when ``word_timestamps`` is true, ``segments[*].words``

Usage::

    ./.venv/bin/python scripts/stub_whisper_server.py --port 8100
    WHISPER_BASE_URL=http://127.0.0.1:8100/v1 scripts/ptm-tui.sh

The fabricated timings span ``--duration`` seconds (default 4.0): one segment
per second, two words per segment. The uploaded bytes are ignored.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_SEGMENT_SECONDS = 1.0
_WORDS_PER_SEGMENT = 2


def _multipart_fields(body: bytes, boundary: str) -> dict[str, bytes]:
    """Parse a multipart/form-data body into ``{field name: raw bytes}``."""
    delimiter = b"--" + boundary.encode("utf-8")
    fields: dict[str, bytes] = {}
    for part in body.split(delimiter):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = None
        for line in head.decode("utf-8", errors="replace").splitlines():
            if line.lower().startswith("content-disposition:"):
                for token in line.split(";"):
                    token = token.strip()
                    if token.startswith("name="):
                        name = token[len("name=") :].strip('"')
        if name:
            # Each part keeps the CRLF that preceded the next boundary.
            fields[name] = data[:-2] if data.endswith(b"\r\n") else data
    return fields


def _payload(duration: float, language: str | None, word_timestamps: bool) -> dict:
    """Fabricate the verbose_json body the real server returns."""
    segments: list[dict] = []
    texts: list[str] = []
    index = 0
    t = 0.0
    while t < duration:
        end = min(t + _SEGMENT_SECONDS, duration)
        text = f"segment {index}"
        texts.append(text)
        segment: dict = {"id": index, "start": t, "end": end, "text": text}
        if word_timestamps:
            words: list[dict] = []
            step = (end - t) / _WORDS_PER_SEGMENT
            for w in range(_WORDS_PER_SEGMENT):
                w_start = t + w * step
                words.append(
                    {
                        "word": f"word {index}.{w}",
                        "start": round(w_start, 3),
                        "end": round(min(w_start + step, end), 3),
                        "probability": 1.0,
                    }
                )
            segment["words"] = words
        segments.append(segment)
        t = end
        index += 1
    return {
        "task": "transcribe",
        "language": language or "en",
        "text": " ".join(texts),
        "segments": segments,
    }


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_model(self) -> str:
        query = parse_qs(urlsplit(self.path).query)
        return (query.get("model_name") or [""])[0]

    def do_GET(self):  # noqa: N802 - stdlib handler method name
        if urlsplit(self.path).path.rstrip("/") != "/v1/models":
            self._send(404, {"detail": "not found"})
            return
        loaded = sorted(getattr(self.server, "loaded", set()))
        self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in loaded]})

    def do_POST(self):  # noqa: N802 - stdlib handler method name
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/v1/models":
            model = self._query_model()
            if not model:
                self._send(400, {"detail": "model_name query parameter is required"})
                return
            self.server.loaded.add(model)  # type: ignore[attr-defined]
            self._send(200, {"status": "success", "message": f"Model {model} added successfully"})
            return
        if path == "/v1/audio/transcriptions":
            self._handle_transcription()
            return
        self._send(404, {"detail": "not found"})

    def do_DELETE(self):  # noqa: N802 - stdlib handler method name
        if urlsplit(self.path).path.rstrip("/") != "/v1/models":
            self._send(404, {"detail": "not found"})
            return
        model = self._query_model()
        loaded = getattr(self.server, "loaded", set())
        if model in loaded:
            loaded.discard(model)
            self.send_response(204)
            self.end_headers()
            return
        self._send(404, {"detail": f"Model '{model}' not found"})

    def _handle_transcription(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        boundary = None
        for token in ctype.split(";"):
            token = token.strip()
            if token.startswith("boundary="):
                boundary = token[len("boundary=") :].strip('"')
        if not boundary:
            self._send(400, {"error": "missing multipart boundary"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        fields = _multipart_fields(body, boundary)
        model = fields.get("model", b"").decode("utf-8", "replace")
        if not model:
            self._send(400, {"error": "missing 'model'"})
            return
        if "file" not in fields:
            self._send(400, {"error": "missing 'file'"})
            return
        language = fields.get("language", b"").decode("utf-8", "replace") or None
        words = fields.get("word_timestamps", b"false").decode("utf-8", "replace").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.server.loaded.add(model)  # type: ignore[attr-defined]
        self._send(200, _payload(self.server.duration, language, words))  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: N802 - quiet the default logging
        pass


def make_server(port: int = 0, duration: float = 4.0) -> ThreadingHTTPServer:
    """Build (and bind) a stub server, ready to ``serve_forever``.

    ``port=0`` picks a free ephemeral port; read it from ``server_address``.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.daemon_threads = True
    httpd.loaded = set()  # type: ignore[attr-defined]
    httpd.duration = duration  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="Stub whisper daemon for testing.")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--duration", type=float, default=4.0)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    httpd.daemon_threads = True
    httpd.loaded = set()
    httpd.duration = args.duration
    print(f"stub whisper daemon on http://{args.host}:{args.port}/v1")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
