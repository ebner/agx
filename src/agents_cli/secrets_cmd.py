"""Per-agent secrets stored in ``<data-dir>/<name>/.env``.

The toolkit owns one project-level ``<data-dir>/.env`` (just for compose
``${VAR}`` interpolation — HERMES_UID/GID, OLLAMA_API_KEY, etc.) and
**one ``.env`` per agent**. The per-agent file is wired into compose
via the service fragment's ``env_file:`` directive, so its KEY=VALUE
pairs become container environment variables — exactly what skills
like ``firecrawl`` read at runtime.

CLI surface:

- ``agents secret set <agent> KEY [VALUE]`` — set; value prompted
  hidden when omitted, never printed back.
- ``agents secret unset <agent> KEY``
- ``agents secret list <agent>`` — names only.
- ``agents secret clear <agent>`` — wipe the whole file (and, for
  Hermes agents, drop each wiped key from ``terminal.env_passthrough``
  — same cleanup ``unset`` does per key).

Mutations hold ``<data-dir>/.agents.lock`` (same flock every other
mutation uses), then restart the agent's container so the new env
takes effect. Compose only re-reads ``env_file`` on container
recreate, so a simple ``compose start`` would silently keep the old
env — we use ``compose up -d`` which recreates if config changed.

Backfill: agents created before this command existed don't have
``env_file:`` in their compose service. The first ``secret set`` for
such an agent adds the directive idempotently.
"""

from __future__ import annotations

import re
from pathlib import Path

from agents_cli import compose, compose_runner, hermes_config, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import _detect_type_from_compose
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError

ENV_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# ---------- path helpers ----------


def agent_env_path(cfg: AgentsConfig, agent_name: str) -> Path:
    """Host-side path. Valid in host-mount mode; in ``--data-dir-in-vm``
    mode the file lives only in the VM and this path is used as the
    staging-temp location via :func:`compose.host_staging_dir`.
    """
    return cfg.paths.host_data_dir / agent_name / ".env"


def vm_agent_env_path(cfg: AgentsConfig, agent_name: str) -> str:
    return f"{cfg.paths.vm_agents_root}/{agent_name}/.env"


def compose_env_file_ref(agent_name: str) -> str:
    """The compose-relative reference (resolved against
    ``--project-directory`` at validate time = VM project dir)."""
    return f"./{agent_name}/.env"


# ---------- public CLI entry points ----------


