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
import multiprocessing
import os
import queue
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIARIZE_MODEL = os.environ.get("PYANNOTE_MODEL", "pyannote/speaker-diarization-3.1")
HF_TOKEN = os.environ.get("HF_TOKEN")
ISOLATE_MODEL = os.environ.get("SEPARATOR_MODEL", "speechbrain/sepformer-whamr")
SEPARATOR_SAVEDIR = os.environ.get(
    "SPEECHBRAIN_SAVEDIR", os.path.join(os.path.expanduser("~/.cache"), "speechbrain")
)
_ATTEN = os.environ.get("AUDIO_ENHANCE_ATTEN_DB")
AUDIO_ENHANCE_ATTEN_DB = float(_ATTEN) if _ATTEN and _ATTEN.strip() else 12.0

_WINDOW = os.environ.get("AUDIO_ISOLATE_WINDOW_SEC")
AUDIO_ISOLATE_WINDOW_SEC = float(_WINDOW) if _WINDOW and _WINDOW.strip() else 20.0
_TIMEOUT = os.environ.get("AUDIO_STAGE_TIMEOUT_SEC")
AUDIO_STAGE_TIMEOUT_SEC = float(_TIMEOUT) if _TIMEOUT and _TIMEOUT.strip() else 1800.0

_VAD_MODEL = os.environ.get("PYANNOTE_VAD_MODEL")
VAD_MODEL = _VAD_MODEL if _VAD_MODEL and _VAD_MODEL.strip() else "pyannote/segmentation-3.0"
_SKIP = os.environ.get("AUDIO_ISOLATE_SKIP_IF_NO_SPEECH")
AUDIO_ISOLATE_SKIP_IF_NO_SPEECH = _SKIP is not None and _SKIP.strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_MIN_SPEECH = os.environ.get("AUDIO_ISOLATE_MIN_SPEECH_SEC")
AUDIO_ISOLATE_MIN_SPEECH_SEC = float(_MIN_SPEECH) if _MIN_SPEECH and _MIN_SPEECH.strip() else 5.0

# The heavy stages (SepFormer isolation, pyannote diarization) run in respawning
# worker subprocesses so a hang/OOM can't take the server down — see _run_in_worker.
# Enhancement and dereverberation stay in-process: they degrade gracefully via
# caught exceptions and don't hard-crash.
_CTX = multiprocessing.get_context("spawn")

_pipeline = None
_enhancer = None
_separator = None
_vad = None


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


def _get_vad():
    global _vad
    if _vad is None:
        from pyannote.audio.pipelines import VoiceActivityDetection

        _vad = VoiceActivityDetection(
            segmentation=VAD_MODEL, use_auth_token=HF_TOKEN
        )
        # ``default_parameters()`` only knows the legacy ``pyannote/segmentation``
        # ids, so set the hysteresis thresholds and post-processing durations
        # ourselves before the first call (``onset``/``offset`` are plain float
        # attributes, ``min_duration_on/off`` are the registered parameters).
        _vad.onset = 0.5
        _vad.offset = 0.5
        _vad.instantiate({"min_duration_on": 0.0, "min_duration_off": 0.0})
    return _vad


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
    enhanced = df_enhance(model, df_state, audio, atten_lim_db=AUDIO_ENHANCE_ATTEN_DB)
    save_audio(output, resample(enhanced, 48000, 16000), sr=16000)


def _run_dereverb(path: str, output: str) -> None:
    """Blindly dereverberate ``path`` (WPE) and write the 16 kHz result to ``output``."""
    import numpy as np
    import torch
    import torchaudio
    from nara_wpe.wpe import wpe
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


_ISOLATE_SR = 8000
_ISOLATE_OVERLAP_SAMPLES = 8000  # ~1 s overlap @ 8 kHz


def _rms(signal) -> float:
    centered = signal - signal.mean()
    return float(centered.pow(2).mean().sqrt())


def _pick_voice_idx(sources, speech_mask, prev_tail) -> int:
    """Pick which of SepFormer's two *unordered* outputs is the tracked voice.

    Preference order:
    1. Speech concentration — the source whose energy is concentrated inside the
       VAD speech regions. This tracks the *talking* stream rather than the
       *loudest* stream, so it stays correct when the noise is far louder than
       the voice (which is exactly where the old higher-RMS heuristic failed).
    2. Cross-correlation with the previous window's chosen voice — continuity
       through a window that happens to contain no speech.
    3. Higher RMS — first window, or when neither of the above applies.
    """
    num_sources = sources.shape[1]

    if speech_mask is not None and bool(speech_mask.any()):
        weighted = speech_mask.float()
        energies = [float(sources[:, i].pow(2).sum()) for i in range(num_sources)]
        if any(e > 0 for e in energies):
            def _concentration(i: int) -> float:
                return float((weighted * sources[:, i].pow(2)).sum()) / (energies[i] + 1e-8)

            return max(range(num_sources), key=_concentration)

    if prev_tail is not None and prev_tail.numel() > 0:
        heads = sources[: prev_tail.shape[0], :]  # (overlap, num_sources)
        a = prev_tail - prev_tail.mean()
        best, best_i = -float("inf"), 0
        for i in range(num_sources):
            b = heads[:, i] - heads[:, i].mean()
            corr = float((a * b).sum() / (a.norm() * b.norm() + 1e-8))
            if corr > best:
                best, best_i = corr, i
        return best_i

    rms = [_rms(sources[:, i]) for i in range(num_sources)]
    return max(range(num_sources), key=lambda i: rms[i])


