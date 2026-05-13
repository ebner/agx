"""Round-trip edits to each Hermes agent's config.yaml.

Each agent has its own ``<data-dir>/<name>/.data/config.yaml`` (the
file the Hermes container sees at ``$HERMES_HOME/config.yaml``). The
file is hand-editable, so we round-trip with ruamel.yaml to preserve
the user's comments and key order.

Write pattern (same contract as compose.py):

1. Mutate in memory.
2. Dump to host temp via ``compose.host_staging_dir(cfg)``.
3. ``multipass transfer`` to a VM-side stage at
   ``<vm_agents_root>/<name>/.data/config.yaml.<pid>.<uuid>.new``.
4. Validate by exec'ing ``hermes config validate -f <staged>`` inside
   the running agent's container.
5. Atomic same-fs rename inside the VM: ``mv <staged> config.yaml``.
6. Clean up host temp; rm VM-side stage on any error path.

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
    """The VM-side path of a Hermes agent's config.yaml — what the
    container sees at ``$HERMES_HOME/config.yaml`` after the bind mount.
    """
    return f"{cfg.paths.vm_agents_root}/{agent_name}/.data/config.yaml"


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
    """Plan-correct write: host-temp → ``multipass transfer`` → validate via
    ``hermes config validate`` inside the agent's container → VM-side ``mv``.

    Falls back to a structural-only check if the container isn't running
    yet (during ``agents create``, the wizard hasn't started it) — the
    dashboard refusal still applies in that case.
    """
    validate_dashboard_binding(doc)

    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime fields not populated; run `agents vm init` first")

    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    vm_target = vm_config_path(cfg, agent_name)
    vm_stage = f"{vm_target}.{pid}.{suffix}.new"
    host_temp = compose.host_staging_dir(cfg) / f"hermesconf-{pid}-{suffix}.yml"

    try:
        host_temp.write_text(dump(doc), encoding="utf-8")
        # Ensure the parent directory exists in the VM (in --data-dir-in-vm
        # mode the per-agent dir may not exist on the host).
        mp.exec_in_vm(
            cfg.vm.name,
            "mkdir",
            "-p",
            f"{cfg.paths.vm_agents_root}/{agent_name}/.data",
            check=False,
        )
        mp.transfer_to_vm(host_temp, cfg.vm.name, vm_stage)
        _validate_in_container(cfg, agent_name, vm_stage)
        mp.exec_in_vm(cfg.vm.name, "mv", vm_stage, vm_target)
    except Exception:
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_stage, check=False)
        raise
    finally:
        host_temp.unlink(missing_ok=True)


def _validate_in_container(cfg: AgentsConfig, agent_name: str, vm_path: str) -> None:
    """Exec ``hermes config validate`` against the staged file inside the
    running container. Best-effort: if the container isn't up, we skip
    (the structural ``validate_dashboard_binding`` already ran).
    """
    from agents_cli.ops import _compose_exec_in_vm_argv

    svc = f"hermes-{agent_name}"
    # `hermes config validate` reads from $HERMES_HOME/config.yaml by
    # default; point it at the staged file by setting HERMES_CONFIG.
    container_stage = f"/opt/data/{Path(vm_path).name}"
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg,
            svc,
            "hermes",
            "config",
            "validate",
            no_tty=True,
            env={"HERMES_CONFIG": container_stage},
        ),
        check=False,
    )
    if result.returncode in (0, None):
        return
    # Container not running yet (most likely cause): silently skip;
    # structural validation already ran. Distinguish from real validation
    # failures by checking stderr for known "no such service" / "not running"
    # phrases.
    err = (result.stderr or "").lower()
    if any(
        marker in err
        for marker in ("not running", "no such service", "no such container", "is not running")
    ):
        return
    raise UserError(
        f"hermes config validate failed for {agent_name!r}: {result.stderr.strip() or result.stdout.strip()}"
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


# ---------- general model.provider helper (informational) ----------


def get_model_provider(doc: Any) -> str | None:
    model = doc.get("model") or {}
    provider = model.get("provider")
    return str(provider) if provider else None
