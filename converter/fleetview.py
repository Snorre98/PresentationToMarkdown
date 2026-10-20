"""Read-only fleet availability projection for the TUI (ADR-0063).

The settings modal asks one question — "which AI services are up, and which can
I turn on?" — so this module folds flaate's ``list`` and ``status/all``
projections into rows keyed by manifest model key (display names are rendered; ADR-0007/0064). It is read-only: it never
starts, stops, or provisions a service; ``start`` is the command a user runs.

The projection prefers the fleet's own state (including ``starting`` and
``provisioning``). When the daemon is unreachable it falls back to the static
``config.SERVERS`` catalogue (resolved by role tags) plus direct probes, exactly like ADR-0029's
offline path. That fallback is labelled ``source="static"`` so the UI never
presents a guess as fleet truth.
"""
from __future__ import annotations

import urllib.parse

from converter import audio, config as _config, fleet


def _servers(cfg) -> dict:
    return getattr(cfg, "SERVERS", {}) or {}


def _features(cfg) -> dict:
    return getattr(cfg, "FEATURES", {}) or {}


def _features_by_server(cfg) -> dict[str, list[str]]:
    """Map a PtM server-catalogue name to the feature keys that need it."""
    out: dict[str, list[str]] = {}
    for key in _features(cfg):
        try:
            endpoints = cfg.feature_endpoints(key)
        except Exception:  # noqa: BLE001 - a bad override must not hide the rest
            continue
        for name, _url in endpoints:
            out.setdefault(name, []).append(key)
    return out


def _server_roles(cfg) -> list[tuple[str, tuple[str, ...]]]:
    """PtM role name -> the tags that model must carry (flaate ADR-0007).

    The AI-pass slots come from ``config.SERVERS`` (so the shared text model
    can carry both ``summary`` and ``structure-text``); the ASR roles come from
    ``audio.ASR_MODELS`` (a whisper model is its own manifest entry, NB-Whisper
    lives on the ``audio`` service; ADR-0062).
    """
    return [(name, tuple(server.tags)) for name, server in _servers(cfg).items()]


def _asr_roles() -> dict[str, list[str]]:
    """Manifest key/service -> ASR role names (ADR-0062)."""
    roles: dict[str, list[str]] = {"audio": ["audio"]}
    for model in audio.ASR_MODELS:
        roles.setdefault(audio.asr_service(model), []).append(model)
    return roles


def _catalogue_roles(cfg) -> dict[str, list[str]]:
    """The static fallback rows: AI-pass roles + ASR service/model rows."""
    roles: dict[str, list[str]] = {name: [name] for name in _servers(cfg)}
    for service, names in _asr_roles().items():
        roles.setdefault(service, []).extend(names)
    return roles


def _role_label(cfg, role: str) -> str:
    """The user-facing name for a PtM role (ADR-0065)."""
    server = _servers(cfg).get(role)
    if server is not None:
        return server.label or server.name
    if role == "audio" or role in audio.ASR_MODELS:
        return "Audio transcription"
    return role


def _role_labels(cfg) -> dict[str, str]:
    labels = {name: _role_label(cfg, name) for name in _servers(cfg)}
    labels["audio"] = "Audio transcription"
    for model in audio.ASR_MODELS:
        labels[model] = "Audio transcription"
    return labels


def _resolve_roles(
    models: dict[str, dict],
    server_roles: list[tuple[str, tuple[str, ...]]],
) -> dict[str, str]:
    """Resolve each PtM role to the first manifest entry carrying its tags.

    This is the same rule as :func:`converter.fleet.find_by_tags` (manifest
    order is the tiebreak), applied to the snapshot's ``models``. A tag match
    that loses is an alternative, not a second server for the role (ADR-0065).
    """
    resolved: dict[str, str] = {}
    for role, wanted in server_roles:
        wanted_set = set(wanted)
        if not wanted_set:
            continue
        for key, entry in models.items():
            tags = set(entry.get("tags") or entry.get("modeTags") or [])
            if wanted_set.issubset(tags):
                resolved[role] = key
                break
    return resolved


def _entry_roles(
    key: str,
    resolved_by_role: dict[str, str],
    asr_roles: dict[str, list[str]],
) -> list[str]:
    """The PtM roles a manifest entry is the *resolved* model for."""
    roles = [role for role, resolved_key in resolved_by_role.items() if resolved_key == key]
    roles.extend(asr_roles.get(key, []))
    return roles


def _service_features(roles: list[str], features_by_server: dict[str, list[str]]) -> list[str]:
    """The deduped feature keys that depend on any of ``roles``."""
    out: list[str] = []
    for role in roles:
        for key in features_by_server.get(role, []):
            if key not in out:
                out.append(key)
    return out