def _run_vad(path: str):
    """Return VAD speech regions as ``[(start, end), ...]`` seconds.

    Returns ``None`` when the VAD is unavailable or failed (so the caller can
    tell "unknown" from "no speech"), and ``[]`` when it ran but found nothing.
    """
    try:
        annotation = _get_vad()(path)
    except Exception as exc:  # noqa: BLE001 - VAD is best-effort; degrade to RMS
        print(f"WARNING: voice-activity detection failed ({exc}); picking source by RMS", flush=True)
        return None
    if annotation is None:
        return []
    return [(float(seg.start), float(seg.end)) for seg in annotation.get_timeline()]


def _run_isolate(path: str, output: str) -> None:
    """Separate the dominant voice (SepFormer) and write a 16 kHz result to ``output``.

    ``sepformer-whamr`` is trained on speech + noise + reverb, so it splits the
    mixture into two unordered streams; we pick the "voice" — the *talking*
    stream, via VAD speech concentration (see ``_pick_voice_idx``), not merely
    the louder one — and resample its native 8 kHz up to 16 kHz for ASR.

    ``SepformerSeparation.separate_batch`` does one full forward pass over the
    whole tensor, and ``sepformer-whamr`` is an 8 kHz 8-layer dual-path
    transformer — on CPU a 90 s clip already hangs >120 s and a 40-min lecture is
    ~27x that. So we feed it bounded, overlapping windows
    (``AUDIO_ISOLATE_WINDOW_SEC``) and stitch the per-window voice sources back
    together with a linear crossfade over the overlap.

    When ``AUDIO_ISOLATE_SKIP_IF_NO_SPEECH`` is set and the VAD finds less than
    ``AUDIO_ISOLATE_MIN_SPEECH_SEC`` of speech, the input is copied straight to
    ``output`` (a no-op) so a music track — where "voice" is ill-defined — can't
    be mangled by a best-effort source pick.
    """
    import torch
    import torchaudio

    separator = _get_separator()
    mix, sr = torchaudio.load(path)
    if sr != _ISOLATE_SR:
        mix = torchaudio.functional.resample(mix, sr, _ISOLATE_SR)
    if mix.shape[0] > 1:
        mix = mix.mean(dim=0, keepdim=True)

    total = mix.shape[1]

    # One VAD pass over the clip (at its native rate — pyannote resamples
    # internally) to build a speech mask on the 8 kHz grid, so each window can be
    # scored by speech concentration rather than loudness.
    speech_segs = _run_vad(path)
    if speech_segs is not None:
        speech_sec = sum(end - start for start, end in speech_segs)
        if AUDIO_ISOLATE_SKIP_IF_NO_SPEECH and speech_sec < AUDIO_ISOLATE_MIN_SPEECH_SEC:
            shutil.copyfile(path, output)
            return
        speech_mask = torch.zeros(total, dtype=torch.bool)
        for start, end in speech_segs:
            a, b = int(start * _ISOLATE_SR), int(end * _ISOLATE_SR)
            if b > a:
                speech_mask[a:b] = True
    else:
        speech_mask = None

    window = int(AUDIO_ISOLATE_WINDOW_SEC * _ISOLATE_SR)
    overlap = _ISOLATE_OVERLAP_SAMPLES
    hop = max(window - overlap, 1)

    if total <= window:
        starts = [0]
    else:
        starts = list(range(0, total - overlap, hop))
        if starts[-1] + window < total:
            starts.append(total - window)  # anchor a final window to cover the tail

    # Overlap-add with a triangular weight (linear crossfade) so the stitched
    # result has no seams and is exactly ``total`` samples long.
    out = torch.zeros(total)
    weights = torch.zeros(total)
    ramp = torch.linspace(0.0, 1.0, overlap)
    prev_voice, prev_end = None, 0

    for start in starts:
        end = min(start + window, total)
        est = separator.separate_batch(mix[:, start:end])  # (1, time, num_sources)
        sources = est[0]  # (time, num_sources)

        if prev_voice is None:
            prev_tail = None
        else:
            overlap_len = prev_end - start
            prev_tail = prev_voice[-overlap_len:] if overlap_len > 0 else None
        mask_slice = speech_mask[start:end] if speech_mask is not None else None
        idx = _pick_voice_idx(sources, mask_slice, prev_tail)
        voice = sources[:, idx]  # (time,)
        prev_voice, prev_end = voice, end

        w = torch.ones(end - start)
        if start > 0:
            n = min(overlap, end - start)
            w[:n] = ramp[:n]
        if end < total:
            n = min(overlap, end - start)
            w[-n:] = ramp.flip(0)[:n]
        out[start:end] += voice * w
        weights[start:end] += w

    out = out / weights.clamp(min=1e-8)
    voice = out.unsqueeze(0)  # (1, time)

    voice = torchaudio.functional.resample(voice, _ISOLATE_SR, 16000)
    torchaudio.save(output, voice, 16000)


