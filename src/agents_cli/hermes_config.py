"""Round-trip edits to each Hermes agent's config.yaml.

Each agent has its own ``<data-dir>/<name>/.data/config.yaml`` (the
file the Hermes container sees at ``$HERMES_HOME/config.yaml``). The
file is hand-editable, so we round-trip with ruamel.yaml to preserve
the user's comments and key order.

Write pattern (different from ``compose.write_compose_atomically`` —
this one goes THROUGH the container):

1. Mutate in memory.
2. ``docker compose exec -T <svc> sh -c 'cat > <staged>'`` with the
   YAML content piped via stdin. Running as the container's runtime
   user (which owns ``/opt/data`` under rootless Docker) is the only
   way to write here in ``--data-dir-in-vm`` mode, where the dir is
   subuid-owned and multipass-SFTP runs as ``ubuntu`` (which isn't
   the owner).
3. Validate via ``hermes config validate`` exec'd in the same
   container.
4. Atomic same-fs rename inside the container: ``mv <staged> config.yaml``.
5. RM the staged file on any error path.

The CLI refuses any change that would bind the dashboard to anything
other than ``127.0.0.1`` / ``localhost`` / ``::1`` — one of the locked
security invariants from the plan.
"""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from agents_cli import compose, mp
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError

ALLOWED_DASHBOARD_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def config_path(cfg: AgentsConfig, agent_name: str) -> Path:
    """The host-side path of a Hermes agent's config.yaml (visible through
    the mount in host-mount mode; only exists in the VM under
    ``--data-dir-in-vm``).
    """
    return cfg.paths.host_data_dir / agent_name / ".data" / "config.yaml"


def vm_config_path(cfg: AgentsConfig, agent_name: str) -> str:
    """VM-host-side path of an agent's config.yaml (the bind-mount source).

    Used by ``read_doc`` in ``--data-dir-in-vm`` mode to pull the file
    out via ``multipass transfer``. Writes don't go through this path
    anymore — see :func:`write_atomically`.
    """
    return f"{cfg.paths.vm_agents_root}/{agent_name}/.data/config.yaml"


def container_config_path() -> str:
    """In-container path of the agent's config.yaml — what processes
    inside the container see at ``$HERMES_HOME/config.yaml``. Used by
    write paths that go through ``docker compose exec``.
    """
    return "/opt/data/config.yaml"


def load(path: Path) -> Any:
    """Round-trip read; returns an empty ruamel mapping if the file is missing."""
    y = _yaml()
    if not path.exists():
        return y.load("{}\n")
    return y.load(path.read_text(encoding="utf-8"))


def read_doc(cfg: AgentsConfig, agent_name: str) -> Any:
    """Read the agent's config.yaml, transparent to ``--data-dir-in-vm``."""
    if cfg.paths.data_dir_in_vm:
        host_temp = (
            compose.host_staging_dir(cfg)
            / f"hermesconf-read-{os.getpid()}-{uuid.uuid4().hex[:8]}.yml"
        )
        try:
            mp.transfer_from_vm(cfg.vm.name, vm_config_path(cfg, agent_name), host_temp)
            return load(host_temp)
        except Exception:
            return _yaml().load("{}\n")
        finally:
            host_temp.unlink(missing_ok=True)
    return load(config_path(cfg, agent_name))


