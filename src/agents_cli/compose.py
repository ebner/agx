"""Compose-file mutation with the strict atomicity contract from the plan.

Read the local compose file (round-trip via ruamel.yaml, preserving
comments + key order), mutate in memory, stage to a sibling
``.compose.<pid>.new`` in the project directory, validate via
``docker compose ... config -q`` inside the VM, then atomic same-filesystem
``os.replace`` to swap. Every mutation runs under a ``flock`` on
``<data-dir>/.agents.lock``.

Two design decisions worth noting:

- The host data-dir is the same path the VM sees through the multipass
  mount. So we read/write on the host (fast, atomic with os.replace) and
  use the VM only to validate via the docker-compose binary that lives
  in it. This avoids the heredoc-through-exec antipattern entirely.
- ``data_dir_in_vm=True`` mode is not yet supported for mutations
  (would need multipass transfer round-trips for every write). We raise
  ``UserError`` rather than silently falling back.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from agents_cli import mp, ui
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError

HERMES_IMAGE_BASE: Final = "nousresearch/hermes-agent"
OPENCLAW_IMAGE_BASE: Final = "openclaw/openclaw"
ALLOWED_IMAGE_BASES: Final = frozenset({HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE})


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096  # never wrap, even on long port mappings
    return y


# ---------- locking ----------


@contextlib.contextmanager
def acquire_lock(data_dir: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``<data-dir>/.agents.lock`` for the
    duration of a mutation. Mutation = any change to the compose file,
    .env, or an agent's config.yaml. Read-only callers do not lock.
    """
    lock_path = data_dir / ".agents.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)


# ---------- file I/O ----------


def load_compose(path: Path) -> Any:
    """Round-trip read. Returns a ruamel doc; falls back to an empty
    ``services: {}`` doc if the file is missing.
    """
    y = _yaml()
    if not path.exists():
        return y.load("services: {}\n")
    return y.load(path.read_text(encoding="utf-8"))


def dump_compose(doc: Any) -> str:
    """Round-trip dump as a string."""
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def cleanup_stale_temps(data_dir: Path) -> None:
    """Sweep any ``.compose.*.new`` files left behind by prior crashes."""
    for tmp in data_dir.glob(".compose.*.new"):
        with contextlib.suppress(OSError):
            tmp.unlink()


def write_compose_atomically(cfg: AgentsConfig, doc: Any) -> None:
    """Stage in the project dir, validate via docker compose in the VM,
    then atomic same-filesystem rename. Cleans up the temp on any error.
    """
    if cfg.paths.data_dir_in_vm:
        raise UserError("compose mutations not supported under --data-dir-in-vm yet")
    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime fields are not populated; run `agents vm init` first")

    project_dir = cfg.paths.host_data_dir
    content = dump_compose(doc)
    tmp_name = f".compose.{os.getpid()}.{uuid.uuid4().hex[:8]}.new"
    tmp_host = project_dir / tmp_name
    target = project_dir / "docker-compose.yml"

    try:
        tmp_host.write_text(content, encoding="utf-8")
        _validate_via_vm(cfg, f"{cfg.paths.vm_agents_root}/{tmp_name}")
        os.replace(tmp_host, target)
        ui.info(f"compose: wrote {target}")
    except Exception:
        if tmp_host.exists():
            with contextlib.suppress(OSError):
                tmp_host.unlink()
        raise


def _validate_via_vm(cfg: AgentsConfig, vm_path: str) -> None:
    env_file = f"{cfg.paths.vm_agents_root}/.env"
    project_dir = str(cfg.paths.vm_agents_root)
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "compose",
        "--project-directory",
        project_dir,
        "-f",
        vm_path,
        "--env-file",
        env_file,
        "config",
        "-q",
        check=False,
    )
    if result.returncode != 0:
        raise UserError(f"compose file invalid: {result.stderr.strip()}")


# ---------- service fragments ----------


