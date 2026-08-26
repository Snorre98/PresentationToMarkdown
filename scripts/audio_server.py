"""Audio-model service (diarization + enhancement + dereverb + isolation).

PyTorch models are deliberately kept out of ``converter`` (ADR-0006/0008/0010),
so speaker labelling, speech enhancement (DeepFilterNet), dereverberation (WPE)
and voice isolation (SepFormer) run here, in their own venv. The converter talks
to it via ``converter.audio``.

One-time setup (see ``docs/runbook.md``):

- diarization: accept the gated terms for ``pyannote/speaker-diarization-3.1``
  and ``pyannote/segmentation-3.0``, then create a read token at
  https://huggingface.co/settings/tokens.
- enhancement: ``pip install deepfilternet`` (small, no gating).
- dereverberation: ``pip install nara_wpe`` (pure NumPy, no gating).
- isolation: ``pip install speechbrain`` (SepFormer, no gating).

Run::

    python3.12 -m venv ~/tools/audio-env
    ~/tools/audio-env/bin/pip install pyannote.audio torch torchaudio deepfilternet nara_wpe speechbrain
    HF_TOKEN=hf_... ~/tools/audio-env/bin/python scripts/audio_server.py --port 8083

Contract (matches ``converter.audio``)::

    POST /v1/diarize
    {"path": "/abs/lecture.flac", "min_speakers": 1, "max_speakers": 4}
    -> [{"start": 0.0, "end": 9.2, "speaker": "SPEAKER_00"}, ...]

    POST /v1/enhance
    {"path": "/abs/lecture.flac", "output": "/abs/lecture.clean.flac"}
    -> {"ok": true}

    POST /v1/dereverb
    {"path": "/abs/lecture.clean.flac", "output": "/abs/lecture.clean.flac"}
    -> {"ok": true}

    POST /v1/isolate
    {"path": "/abs/lecture.clean.flac", "output": "/abs/lecture.isolated.flac"}
    -> {"ok": true}
"""
from __future__ import annotations

import argparse
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIARIZE_MODEL = os.environ.get("PYANNOTE_MODEL", "pyannote/speaker-diarization-3.1")
HF_TOKEN = os.environ.get("HF_TOKEN")
ISOLATE_MODEL = os.environ.get("SEPARATOR_MODEL", "speechbrain/sepformer-whamr")
SEPARATOR_SAVEDIR = os.environ.get(
    "SPEECHBRAIN_SAVEDIR", os.path.join(os.path.expanduser("~/.cache"), "speechbrain")
)

_pipeline = None
_enhancer = None
_separator = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from pyannote.audio import Pipeline

        try:
            _pipeline = Pipeline.from_pretrained(DIARIZE_MODEL, token=HF_TOKEN)
        except TypeError:
            # Older pyannote used `use_auth_token` instead of `token`.
            _pipeline = Pipeline.from_pretrained(DIARIZE_MODEL, use_auth_token=HF_TOKEN)
    return _pipeline


def _get_enhancer():
    global _enhancer
    if _enhancer is None:
        from df.enhance import enhance as _df_enhance
        from df.enhance import init_df, load_audio, save_audio

        model, df_state, _ = init_df()
        _enhancer = (model, df_state, _df_enhance, load_audio, save_audio)
    return _enhancer


def _get_separator():
    global _separator
    if _separator is None:
        from speechbrain.inference.separation import SepformerSeparation

        os.makedirs(SEPARATOR_SAVEDIR, exist_ok=True)
        _separator = SepformerSeparation.from_hparams(
            source=ISOLATE_MODEL, savedir=SEPARATOR_SAVEDIR
        )
    return _separator


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


def _run_enhance(path: str, output: str) -> None:
    model, df_state, df_enhance, load_audio, save_audio = _get_enhancer()
    from df.io import resample

    # DeepFilterNet is full-band (48 kHz); upsample in, resample out for ASR.
    # ``load_audio`` returns ``(tensor, metadata)`` and ``save_audio`` does *not*
    # resample (it only writes the given ``sr`` into the header), so we must
    # resample back down explicitly — otherwise a 48 kHz tensor written under a
    # 16 kHz header comes out 3x the correct length.
    audio, _ = load_audio(path, sr=48000)
    enhanced = df_enhance(model, df_state, audio)
    save_audio(output, resample(enhanced, 48000, 16000), sr=16000)