def set_secret(
    agent_name: str,
    key: str,
    value: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    _validate_key(key)
    if "\n" in value or "\r" in value:
        raise UserError("secret value must not contain newlines")
    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_service(cfg, agent_name)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        env = _read_env(cfg, agent_name)
        env[key] = value
        _write_env(cfg, agent_name, env)
        _ensure_env_file_in_compose(cfg, agent_name, svc)
        if agent_type == "hermes":
            _ensure_env_passthrough(cfg, agent_name, key)

    compose_runner.up(cfg, svc)
    ui.emit(
        {"agent": agent_name, "key": key, "status": "set"},
        human=f"secret[{agent_name}] {key}: set",
    )


def unset_secret(
    agent_name: str,
    key: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    _validate_key(key)
    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_service(cfg, agent_name)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        env = _read_env(cfg, agent_name)
        if key not in env:
            raise UserError(f"secret {key!r} not set for {agent_name!r}")
        del env[key]
        _write_env(cfg, agent_name, env)
        if agent_type == "hermes":
            _drop_env_passthrough(cfg, agent_name, key)

    compose_runner.up(cfg, svc)
    ui.emit(
        {"agent": agent_name, "key": key, "status": "unset"},
        human=f"secret[{agent_name}] {key}: unset",
    )


def list_secrets(
    agent_name: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> list[str]:
    cfg = cfg_mod.load(config_path)
    _resolve_service(cfg, agent_name)
    keys = sorted(_read_env(cfg, agent_name).keys())
    ui.table(
        [{"key": k} for k in keys],
        ["key"],
    )
    return keys


def clear_secrets(
    agent_name: str,
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_service(cfg, agent_name)
    if not yes and not ui.confirm(f"clear ALL secrets for {agent_name!r}?", default=False):
        raise UserError("aborted by user")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Capture the key names BEFORE wiping: `set` mirrors each key
        # into terminal.env_passthrough (Hermes only), so `clear` must
        # drop those entries too or they linger forever. Same mechanics
        # and OpenClaw no-op posture as unset_secret; idempotent (a
        # second clear finds no keys and touches nothing).
        keys = sorted(_read_env(cfg, agent_name))
        _write_env(cfg, agent_name, {})
        if agent_type == "hermes" and keys:
            doc = hermes_config.read_doc(cfg, agent_name)
            for key in keys:
                hermes_config.remove_env_passthrough(doc, key)
            hermes_config.write_atomically(cfg, agent_name, doc)

    compose_runner.up(cfg, svc)
    ui.emit(
        {"agent": agent_name, "status": "cleared"},
        human=f"secret[{agent_name}]: all cleared",
    )


# ---------- internals ----------


def _validate_key(key: str) -> None:
    if not ENV_KEY_NAME_RE.match(key):
        raise UserError(
            f"invalid secret name {key!r}; must match ^[A-Z][A-Z0-9_]*$ "
            "(uppercase letters/digits/underscore, leading letter)"
        )


def _resolve_service(cfg: AgentsConfig, agent_name: str) -> tuple[str, str]:
    agent_type = _detect_type_from_compose(cfg, agent_name)
    if agent_type is None:
        raise UserError(f"agent {agent_name!r} not in compose")
    return _service_name_for(agent_name, agent_type), agent_type


def _read_env(cfg: AgentsConfig, agent_name: str) -> dict[str, str]:
    """Read the per-agent ``.env``, transparent to ``--data-dir-in-vm``."""
    if cfg.paths.data_dir_in_vm:
        import os
        import uuid

        host_temp = (
            compose.host_staging_dir(cfg)
            / f"agentenv-read-{os.getpid()}-{uuid.uuid4().hex[:8]}.env"
        )
        try:
            result = mp.exec_in_vm(
                cfg.vm.name, "test", "-f", vm_agent_env_path(cfg, agent_name), check=False
            )
            if result.returncode != 0:
                return {}
            mp.transfer_from_vm(cfg.vm.name, vm_agent_env_path(cfg, agent_name), host_temp)
            return _parse_env_file(host_temp.read_text(encoding="utf-8"))
        finally:
            host_temp.unlink(missing_ok=True)
    path = agent_env_path(cfg, agent_name)
    if not path.exists():
        return {}
    return _parse_env_file(path.read_text(encoding="utf-8"))


def _write_env(cfg: AgentsConfig, agent_name: str, env: dict[str, str]) -> None:
    """Write the per-agent ``.env``. In ``--data-dir-in-vm`` mode, stages
    on host and ``multipass transfer``s into the VM.
    """
    content = _render_env_file(env)
    if cfg.paths.data_dir_in_vm:
        import os
        import uuid

        host_temp = (
            compose.host_staging_dir(cfg)
            / f"agentenv-write-{os.getpid()}-{uuid.uuid4().hex[:8]}.env"
        )
        try:
            host_temp.write_text(content, encoding="utf-8")
            mp.exec_in_vm(
                cfg.vm.name,
                "mkdir",
                "-p",
                f"{cfg.paths.vm_agents_root}/{agent_name}",
            )
            mp.transfer_to_vm(host_temp, cfg.vm.name, vm_agent_env_path(cfg, agent_name))
        finally:
            host_temp.unlink(missing_ok=True)
        return
    path = agent_env_path(cfg, agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _parse_env_file(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        out[k.strip()] = v
    return out


def _render_env_file(env: dict[str, str]) -> str:
    if not env:
        return ""
    lines = [f"{k}={v}" for k, v in sorted(env.items())]
    return "\n".join(lines) + "\n"


def _ensure_env_passthrough(cfg: AgentsConfig, agent_name: str, key: str) -> None:
    """Add ``key`` to the agent's ``terminal.env_passthrough`` so it
    actually reaches Hermes' sandboxed shell / execute_code subprocesses.

    Without this, the var lives in the Hermes Python process (visible
    to built-in tools that read ``os.environ``) but is filtered out of
    every subprocess Hermes spawns — meaning the LLM's own shell-based
    diagnostics report "not set" even though the integration works.
    """
    doc = hermes_config.read_doc(cfg, agent_name)
    hermes_config.add_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc)


def _drop_env_passthrough(cfg: AgentsConfig, agent_name: str, key: str) -> None:
    doc = hermes_config.read_doc(cfg, agent_name)
    hermes_config.remove_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc)


def _ensure_env_file_in_compose(cfg: AgentsConfig, agent_name: str, svc_name: str) -> None:
    """Backfill ``env_file: ./<name>/.env`` on the service if not already
    present. Idempotent.

    Agents created before this command existed had no ``env_file``
    clause; first ``secret set`` repairs them via the standard compose
    mutation path (atomic VM-side write + ``docker compose ... config -q``
    validation).
    """
    ref = compose_env_file_ref(agent_name)
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    svc = services.get(svc_name)
    if not isinstance(svc, dict):
        # Service vanished between resolve and lock-acquire — let the
        # restart fail loudly with the real reason.
        return
    existing = svc.get("env_file")
    if existing is None:
        svc["env_file"] = [ref]
    elif isinstance(existing, str):
        if existing == ref:
            return
        svc["env_file"] = [existing, ref]
    elif isinstance(existing, list):
        if ref in existing:
            return
        existing.append(ref)
    else:
        # Unknown shape; replace with what we want.
        svc["env_file"] = [ref]
    compose.write_compose_atomically(cfg, doc)


# ---------- doctor helpers (optional surface) ----------


def all_secret_keys(cfg: AgentsConfig) -> dict[str, list[str]]:
    """For every agent in compose, return the list of secret-key names
    (no values). Used by ``status``-style commands that want to surface
    "coder has 3 secrets set" without leaking content.
    """
    out: dict[str, list[str]] = {}
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    for svc_name in services:
        for prefix in ("hermes-", "openclaw-"):
            if svc_name.startswith(prefix):
                agent_name = svc_name[len(prefix) :]
                out[agent_name] = sorted(_read_env(cfg, agent_name).keys())
                break
    return out


# Public re-export — useful in tests and from `agents secret list --json`.
read_env = _read_env
write_env = _write_env
