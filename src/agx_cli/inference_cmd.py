"""Local inference engine lifecycle (Phase 1: Ollama, Phase 2: llama.cpp).

Project-scoped sibling services that run INSIDE the toolkit's VM,
serving OpenAI-compatible HTTP endpoints over the project Docker
network. Hermes agents (and OpenClaw if pointed there manually) reach
them via DNS names like ``http://ollama:11434/v1``.

Why in-VM instead of separate hosts:

- Same trust boundary as agents — the inference server sees only what
  the agent sends it; no new external attack surface
- Shared model storage at ``<data-dir>/models/`` keeps weights out of
  individual agent dirs and out of ``hermes`` per-agent backups
- One ``agx inference up`` covers every agent; no per-agent wiring
  of model endpoints

Engines:

- **Ollama** (Phase 1) — single service, model catalog + native HF
  support (``hf.co/<repo>:<quant>``), simplest UX. Multi-arch image.
- **llama.cpp** (Phase 2) — one service per loaded model. Direct GGUF
  loading from ``<data-dir>/models/llama-cpp/``; for models not on
  Ollama Hub or when fine-grained quant/sampling control matters.
- **vLLM** — deliberately not supported here. Requires GPU
  passthrough which Multipass doesn't offer (see GH issue #2).
  Use ``provider: custom + base_url`` to point Hermes at an external
  vLLM if you have one.

Pinning policy mirrors Firecrawl: pull-and-digest-pin on first ``up``,
re-resolve on ``--upgrade``. ``cfg.inference.ollama_image`` /
``llamacpp_image`` persist the digest across CLI invocations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agx_cli import compose, compose_runner, mp, sidecar, ui
from agx_cli import config as cfg_mod
from agx_cli.compose import (
    LLAMACPP_IMAGE_BASE,
    LLAMACPP_SERVICE_PREFIX,
    OLLAMA_IMAGE_BASE,
    OLLAMA_SERVICE_NAME,
)
from agx_cli.config import AgentsConfig
from agx_cli.errors import RuntimeFailure, UserError

SUPPORTED_ENGINES: tuple[str, ...] = ("ollama", "llama-cpp")


# ---------- public CLI entry points ----------


def up(
    engine: str,
    *,
    upgrade: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
    emit: bool = True,
) -> dict[str, Any]:
    """Start a local inference engine.

    For Ollama: idempotent — re-running ``up`` reuses the pinned image
    and just ensures the service is running. ``--upgrade`` re-resolves
    the upstream tag's digest.

    For llama-cpp: ``up llama-cpp`` only pulls + pins the image; loading
    a specific model is a separate command (see ``up_llamacpp_model``)
    because each model is a distinct service. This split keeps the CLI
    surface symmetric with Ollama while honouring llama-server's one-
    model-per-process design.

    ``emit=False`` skips the final :func:`ui.emit` so composite callers
    (``vm rebuild``) that own the command's single stdout object can
    embed this flow; the payload is still returned.
    """
    if engine not in SUPPORTED_ENGINES:
        raise UserError(
            f"unknown inference engine {engine!r}; supported: {', '.join(SUPPORTED_ENGINES)}"
        )
    cfg = cfg_mod.load(config_path)
    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime not initialised — run `agx vm init` first")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        if engine == "ollama":
            return _up_ollama(cfg, upgrade=upgrade, config_path=config_path, emit=emit)
        return _pin_llamacpp_image(cfg, upgrade=upgrade, config_path=config_path, emit=emit)


def down(
    engine: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Stop and remove the engine service from compose. Model weights
    on disk are preserved (under ``<data-dir>/models/``) so a later
    ``up`` resumes without re-downloading.

    For llama-cpp this stops EVERY ``llama-cpp-*`` service; pass a
    specific model via ``down_llamacpp_model(slug)`` to stop one.
    """
    if engine not in SUPPORTED_ENGINES:
        raise UserError(
            f"unknown inference engine {engine!r}; supported: {', '.join(SUPPORTED_ENGINES)}"
        )
    cfg = cfg_mod.load(config_path)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        if engine == "ollama":
            return _down_ollama(cfg, config_path=config_path)
        return _down_all_llamacpp(cfg)


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Snapshot every in-VM inference engine: image digest, service
    state, loaded models (Ollama), per-model llama-cpp services.

    Read-only. Safe to run in tight loops.
    """
    cfg = cfg_mod.load(config_path)
    payload: dict[str, Any] = {
        "ollama": _status_ollama(cfg),
        "llama_cpp": _status_llamacpp(cfg),
    }
    _emit_status(payload)
    return payload


# ---------- ollama ----------


def _up_ollama(
    cfg: AgentsConfig, *, upgrade: bool, config_path: Path, emit: bool = True
) -> dict[str, Any]:
    image_digest = cfg.inference.ollama_image
    if not image_digest or upgrade:
        # resolve_digest allowlist-validates before returning (rule #5).
        ui.progress(
            f"pulling {OLLAMA_IMAGE_BASE}:latest and resolving its digest "
            f"(first pull can take minutes)..."
        )
        image_digest = sidecar.resolve_digest(cfg, f"{OLLAMA_IMAGE_BASE}:latest")
        cfg.inference.ollama_image = image_digest
    else:
        # Reuse path: the ref comes straight out of config.toml — a
        # doctored config must not deploy an off-allowlist image, so
        # validate before anything lands in compose (rule #5).
        compose.validate_image_ref(image_digest)

    _ensure_models_dir(cfg, "ollama")
    # The ollama fragment also bind-mounts ./models/llama-cpp (the HF
    # download bridge, see compose.LLAMACPP_DOWNLOAD_MOUNT). Pre-create
    # it too so the toolkit owns it with correct ownership instead of
    # Docker auto-creating it root-owned on first start.
    _ensure_models_dir(cfg, "llama-cpp")

    doc = compose.read_compose_doc(cfg)
    services = doc.setdefault("services", {})
    services[OLLAMA_SERVICE_NAME] = compose.ollama_service_fragment(
        image=image_digest, port=cfg.inference.ollama_port
    )
    compose.write_compose_atomically(cfg, doc)

    # Compose-up brings it up (raises RuntimeFailure on a non-zero exit).
    compose_runner.up(cfg, OLLAMA_SERVICE_NAME)

    # Persist enabled + digest pin only AFTER a successful up — a failed
    # first up must not leave enabled-but-never-ran state in config.toml
    # (doctor would then probe a service that never existed).
    cfg.inference.ollama_enabled = True
    cfg_mod.save(cfg, config_path)

    payload = {
        "engine": "ollama",
        "running": True,
        "image": image_digest,
        "url_in_vm": f"http://{OLLAMA_SERVICE_NAME}:11434/v1",
        "url_host": f"http://127.0.0.1:{cfg.inference.ollama_port}/v1",
    }
    if emit:
        ui.emit(
            payload,
            human=(
                f"ollama: up\n"
                f"  in-VM URL:  {payload['url_in_vm']}\n"
                f"  host URL:   {payload['url_host']}\n"
                f"  image:      {image_digest}\n"
                f"  models dir: {cfg.paths.host_data_dir}/models/ollama/\n"
                f"  next: `agx model pull ollama <model>` then "
                f"`agx model use <agent> --engine ollama --model <model>`"
            ),
        )
    return payload


def _down_ollama(cfg: AgentsConfig, *, config_path: Path) -> dict[str, Any]:
    if not _ollama_in_compose(cfg):
        raise UserError("ollama is not installed")

    compose_runner.stop(cfg, OLLAMA_SERVICE_NAME)
    compose_runner.rm(cfg, OLLAMA_SERVICE_NAME)

    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    if OLLAMA_SERVICE_NAME in services:
        del services[OLLAMA_SERVICE_NAME]
    compose.write_compose_atomically(cfg, doc)

    cfg.inference.ollama_enabled = False
    cfg_mod.save(cfg, config_path)
    ui.emit(
        {"engine": "ollama", "running": False},
        human="ollama: stopped (model weights preserved under <data-dir>/models/ollama/)",
    )
    return {"engine": "ollama", "running": False}


def _status_ollama(cfg: AgentsConfig) -> dict[str, Any]:
    installed = _ollama_in_compose(cfg)
    if not installed:
        return {"installed": False, "running": False}
    running = _service_running(cfg, OLLAMA_SERVICE_NAME)
    out: dict[str, Any] = {
        "installed": True,
        "running": running,
        "image": cfg.inference.ollama_image,
        "url_in_vm": f"http://{OLLAMA_SERVICE_NAME}:11434/v1",
        "models": [],
    }
    if running:
        out["models"] = _ollama_list_models(cfg)
    return out


def _ollama_list_models(cfg: AgentsConfig) -> list[dict[str, Any]]:
    """Query `ollama list` inside the container; parse the table.

    Returns one dict per model: ``{name, id, size, modified}``. Empty
    list when no models loaded. Failures yield empty list — status is
    read-only and never raises.
    """
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        OLLAMA_SERVICE_NAME,
        "ollama",
        "list",
        check=False,
    )
    if result.returncode != 0:
        return []
    out: list[dict[str, Any]] = []
    for line in result.stdout.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) >= 4:
            out.append(
                {
                    "name": parts[0],
                    "id": parts[1],
                    "size": " ".join(parts[2:4]),
                    "modified": " ".join(parts[4:]) if len(parts) > 4 else "",
                }
            )
    return out


# ---------- llama.cpp ----------


def _pin_llamacpp_image(
    cfg: AgentsConfig, *, upgrade: bool, config_path: Path, emit: bool = True
) -> dict[str, Any]:
    """Resolve and pin the llama-cpp image digest; doesn't start any
    service. Per-model llama-server services are spawned by
    ``up_llamacpp_model`` once a GGUF is on disk.
    """
    if cfg.inference.llamacpp_image and not upgrade:
        digest = cfg.inference.llamacpp_image
        # Reuse path: cfg-supplied ref — allowlist-validate before it can
        # reach any compose write (rule #5).
        compose.validate_image_ref(digest)
    else:
        # resolve_digest allowlist-validates before returning (rule #5).
        # The llama.cpp image is multi-arch (amd64/arm64/s390x); the
        # daemon picks the right manifest for the VM's arch.
        ui.progress(
            f"pulling {LLAMACPP_IMAGE_BASE}:server and resolving its digest "
            f"(first pull can take minutes)..."
        )
        digest = sidecar.resolve_digest(cfg, f"{LLAMACPP_IMAGE_BASE}:server")
        cfg.inference.llamacpp_image = digest
        cfg_mod.save(cfg, config_path)

    _ensure_models_dir(cfg, "llama-cpp")

    payload = {
        "engine": "llama-cpp",
        "running": False,
        "image": digest,
        "next": (
            "Place a GGUF under <data-dir>/models/llama-cpp/ "
            "and call `agx model load llama-cpp <name>` to spawn "
            "a llama-server for it."
        ),
    }
    if emit:
        ui.emit(
            payload,
            human=(
                f"llama-cpp: image pinned\n"
                f"  image:      {digest}\n"
                f"  models dir: {cfg.paths.host_data_dir}/models/llama-cpp/\n"
                f"  next: download a GGUF, then\n"
                f"        `agx model load llama-cpp <slug> --gguf <path-rel-to-models-dir>`"
            ),
        )
    return payload


def _down_all_llamacpp(cfg: AgentsConfig) -> dict[str, Any]:
    """Stop and remove every ``llama-cpp-*`` service. Model files on
    disk untouched.
    """
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    targets = [s for s in services if s.startswith(LLAMACPP_SERVICE_PREFIX)]
    if not targets:
        ui.emit(
            {"engine": "llama-cpp", "stopped": []},
            human="llama-cpp: no per-model services running",
        )
        return {"engine": "llama-cpp", "stopped": []}
    for svc in targets:
        compose_runner.stop(cfg, svc)
        compose_runner.rm(cfg, svc)
        del services[svc]
    compose.write_compose_atomically(cfg, doc)
    ui.emit(
        {"engine": "llama-cpp", "stopped": targets},
        human=f"llama-cpp: stopped {len(targets)} per-model service(s): {', '.join(targets)}",
    )
    return {"engine": "llama-cpp", "stopped": targets}


def _status_llamacpp(cfg: AgentsConfig) -> dict[str, Any]:
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return {"installed": False, "loaded_models": []}
    services = doc.get("services") or {}
    loaded = []
    for svc_name in services:
        if not svc_name.startswith(LLAMACPP_SERVICE_PREFIX):
            continue
        slug = svc_name[len(LLAMACPP_SERVICE_PREFIX) :]
        running = _service_running(cfg, svc_name)
        loaded.append({"slug": slug, "service": svc_name, "running": running})
    return {
        "installed": bool(cfg.inference.llamacpp_image),
        "image": cfg.inference.llamacpp_image,
        "loaded_models": loaded,
    }


def up_llamacpp_model(
    slug: str,
    *,
    gguf_relative_path: str,
    port: int,
    ctx_size: int = 4096,
    n_gpu_layers: int = 0,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Spawn a llama-server for one GGUF model. ``slug`` is a sanitized
    short name (lowercase letters / digits / hyphens) that becomes the
    docker service + container name suffix.

    Idempotent: re-running with the same slug overwrites the existing
    service block (useful for changing ctx_size / port / gpu_layers).
    """
    cfg = cfg_mod.load(config_path)
    if not cfg.inference.llamacpp_image:
        raise UserError("llama-cpp image not pinned — run `agx inference up llama-cpp` first")
    # cfg-supplied ref goes verbatim into the service fragment below —
    # allowlist-validate first (rule #5; a doctored config.toml must not
    # deploy an arbitrary registry image).
    compose.validate_image_ref(cfg.inference.llamacpp_image)
    _validate_slug(slug)
    gguf_abs = cfg.paths.host_data_dir / "models" / "llama-cpp" / gguf_relative_path
    if not gguf_abs.exists():
        raise UserError(
            f"GGUF not found at {gguf_abs}. Download it first into "
            f"<data-dir>/models/llama-cpp/ (HF, manual, etc.)."
        )

    with compose.acquire_lock(cfg.paths.host_data_dir):
        doc = compose.read_compose_doc(cfg)
        services = doc.setdefault("services", {})
        svc_name = f"{LLAMACPP_SERVICE_PREFIX}{slug}"
        services[svc_name] = compose.llamacpp_service_fragment(
            model_slug=slug,
            image=cfg.inference.llamacpp_image,
            gguf_relative_path=gguf_relative_path,
            port=port,
            ctx_size=ctx_size,
            n_gpu_layers=n_gpu_layers,
        )
        compose.write_compose_atomically(cfg, doc)

    compose_runner.up(cfg, svc_name)
    payload = {
        "engine": "llama-cpp",
        "slug": slug,
        "service": svc_name,
        "url_in_vm": f"http://{svc_name}:8080/v1",
        "url_host": f"http://127.0.0.1:{port}/v1",
        "gguf": gguf_relative_path,
    }
    ui.emit(
        payload,
        human=(
            f"llama-cpp[{slug}]: up\n"
            f"  in-VM URL:  {payload['url_in_vm']}\n"
            f"  host URL:   {payload['url_host']}\n"
            f"  gguf:       {gguf_relative_path}\n"
            f"  next: `agx model use <agent> --engine llama-cpp --model {slug}`"
        ),
    )
    return payload