def _select(cfg) -> tuple[str, bool]:
    """The persisted ASR target and diarization toggle (never raises)."""
    try:
        snap = cfg.snapshot(probe=False)
    except Exception:  # noqa: BLE001 - the settings store is optional for a view
        return "nb-whisper-large", False
    return snap.get("audio_model") or "nb-whisper-large", bool(snap.get("audio_diarize"))


def _is_enabled(cfg, key: str) -> bool:
    try:
        return bool(cfg.is_enabled(key))
    except Exception:  # noqa: BLE001
        return False


def _required_by(cfg, features: list[str]) -> list[str]:
    """The enabled AI passes that need this row.

    ASR selection is deliberately not included: transcription is a separate
    engine job, not a conversion dependency (ADR-0065).
    """
    return [key for key in features if _is_enabled(cfg, key)]


def _needed(cfg, features: list[str]) -> bool:
    """Whether an enabled AI pass needs this row."""
    return bool(_required_by(cfg, features))


def _host_port(base_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.hostname or "127.0.0.1", parsed.port or 0


def _service_meta(cfg, service: str, roles: list[str]):
    """``(host, port, runner, model_id, base_url, start)`` for one service.

    Catalogue roles win over ASR roles; a service with neither (a renamed
    manifest entry) still gets a start command.
    """
    for role in roles:
        server = _servers(cfg).get(role)
        if server is not None:
            return (
                server.host,
                server.port,
                server.runner,
                server.model,
                server.base_url,
                server.start_command,
            )
    for role in roles:
        spec = audio.ASR_MODELS.get(role)
        if spec is not None:
            url = audio.asr_base_url(role)
            host, port = _host_port(url)
            return host, port, "", spec.repo, url, fleet.start_command(service)
    return "127.0.0.1", 0, "", "", "", fleet.start_command(service)


def _catalogue_row(
    cfg,
    service: str,
    roles: list[str],
    features_by_server: dict[str, list[str]],
    selected_service: str,
    *,
    state: str,
) -> dict:
    """A row for a role that is absent from the manifest (or all of them)."""
    host, port, runner, model_id, _base_url, start = _service_meta(cfg, service, roles)
    features = _service_features(roles, features_by_server)
    tags: list[str] = []
    for role in roles:
        server = _servers(cfg).get(role)
        if server is not None:
            tags = list(server.tags)
            break
    required_by = _required_by(cfg, features)
    label = _role_label(cfg, roles[0] if roles else service)
    return {
        "key": service,
        "name": label,
        "tags": tags,
        "daemon": service,
        "host": host,
        "port": port,
        "runner": runner,
        "modelId": model_id,
        "state": state,
        "roles": list(roles),
        "features": features,
        "required_by": required_by,
        "available_for": list(features),
        "needed": bool(required_by),
        "selected": service == selected_service,
        "in_manifest": False,
        "start": start,
    }


def _manifest_rows(
    cfg,
    models: dict[str, dict],
    states: dict[str, str],
    roles_by_key: dict[str, list[str]],
    catalogue_roles: dict[str, list[str]],
    features_by_server: dict[str, list[str]],
    selected_service: str,
) -> list[dict]:
    """One row per manifest model, in manifest order, plus missing PtM roles."""
    rows: list[dict] = []
    covered: set[str] = set()
    for key, entry in models.items():
        roles = list(roles_by_key.get(key, ()))
        covered.update(roles)
        features = _service_features(roles, features_by_server)
        required_by = _required_by(cfg, features)
        rows.append(
            {
                "key": key,
                "name": entry.get("name") or key,
                "tags": list(entry.get("tags") or entry.get("modeTags") or []),
                "daemon": entry.get("daemon") or key,
                "host": entry.get("host") or "127.0.0.1",
                "port": entry.get("port") or 0,
                "runner": entry.get("runner") or "",
                "modelId": entry.get("modelId") or "",
                "state": states.get(key, "unknown"),
                "roles": roles,
                "features": features,
                "required_by": required_by,
                "available_for": list(features),
                "needed": bool(required_by),
                "selected": key == selected_service,
                "in_manifest": True,
                "start": fleet.start_command(key),
            }
        )

    for service, roles in catalogue_roles.items():
        if service in covered:
            continue
        rows.append(
            _catalogue_row(
                cfg,
                service,
                roles,
                features_by_server,
                selected_service,
                state="unknown",
            )
        )
    return rows


def _static_rows(
    cfg,
    catalogue_roles: dict[str, list[str]],
    features_by_server: dict[str, list[str]],
    selected_service: str,
) -> list[dict]:
    """Catalogue + ASR rows with direct probes when flaate is unreachable."""
    rows: list[dict] = []
    for service, roles in catalogue_roles.items():
        _host, _port, _runner, _model_id, base_url, _start = _service_meta(
            cfg, service, roles
        )
        state = "up" if base_url and cfg.probe(base_url) else "down"
        rows.append(
            _catalogue_row(
                cfg,
                service,
                roles,
                features_by_server,
                selected_service,
                state=state,
            )
        )
    return rows


def _requirements(
    cfg,
    rows: list[dict],
    resolved_by_role: dict[str, str],
) -> list[dict]:
    """The feature-centric "what does this pass need?" projection (ADR-0065).

    Each pass lists the models it will actually request, with the fleet state
    and the on-demand start command. ``optional`` marks the graceful-degradation
    dependencies (need-check gate, summary embeddings); they are reported but do
    not make a pass unready.
    """
    rows_by_key = {row["key"]: row for row in rows}
    role_to_row: dict[str, str] = {}
    for key, row in rows_by_key.items():
        for role in row["roles"]:
            role_to_row.setdefault(role, key)

    out: list[dict] = []
    for key, feature in _features(cfg).items():
        services: list[dict] = []
        missing: list[str] = []
        optional_missing: list[str] = []
        states: list[str] = []
        for req in cfg.feature_requirements(key):
            role = req["role"]
            row_key = resolved_by_role.get(role) or role_to_row.get(role) or role
            row = rows_by_key.get(row_key) or {}
            state = str(row.get("state") or "unknown")
            states.append(state)
            item = {
                "role": role,
                "label": req["label"],
                "key": row.get("key") or req["key"],
                "name": row.get("name") or req["name"],
                "base_url": req["base_url"],
                "model": req["model"],
                "state": state,
                "start": row.get("start") or req["start"],
                "optional": bool(req["optional"]),
            }
            services.append(item)
            if state != "up" and state not in ("starting", "stopping", "provisioning"):
                if item["optional"]:
                    optional_missing.append(item["key"])
                else:
                    missing.append(item["key"])

        if missing:
            readiness = "missing"
        elif any(state in ("starting", "stopping", "provisioning") for state in states):
            readiness = "pending"
        elif not services:
            readiness = "unknown"
        else:
            readiness = "ready"

        out.append(
            {
                "key": key,
                "label": feature.label,
                "description": feature.description,
                "scope": feature.scope,
                "enabled": _is_enabled(cfg, key),
                "readiness": readiness,
                "services": services,
                "missing": missing,
                "optional_missing": optional_missing,
            }
        )
    return out


def _other_models(rows: list[dict]) -> int:
    """Manifest entries that serve no PtM role (the ``+N other models`` count)."""
    return sum(1 for row in rows if row["in_manifest"] and not row["roles"])


def snapshot(cfg=None) -> dict:
    """The TUI's fleet availability payload (ADR-0063/0065).

    Prefers flaate's ``status/all`` (the liveness check) and ``list``; when the
    daemon does not answer, returns direct-probe rows from the static
    catalogue. Rows are the models PtM resolves a role to, not every tag match;
    the feature-centric ``requirements`` list answers "what does this pass need?".
    Never raises — a broken override degrades to the static view.
    """
    cfg = cfg or _config
    server_roles = _server_roles(cfg)
    asr_roles = _asr_roles()
    catalogue_roles = _catalogue_roles(cfg)
    features_by_server = _features_by_server(cfg)
    selected, _diarize = _select(cfg)
    # An unknown persisted ASR name passes through settings; it must not mark
    # the NB-Whisper row as selected just because audio.asr_service() degrades.
    selected_service = audio.asr_service(selected) if selected in audio.ASR_MODELS else ""

    states = fleet.status_all()
    if states is None:
        rows = _static_rows(cfg, catalogue_roles, features_by_server, selected_service)
        resolved_by_role = {name: name for name in _servers(cfg)}
        source = "static"
        reachable = False
    else:
        models = fleet.list_models(force=True)
        resolved_by_role = _resolve_roles(models, server_roles)
        roles_by_key = {
            key: _entry_roles(key, resolved_by_role, asr_roles)
            for key in models
        }
        rows = _manifest_rows(
            cfg,
            models,
            states,
            roles_by_key,
            catalogue_roles,
            features_by_server,
            selected_service,
        )
        source = "fleet"
        reachable = True

    return {
        "daemon": {"url": fleet.DAEMON_URL, "reachable": reachable},
        "source": source,
        "services": rows,
        "other_models": _other_models(rows),
        "role_labels": _role_labels(cfg),
        "requirements": _requirements(cfg, rows, resolved_by_role),
    }