def hermes_service_fragment(
    *,
    name: str,
    cfg: AgentsConfig,
    port: int,
    image_tag: str,
) -> dict[str, Any]:
    """Compose service block for a Hermes agent.

    Mounts the per-agent data dir at /opt/data ($HERMES_HOME for the
    official image), the rootless Docker socket so Hermes can spawn its
    nested sandbox, and the toolkit-scoped Codex auth at the container
    user's ~/.codex for setup-time import. Sets DOCKER_HOST explicitly
    so Hermes' Docker CLI finds the socket.
    """
    socket_path = cfg.vm_runtime.docker_socket_path
    return {
        "image": f"{HERMES_IMAGE_BASE}:{image_tag}",
        "container_name": f"hermes-{name}",
        "restart": "unless-stopped",
        "command": "gateway run",
        "user": "${HERMES_UID}:${HERMES_GID}",
        "ports": [f"127.0.0.1:{port}:8642"],
        "volumes": [
            f"./{name}/.data:/opt/data",
            f"{socket_path}:{socket_path}",
            "./.codex:/home/hermes/.codex:ro",
        ],
        "environment": {
            "DOCKER_HOST": f"unix://{socket_path}",
        },
        "mem_limit": cfg.defaults.hermes_mem_limit,
        "cpus": cfg.defaults.hermes_cpus,
    }


def openclaw_service_fragment(
    *,
    name: str,
    port: int,
    image_tag: str,
) -> dict[str, Any]:
    """Compose service block for an OpenClaw agent.

    No Codex mount — OpenClaw manages its own bundled Codex plugin.
    Multicast (Bonjour) is disabled because Docker bridges don't forward
    it reliably.
    """
    return {
        "image": f"{OPENCLAW_IMAGE_BASE}:{image_tag}",
        "container_name": f"openclaw-{name}",
        "restart": "unless-stopped",
        "ports": [f"127.0.0.1:{port}:18789"],
        "volumes": [
            f"./{name}/.openclaw:/home/node/.openclaw",
            f"./{name}/workspace:/home/node/.openclaw/workspace",
        ],
        "environment": {
            "OPENCLAW_DISABLE_BONJOUR": "1",
        },
    }


def validate_image_ref(image_ref: str) -> str:
    """Return the ``<registry>/<repo>`` base (without tag), raising
    ``UserError`` if it isn't on the allowlist. Handles refs with or
    without a tag.
    """
    repo, _, _tag = image_ref.partition(":")
    if repo not in ALLOWED_IMAGE_BASES:
        raise UserError(
            f"image base {repo!r} not on the allowlist; permitted: {sorted(ALLOWED_IMAGE_BASES)}"
        )
    return repo


# ---------- service add / remove ----------


def add_service(doc: Any, service_name: str, fragment: Mapping[str, Any]) -> None:
    services = doc.setdefault("services", {})
    if service_name in services:
        raise UserError(f"service {service_name!r} already exists in compose")
    services[service_name] = dict(fragment)


def remove_service(doc: Any, service_name: str) -> None:
    services = doc.get("services") or {}
    if service_name not in services:
        raise UserError(f"service {service_name!r} not in compose")
    del services[service_name]


def rename_service(doc: Any, old_name: str, new_name: str) -> None:
    services = doc.get("services") or {}
    if old_name not in services:
        raise UserError(f"service {old_name!r} not in compose")
    if new_name in services:
        raise UserError(f"service {new_name!r} already exists")
    services[new_name] = services.pop(old_name)


def list_services(doc: Any) -> list[str]:
    return list((doc.get("services") or {}).keys())


# ---------- port allocator ----------


def used_ports(cfg: AgentsConfig) -> set[int]:
    """Read the live compose (which includes any docker-compose.override.yml)
    and return all published host ports."""
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "compose",
        "--project-directory",
        str(cfg.paths.vm_agents_root),
        "-f",
        f"{cfg.paths.vm_agents_root}/docker-compose.yml",
        "--env-file",
        f"{cfg.paths.vm_agents_root}/.env",
        "config",
        "--format",
        "json",
        check=False,
    )
    if result.returncode != 0:
        return set()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    ports: set[int] = set()
    for service in (data.get("services") or {}).values():
        for entry in service.get("ports", []) or []:
            published = entry.get("published") if isinstance(entry, dict) else None
            if published is None:
                continue
            with contextlib.suppress(TypeError, ValueError):
                ports.add(int(published))
    return ports


def next_free_port(cfg: AgentsConfig, *, base: int, used: set[int] | None = None) -> int:
    """First port at or above ``base`` that isn't in ``used`` (or in the
    live compose if ``used`` is omitted)."""
    if used is None:
        used = used_ports(cfg)
    p = base
    while p in used:
        p += 1
        if p > 65535:
            raise RuntimeFailure(f"no free port >= {base}")
    return p
