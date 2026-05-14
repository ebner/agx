"""Self-hosted Firecrawl stack, orchestrated as a project-level sibling.

One Firecrawl deployment per VM, shared across every Hermes agent the
toolkit manages. ``agents firecrawl up`` adds five docker compose
services (api / playwright / redis / rabbitmq / postgres), generates
the auth bits into ``<data-dir>/.env``, and auto-wires every Hermes
agent's ``FIRECRAWL_API_KEY`` + ``FIRECRAWL_API_URL`` into
``terminal.env_passthrough`` so both the Hermes Python tools AND the
LLM's shell-based diagnostics see the configuration.

Pinning policy
--------------

Upstream ships only floating tags (``latest``, ``linux-amd64``). The
toolkit's locked "no ``:latest``" rule means we have to digest-pin —
but we can't bake digests as constants because they drift weekly.
Resolution: on first ``firecrawl up``, the toolkit pulls each
upstream floating tag, captures the resulting ``RepoDigest`` via
``docker image inspect``, writes the digests to
``<data-dir>/.firecrawl-state.json``, and the generated compose entry
references only the digest-pinned form (``image@sha256:…``). Subsequent
``up`` invocations reuse the captured digests. ``firecrawl up
--upgrade`` re-resolves them.

State file: ``<data-dir>/.firecrawl-state.json``
------------------------------------------------

::

    {
      "images": {
        "api":        "ghcr.io/firecrawl/firecrawl@sha256:…",
        "playwright": "ghcr.io/firecrawl/playwright-service@sha256:…",
        "postgres":   "ghcr.io/firecrawl/nuq-postgres@sha256:…",
        "redis":      "redis@sha256:…",
        "rabbitmq":   "rabbitmq@sha256:…"
      },
      "api_url":    "http://firecrawl-api:3002"
    }

The other auth secrets (``FIRECRAWL_API_KEY``, ``BULL_AUTH_KEY``,
``POSTGRES_PASSWORD``) live in ``<data-dir>/.env`` — same place as
``HERMES_UID`` / ``HERMES_GID`` — so compose ``${VAR}`` interpolation
resolves them naturally.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from agents_cli import compose, compose_runner, hermes_config, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.compose import (
    FIRECRAWL_API_IMAGE_BASE,
    FIRECRAWL_PLAYWRIGHT_IMAGE_BASE,
    FIRECRAWL_POSTGRES_IMAGE_BASE,
    FIRECRAWL_RABBITMQ_IMAGE_BASE,
    FIRECRAWL_REDIS_IMAGE_BASE,
    FIRECRAWL_SERVICE_PREFIX,
)
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError

# Upstream-floating references the toolkit uses ONCE to resolve digests.
# These get written to the compose file only as ``<repo>@sha256:<digest>``,
# never as ``<repo>:<tag>`` — see module docstring on pinning policy.
FLOATING_REFS: dict[str, str] = {
    "api": f"{FIRECRAWL_API_IMAGE_BASE}:linux-amd64",
    "playwright": f"{FIRECRAWL_PLAYWRIGHT_IMAGE_BASE}:latest",
    "postgres": f"{FIRECRAWL_POSTGRES_IMAGE_BASE}:latest",
    "redis": f"{FIRECRAWL_REDIS_IMAGE_BASE}:alpine",
    "rabbitmq": f"{FIRECRAWL_RABBITMQ_IMAGE_BASE}:3-management",
}

# Env-var names the project-level .env carries when firecrawl is up.
# Hermes agents see API_KEY + API_URL via compose ``${VAR:-}``
# interpolation in their service fragment; passthrough is a separate
# Hermes-internal allowlist for the sandboxed terminal/execute_code
# subprocesses (see hermes_config.add_env_passthrough).
PROJECT_ENV_KEYS: tuple[str, ...] = (
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "FIRECRAWL_BULL_AUTH_KEY",
    "FIRECRAWL_POSTGRES_PASSWORD",
)

# Subset that's also propagated into Hermes' env_passthrough — only the
# pair the agent's tools actually use, not the internal-only
# BULL/POSTGRES secrets that are for the Firecrawl stack itself.
AGENT_PASSTHROUGH_KEYS: tuple[str, ...] = (
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
)

# `agents firecrawl status` returns these payload keys so JSON callers
# can rely on them.
STATUS_KEYS: tuple[str, ...] = ("running", "api_url", "wired_agents", "images")


def _state_path(cfg: AgentsConfig) -> Path:
    return cfg.paths.host_data_dir / ".firecrawl-state.json"


def read_state(cfg: AgentsConfig) -> dict[str, Any]:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(cfg: AgentsConfig, payload: dict[str, Any]) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def clear_state(cfg: AgentsConfig) -> None:
    _state_path(cfg).unlink(missing_ok=True)


def is_installed(cfg: AgentsConfig) -> bool:
    """True iff the project compose has firecrawl services AND the state
    file says we own them. Used by ``agents create`` to decide whether
    to auto-wire a new agent's passthrough.
    """
    state = read_state(cfg)
    if "images" not in state:
        return False
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return False
    services = doc.get("services") or {}
    return any(s.startswith(FIRECRAWL_SERVICE_PREFIX) for s in services)


# ---------- CLI entry points ----------


def up(
    *,
    upgrade: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Install (or upgrade) the firecrawl stack and wire every Hermes agent.

    First-run flow: pull floating tags, resolve digests, generate
    compose entries, populate project ``.env`` secrets, ``compose up``,
    wait for ``firecrawl-api`` healthcheck, add the FIRECRAWL_API_KEY /
    FIRECRAWL_API_URL pair to each Hermes agent's
    ``terminal.env_passthrough`` so the wiring is visible to sandboxed
    tools too.

    ``upgrade=True`` re-resolves digests against the upstream floating
    tags, even if a state file already exists.
    """
    cfg = cfg_mod.load(config_path)
    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime not initialised — run `agents vm init` first")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        state = read_state(cfg)
        if not state or upgrade:
            images = _resolve_digests(cfg)
            state = {"images": images, "api_url": "http://firecrawl-api:3002"}
        else:
            images = state["images"]

        _ensure_project_env(cfg)
        _ensure_postgres_dir(cfg)

        doc = compose.read_compose_doc(cfg)
        services = doc.setdefault("services", {})
        fragments = compose.firecrawl_service_fragments(images=images)
        for svc_name, fragment in fragments.items():
            services[svc_name] = fragment
        compose.write_compose_atomically(cfg, doc)

        write_state(cfg, state)

        # Auto-wire every existing Hermes agent's passthrough.
        wired = _ensure_all_agents_wired(cfg)

    # Bring the stack up. Healthchecks declared in the fragments mean
    # `compose up -d` returns after the api is healthy.
    compose_runner.up(cfg, *fragments.keys())

    stale = _agents_with_stale_fragment(cfg)
    payload = {
        "running": True,
        "api_url": state["api_url"],
        "wired_agents": wired,
        "stale_agents": stale,
        "images": images,
    }
    ui.emit(
        payload,
        human=(
            f"firecrawl: up at {state['api_url']}\n"
            f"  wired agents: {', '.join(wired) or '(none)'}\n"
            f"  images:\n" + "\n".join(f"    {k}: {v}" for k, v in images.items())
        ),
    )
    if stale:
        ui.warn(
            "Agents created before the firecrawl interpolation entries "
            "landed in the compose fragment don't yet have FIRECRAWL_API_URL "
            "in their container env. Reseat them to pick it up:\n  "
            + "\n  ".join(f"agents reseat {n}" for n in stale)
        )
    return payload