def down_llamacpp_model(
    slug: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH
) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    _validate_slug(slug)
    svc_name = f"{LLAMACPP_SERVICE_PREFIX}{slug}"
    with compose.acquire_lock(cfg.paths.host_data_dir):
        doc = compose.read_compose_doc(cfg)
        services = doc.get("services") or {}
        if svc_name not in services:
            raise UserError(f"llama-cpp model {slug!r} not loaded")
        compose_runner.stop(cfg, svc_name)
        compose_runner.rm(cfg, svc_name)
        del services[svc_name]
        compose.write_compose_atomically(cfg, doc)
    ui.emit(
        {"engine": "llama-cpp", "slug": slug, "stopped": True},
        human=f"llama-cpp[{slug}]: stopped",
    )
    return {"engine": "llama-cpp", "slug": slug, "stopped": True}


def reup_llamacpp_services(cfg: AgentsConfig) -> dict[str, list[str]]:
    """Bring every ``llama-cpp-*`` service already defined in compose
    back up. Used by ``vm rebuild``: the per-model service blocks
    (digest-pinned) survive on the host data-dir, but the containers
    live only in the old VM's Docker engine, so a fresh VM starts with
    none of them running.

    No compose mutation and no lock — each service block is upped
    verbatim, one ``compose up`` per service so a single broken model
    doesn't block the rest. Best-effort: a model that fails to start is
    warned about (with the manual recovery command) and skipped, never
    raised — the caller is restoring a whole VM and the agents matter
    more.

    A zero exit from detached ``compose up`` only means "container
    created" — llama-server exits immediately when its GGUF is missing
    or corrupt (e.g. after a ``--data-dir-in-vm`` rebuild wiped the
    models dir), leaving a stopped/restart-looping container behind a
    successful-looking up. Each service is therefore probed for a
    sustained ``running`` state before it's counted as started.

    Returns ``{"started": [...], "failed": [...]}`` (service names).
    """
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    started: list[str] = []
    failed: list[str] = []
    for svc_name in services:
        if not svc_name.startswith(LLAMACPP_SERVICE_PREFIX):
            continue
        slug = svc_name[len(LLAMACPP_SERVICE_PREFIX) :]
        gguf = _gguf_from_fragment(services.get(svc_name)) or "<file>"
        recovery = f"agx inference load llama-cpp {slug} --gguf {gguf}"
        try:
            compose_runner.up(cfg, svc_name)
        except RuntimeFailure as e:
            failed.append(svc_name)
            ui.warn(
                f"{svc_name} did not come back up: {e}. Its compose block is "
                f"intact; retry with `{recovery}`."
            )
            continue
        if _wait_service_running(cfg, svc_name):
            started.append(svc_name)
        else:
            failed.append(svc_name)
            ui.warn(
                f"{svc_name} was created but is not running — llama-server "
                f"likely crashed on start (missing or corrupt GGUF after the "
                f"rebuild?). Re-download the model into the models dir, then "
                f"retry with `{recovery}`."
            )
    return {"started": started, "failed": failed}


