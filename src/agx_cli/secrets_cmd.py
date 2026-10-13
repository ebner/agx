"""Per-agent secrets stored in ``<data-dir>/<name>/.env``.

The toolkit owns one project-level ``<data-dir>/.env`` (just for compose
``${VAR}`` interpolation — HERMES_UID/GID, OLLAMA_API_KEY, etc.) and
**one ``.env`` per agent**. The per-agent file is wired into compose
via the service fragment's ``env_file:`` directive, so its KEY=VALUE
pairs become container environment variables — exactly what skills
like ``firecrawl`` read at runtime.

CLI surface:

- ``agx secret set <agent> KEY [VALUE]`` — set; value prompted
  hidden when omitted, never printed back.
- ``agx secret unset <agent> KEY``
- ``agx secret list <agent>`` — names only.
- ``agx secret clear <agent>`` — wipe the whole file (and, for
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

from agx_cli import compose, compose_runner, hermes_config, mp, ui
from agx_cli import config as cfg_mod
from agx_cli.agents_cmd import _detect_type_from_compose
from agx_cli.agents_cmd import service_name as _service_name_for
from agx_cli.config import AgentsConfig
from agx_cli.errors import UserError

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
    profile: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    _validate_key(key)
    if "\n" in value or "\r" in value:
        raise UserError("secret value must not contain newlines")
    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_service(cfg, agent_name)
    profile = _require_hermes_for_profile(agent_type, profile)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # The .env half is container-wide: env_file values become container
        # env, which every profile's gateway shares. Only the
        # terminal.env_passthrough half is per-profile (each profile's
        # config.yaml filters its own sandbox subprocesses) — GH #6 tier 2.
        env = _read_env(cfg, agent_name)
        env[key] = value
        _write_env(cfg, agent_name, env)
        _ensure_env_file_in_compose(cfg, agent_name, svc)
        if agent_type == "hermes":
            _ensure_env_passthrough(cfg, agent_name, key, profile=profile)

    compose_runner.up(cfg, svc)
    # A profile-only passthrough edit changes config.yaml, not compose
    # inputs — `up` is then a NO-OP and the running gateway would keep
    # filtering the var until the next unrelated restart.
    if profile is not None:
        compose_runner.restart(cfg, svc)
    ui.emit(
        {"agent": agent_name, "key": key, "status": "set"},
        human=f"secret[{agent_name}] {key}: set",
    )


def unset_secret(
    agent_name: str,
    key: str,
    *,
    profile: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    _validate_key(key)
    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_service(cfg, agent_name)
    profile = _require_hermes_for_profile(agent_type, profile)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        env = _read_env(cfg, agent_name)
        if key not in env:
            raise UserError(f"secret {key!r} not set for {agent_name!r}")
        del env[key]
        _write_env(cfg, agent_name, env)
        if agent_type == "hermes":
            _drop_env_passthrough(cfg, agent_name, key, profile=profile)

    compose_runner.up(cfg, svc)
    if profile is not None:
        compose_runner.restart(cfg, svc)
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
            # Sweep the DEFAULT profile plus every named profile: the
            # .env wipe below erases the key inventory, so a per-profile
            # clear could never clean the remaining profiles afterwards —
            # stale passthrough entries would re-expose a future value
            # set under the same key.
            for prof in [None, *_list_profile_names(cfg, svc)]:
                doc = hermes_config.read_doc(cfg, agent_name, prof)
                before = list((doc.get("terminal") or {}).get("env_passthrough") or [])
                for key in keys:
                    hermes_config.remove_env_passthrough(doc, key)
                after = list((doc.get("terminal") or {}).get("env_passthrough") or [])
                if before != after:
                    hermes_config.write_atomically(cfg, agent_name, doc, prof)

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
    compose.await_vm_view(cfg, path)


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


def _ensure_env_passthrough(
    cfg: AgentsConfig, agent_name: str, key: str, profile: str | None = None
) -> None:
    """Add ``key`` to the agent's ``terminal.env_passthrough`` so it
    actually reaches Hermes' sandboxed shell / execute_code subprocesses.

    Without this, the var lives in the Hermes Python process (visible
    to built-in tools that read ``os.environ``) but is filtered out of
    every subprocess Hermes spawns — meaning the LLM's own shell-based
    diagnostics report "not set" even though the integration works.
    """
    doc = hermes_config.read_doc(cfg, agent_name, profile)
    hermes_config.add_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc, profile)


def _drop_env_passthrough(
    cfg: AgentsConfig, agent_name: str, key: str, profile: str | None = None
) -> None:
    doc = hermes_config.read_doc(cfg, agent_name, profile)
    hermes_config.remove_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc, profile)


def _list_profile_names(cfg: AgentsConfig, svc: str) -> list[str]:
    """Named profiles present in the running container (empty when the
    exec fails — e.g. no profiles dir yet)."""
    from agx_cli.ops import _compose_exec_in_vm_argv

    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "sh", "-c", "ls /opt/data/profiles 2>/dev/null || true", no_tty=True
        ),
        check=False,
    )
    if result.returncode != 0:
        return []
    names = []
    for line in result.stdout.splitlines():
        try:
            normalized = hermes_config.normalize_profile(line.strip())
        except Exception:
            continue
        if normalized:
            names.append(normalized)
    return names


def _require_hermes_for_profile(agent_type: str, profile: str | None) -> str | None:
    """Normalize + gate the --profile flag: Hermes-only, since the profile
    dimension only exists for the env_passthrough half (OpenClaw has no
    sandbox env filter and no profiles in this sense)."""
    normalized = hermes_config.normalize_profile(profile)
    if normalized is not None and agent_type != "hermes":
        raise UserError("--profile is Hermes-only (OpenClaw has no profile config surface)")
    return normalized


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