def _run_dereverb(path: str, output: str) -> None:
    """Blindly dereverberate ``path`` (WPE) and write the 16 kHz result to ``output``."""
    import numpy as np
    import torch
    import torchaudio
    from nara_wpe import np_wpe as wpe
    from nara_wpe.utils import istft, stft

    waveform, sr = torchaudio.load(path)
    if sr != 16000:
        waveform = torchaudio.functional.resample(waveform, sr, 16000)
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)

    # WPE expects a (channels, samples) float array; STFT -> (freq, ch, frames).
    y = waveform.numpy().astype(np.float64)
    stft_kw = dict(size=512, shift=128)
    Y = stft(y, **stft_kw).transpose(2, 0, 1)
    Z = wpe(Y, taps=10, delay=3, iterations=5)
    z = istft(Z.transpose(1, 2, 0), **stft_kw)

    out = torch.from_numpy(z).float()  # (channels, samples)
    torchaudio.save(output, out, 16000)


def _run_isolate(path: str, output: str) -> None:
    """Separate the dominant voice (SepFormer) and write a 16 kHz result to ``output``.

    ``sepformer-whamr`` is trained on speech + noise + reverb, so it splits the
    mixture into two unordered streams; we pick the one with the higher RMS
    (best-effort — the "voice") and resample its native 8 kHz up to 16 kHz for ASR.
    """
    import torch
    import torchaudio

    separator = _get_separator()
    mix, sr = torchaudio.load(path)
    if sr != 8000:
        mix = torchaudio.functional.resample(mix, sr, 8000)
    if mix.shape[0] > 1:
        mix = mix.mean(dim=0, keepdim=True)

    est = separator.separate_batch(mix)  # (1, time, num_sources)
    sources = est[0]  # (time, num_sources)

    def _rms(signal: torch.Tensor) -> float:
        centered = signal - signal.mean()
        return float(centered.pow(2).mean().sqrt())

    idx = int(torch.argmax(torch.tensor([_rms(sources[:, i]) for i in range(sources.shape[1])])))
    voice = sources[:, idx].unsqueeze(0)  # (1, time)

    voice = torchaudio.functional.resample(voice, 8000, 16000)
    torchaudio.save(output, voice, 16000)


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
            self._handle_dereverb()
        elif route == "/v1/isolate":
            self._handle_isolate()
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
        try:
            turns = _run_diarization(path, req.get("min_speakers"), req.get("max_speakers"))
        except Exception as exc:  # noqa: BLE001 - report a clean 500
            traceback.print_exc()
            self._send(500, {"error": str(exc)})
            return
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
            _run_enhance(path, output)
        except Exception as exc:  # noqa: BLE001 - report a clean 500
            traceback.print_exc()
            self._send(500, {"error": str(exc)})
            return
        self._send(200, {"ok": True})

    def _handle_dereverb(self):
        req = self._read_json()
        if req is None:
            return
        path = req.get("path")
        output = req.get("output")
        if not path or not output:
            self._send(400, {"error": "missing 'path' or 'output'"})
            return
        try:
            _run_dereverb(path, output)
        except Exception as exc:  # noqa: BLE001 - report a clean 500
            traceback.print_exc()
            self._send(500, {"error": str(exc)})
            return
        self._send(200, {"ok": True})

    def _handle_isolate(self):
        req = self._read_json()
        if req is None:
            return
        path = req.get("path")
        output = req.get("output")
        if not path or not output:
            self._send(400, {"error": "missing 'path' or 'output'"})
            return
        try:
            _run_isolate(path, output)
        except Exception as exc:  # noqa: BLE001 - report a clean 500
            traceback.print_exc()
            self._send(500, {"error": str(exc)})
            return
        self._send(200, {"ok": True})

    def log_message(self, fmt, *args):  # noqa: N802 - quiet the default logging
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="pyannote + DeepFilterNet + WPE + SepFormer audio service.")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not HF_TOKEN:
        print(
            "WARNING: HF_TOKEN is not set — /v1/diarize will fail (gated pyannote "
            "model). /v1/enhance (DeepFilterNet), /v1/dereverb (WPE) and "
            "/v1/isolate (SepFormer) work without it.",
            flush=True,
        )

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"audio server on http://{args.host}:{args.port}/v1/{{diarize,enhance,dereverb,isolate}}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