# ── Worker subprocesses for the heavy stages ─────────────────────────────────
# SepFormer (isolate) and pyannote (diarize) can hang (huge tensor forward pass)
# or OOM-kill their process on real lecture-length input. A hard death inside the
# HTTP server would drop the socket mid-request and, worse, kill every endpoint —
# which is how a runaway isolate used to make the later diarize "connection
# refused". So the heavy stages run in their own long-lived subprocesses that we
# can kill and respawn without touching the server. Only file paths cross in and
# small results cross back; the worker reads the input and writes the output.

_workers: dict[str, dict] = {}
_registry_lock = threading.Lock()


def _worker_main(stage: str, task_q, result_q) -> None:
    """Worker entry point (top-level so it's picklable under ``spawn``).

    Runs in a child process and serves tasks until a ``None`` sentinel. The
    stage's model is loaded lazily on the first task via the ``_get_*`` loaders
    (which cache on this child's own globals), so a model-load failure surfaces
    as a clean error result instead of killing the worker.
    """
    runner = {"isolate": _run_isolate, "diarize": _run_diarization}[stage]
    while True:
        task = task_q.get()
        if task is None:
            break
        try:
            result = runner(**task)
        except Exception as exc:  # noqa: BLE001 - surface as a clean 500 upstream
            result_q.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            continue
        result_q.put({"ok": True, "result": result})


def _reap_worker(stage: str) -> None:
    """Kill/join the worker for ``stage`` and drop it from the registry."""
    w = _workers.pop(stage, None)
    if w is None:
        return
    if w["proc"].is_alive():
        w["proc"].terminate()
    w["proc"].join(timeout=5)
    for q in (w["task_q"], w["result_q"]):
        try:
            q.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def _get_worker(stage: str) -> dict:
    """Return (spawning on first use / after a death) the worker for ``stage``."""
    with _registry_lock:
        w = _workers.get(stage)
        if w is None or not w["proc"].is_alive():
            if w is not None:
                _reap_worker(stage)
            task_q = _CTX.Queue()
            result_q = _CTX.Queue()
            proc = _CTX.Process(
                target=_worker_main, args=(stage, task_q, result_q), daemon=True
            )
            proc.start()
            w = {
                "proc": proc,
                "task_q": task_q,
                "result_q": result_q,
                "lock": threading.Lock(),
            }
            _workers[stage] = w
        return w


def _run_in_worker(stage: str, task: dict, timeout: float) -> dict:
    """Run ``task`` on ``stage``'s worker, returning ``{"ok": bool, "result"|"error"}``.

    A hung worker (no result before ``timeout``) or a crashed worker (process
    died) is reaped here so the next request respawns it; the endpoint degrades
    to a clean 500 instead of a dropped socket.
    """
    w = _get_worker(stage)
    with w["lock"]:  # one request per worker at a time (single worker, FIFO queue)
        w["task_q"].put(task)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _reap_worker(stage)
                return {"ok": False, "error": f"{stage} timed out after {timeout:.0f}s"}
            try:
                res = w["result_q"].get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if not w["proc"].is_alive():
                    _reap_worker(stage)
                    return {"ok": False, "error": f"{stage} worker process died"}
                continue
            return res


def _shutdown_workers() -> None:
    for stage in list(_workers):
        _reap_worker(stage)


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
            res = _run_in_worker(
                "diarize",
                {
                    "path": path,
                    "min_speakers": req.get("min_speakers"),
                    "max_speakers": req.get("max_speakers"),
                },
                AUDIO_STAGE_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 - report a clean 500
            traceback.print_exc()
            self._send(500, {"error": str(exc)})
            return
        if not res["ok"]:
            self._send(500, {"error": res["error"]})
            return
        self._send(200, res["result"])

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
            res = _run_in_worker(
                "isolate", {"path": path, "output": output}, AUDIO_STAGE_TIMEOUT_SEC
            )
        except Exception as exc:  # noqa: BLE001 - report a clean 500
            traceback.print_exc()
            self._send(500, {"error": str(exc)})
            return
        if not res["ok"]:
            self._send(500, {"error": res["error"]})
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
        _shutdown_workers()
        httpd.server_close()


if __name__ == "__main__":
    main()
