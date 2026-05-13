"""Compose-file mutation with the strict atomicity contract from the plan.

Pattern, plan-locked:

1. Hold ``flock`` on ``<data-dir>/.agents.lock`` for the duration.
2. Read+mutate the compose doc with ``ruamel.yaml``. Reads come straight
   off the host mount in host-mount mode (fast), or are pulled from the
   VM in ``--data-dir-in-vm`` mode.
3. Dump the new content to a host-side temp at ``<host_data_dir>/.staging/<pid>-<uuid>.yml``.
4. ``multipass transfer`` the host temp into the VM's project dir at
   ``<vm_agents_root>/.compose.<pid>.<uuid>.new`` — same filesystem as
   the final ``docker-compose.yml``, so the subsequent rename is
   guaranteed atomic. Staging in the project dir also means the
   validation step sees the same relative ``volumes:`` paths that will
   run in production.
5. Validate inside the VM: ``docker compose --project-directory ...
   -f <staged> --env-file ... config -q``.
6. Atomic same-fs rename inside the VM: ``mv <staged> docker-compose.yml``.
7. Cleanup. Host temp goes away in ``finally``; VM-side stage gets a
   sweep on success and on any error path.

``cleanup_stale_temps(cfg)`` is called by ``vm.start`` and at the head
of every mutation, so stragglers from a prior crash never accumulate.

Works uniformly in both modes:

- **Host-mount mode** (default): the VM-side write also lands on the
  host through the multipass mount, so ``<data-dir>/docker-compose.yml``
  on the host is always the same file as ``/home/ubuntu/agents/docker-compose.yml``
  in the VM.
- **--data-dir-in-vm mode**: same VM-side mechanics; nothing lands on
  the host. Reads pull via ``multipass transfer`` first.
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
# Sibling-service memory backends managed by the toolkit. Whitelisted by
# the same parsed (registry, repo) tuple check as the agent images so an
# attacker-controlled compose can't sneak in a typo-squatted variant.
OPENVIKING_IMAGE_BASE: Final = "volcengine/openviking"
ALLOWED_IMAGE_BASES: Final = frozenset(
    {HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE, OPENVIKING_IMAGE_BASE}
)


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


def host_staging_dir(cfg: AgentsConfig) -> Path:
    """Host-side scratch dir for anything that's about to be
    ``multipass transfer``\\ -ed into the VM (compose files, Hermes
    config.yaml writes, codex sandbox Dockerfiles, codex auth tars).

    Lives under ``<host_data_dir>/.staging/``. Two reasons:

    - Snap-confined ``multipass`` (the default Ubuntu install) cannot
      SFTP from ``/tmp/`` (``cannot access``) and the snap home
      interface excludes dotfiles in ``$HOME`` (so ``~/.cache/`` also
      fails with ``Permission denied``).
    - The host data dir is already ``multipass mount``ed into the VM,
      which means multipass has explicit access to it — guaranteed
      readable across both snap and deb installs of multipass.
    """
    d = cfg.paths.host_data_dir / ".staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_stale_temps(cfg: AgentsConfig) -> None:
    """Sweep any ``.compose.*.new`` left behind in the VM project dir by
    prior crashes. Also sweeps host-side stragglers from this process.

    Safe to call before the VM exists (it just no-ops).
    """
    for tmp in host_staging_dir(cfg).glob("*.yml"):
        with contextlib.suppress(OSError):
            tmp.unlink()
    if not cfg.vm_runtime.is_populated():
        return
    mp.exec_bash(
        cfg.vm.name,
        f"rm -f {cfg.paths.vm_agents_root}/.compose.*.new 2>/dev/null || true",
        check=False,
    )


def read_compose_doc(cfg: AgentsConfig) -> Any:
    """Read the current compose YAML, transparent to ``data_dir_in_vm`` mode.

    In host-mount mode this is a direct file read off the mount. In
    ``--data-dir-in-vm`` mode it ``multipass transfer``s from the VM
    into a host temp, parses, and deletes the temp.
    """
    if cfg.paths.data_dir_in_vm:
        host_temp = host_staging_dir(cfg) / f"read-{os.getpid()}-{uuid.uuid4().hex[:8]}.yml"
        try:
            mp.transfer_from_vm(
                cfg.vm.name,
                f"{cfg.paths.vm_agents_root}/docker-compose.yml",
                host_temp,
            )
            return load_compose(host_temp)
        finally:
            host_temp.unlink(missing_ok=True)
    return load_compose(cfg.paths.host_data_dir / "docker-compose.yml")


def write_compose_atomically(cfg: AgentsConfig, doc: Any) -> None:
    """Host-temp → multipass transfer → validate via docker compose config -q →
    VM-side ``mv``. Same flow in both ``data_dir_in_vm`` modes.

    Sweeps stale temps before staging so a half-finished previous run
    can never confuse this one. Cleans up its own host temp in
    ``finally`` and rms the VM stage on any error.
    """
    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime fields are not populated; run `agents vm init` first")

    cleanup_stale_temps(cfg)

    content = dump_compose(doc)
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    stage_name = f".compose.{pid}.{suffix}.new"
    vm_stage = f"{cfg.paths.vm_agents_root}/{stage_name}"
    vm_target = f"{cfg.paths.vm_agents_root}/docker-compose.yml"
    host_temp = host_staging_dir(cfg) / f"{pid}-{suffix}.yml"

    try:
        host_temp.write_text(content, encoding="utf-8")
        mp.transfer_to_vm(host_temp, cfg.vm.name, vm_stage)
        _validate_via_vm(cfg, vm_stage)
        mp.exec_in_vm(cfg.vm.name, "mv", vm_stage, vm_target)
        ui.info(f"compose: wrote {vm_target}")
    except Exception:
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_stage, check=False)
        raise
    finally:
        host_temp.unlink(missing_ok=True)


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
    user's ~/.codex for setup-time import.

    No ``user:`` override. Under rootless Docker, container UID 0 (the
    image's default entrypoint user) maps to the rootless host user
    (``ubuntu`` / uid 1000 in the VM), which through the multipass
    ``-u 1000:1000`` mapping is the host user owning the data dir on
    the host. Setting ``user: 1000:1000`` would force the container into
    a subuid that does NOT match the multipass mount ownership, breaking
    every write into ``/opt/data``. HOME is pinned in env so the Codex
    import bridge resolves to the same path we mount, independent of
    image USER/HOME drift.
    """
    socket_path = cfg.vm_runtime.docker_socket_path
    return {
        "image": f"{HERMES_IMAGE_BASE}:{image_tag}",
        "container_name": f"hermes-{name}",
        "restart": "unless-stopped",
        "command": "gateway run",
        "ports": [f"127.0.0.1:{port}:8642"],
        "volumes": [
            f"./{name}/.data:/opt/data",
            f"{socket_path}:{socket_path}",
            "./.codex:/home/hermes/.codex:ro",
        ],
        "environment": {
            "DOCKER_HOST": f"unix://{socket_path}",
            "HOME": "/home/hermes",
            # The image entrypoint prepends /opt/hermes/.venv/bin to PATH
            # internally, but `docker exec` bypasses the entrypoint, so
            # `agents doctor` (which exec's `hermes doctor` in the container)
            # would otherwise hit "executable file not found". Set the full
            # PATH explicitly to mirror the image default plus the venv.
            "PATH": "/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            # The Hermes image entrypoint reads HERMES_UID/GID to remap
            # its internal `hermes` user (default uid 10000) and chown
            # /opt/data before `gosu hermes`. WITHOUT these vars the
            # entrypoint skips the remap, hermes stays at uid 10000, and
            # the toolkit's `--user 1000:1000` exec ends up at a uid no
            # in-container user matches — every `agents exec coder
            # hermes ...` then hits "permission denied" on /opt/data
            # (which is mode 700 owned by the entrypoint's hermes).
            # Interpolated from `.env` (HERMES_UID/HERMES_GID are written
            # there by `vm init` to the host user's primary uid/gid).
            "HERMES_UID": "${HERMES_UID}",
            "HERMES_GID": "${HERMES_GID}",
        },
        "mem_limit": cfg.defaults.hermes_mem_limit,
        "cpus": cfg.defaults.hermes_cpus,
    }