# Bounded probe for a re-upped service to prove it STAYS up: detached
# `compose up` returns 0 once the container is created, even when the
# process inside exits immediately.
_REUP_PROBE_TIMEOUT_S = 10.0
_REUP_PROBE_INTERVAL_S = 1.0


def _wait_service_running(
    cfg: AgentsConfig,
    svc: str,
    *,
    timeout_s: float = _REUP_PROBE_TIMEOUT_S,
    interval_s: float = _REUP_PROBE_INTERVAL_S,
) -> bool:
    """Poll :func:`_service_running` until the service reports running
    (or healthy) or the window closes. Host-side module — plain
    time.sleep is fine.
    """
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        if _service_running(cfg, svc):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def _gguf_from_fragment(svc: Any) -> str | None:
    """Extract the GGUF path (relative to the models dir) from a
    ``llama-cpp-*`` service block's ``command`` list, for recovery
    hints. Returns ``None`` when the fragment doesn't carry one.
    """
    if not isinstance(svc, dict):
        return None
    command = svc.get("command")
    if not isinstance(command, list):
        return None
    for i, token in enumerate(command):
        if token == "-m" and i + 1 < len(command):
            return str(command[i + 1]).removeprefix("/models/")
    return None


# ---------- helpers ----------


def _ollama_in_compose(cfg: AgentsConfig) -> bool:
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return False
    return OLLAMA_SERVICE_NAME in (doc.get("services") or {})


