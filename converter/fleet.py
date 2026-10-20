"""Client for the macos-dev-config control daemon (ADR-0029).

The control daemon is the machine-local LLM control plane: the sole reader of
``models.json`` and the HTTP lifecycle surface over it (``list``/``status``/
``start``/``stop``/``provision``/``log``/``reach`` — its canonical contract is
``macos-dev-config/docs/contracts/daemon-http.md``). PresentationToMarkdown
consumes it for model discovery, health, on-demand start commands, and runner
identity; the static ``config.SERVERS`` defaults remain the offline fallback
projection of the same manifest.

Everything here is best-effort and never raises: an unreachable daemon means the
callers fall back to their static defaults, which is the pre-daemon behaviour.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.parse import urlparse

DAEMON_URL = os.environ.get("DAEMON_URL", "http://127.0.0.1:9300").rstrip("/")

_TIMEOUT = 1.5
_CACHE_TTL = 30.0

_cache: dict[str, dict] = {}
_cached_at: float = 0.0


def _get(path: str, timeout: float = _TIMEOUT) -> dict | None:
    """GET one daemon verb as parsed JSON; ``None`` when unreachable/invalid."""
    req = urllib.request.Request(f"{DAEMON_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure means "daemon not answering"
        return None


def _identity(entry: dict) -> str:
    """The entry's stable identity: ``key``, or the pre-ADR-0007 ``name``."""
    return str(entry.get("key") or entry.get("name") or "")


def effective_tags(entry: dict) -> list[str]:
    """The entry's canonical tags, falling back to the deprecated alias."""
    tags = entry.get("tags")
    if tags:
        return list(tags)
    return list(entry.get("modeTags") or [])


def list_models(force: bool = False) -> dict[str, dict]:
    """The daemon's ``list`` projection as ``{key: entry}``, cached.

    ``entry`` carries ``key``, display ``name``, ``tags`` (and the deprecated
    ``modeTags`` alias), ``runner``, ``daemon``, ``host``, ``port``,
    ``capabilities``, ``defaults`` and optional ``modelId``/``fingerprint``
    (daemon-http.md §2, flaate ADR-0007). On daemon failure the last good cache
    is kept (possibly empty).
    """
    global _cache, _cached_at
    now = time.monotonic()
    if not force and _cache and now - _cached_at < _CACHE_TTL:
        return _cache
    rsp = _get("/list")
    if rsp is None:
        return _cache
    _cache = {
        _identity(entry): entry
        for entry in rsp.get("models", [])
        if _identity(entry)
    }
    _cached_at = now
    return _cache


def status(key: str) -> str:
    """One model's live state via ``GET /status/{key}``.

    ``"unknown"`` when the daemon is unreachable or the key is not in the
    manifest.
    """
    rsp = _get(f"/status/{key}")
    if rsp is None:
        return "unknown"
    return str(rsp.get("state", "unknown"))


def status_all() -> dict[str, str] | None:
    """Every model's live state via ``GET /status/all`` (ADR-0063), uncached.

    Returns ``{key: state}`` in manifest order, or ``None`` when the daemon is
    unreachable. Unlike :func:`list_models`, this call is never
    cached: the settings panel's ``r`` must observe the fleet as it is now, and
    a stale state is worse than a fallback probe. States fold ``unknown →
    down`` in the daemon itself; a missing entry degrades to ``"unknown"``.
    """
    rsp = _get("/status/all")
    if rsp is None:
        return None
    states: dict[str, str] = {}
    for entry in rsp.get("states") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key") or entry.get("name")
        if key:
            states[str(key)] = str(entry.get("state", "unknown"))
    return states


def start_command(key: str) -> str:
    """The command a user runs to start ``key`` via the daemon's ``start`` verb."""
    return f"curl -X POST {DAEMON_URL}/start/{key}"


def base_url(key: str) -> str | None:
    """OpenAI-compatible base URL for a manifest model key, from ``list``.

    ``None`` when the daemon is unreachable or ``key`` is absent — callers fall
    back to their static default.
    """
    entry = list_models().get(key)
    if not entry:
        return None
    host = entry.get("host") or "127.0.0.1"
    return f"http://{host}:{entry['port']}/v1"


def find_by_tags(tags: tuple[str, ...] | list[str]) -> dict | None:
    """First manifest entry carrying every tag, in manifest order.

    This is the role-based resolution path (flaate ADR-0007 / PtM ADR-0064):
    consumers ask for ``("vision", "transcriber")`` rather than hardcoding a
    model key. Manifest order is the deterministic tiebreak (the ADR-0015
    fallback rule).
    """
    wanted = set(tags)
    if not wanted:
        return None
    for entry in list_models().values():
        if wanted.issubset(set(effective_tags(entry))):
            return entry
    return None


def base_url_for_tags(tags: tuple[str, ...] | list[str]) -> str | None:
    """Base URL of the first model matching every tag, or ``None``."""
    entry = find_by_tags(tags)
    if not entry:
        return None
    host = entry.get("host") or "127.0.0.1"
    return f"http://{host}:{entry['port']}/v1"


def status_for_tags(tags: tuple[str, ...] | list[str]) -> str:
    """Live state of the first model matching every tag, or ``"unknown"``."""
    entry = find_by_tags(tags)
    if not entry:
        return "unknown"
    return status(_identity(entry))


def _normalize_host(host: str) -> str:
    """Fold ``localhost`` and ``127.0.0.1`` into one key for host:port matching."""
    h = (host or "").lower().rstrip(".")
    return "127.0.0.1" if h in ("localhost", "127.0.0.1") else h


def _host_port(base_url: str) -> tuple[str, int] | None:
    try:
        parsed = urlparse(base_url)
        return _normalize_host(parsed.hostname or ""), parsed.port or 0
    except Exception:  # noqa: BLE001 - a malformed URL just means "unresolvable"
        return None


def runner_for(base_url: str) -> str | None:
    """Resolve an effective base URL to its manifest ``runner`` by host:port.

    The runner enum (``llama.cpp | mlx-lm | mlx-vlm | delegate``) drives the
    runner-specific memory-release paths in ``converter.lifecycle`` (ADR-0017).
    ``None`` when the daemon is unreachable or the URL matches no manifest entry.
    """
    hp = _host_port(base_url)
    if hp is None:
        return None
    for entry in list_models().values():
        host = _normalize_host(entry.get("host") or "127.0.0.1")
        if host == hp[0] and entry.get("port") == hp[1]:
            return entry.get("runner")
    return None