def openviking_sidecar_fragment(
    *,
    agent_name: str,
    image_tag: str,
    api_key_env: str | None,
    embed_provider_url: str,
    embed_model: str,
    llm_provider_url: str,
    llm_model: str,
) -> dict[str, Any]:
    """Compose service block for a per-agent OpenViking memory sidecar.

    OpenViking ships as a long-running HTTP service. We bind it to
    ``127.0.0.1`` only — the Hermes container reaches it over the
    docker default network using the service name as DNS. The data
    volume sits under the agent's host dir so ``agents export``
    captures it.

    ``api_key_env``, if set, is the name of an env var present in the
    project ``.env`` carrying the user's OpenAI-compatible bearer
    token (for vendor-cloud Ollama endpoints, or an nginx-protected
    Ollama). Vanilla Ollama uses no auth — leave it None.
    """
    env: dict[str, str] = {
        "OPENVIKING_EMBEDDING_PROVIDER": "openai_compatible",
        "OPENVIKING_EMBEDDING_BASE_URL": embed_provider_url,
        "OPENVIKING_EMBEDDING_MODEL": embed_model,
        "OPENVIKING_LLM_PROVIDER": "openai_compatible",
        "OPENVIKING_LLM_BASE_URL": llm_provider_url,
        "OPENVIKING_LLM_MODEL": llm_model,
    }
    if api_key_env:
        env["OPENVIKING_EMBEDDING_API_KEY"] = "${" + api_key_env + "}"
        env["OPENVIKING_LLM_API_KEY"] = "${" + api_key_env + "}"
    return {
        "image": f"{OPENVIKING_IMAGE_BASE}:{image_tag}",
        "container_name": f"openviking-{agent_name}",
        "restart": "unless-stopped",
        "volumes": [
            f"./{agent_name}/.openviking:/home/openviking/data",
        ],
        "environment": env,
        # No host port mapping — only the Hermes container talks to it
        # via the default docker network. Reachable inside the network
        # as `http://openviking-<agent>:1933`.
        "expose": ["1933"],
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