def _service_running(cfg: AgentsConfig, svc: str) -> bool:
    """Thin alias for :func:`sidecar.service_running` — kept because
    models_cmd imports it by this name.
    """
    return sidecar.service_running(cfg, svc)


def _ensure_models_dir(cfg: AgentsConfig, engine: str) -> None:
    """Create <data-dir>/models/<engine>/ if missing. Models dir is
    host-visible (unless --data-dir-in-vm). Owned by host user; the
    container writes via the volume mount with appropriate uid
    (Ollama image runs as root and chowns internally).
    """
    if cfg.paths.data_dir_in_vm:
        # The VM-side path; mp creates if missing via mkdir -p.
        target = f"{cfg.paths.vm_agents_root}/models/{engine}"
        mp.exec_in_vm(cfg.vm.name, "mkdir", "-p", target, check=False)
        return
    (cfg.paths.host_data_dir / "models" / engine).mkdir(parents=True, exist_ok=True)


def _validate_slug(slug: str) -> None:
    """Slug becomes a docker container name; enforce safe charset."""
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", slug):
        raise UserError(
            f"invalid llama-cpp model slug {slug!r}: must be lowercase "
            f"alphanumeric + hyphens, 1-39 chars, starting with [a-z0-9]"
        )


def _emit_status(payload: dict[str, Any]) -> None:
    lines: list[str] = ["inference engines:"]
    o = payload["ollama"]
    if not o["installed"]:
        lines.append("  ollama:    not installed (run `agx inference up ollama`)")
    else:
        state = "running" if o["running"] else "stopped"
        lines.append(f"  ollama:    {state}")
        lines.append(f"             image: {o.get('image') or '(unknown)'}")
        lines.append(f"             url:   {o['url_in_vm']}")
        models = o.get("models") or []
        if models:
            lines.append(f"             models: {len(models)} loaded")
            for m in models[:10]:
                lines.append(f"               {m['name']:<32}  {m['size']}")
            if len(models) > 10:
                lines.append(f"               ... ({len(models) - 10} more)")
        else:
            lines.append("             models: none (run `agx model pull ollama <name>`)")
    lc = payload["llama_cpp"]
    if not lc.get("image"):
        lines.append("  llama-cpp: image not pinned (optional; run `agx inference up llama-cpp`)")
    else:
        loaded = lc.get("loaded_models") or []
        lines.append(f"  llama-cpp: image pinned, {len(loaded)} per-model service(s) loaded")
        for m in loaded:
            state = "running" if m["running"] else "stopped"
            lines.append(f"             {m['slug']:<24}  {state}")
    ui.emit(payload, human="\n".join(lines))
