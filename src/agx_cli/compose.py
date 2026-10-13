"""Compose-file mutation with a strict atomicity contract.

Pattern (a locked architecture rule — see AGENTS.md and
docs/architecture.md):

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
import hashlib
import io
import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from agx_cli import mp, templates, ui
from agx_cli.config import AgentsConfig
from agx_cli.errors import RuntimeFailure, UserError

HERMES_IMAGE_BASE: Final = "nousresearch/hermes-agent"
OPENCLAW_IMAGE_BASE: Final = "openclaw/openclaw"

# Hermes runtime env pins (see hermes_service_fragment).
HERMES_HOME_DIR: Final = "/opt/data"
HERMES_PATH: Final = (
    "/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

# OpenClaw: the image runs as ``node`` (uid 1000). Its state lives in
# these per-agent dirs (relative to ``<data-dir>/<name>/``) mounted under
# /home/node. ``.config`` carries OpenClaw's auth-profile secret key
# (``.config/openclaw``, part of upstream's official compose since 2.0)
# plus gh/git/... config; ``.local`` carries tools the agent installs
# (``~/.local/bin`` is on the image PATH, pnpm's global dir lives under
# ``.local/share``). The rest of /home/node stays image-owned on purpose:
# bind-mounting over it would hide files the image ships there.
OPENCLAW_RUNTIME_UID: Final = 1000
OPENCLAW_STATE_MOUNTS: Final = (
    (".openclaw", "/home/node/.openclaw"),
    ("workspace", "/home/node/.openclaw/workspace"),
    (".config", "/home/node/.config"),
    (".local", "/home/node/.local"),
)

# Toolkit-managed s6 cont-init.d perms hook (see templates.HERMES_CONT_INIT_FIXPERMS).
# Lives in the project dir (shared across all Hermes agents — it reads the
# runtime uid from the live container, so it's agent-independent) and is
# bind-mounted read-only into each Hermes container.
CONT_INIT_REL: Final = ".toolkit/cont-init/99-fixperms"
CONT_INIT_CONTAINER_PATH: Final = "/etc/cont-init.d/99-toolkit-fixperms"
# Sibling-service memory backends managed by the toolkit. Whitelisted by
# the same parsed (registry, repo) tuple check as the agent images so an
# attacker-controlled compose can't sneak in a typo-squatted variant.
OPENVIKING_IMAGE_BASE: Final = "volcengine/openviking"
# Firecrawl (self-hosted web-scrape backend). Five images; upstream
# publishes only floating tags (linux-amd64, latest) on GHCR — `agx
# firecrawl up` self-pins to the resolved digest on first run, so the
# compose file only ever references images by `@sha256:…`.
FIRECRAWL_API_IMAGE_BASE: Final = "ghcr.io/firecrawl/firecrawl"
FIRECRAWL_PLAYWRIGHT_IMAGE_BASE: Final = "ghcr.io/firecrawl/playwright-service"
FIRECRAWL_POSTGRES_IMAGE_BASE: Final = "ghcr.io/firecrawl/nuq-postgres"
FIRECRAWL_REDIS_IMAGE_BASE: Final = "redis"
FIRECRAWL_RABBITMQ_IMAGE_BASE: Final = "rabbitmq"
# Local-inference backends (Phase 1: Ollama, Phase 2: llama.cpp).
# Project-scoped sibling services like Firecrawl; one Ollama service
# serves every agent via Docker DNS. vLLM intentionally not in the
# allowlist — see GitHub issue #2 (libvirt + GPU passthrough); use
# `provider: custom + base_url` to point at an externally-hosted vLLM
# instead.
OLLAMA_IMAGE_BASE: Final = "ollama/ollama"
LLAMACPP_IMAGE_BASE: Final = "ghcr.io/ggml-org/llama.cpp"
ALLOWED_IMAGE_BASES: Final = frozenset(
    {
        HERMES_IMAGE_BASE,
        OPENCLAW_IMAGE_BASE,
        OPENVIKING_IMAGE_BASE,
        FIRECRAWL_API_IMAGE_BASE,
        FIRECRAWL_PLAYWRIGHT_IMAGE_BASE,
        FIRECRAWL_POSTGRES_IMAGE_BASE,
        FIRECRAWL_REDIS_IMAGE_BASE,
        FIRECRAWL_RABBITMQ_IMAGE_BASE,
        OLLAMA_IMAGE_BASE,
        LLAMACPP_IMAGE_BASE,
    }
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


OVERRIDE_FILENAME = "docker-compose.override.yml"


def override_path(cfg: AgentsConfig) -> Path:
    """Host-side location of the user/toolkit-extra compose override.

    ``docker compose`` auto-merges this file alongside ``docker-compose.yml``
    when it sits next to it. The toolkit's main compose file is owned by
    the CLI (``agx reseat`` / ``upgrade`` rewrite blocks in it), so any
    user customization or `agx mount`-style additions live here instead.
    """
    return cfg.paths.host_data_dir / OVERRIDE_FILENAME


def read_override_doc(cfg: AgentsConfig) -> Any:
    """Round-trip read of the override file. Returns an empty
    ``services: {}`` doc if it doesn't exist yet — callers can build up
    additions and then write atomically.
    """
    return load_compose(override_path(cfg))


def override_file_args(cfg: AgentsConfig) -> list[str]:
    """Extra ``-f`` pair for the override file — present iff it exists.

    ``docker compose`` only auto-merges ``docker-compose.override.yml``
    when NO explicit ``-f`` is given. Every toolkit invocation pins the
    main compose with ``-f``, so the override layer (the delivery
    mechanism for ``agx mount`` and user hand edits — architecture
    rule #10) must be passed explicitly or it is silently ignored.

    Existence is checked against the host-side authoritative copy;
    the path handed to ``docker compose`` is the VM-side one (identical
    on bare-metal, where ``vm_agents_root == host_data_dir``).
    """
    if override_path(cfg).exists():
        return ["-f", f"{cfg.paths.vm_agents_root}/{OVERRIDE_FILENAME}"]
    return []


def write_override_atomically(cfg: AgentsConfig, doc: Any) -> None:
    """Write the override file via host-temp → rename (same-filesystem).

    The host-side copy is authoritative — :func:`override_file_args`
    checks it to decide whether the extra ``-f`` gets passed. In
    host-mount mode (and on bare-metal) the host write is all that's
    needed: the file lands in the very project dir the VM's ``docker
    compose`` reads. In ``--data-dir-in-vm`` mode the VM-side project
    dir is a separate filesystem, so the file is additionally mirrored
    into the VM via the same staging → transfer → atomic ``mv`` pattern
    the main compose uses. The VM mirror happens BEFORE the host
    ``os.replace``: if the transfer fails, the host copy — and thus the
    extra ``-f`` — stays at its previous state instead of pointing at a
    file the VM doesn't have.

    Atomic via ``os.replace`` on the same filesystem (host data-dir).
    """
    content = dump_compose(doc)
    target = override_path(cfg)
    tmp = host_staging_dir(cfg) / f"override-{os.getpid()}-{uuid.uuid4().hex[:8]}.yml"
    try:
        tmp.write_text(content, encoding="utf-8")
        if cfg.paths.data_dir_in_vm:
            _transfer_override_to_vm(cfg, tmp)
        os.replace(tmp, target)
        ui.info(f"compose: wrote {target}")
        await_vm_view(cfg, target)
    finally:
        tmp.unlink(missing_ok=True)


def _transfer_override_to_vm(cfg: AgentsConfig, src: Path) -> None:
    """Mirror a host file into the VM as ``<vm_agents_root>/docker-compose.override.yml``
    via stage → atomic ``mv`` (``--data-dir-in-vm`` only; the VM project
    dir is a separate filesystem there)."""
    vm_stage = f"{cfg.paths.vm_agents_root}/.compose.{os.getpid()}.{uuid.uuid4().hex[:8]}.new"
    vm_target = f"{cfg.paths.vm_agents_root}/{OVERRIDE_FILENAME}"
    try:
        mp.transfer_to_vm(src, cfg.vm.name, vm_stage)
        mp.exec_in_vm(cfg.vm.name, "mv", vm_stage, vm_target)
    except Exception:
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_stage, check=False)
        raise


def sync_override_to_vm(cfg: AgentsConfig, *, only_if_missing: bool = False) -> bool:
    """Push the host-side override file into the VM project dir
    (``--data-dir-in-vm`` only). Returns ``True`` iff a file was pushed.
    ``only_if_missing`` skips the push when the VM already has a mirror
    (``vm start``'s heal path — never clobbers a live copy).

    The host copy is authoritative and outlives the VM: ``vm rebuild``
    destroys the VM disk (and with it the mirror that
    :func:`write_override_atomically` made), while the host file — and
    therefore the extra ``-f`` from :func:`override_file_args` — stays.
    Without re-pushing, every ``docker compose`` verb in the fresh VM
    fails on the missing file, which stranded ``agx mount`` users
    after a rebuild. Called from the in-VM data-dir seeding that
    ``vm init`` (and hence ``vm rebuild``) runs. The raw file is
    transferred verbatim so user hand edits keep their formatting.
    No-op in host-mount mode and on bare-metal (one filesystem) or when
    there is no host override.
    """
    src = override_path(cfg)
    if not cfg.paths.data_dir_in_vm or not src.exists():
        return False
    if only_if_missing:
        vm_copy = f"{cfg.paths.vm_agents_root}/{OVERRIDE_FILENAME}"
        if mp.exec_in_vm(cfg.vm.name, "test", "-f", vm_copy, check=False).returncode == 0:
            return False
    _transfer_override_to_vm(cfg, src)
    ui.info(f"compose: mirrored {OVERRIDE_FILENAME} into the VM")
    return True


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


def ensure_cont_init_script(cfg: AgentsConfig) -> None:
    """Materialize the toolkit-managed s6 cont-init.d perms hook in the
    project dir so the Hermes fragment's read-only bind-mount source exists
    (and is executable) before any container start.

    The script is shared across all Hermes agents and rewritten on every
    call, so a template bump propagates on the next create / reseat /
    recreate. Idempotent. Materialized by the toolkit paths that (re)start a
    Hermes container with this fragment — ``create`` (before the fragment is
    written), ``reseat``, ``fix-perms``, and ``upgrade`` — *before* the
    ``compose up`` they issue. The file then persists on the project dir, so
    other recreate paths (``secret set``, ``memory``, ``codex skill-*``, vm
    rebuild) find it already present; a template bump is re-applied on the
    next ``reseat``.

    Defends against the "Docker auto-created a directory at the bind source"
    race: if a ``compose up`` ran before the file existed, the daemon creates
    an (empty) directory at the mount path. We detect that and remove it so
    the atomic file write below succeeds rather than raising ``IsADirectoryError``.

    Two write paths, mirroring the rest of the module:

    - Host-mounted data dir (multipass / libvirt / bare-metal): write
      straight into ``<host_data_dir>/<CONT_INIT_REL>``. That dir is already
      mounted into the VM at ``vm_agents_root``, so the file appears there
      with its mode bits intact — the exec bit survives host → 9p →
      container bind-mount (verified), which s6-overlay requires for
      cont-init.d scripts.
    - ``--data-dir-in-vm``: stage on the host, ``multipass transfer`` into a
      VM-side temp in the target dir, ``chmod 0755`` the temp, then ``mv`` it
      into place — so a crash never leaves a partial/non-executable hook.
    """
    body = templates.HERMES_CONT_INIT_FIXPERMS
    if cfg.paths.data_dir_in_vm:
        vm_path = f"{cfg.paths.vm_agents_root}/{CONT_INIT_REL}"
        vm_dir = vm_path.rsplit("/", 1)[0]
        vm_tmp = f"{vm_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.new"
        host_temp = host_staging_dir(cfg) / f"continit-{os.getpid()}-{uuid.uuid4().hex[:8]}.sh"
        try:
            host_temp.write_text(body, encoding="utf-8")
            mp.exec_in_vm(cfg.vm.name, "mkdir", "-p", vm_dir)
            # Clear a stray Docker-auto-created directory at the final path.
            mp.exec_in_vm(cfg.vm.name, "rmdir", vm_path, check=False)
            mp.transfer_to_vm(host_temp, cfg.vm.name, vm_tmp)
            mp.exec_in_vm(cfg.vm.name, "chmod", "0755", vm_tmp)
            mp.exec_in_vm(cfg.vm.name, "mv", vm_tmp, vm_path)
        except Exception:
            mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_tmp, check=False)
            raise
        finally:
            host_temp.unlink(missing_ok=True)
        return

    target = cfg.paths.host_data_dir / CONT_INIT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    # If a `compose up` beat us to it, Docker created an (empty) directory at
    # the bind source. Remove it so the rename below lands a file, not EISDIR.
    if target.is_dir() and not target.is_symlink():
        target.rmdir()
    # Atomic create with the exec bit set, via same-filesystem rename
    # (staging dir and target both live under the host data dir).
    tmp = host_staging_dir(cfg) / f"continit-{os.getpid()}-{uuid.uuid4().hex[:8]}.sh"
    try:
        tmp.write_text(body, encoding="utf-8")
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    await_vm_view(cfg, target)


# How long a host-side write may take to show up in the VM. virtiofs
# (libvirt) caches attributes/dentries for ~1 s by default: a host
# rename was measured invisible to the VM for 0.5-1 s, so an
# `agx mount remove --restart` handed `docker compose up` the OLD
# override, compose saw no change, and the container kept the removed
# bind mount. 9p (multipass) has its own caching; the check is cheap.
VM_VIEW_TIMEOUT_S = 5.0


def await_vm_view(cfg: AgentsConfig, host_path: Path) -> None:
    """Block until the VM reads ``host_path``'s current content.

    Call after a host-side write that a VM-side ``docker compose`` (or
    container start) consumes right away: override file, per-agent and
    project ``.env``, the cont-init hook. No-op on bare-metal (same
    filesystem) and under ``--data-dir-in-vm`` (those writes go through
    the VM already). Never raises: on timeout it warns and the caller
    proceeds.
    """
    if cfg.vm.backend not in ("multipass", "libvirt") or cfg.paths.data_dir_in_vm:
        return
    try:
        rel = host_path.relative_to(cfg.paths.host_data_dir)
        want = hashlib.sha256(host_path.read_bytes()).hexdigest()
    except (ValueError, OSError):
        return
    vm_path = f"{cfg.paths.vm_agents_root}/{rel.as_posix()}"
    deadline = time.monotonic() + VM_VIEW_TIMEOUT_S
    while True:
        res = mp.exec_in_vm(cfg.vm.name, "sha256sum", vm_path, check=False)
        got = res.stdout.split()[0] if res.returncode == 0 and res.stdout.split() else None
        if got == want:
            return
        if time.monotonic() >= deadline:
            ui.warn(
                f"the VM still sees an outdated {vm_path} after {VM_VIEW_TIMEOUT_S:.0f}s "
                "(shared-folder caching); if the change doesn't take effect, re-run the command"
            )
            return
        time.sleep(0.2)


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
        raise UserError("vm_runtime fields are not populated; run `agx vm init` first")

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
    # The override is merged into every runtime compose invocation, so
    # validate the same combined view — a broken override (or a staged
    # main compose that conflicts with it) is caught at write time
    # instead of on the next `compose up`.
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
        *override_file_args(cfg),
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
    nested sandbox, and the toolkit's cont-init perms hook.

    Deliberately NO mount of the toolkit-scoped ``<data-dir>/.codex``: a
    Hermes agent authenticates its ``openai-codex`` provider inside the
    container (``hermes auth add openai-codex``) so the credential and
    its single-use OAuth refresh chain are owned by the agent alone —
    two copies of one chain invalidate each other on refresh.

    No ``user:`` override. Under rootless Docker, container UID 0 (the
    image's default entrypoint user) maps to the rootless host user
    (``ubuntu`` / uid 1000 in the VM), which through the multipass
    ``-u 1000:1000`` mapping is the host user owning the data dir on
    the host. Setting ``user: 1000:1000`` would force the container into
    a subuid that does NOT match the multipass mount ownership, breaking
    every write into ``/opt/data``.

    HOME and PATH mirror the image instead of fighting it: ``docker
    exec`` (``agx exec``/``doctor``) bypasses the entrypoint, so both
    are pinned, but to the image's own values — HOME is the runtime
    user's passwd home ``/opt/data`` (persisted: anything a shell, the
    gateway or ``gh``/``git``/``npm`` writes under ``~`` survives every
    recreate), PATH the image ``Config.Env`` PATH (incl. the
    ``/opt/hermes/bin`` wrapper). Tool subprocesses get Hermes'
    per-profile ``$HERMES_HOME/home`` on top of that.
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
            # Toolkit-managed perms hook. Runs as root on every boot to
            # chown image-baked install paths + pre-create the gateway
            # logdir, so rootless-Docker perm drift no longer needs a
            # post-recreate `agx fix-perms`. Source materialized by
            # compose.ensure_cont_init_script before any container start.
            f"./{CONT_INIT_REL}:{CONT_INIT_CONTAINER_PATH}:ro",
        ],
        # Per-agent secrets: KEY=VALUE pairs in `<data-dir>/<name>/.env`
        # become container env. Skills like firecrawl read their API keys
        # from there. Managed via `agx secret {set,unset,list,clear}`.
        # File is created (empty) by `agx create` so compose validation
        # never trips on a missing path.
        "env_file": [f"./{name}/.env"],
        "environment": {
            "DOCKER_HOST": f"unix://{socket_path}",
            # The runtime user's passwd home — ON the bind mount, so what
            # `docker exec` processes (`agx exec` sessions) write under ~
            # survives recreates; a HOME on the container overlay would be
            # wiped each time. The gateway itself always gets HOME from
            # passwd (s6 resets it).
            "HOME": HERMES_HOME_DIR,
            # Mirrors the image's Config.Env PATH. Pinned because images
            # predating that env relied on the entrypoint to add the venv,
            # which `docker exec` bypasses ("executable file not found").
            "PATH": HERMES_PATH,
            # The Hermes image entrypoint reads HERMES_UID/GID to remap
            # its internal `hermes` user (default uid 10000) and chown
            # /opt/data before `gosu hermes`. WITHOUT these vars the
            # entrypoint skips the remap, hermes stays at uid 10000, and
            # the toolkit's `--user 1000:1000` exec ends up at a uid no
            # in-container user matches — every `agx exec coder
            # hermes ...` then hits "permission denied" on /opt/data
            # (which is mode 700 owned by the entrypoint's hermes).
            # Interpolated from `.env` (HERMES_UID/HERMES_GID are written
            # there by `vm init` to the host user's primary uid/gid).
            "HERMES_UID": "${HERMES_UID}",
            "HERMES_GID": "${HERMES_GID}",
            # Firecrawl self-host wiring (project-level, set by
            # `agx firecrawl up`). ``${X:-}`` keeps these empty when
            # firecrawl is off so firecrawl-py's "non-empty key" check
            # only succeeds when the user actually opted in.
            "FIRECRAWL_API_KEY": "${FIRECRAWL_API_KEY:-}",
            "FIRECRAWL_API_URL": "${FIRECRAWL_API_URL:-}",
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
    volume sits under the agent's host dir so ``agx export``
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
        "volumes": [f"./{name}/{sub}:{target}" for sub, target in OPENCLAW_STATE_MOUNTS],
        "environment": {
            "OPENCLAW_DISABLE_BONJOUR": "1",
            # Firecrawl self-host wiring (project-level, set by
            # `agx firecrawl up`). OpenClaw's bundled Firecrawl
            # plugin reads ``FIRECRAWL_API_KEY`` + ``FIRECRAWL_BASE_URL``
            # — NOT ``FIRECRAWL_API_URL`` like Hermes — so we bridge
            # the env-var-name asymmetry here via compose interpolation
            # from the same project ``.env`` entry. ``${X:-}`` keeps
            # the values empty when firecrawl is off so OpenClaw's
            # "plugin configured?" check correctly reports unconfigured.
            #
            # Note: setting the env vars is NOT enough on its own —
            # OpenClaw also needs ``plugins.entries.firecrawl.enabled:
            # true`` flipped in its in-container config, which happens
            # the first time the user picks Firecrawl during
            # ``openclaw onboard`` / ``openclaw configure``. The toolkit
            # stays out of OpenClaw's plugin lifecycle by design.
            "FIRECRAWL_API_KEY": "${FIRECRAWL_API_KEY:-}",
            "FIRECRAWL_BASE_URL": "${FIRECRAWL_API_URL:-}",
        },
        # See hermes_service_fragment for the per-agent .env story.
        "env_file": [f"./{name}/.env"],
    }


# ---------- firecrawl (project-level self-host) ----------


FIRECRAWL_SERVICE_PREFIX: Final = "firecrawl-"

# ---------- local inference (Phase 1: Ollama, Phase 2: llama.cpp) ----------

OLLAMA_SERVICE_NAME: Final = "ollama"
OLLAMA_DEFAULT_PORT: Final = 11434
LLAMACPP_SERVICE_PREFIX: Final = "llama-cpp-"
# Where the ollama container sees the shared llama-cpp GGUF dir
# (``<data-dir>/models/llama-cpp/``). ``agx model pull --engine
# llama-cpp`` runs hf_hub_download inside the ollama container and MUST
# target this path — anything else lands on the container overlay and is
# wiped on recreate.
LLAMACPP_DOWNLOAD_MOUNT: Final = "/llama-cpp"


def ollama_service_fragment(*, image: str, port: int = OLLAMA_DEFAULT_PORT) -> dict[str, Any]:
    """Compose block for the project-scoped Ollama inference server.

    One Ollama instance serves every Hermes agent over the project's
    default docker network as ``http://ollama:11434/v1``. The host port
    is bound to ``127.0.0.1`` so a host-side ``ollama`` CLI invocation
    works (useful for debugging), but it is NEVER exposed to the
    network — Ollama has no auth, so a bind to 0.0.0.0 would expose
    every loaded model to the LAN.

    Models live on a bind mount at ``./models/ollama/`` (relative to
    the project compose dir, i.e. ``<data-dir>/models/ollama/``) so
    they're host-visible for inspection and survive ``agx inference
    down``. The full Ollama blob store layout is under that path
    (manifests + blobs subdirs).

    A second bind at ``./models/llama-cpp/`` → :data:`LLAMACPP_DOWNLOAD_MOUNT`
    exists because ``agx model pull --engine llama-cpp`` reuses this
    container as its HF download host (it has Python; no extra image
    needed). Downloads land host-visible in the same dir the per-model
    ``llama-cpp-*`` services mount read-only at ``/models``. An ollama
    container created from the pre-bind fragment only gains this mount
    on recreate (``agx inference down ollama && agx inference up
    ollama``) — the pull path probes for it and refuses with that hint.

    ``OLLAMA_KEEP_ALIVE: -1m`` keeps loaded models in RAM until
    explicitly evicted; the default 5-minute idle timeout would
    constantly unload-and-reload the agent's chosen model between
    requests on a low-traffic agent setup, defeating the cache.

    ``mem_limit`` deliberately not set: model weights + KV cache size
    varies wildly across models (~2 GB for 1.5B Q4, ~6 GB for 7B Q4,
    ~10 GB for 13B Q4), and a hard cap would kill loads. Users on
    constrained VMs (RPi5) pick smaller models; doctor's
    ``inference_model_memory_fit`` warns if the chosen model exceeds
    75% of VM RAM.
    """
    return {
        "image": image,
        "container_name": OLLAMA_SERVICE_NAME,
        "restart": "unless-stopped",
        "ports": [f"127.0.0.1:{port}:11434"],
        "volumes": [
            "./models/ollama:/root/.ollama",
            f"./models/llama-cpp:{LLAMACPP_DOWNLOAD_MOUNT}",
        ],
        "environment": {
            "OLLAMA_HOST": "0.0.0.0:11434",
            "OLLAMA_KEEP_ALIVE": "-1m",
        },
        "expose": ["11434"],
        # Healthcheck via Ollama's own /api/tags. Doctor reuses the
        # same path for its `inference_health` probe.
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                "ollama list >/dev/null 2>&1 || exit 1",
            ],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "20s",
        },
    }


def llamacpp_service_fragment(
    *,
    model_slug: str,
    image: str,
    gguf_relative_path: str,
    port: int,
    ctx_size: int = 4096,
    n_gpu_layers: int = 0,
) -> dict[str, Any]:
    """Compose block for ONE llama.cpp server bound to ONE GGUF file.

    Unlike Ollama, llama.cpp's server is single-model — each loaded
    model is a separate `llama-server` process. We model this as one
    compose service per model, named ``llama-cpp-<slug>``. The slug is
    a sanitized form of the model name (lowercase, ``[a-z0-9-]+``).

    ``gguf_relative_path`` is relative to the shared models dir
    (``<data-dir>/models/llama-cpp/``); the container mounts that dir
    read-only at ``/models``.

    ``ctx_size`` and ``n_gpu_layers`` are exposed because they're the
    two knobs that materially affect a llama-server's resource budget;
    other tuning belongs in ``docker-compose.override.yml``.

    The host port is bound to ``127.0.0.1`` for the same reason as
    Ollama (no auth on the server).
    """
    return {
        "image": image,
        "container_name": f"{LLAMACPP_SERVICE_PREFIX}{model_slug}",
        "restart": "unless-stopped",
        "ports": [f"127.0.0.1:{port}:8080"],
        "volumes": ["./models/llama-cpp:/models:ro"],
        "command": [
            "--server",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "-m",
            f"/models/{gguf_relative_path}",
            "-c",
            str(ctx_size),
            "-ngl",
            str(n_gpu_layers),
        ],
        "expose": ["8080"],
    }


def firecrawl_service_fragments(
    *,
    images: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Build the five compose service blocks for a self-hosted Firecrawl
    stack: ``firecrawl-api``, ``firecrawl-playwright``, ``firecrawl-redis``,
    ``firecrawl-rabbitmq``, ``firecrawl-postgres``.

    ``images`` keys: ``api``, ``playwright``, ``redis``, ``rabbitmq``,
    ``postgres`` — each maps to a digest-pinned image reference
    (``ghcr.io/firecrawl/firecrawl@sha256:…``). The toolkit resolves
    floating upstream tags to digests on first ``agx firecrawl up``
    so the compose file only ever sees pinned refs.

    Design notes:

    - Only the api expects to be reachable from sibling containers; it
      listens on port 3002 over the project's default docker network
      and is NOT published on the host (use ``agx firecrawl logs``
      / shell-into-VM debugging if you need to inspect it).
    - Postgres is on a bind-mount under ``./firecrawl/postgres/`` so
      ``agx export`` and duplicity cover it. Redis + RabbitMQ stay
      ephemeral — losing in-flight scrape jobs on restart is fine.
    - Playwright is the resource hog. We cap concurrency at 2 pages
      and block media downloads via env, which keeps idle RAM under
      ~600 MB on a single-user setup.
    - ``host.docker.internal:host-gateway`` mirrors upstream's
      compose so the api can reach the host where applicable; it
      works fine under rootless Docker on Linux 6.x.
    """
    return {
        "firecrawl-api": {
            "image": images["api"],
            "container_name": "firecrawl-api",
            "restart": "unless-stopped",
            "environment": {
                "PORT": "3002",
                "HOST": "0.0.0.0",
                "USE_DB_AUTHENTICATION": "false",
                "BULL_AUTH_KEY": "${FIRECRAWL_BULL_AUTH_KEY}",
                "REDIS_URL": "redis://firecrawl-redis:6379",
                # Firecrawl uses TWO distinct Redis clients: REDIS_URL
                # for the job queue, REDIS_RATE_LIMIT_URL for the
                # rate-limiter + session store. Both default to
                # 127.0.0.1:6379 in-source if unset, which fails
                # silently in a multi-container setup — the api logs
                # ECONNREFUSED on the rate-limit client and every
                # /v1/scrape request 408s.
                "REDIS_RATE_LIMIT_URL": "redis://firecrawl-redis:6379",
                "PLAYWRIGHT_MICROSERVICE_URL": "http://firecrawl-playwright:3000/scrape",
                "NUQ_RABBITMQ_URL": "amqp://firecrawl-rabbitmq:5672",
                "POSTGRES_HOST": "firecrawl-postgres",
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "${FIRECRAWL_POSTGRES_PASSWORD}",
                "POSTGRES_DB": "postgres",
            },
            "depends_on": {
                "firecrawl-redis": {"condition": "service_started"},
                "firecrawl-playwright": {"condition": "service_started"},
                "firecrawl-rabbitmq": {"condition": "service_healthy"},
                "firecrawl-postgres": {"condition": "service_healthy"},
            },
            "extra_hosts": ["host.docker.internal:host-gateway"],
            "expose": ["3002"],
            "mem_limit": "3g",
            "cpus": 2.0,
        },
        "firecrawl-playwright": {
            "image": images["playwright"],
            "container_name": "firecrawl-playwright",
            "restart": "unless-stopped",
            "environment": {
                "PORT": "3000",
                "BLOCK_MEDIA": "true",
                "MAX_CONCURRENT_PAGES": "2",
            },
            "tmpfs": ["/tmp/.cache:size=1g"],
            "expose": ["3000"],
            "mem_limit": "2g",
            "cpus": 2.0,
        },
        "firecrawl-redis": {
            "image": images["redis"],
            "container_name": "firecrawl-redis",
            "restart": "unless-stopped",
            # Disable both RDB snapshots and AOF — queue state is fine
            # to drop on restart for a single-user setup, and avoids the
            # SQLite-WAL-on-9p class of issue documented in AGENTS.md
            # for redis' append-only-log under host-mount mode.
            "command": ["redis-server", "--save", "", "--appendonly", "no"],
            "expose": ["6379"],
            "mem_limit": "512m",
        },
        "firecrawl-rabbitmq": {
            "image": images["rabbitmq"],
            "container_name": "firecrawl-rabbitmq",
            "restart": "unless-stopped",
            "expose": ["5672"],
            "healthcheck": {
                "test": ["CMD", "rabbitmq-diagnostics", "ping"],
                "interval": "10s",
                "timeout": "5s",
                "retries": 6,
            },
            "mem_limit": "512m",
        },
        "firecrawl-postgres": {
            "image": images["postgres"],
            "container_name": "firecrawl-postgres",
            "restart": "unless-stopped",
            "environment": {
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "${FIRECRAWL_POSTGRES_PASSWORD}",
                "POSTGRES_DB": "postgres",
            },
            "volumes": ["./firecrawl/postgres:/var/lib/postgresql/data"],
            "expose": ["5432"],
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U postgres"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 10,
            },
            "mem_limit": "512m",
        },
    }


def is_firecrawl_service(svc_name: str) -> bool:
    return svc_name.startswith(FIRECRAWL_SERVICE_PREFIX)


def validate_image_ref(image_ref: str) -> str:
    """Return the ``<registry>/<repo>`` base, raising ``UserError`` if
    it isn't on the allowlist. Handles three ref shapes:

    - bare ``repo``
    - tag-pinned ``repo:tag``
    - digest-pinned ``repo@sha256:...`` (used by firecrawl self-pinning)

    Strips the ``@sha256:...`` part first, then the ``:tag`` part — so
    ``ghcr.io/firecrawl/firecrawl@sha256:abc`` resolves to the right
    base without the digest's leading-colon confusing the tag split.
    """
    repo_and_tag = image_ref.split("@", 1)[0]
    repo, _, _tag = repo_and_tag.partition(":")
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


# ---------- port allocator ----------


def used_ports(cfg: AgentsConfig) -> set[int]:
    """Read the live compose (main file + any docker-compose.override.yml)
    and return all published host ports."""
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "compose",
        "--project-directory",
        str(cfg.paths.vm_agents_root),
        "-f",
        f"{cfg.paths.vm_agents_root}/docker-compose.yml",
        *override_file_args(cfg),
        "--env-file",
        f"{cfg.paths.vm_agents_root}/.env",
        "config",
        "--format",
        "json",
        check=False,
    )
    data: Any = None
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None
    if data is None:
        # Don't fail open: "couldn't determine" must not read as "no
        # ports in use" — the port allocator would hand a fresh agent an
        # already-published port. Fall back to parsing the compose docs
        # directly (less precise — no ${VAR} interpolation — but the
        # toolkit always writes literal host ports).
        ui.warn(
            "could not read published ports via `docker compose config` "
            f"(rc={result.returncode}); falling back to parsing the compose files directly"
        )
        return _used_ports_from_docs(cfg)
    ports: set[int] = set()
    for service in (data.get("services") or {}).values():
        for entry in service.get("ports", []) or []:
            published = entry.get("published") if isinstance(entry, dict) else None
            if published is None:
                continue
            with contextlib.suppress(TypeError, ValueError):
                ports.add(int(published))
    return ports


def _used_ports_from_docs(cfg: AgentsConfig) -> set[int]:
    """Fallback for :func:`used_ports`: parse the main compose + override
    docs directly. Handles both the short string form the toolkit writes
    (``127.0.0.1:8642:8642`` — host port is the second-to-last field)
    and the long dict form (``{target: ..., published: ...}``).
    """
    ports: set[int] = set()
    for doc in (read_compose_doc(cfg), read_override_doc(cfg)):
        for service in (doc.get("services") or {}).values():
            for entry in service.get("ports", []) or []:
                published: Any = None
                if isinstance(entry, str):
                    parts = entry.split(":")
                    if len(parts) >= 2:
                        published = parts[-2]
                elif isinstance(entry, dict):
                    published = entry.get("published")
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
