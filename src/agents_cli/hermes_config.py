"""Round-trip edits to each Hermes agent's config.yaml.

Each agent has its own ``<data-dir>/<name>/.data/config.yaml`` (the
file the Hermes container sees at ``$HERMES_HOME/config.yaml``). The
file is hand-editable, so we round-trip with ruamel.yaml to preserve
the user's comments and key order, then stage+rename atomically with
the same contract as compose.py.

The CLI refuses any change that would bind the dashboard to anything
other than 127.0.0.1/localhost — that rule is one of the locked
security invariants from the plan.
"""

from __future__ import annotations

import contextlib
import io
import os
import uuid
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

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
    """The host-side path of a Hermes agent's config.yaml."""
    return cfg.paths.host_data_dir / agent_name / ".data" / "config.yaml"


def load(path: Path) -> Any:
    """Round-trip read; returns an empty ruamel mapping if the file is missing."""
    y = _yaml()
    if not path.exists():
        return y.load("{}\n")
    return y.load(path.read_text(encoding="utf-8"))


def dump(doc: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def write_atomically(cfg: AgentsConfig, agent_name: str, doc: Any) -> None:
    """Validate dashboard binding, then stage+rename inside the agent's data dir."""
    validate_dashboard_binding(doc)

    target = config_path(cfg, agent_name)
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.parent / f"config.yaml.{os.getpid()}.{uuid.uuid4().hex[:8]}.new"
    try:
        tmp.write_text(dump(doc), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise


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