def down(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    """Stop the firecrawl stack — keeps the postgres data, removes the
    agent passthrough wiring so the LLM's diagnostics correctly report
    "not configured" again.
    """
    cfg = cfg_mod.load(config_path)
    if not is_installed(cfg):
        raise UserError("firecrawl is not installed")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        services = list(_firecrawl_services_in_compose(cfg))
        if services:
            compose_runner.stop(cfg, *services)
            compose_runner.rm(cfg, *services)
        _drop_all_agents_wired(cfg)

    ui.emit({"running": False}, human="firecrawl: stopped (postgres data preserved)")


def destroy(
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Tear down completely: stop, drop compose entries, wipe state file,
    drop project ``.env`` keys, drop agent passthrough wiring, delete
    the postgres data dir.
    """
    cfg = cfg_mod.load(config_path)
    if not yes and not ui.confirm(
        "destroy the firecrawl stack? This deletes the postgres data dir too.",
        default=False,
    ):
        raise UserError("aborted by user")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        services = list(_firecrawl_services_in_compose(cfg))
        if services:
            compose_runner.stop(cfg, *services)
            compose_runner.rm(cfg, *services)

        doc = compose.read_compose_doc(cfg)
        compose_services = doc.get("services") or {}
        for svc_name in list(compose_services):
            if svc_name.startswith(FIRECRAWL_SERVICE_PREFIX):
                del compose_services[svc_name]
        compose.write_compose_atomically(cfg, doc)

        _drop_all_agents_wired(cfg)
        _drop_project_env_keys(cfg)
        clear_state(cfg)
        _remove_postgres_dir(cfg)

    ui.emit({"running": False, "destroyed": True}, human="firecrawl: destroyed")


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    state = read_state(cfg)
    services = list(_firecrawl_services_in_compose(cfg))
    running = bool(services) and _service_running(cfg, "firecrawl-api")
    wired = _list_wired_agents(cfg)
    payload = {
        "running": running,
        "installed": bool(services),
        "api_url": state.get("api_url"),
        "wired_agents": wired,
        "images": state.get("images", {}),
    }
    if not services:
        ui.emit(payload, human="firecrawl: not installed (run `agents firecrawl up`)")
        return payload
    ui.emit(
        payload,
        human=(
            f"firecrawl: {'running' if running else 'stopped'} at "
            f"{state.get('api_url', '?')}\n"
            f"  wired agents: {', '.join(wired) or '(none)'}"
        ),
    )
    return payload


def logs(
    *,
    service: str | None = None,
    follow: bool = False,
    since: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    cfg = cfg_mod.load(config_path)
    target = service or "firecrawl-api"
    if not target.startswith(FIRECRAWL_SERVICE_PREFIX):
        target = f"{FIRECRAWL_SERVICE_PREFIX}{target}"
    if not _service_in_compose(cfg, target):
        raise UserError(f"unknown firecrawl service {target!r}")
    result = compose_runner.logs(cfg, target, follow=follow, since=since)
    ui.emit({"service": target, "logs": result.stdout}, human=result.stdout.rstrip())


# ---------- helpers exposed to agents_cmd.create ----------


def ensure_agent_wired(cfg: AgentsConfig, agent_name: str) -> None:
    """Add the FIRECRAWL_API_KEY / FIRECRAWL_API_URL pair to a single
    agent's ``terminal.env_passthrough`` — called from ``agents create``
    so a new Hermes agent auto-picks-up firecrawl when it's already up.

    No-op when firecrawl isn't installed. Idempotent on the agent side
    (``hermes_config.add_env_passthrough`` deduplicates).
    """
    if not is_installed(cfg):
        return
    _wire_agent(cfg, agent_name)


# ---------- internals ----------


def _resolve_digests(cfg: AgentsConfig) -> dict[str, str]:
    """Pull each upstream floating tag, harvest its repo-digest, return
    a map of role → ``<repo>@sha256:…``. The compose file only ever
    references the digest form so the toolkit's "no ``:latest``" rule
    holds.
    """
    out: dict[str, str] = {}
    for role, floating in FLOATING_REFS.items():
        ui.info(f"pulling {floating}")
        pull = mp.exec_in_vm(cfg.vm.name, "docker", "pull", floating, check=False)
        if pull.returncode != 0:
            raise RuntimeFailure(f"docker pull failed for {floating}: {pull.stderr.strip()}")
        inspect = mp.exec_in_vm(
            cfg.vm.name,
            "docker",
            "image",
            "inspect",
            floating,
            "--format",
            "{{index .RepoDigests 0}}",
            check=False,
        )
        if inspect.returncode != 0 or not inspect.stdout.strip():
            raise RuntimeFailure(
                f"could not resolve digest for {floating}: {inspect.stderr.strip()}"
            )
        digest_ref = inspect.stdout.strip()
        # Validate it's on the allowlist BEFORE we let it land in compose.
        compose.validate_image_ref(digest_ref)
        out[role] = digest_ref
    return out


def _ensure_project_env(cfg: AgentsConfig) -> None:
    """Write the four project-level env vars (api key, url, bull auth,
    postgres password) into the project ``.env`` if not already set.
    Existing values are preserved across re-ups.

    Picks the right ``.env`` for the current mode: host file in
    host-mount mode (visible to VM via the multipass mount), VM-side
    file in ``--data-dir-in-vm`` mode (where the host has no copy).
    """
    existing = _read_project_env_file(cfg)
    if not existing.get("FIRECRAWL_API_KEY"):
        existing["FIRECRAWL_API_KEY"] = f"fc-self-hosted-{secrets.token_hex(8)}"
    if not existing.get("FIRECRAWL_API_URL"):
        existing["FIRECRAWL_API_URL"] = "http://firecrawl-api:3002"
    if not existing.get("FIRECRAWL_BULL_AUTH_KEY"):
        existing["FIRECRAWL_BULL_AUTH_KEY"] = secrets.token_hex(32)
    if not existing.get("FIRECRAWL_POSTGRES_PASSWORD"):
        existing["FIRECRAWL_POSTGRES_PASSWORD"] = secrets.token_hex(16)
    _write_project_env_file(cfg, existing)


def _drop_project_env_keys(cfg: AgentsConfig) -> None:
    existing = _read_project_env_file(cfg)
    if not existing:
        return
    for k in PROJECT_ENV_KEYS:
        existing.pop(k, None)
    _write_project_env_file(cfg, existing)


def _read_project_env_file(cfg: AgentsConfig) -> dict[str, str]:
    """Mode-aware read of the project ``.env``. Returns ``{}`` when the
    file is missing.
    """
    if cfg.paths.data_dir_in_vm:
        import os as _os
        import uuid as _uuid

        vm_path = f"{cfg.paths.vm_agents_root}/.env"
        check = mp.exec_in_vm(cfg.vm.name, "test", "-f", vm_path, check=False)
        if check.returncode != 0:
            return {}
        host_temp = (
            compose.host_staging_dir(cfg)
            / f"projectenv-read-{_os.getpid()}-{_uuid.uuid4().hex[:8]}.env"
        )
        try:
            mp.transfer_from_vm(cfg.vm.name, vm_path, host_temp)
            return _parse_env_string(host_temp.read_text(encoding="utf-8"))
        finally:
            host_temp.unlink(missing_ok=True)
    path = cfg.paths.host_data_dir / ".env"
    if not path.exists():
        return {}
    return _parse_env_string(path.read_text(encoding="utf-8"))


def _write_project_env_file(cfg: AgentsConfig, env: dict[str, str]) -> None:
    """Mode-aware write to the project ``.env``. In ``--data-dir-in-vm``
    mode, stages on host and ``multipass transfer``s into the VM.

    The project ``.env`` is at ``<vm_agents_root>/.env`` (ubuntu-owned),
    NOT inside any container — so SFTP works in both modes. Unlike
    per-agent ``.env`` files which live under subuid-owned ``/opt/data``.
    """
    content = _render_env_string(env)
    if cfg.paths.data_dir_in_vm:
        import os as _os
        import uuid as _uuid

        vm_path = f"{cfg.paths.vm_agents_root}/.env"
        host_temp = (
            compose.host_staging_dir(cfg)
            / f"projectenv-write-{_os.getpid()}-{_uuid.uuid4().hex[:8]}.env"
        )
        try:
            host_temp.write_text(content, encoding="utf-8")
            mp.transfer_to_vm(host_temp, cfg.vm.name, vm_path)
        finally:
            host_temp.unlink(missing_ok=True)
        return
    path = cfg.paths.host_data_dir / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _parse_env_string(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v
    return out


def _render_env_string(env: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in env.items()) + ("\n" if env else "")


def _ensure_postgres_dir(cfg: AgentsConfig) -> None:
    """Make sure ``<data-dir>/firecrawl/postgres/`` exists. Postgres
    initialises an empty dir on first start. In ``--data-dir-in-vm``
    mode this happens in the VM; otherwise on the host mount.
    """
    if cfg.paths.data_dir_in_vm:
        mp.exec_in_vm(
            cfg.vm.name,
            "mkdir",
            "-p",
            f"{cfg.paths.vm_agents_root}/firecrawl/postgres",
            check=False,
        )
        return
    (cfg.paths.host_data_dir / "firecrawl" / "postgres").mkdir(parents=True, exist_ok=True)


def _remove_postgres_dir(cfg: AgentsConfig) -> None:
    if cfg.paths.data_dir_in_vm:
        mp.exec_in_vm(
            cfg.vm.name,
            "rm",
            "-rf",
            f"{cfg.paths.vm_agents_root}/firecrawl",
            check=False,
        )
        return
    import shutil

    path = cfg.paths.host_data_dir / "firecrawl"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _agents_with_stale_fragment(cfg: AgentsConfig) -> list[str]:
    """Return Hermes agents whose compose service ``environment`` block
    doesn't yet have ``FIRECRAWL_API_URL`` (and friends) as
    ``${VAR:-}`` interpolation entries. These were created before the
    firecrawl wiring landed in the service fragment template — running
    ``agents reseat <name>`` regenerates the block from the current
    template, picking up the missing entries.

    Auto-reseating from within ``firecrawl up`` would force-recreate
    the agent's container and interrupt its gateway — too disruptive
    as a silent side-effect, so we surface a hint instead.
    """
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return []
    out: list[str] = []
    services = doc.get("services") or {}
    for svc_name, svc in services.items():
        if not svc_name.startswith("hermes-") or not isinstance(svc, dict):
            continue
        env = svc.get("environment") or {}
        if not all(k in env for k in AGENT_PASSTHROUGH_KEYS):
            out.append(svc_name[len("hermes-") :])
    return out


def _hermes_agents(cfg: AgentsConfig) -> list[str]:
    doc = compose.read_compose_doc(cfg)
    return [s[len("hermes-") :] for s in (doc.get("services") or {}) if s.startswith("hermes-")]


def _ensure_all_agents_wired(cfg: AgentsConfig) -> list[str]:
    wired: list[str] = []
    for agent in _hermes_agents(cfg):
        _wire_agent(cfg, agent)
        wired.append(agent)
    return wired


def _drop_all_agents_wired(cfg: AgentsConfig) -> None:
    for agent in _hermes_agents(cfg):
        _unwire_agent(cfg, agent)


def _wire_agent(cfg: AgentsConfig, agent_name: str) -> None:
    doc = hermes_config.read_doc(cfg, agent_name)
    for key in AGENT_PASSTHROUGH_KEYS:
        hermes_config.add_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc)


def _unwire_agent(cfg: AgentsConfig, agent_name: str) -> None:
    doc = hermes_config.read_doc(cfg, agent_name)
    for key in AGENT_PASSTHROUGH_KEYS:
        hermes_config.remove_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc)


def _list_wired_agents(cfg: AgentsConfig) -> list[str]:
    out: list[str] = []
    for agent in _hermes_agents(cfg):
        try:
            doc = hermes_config.read_doc(cfg, agent)
        except (RuntimeFailure, UserError):
            continue
        passthrough = (doc.get("terminal") or {}).get("env_passthrough") or []
        if all(k in passthrough for k in AGENT_PASSTHROUGH_KEYS):
            out.append(agent)
    return out


def _firecrawl_services_in_compose(cfg: AgentsConfig) -> list[str]:
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return []
    services = doc.get("services") or {}
    return [s for s in services if s.startswith(FIRECRAWL_SERVICE_PREFIX)]


def _service_in_compose(cfg: AgentsConfig, name: str) -> bool:
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return False
    return name in (doc.get("services") or {})


def _service_running(cfg: AgentsConfig, svc: str) -> bool:
    result = compose_runner.ps(cfg)
    if result.returncode != 0:
        return False
    try:
        entries = json.loads(result.stdout) if result.stdout.strip() else []
        if isinstance(entries, dict):
            entries = [entries]
    except json.JSONDecodeError:
        entries = []
        for line in result.stdout.splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("Service") == svc and entry.get("State") == "running":
            return True
    return False