def dump(doc: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def write_atomically(cfg: AgentsConfig, agent_name: str, doc: Any) -> None:
    """Atomic write via ``docker compose exec`` inside the running container.

    Writes the new content to a staged path inside ``/opt/data`` via
    ``cat > <staged>`` exec'd as the container's runtime user, validates
    via ``hermes config validate`` (also exec'd), then ``mv`` to
    ``config.yaml`` — all within the same filesystem, so the rename is
    atomic.

    Why not ``multipass transfer`` like ``compose.write_compose_atomically``
    does for the project-level compose file? Because under rootless
    Docker, ``/opt/data`` is owned by a subuid (e.g. host uid 100999
    for container uid 1000) — not by ``ubuntu``. Multipass-SFTP runs
    as the ``ubuntu`` user and silently can't write to subuid-owned
    paths (the failure surfaces only on first attempt: ``cannot open
    remote file ...: Permission denied``). Going through
    ``docker compose exec`` puts the writer inside the container's
    user namespace where it owns the dir.

    The container MUST be running. ``agents create`` brings it up
    during the ``service_up`` phase before the wizard runs, and every
    later caller (``codex skill-enable``, memory plugin enable,
    ``agents secret set``) runs against an already-running container.
    """
    validate_dashboard_binding(doc)

    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime fields not populated; run `agents vm init` first")

    from agents_cli.ops import _compose_exec_in_vm_argv

    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    target = container_config_path()
    stage = f"{target}.{pid}.{suffix}.new"
    svc = f"hermes-{agent_name}"
    content = dump(doc).encode("utf-8")

    # Write the staged file from inside the container. `sh -c 'cat > FILE'`
    # is the simplest way to get arbitrary content onto disk as the runtime
    # user; the content is piped via stdin so it never appears on the argv
    # (avoiding both quoting hell and process-listing leaks for sensitive
    # config like API keys).
    write_argv = _compose_exec_in_vm_argv(cfg, svc, "sh", "-c", f"cat > {stage}", no_tty=True)
    write = mp.exec_in_vm_with_stdin(cfg.vm.name, *write_argv, input_bytes=content, check=False)
    if write.returncode != 0:
        err = (write.stderr or "").lower()
        if any(m in err for m in ("not running", "no such service", "no such container")):
            raise UserError(
                f"cannot write {agent_name}'s config.yaml — container "
                f"{svc!r} is not running. Start it with "
                f"`agents start {agent_name}` first."
            )
        raise UserError(
            f"failed to stage {agent_name}'s config.yaml inside {svc!r}: "
            f"{(write.stderr or '').strip() or 'rc=' + str(write.returncode)}"
        )

    try:
        _validate_in_container(cfg, agent_name, stage)
        mv_argv = _compose_exec_in_vm_argv(cfg, svc, "mv", stage, target, no_tty=True)
        mp.exec_in_vm(cfg.vm.name, *mv_argv)
    except Exception:
        rm_argv = _compose_exec_in_vm_argv(cfg, svc, "rm", "-f", stage, no_tty=True)
        mp.exec_in_vm(cfg.vm.name, *rm_argv, check=False)
        raise


def _validate_in_container(cfg: AgentsConfig, agent_name: str, vm_path: str) -> None:
    """Best-effort validation of the staged config file inside the container.

    Hermes' CLI evolved here — older versions had ``hermes config
    validate``, newer ones use ``hermes config check``. We try each;
    if NEITHER is available the structural ``validate_dashboard_binding``
    upstream is the only guard, which is acceptable: every writer
    goes through this module's helpers that produce well-formed YAML.

    Skipped silently when the container isn't running.
    """
    from agents_cli.ops import _compose_exec_in_vm_argv

    svc = f"hermes-{agent_name}"
    container_stage = f"/opt/data/{Path(vm_path).name}"

    for sub in ("check", "validate"):
        result = mp.exec_in_vm(
            cfg.vm.name,
            *_compose_exec_in_vm_argv(
                cfg,
                svc,
                "hermes",
                "config",
                sub,
                no_tty=True,
                env={"HERMES_CONFIG": container_stage},
            ),
            check=False,
        )
        if result.returncode in (0, None):
            return
        err = (result.stderr or "").lower()
        if any(
            marker in err
            for marker in (
                "not running",
                "no such service",
                "no such container",
                "is not running",
            )
        ):
            return
        # Subcommand not recognised → try the next one.
        if "invalid choice" in err or "usage: hermes config" in err:
            continue
        raise UserError(
            f"hermes config {sub} failed for {agent_name!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def validate_dashboard_binding(doc: Any) -> None:
    """Refuse any dashboard.address that isn't localhost — the dashboard
    is meant to be accessible only via SSH tunnel.
    """
    dashboard = doc.get("dashboard") or {}
    addr = dashboard.get("address")
    if addr is None:
        return
    if addr not in ALLOWED_DASHBOARD_HOSTS:
        raise UserError(
            f"refusing to set dashboard.address={addr!r}; allowed: "
            f"{sorted(ALLOWED_DASHBOARD_HOSTS)}"
        )


# ---------- terminal.docker_image (used by codex skill-enable) ----------


def get_terminal_docker_image(doc: Any) -> str | None:
    terminal = doc.get("terminal") or {}
    image = terminal.get("docker_image")
    return str(image) if image else None


def set_terminal_docker_image(doc: Any, image: str) -> None:
    terminal = doc.setdefault("terminal", {})
    terminal["docker_image"] = image
    terminal.setdefault("backend", "docker")


# ---------- terminal.docker_volumes (used by codex skill-enable) ----------


def add_docker_volume(doc: Any, mapping: str) -> None:
    """Append a host:container[:ro] entry to terminal.docker_volumes. Idempotent."""
    terminal = doc.setdefault("terminal", {})
    volumes = terminal.setdefault("docker_volumes", [])
    if mapping not in volumes:
        volumes.append(mapping)


def remove_docker_volume(doc: Any, mapping: str) -> None:
    """Remove a previously-added volume entry, if present."""
    terminal = doc.get("terminal") or {}
    volumes = terminal.get("docker_volumes") or []
    if mapping in volumes:
        volumes.remove(mapping)


# ---------- terminal.env_passthrough (used by secrets_cmd) ----------


def add_env_passthrough(doc: Any, key: str) -> None:
    """Append ``key`` to ``terminal.env_passthrough`` so it forwards into
    sandboxed shell / execute_code calls. Idempotent.

    Hermes' default ``terminal.env_passthrough: []`` is a security feature:
    env vars present in the Hermes Python process don't leak into the
    subprocesses it spawns for the ``terminal`` and ``execute_code``
    tools unless the user explicitly allowlists them. Per-agent secrets
    written by ``agents secret set`` need this entry too, otherwise the
    LLM's own shell-based "is this configured?" diagnostics will report
    "not set" even though Hermes' built-in tools see the var fine.
    """
    terminal = doc.setdefault("terminal", {})
    passthrough = terminal.setdefault("env_passthrough", [])
    if key not in passthrough:
        passthrough.append(key)


def remove_env_passthrough(doc: Any, key: str) -> None:
    """Drop ``key`` from ``terminal.env_passthrough`` if present.

    Used by ``agents secret unset``. Leaving a stale entry would be
    harmless (the var no longer exists in the container env, so nothing
    propagates), but removing it keeps the config tidy and avoids
    confusing future reads.
    """
    terminal = doc.get("terminal") or {}
    passthrough = terminal.get("env_passthrough") or []
    if key in passthrough:
        passthrough.remove(key)


# ---------- general model.provider helper (informational) ----------


def get_model_provider(doc: Any) -> str | None:
    model = doc.get("model") or {}
    provider = model.get("provider")
    return str(provider) if provider else None
