"""VM lifecycle: init, start, stop, shell, status, snapshot, restore, destroy.

Orchestration only. Every multipass call goes through ``mp``, every config
read/write goes through ``config``. That way the unit tests mock at the
``mp`` boundary and never touch a real VM.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from agents_cli import config as cfg_mod
from agents_cli import mp, templates, ui
from agents_cli.config import AgentsConfig, ExportCronConfig, HostConfig, PathsConfig, VmConfig
from agents_cli.errors import HealthCheckFailure, RuntimeFailure, UserError
from agents_cli.mp import BootstrapStatus

GITHUB_LATEST_URLS = {
    "hermes": "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest",
    "openclaw": "https://api.github.com/repos/openclaw/openclaw/releases/latest",
}


# ---------- init ----------


def init(
    *,
    data_dir: Path,
    data_dir_in_vm: bool = False,
    cpus: int | None = None,
    memory_gb: int | None = None,
    disk_gb: int | None = None,
    ubuntu_release: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Idempotently provision the agents VM.

    Steps:
      1. Validate / create the host data-dir.
      2. Build (or update) ~/.config/agents/config.toml with the chosen sizing.
      3. Pick a usable Ubuntu image (fall back from configured release if `multipass find` doesn't list it).
      4. Launch the VM if absent.
      5. Mount the host data-dir into the VM at /home/ubuntu/agents.
      6. Pipe the bootstrap script in, parse the BOOTSTRAP_OK handshake.
      7. Populate `[vm_runtime]` and pinned image tags.
      8. Seed an empty docker-compose.yml and .env on the data-dir.
    """
    cfg = _load_or_default_config(config_path)

    cfg.paths = PathsConfig.model_validate(
        {
            "host_data_dir": str(data_dir),
            "vm_agents_root": str(cfg.paths.vm_agents_root),
            "data_dir_in_vm": data_dir_in_vm,
        }
    )
    if cpus is not None:
        cfg.vm.cpus = cpus
    if memory_gb is not None:
        cfg.vm.memory_gb = memory_gb
    if disk_gb is not None:
        cfg.vm.disk_gb = disk_gb
    if ubuntu_release is not None:
        cfg.vm.ubuntu_release = ubuntu_release

    cfg.host = HostConfig(uid=os.getuid(), gid=os.getgid())

    _prepare_host_data_dir(cfg.paths.host_data_dir)

    image = _select_ubuntu_image(cfg.vm.ubuntu_release)
    if image != cfg.vm.ubuntu_release:
        ui.warn(
            f"Ubuntu {cfg.vm.ubuntu_release} not listed by `multipass find`; "
            f"falling back to {image}."
        )
        cfg.vm.ubuntu_release = image

    cfg_mod.save(cfg, config_path)

    if not _vm_exists(cfg.vm.name):
        ui.emit(
            {"action": "vm_launch", "vm": cfg.vm.name, "image": image},
            human=f"launching VM {cfg.vm.name!r} ({image}) — this can take a few minutes...",
        )
        mp.launch(
            cfg.vm.name,
            image=image,
            cpus=cfg.vm.cpus,
            memory_gb=cfg.vm.memory_gb,
            disk_gb=cfg.vm.disk_gb,
        )

    if not data_dir_in_vm:
        _ensure_mount(cfg)

    status, boot_stdout, boot_stderr = _run_bootstrap(cfg.vm.name)
    if not status.is_ok():
        # Dump the captured streams so the user sees what actually failed.
        # The per-field flags ("net=fail", "daemon=fail") rarely tell the
        # real story on their own.
        tail = "\n".join(boot_stdout.splitlines()[-60:])
        err_tail = "\n".join(boot_stderr.splitlines()[-20:]) if boot_stderr else ""
        details = f"\n--- bootstrap stdout (last 60 lines) ---\n{tail}"
        if err_tail:
            details += f"\n--- bootstrap stderr (last 20 lines) ---\n{err_tail}"
        hint = ""
        if "net=fail" in status.fail_reasons() and cfg.vm.ubuntu_release != "24.04":
            hint = (
                f"\nHint: ubuntu_release={cfg.vm.ubuntu_release!r}. Docker CE's apt repos may "
                "not include this release yet. Try `agents vm destroy --yes` and re-init with "
                "`--ubuntu-release 24.04` (the toolkit default)."
            )
        raise RuntimeFailure(
            "bootstrap failed: " + ", ".join(status.fail_reasons()) + hint + details
        )

    cfg.vm_runtime.docker_socket_path = status.socket_path()
    cfg.vm_runtime.ubuntu_uid = status.ubuntu_uid()
    cfg.vm_runtime.ubuntu_gid = status.ubuntu_gid()

    cfg.defaults.hermes_image_tag = _fetch_latest_tag("hermes") or _fallback_tag("hermes")
    cfg.defaults.openclaw_image_tag = _fetch_latest_tag("openclaw") or _fallback_tag("openclaw")

    cfg_mod.save(cfg, config_path)
    _seed_data_dir(cfg)

    ui.emit(
        {
            "vm": cfg.vm.name,
            "data_dir": str(cfg.paths.host_data_dir),
            "docker_socket": cfg.vm_runtime.docker_socket_path,
            "hermes_image_tag": cfg.defaults.hermes_image_tag,
            "openclaw_image_tag": cfg.defaults.openclaw_image_tag,
        },
        human=(
            f"VM {cfg.vm.name!r} ready.\n"
            f"  data-dir: {cfg.paths.host_data_dir}\n"
            f"  docker socket: {cfg.vm_runtime.docker_socket_path}\n"
            f"  hermes image: nousresearch/hermes-agent:{cfg.defaults.hermes_image_tag}\n"
            f"  openclaw image: openclaw/openclaw:{cfg.defaults.openclaw_image_tag}"
        ),
    )


# ---------- start / stop / shell / status ----------


def start(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    from agents_cli import compose

    cfg = cfg_mod.load(config_path)
    mp.start_vm(cfg.vm.name)
    if not cfg.paths.data_dir_in_vm:
        _ensure_mount(cfg)
        _mount_probe(cfg)
    # Sweep stale .compose.*.new in the VM and host-side compose-staging
    # files from prior crashes.
    compose.cleanup_stale_temps(cfg)
    ui.emit({"vm": cfg.vm.name, "status": "running"}, human=f"{cfg.vm.name}: running")


def stop(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    mp.stop_vm(cfg.vm.name)
    ui.emit({"vm": cfg.vm.name, "status": "stopped"}, human=f"{cfg.vm.name}: stopped")


def resize(
    *,
    cpus: int | None = None,
    memory_gb: int | None = None,
    disk_gb: int | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Reallocate cpus/memory/disk on the existing VM.

    Multipass requires the VM to be stopped for these changes, and disk
    can only grow. We refuse disk shrinks with a clear error pointing at
    ``vm destroy`` + re-init (the host data-dir is preserved across
    destroys, so this is non-destructive for agent state).
    """
    if cpus is None and memory_gb is None and disk_gb is None:
        raise UserError("provide at least one of --cpus, --memory, --disk")

    cfg = cfg_mod.load(config_path)

    if disk_gb is not None and disk_gb < cfg.vm.disk_gb:
        raise UserError(
            f"disk shrink not supported by multipass ({cfg.vm.disk_gb}G -> {disk_gb}G). "
            "If you really need a smaller disk: `agents vm destroy` then re-run "
            "`agents vm init --data-dir <path> --disk <size>` — your host data-dir "
            "survives the teardown."
        )

    mp.stop_vm(cfg.vm.name)
    try:
        if cpus is not None:
            mp.set_config(f"local.{cfg.vm.name}.cpus", cpus)
        if memory_gb is not None:
            mp.set_config(f"local.{cfg.vm.name}.memory", f"{memory_gb}G")
        if disk_gb is not None:
            mp.set_config(f"local.{cfg.vm.name}.disk", f"{disk_gb}G")
    finally:
        mp.start_vm(cfg.vm.name)

    # Mount survival across stop/start isn't always guaranteed.
    if not cfg.paths.data_dir_in_vm:
        _ensure_mount(cfg)
        _mount_probe(cfg)

    if cpus is not None:
        cfg.vm.cpus = cpus
    if memory_gb is not None:
        cfg.vm.memory_gb = memory_gb
    if disk_gb is not None:
        cfg.vm.disk_gb = disk_gb
    cfg_mod.save(cfg, config_path)

    ui.emit(
        {"cpus": cfg.vm.cpus, "memory_gb": cfg.vm.memory_gb, "disk_gb": cfg.vm.disk_gb},
        human=(
            f"resized {cfg.vm.name!r}: "
            f"cpus={cfg.vm.cpus}, memory={cfg.vm.memory_gb}G, disk={cfg.vm.disk_gb}G"
        ),
    )


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    result = mp.info_vm(cfg.vm.name, fmt="json")
    if result.returncode != 0:
        raise RuntimeFailure(f"multipass info failed: {result.stderr.strip()}")
    info = json.loads(result.stdout)
    vm_info = info.get("info", {}).get(cfg.vm.name, {})
    ui.emit(
        {
            "vm": cfg.vm.name,
            "state": vm_info.get("state"),
            "mounts": list(vm_info.get("mounts", {}).keys()),
            "ipv4": vm_info.get("ipv4", []),
        },
        human=(
            f"{cfg.vm.name}: {vm_info.get('state', 'unknown')}\n"
            f"  mounts: {', '.join(vm_info.get('mounts', {}).keys()) or '(none)'}\n"
            f"  ipv4: {', '.join(vm_info.get('ipv4', [])) or '(none)'}"
        ),
    )


def shell(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    """Drop into a multipass shell. Replaces the process so click can't wrap."""
    cfg = cfg_mod.load(config_path)
    os.execvp("multipass", ["multipass", "shell", cfg.vm.name])


# ---------- snapshots ----------


def take_snapshot(
    name: str, *, comment: str | None = None, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH
) -> str:
    cfg = cfg_mod.load(config_path)
    full_name = _unique_snapshot_name(cfg.vm.name, name)
    mp.snapshot(cfg.vm.name, full_name, comment=comment)
    ui.emit({"vm": cfg.vm.name, "snapshot": full_name}, human=f"snapshot: {full_name}")
    return full_name


def restore_snapshot(
    snapshot_name: str,
    *,
    destructive: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    cfg = cfg_mod.load(config_path)
    snapshot_id = f"{cfg.vm.name}.{snapshot_name}"
    mp.restore(snapshot_id, destructive=destructive)
    ui.emit({"vm": cfg.vm.name, "restored": snapshot_id}, human=f"restored: {snapshot_id}")


def list_snapshots(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> list[str]:
    cfg = cfg_mod.load(config_path)
    result = mp.info_vm(cfg.vm.name, fmt="json")
    if result.returncode != 0:
        return []
    info = json.loads(result.stdout)
    snapshots = info.get("info", {}).get(cfg.vm.name, {}).get("snapshots", {})
    names = sorted(snapshots.keys())
    ui.table(
        [{"snapshot": n, "comment": snapshots[n].get("comment", "")} for n in names],
        ["snapshot", "comment"],
    )
    return names


# ---------- rebuild ----------


REBUILD_PHASES = [
    "staged_exports",
    "vm_destroyed",
    "vm_initted",
    "state_restored",
    "services_started",
]


def rebuild(
    *,
    cpus: int | None = None,
    memory_gb: int | None = None,
    disk_gb: int | None = None,
    ubuntu_release: str | None = None,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Non-destructively re-create the VM with new sizing.

    The case ``vm resize`` can't handle: disk shrink (multipass refuses),
    bumping the Ubuntu release, or fixing a broken VM. Sequence:

    1. **staged_exports** — ``agents export --all`` writes ``.tar.zst``
       archives into ``<data-dir>/.rebuild-staging/``.
    2. **vm_destroyed** — ``mp.delete_vm`` removes the VM. Host data-dir
       stays put (in host-mount mode all the agent state is here
       already; in ``--data-dir-in-vm`` only the staged archives are).
    3. **vm_initted** — fresh ``vm init`` with the (possibly overridden)
       sizing. ``cpus``/``memory_gb``/``disk_gb``/``ubuntu_release``
       default to the existing config; pass any to override.
    4. **state_restored** — ``agents import`` each archive back into its
       agent's data dir.
    5. **services_started** — bring every agent's container up via
       ``docker compose up -d``.

    Journaled — each phase advances the on-disk journal so a crashed
    rebuild can resume by re-running the command. Phases are
    independently idempotent (each checks whether its work is already
    done before redoing it).

    The staging dir is left in place for inspection; you can ``rm -rf
    <data-dir>/.rebuild-staging/`` after confirming everything's back
    up.
    """
    from agents_cli import agents_cmd, compose, compose_runner, export_cmd
    from agents_cli.journal import Journal

    cfg = cfg_mod.load(config_path)

    target_cpus = cpus if cpus is not None else cfg.vm.cpus
    target_memory = memory_gb if memory_gb is not None else cfg.vm.memory_gb
    target_disk = disk_gb if disk_gb is not None else cfg.vm.disk_gb
    target_release = ubuntu_release if ubuntu_release is not None else cfg.vm.ubuntu_release

    if not yes and not ui.confirm(
        f"rebuild VM {cfg.vm.name!r}? Plan:\n"
        f"  - export all agents to <data-dir>/.rebuild-staging/\n"
        f"  - destroy and re-create the VM (cpus={target_cpus}, "
        f"memory={target_memory}G, disk={target_disk}G, "
        f"ubuntu={target_release})\n"
        f"  - import + start each agent",
        default=False,
    ):
        raise UserError("aborted by user")

    staging_dir = cfg.paths.host_data_dir / ".rebuild-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Names of all agents currently in the compose file, captured BEFORE
    # destroy so we know what to restore.
    doc = compose.read_compose_doc(cfg)
    services = list((doc.get("services") or {}).keys())
    agent_specs: list[tuple[str, str]] = []
    for svc in services:
        agent_type = agents_cmd._agent_type_from_service(svc)
        if agent_type is None:
            continue
        name = agents_cmd._name_from_service(svc, agent_type)
        agent_specs.append((name, agent_type))

    with compose.acquire_lock(cfg.paths.host_data_dir):
        journal = Journal.find_resumable(
            cfg.paths.host_data_dir, "rebuild", cfg.vm.name
        ) or Journal.begin(cfg.paths.host_data_dir, "rebuild", cfg.vm.name, REBUILD_PHASES)

        if not journal.is_phase_done("staged_exports"):
            for name, _ in agent_specs:
                export_cmd.export(name, to=staging_dir, config_path=config_path)
            journal.advance("staged_exports")

        if not journal.is_phase_done("vm_destroyed"):
            if _vm_exists(cfg.vm.name):
                mp.delete_vm(cfg.vm.name, purge=True)
            journal.advance("vm_destroyed")

        if not journal.is_phase_done("vm_initted"):
            # Re-init from scratch with the chosen sizing. init() persists
            # the new values back to config.toml.
            init(
                data_dir=cfg.paths.host_data_dir,
                data_dir_in_vm=cfg.paths.data_dir_in_vm,
                cpus=target_cpus,
                memory_gb=target_memory,
                disk_gb=target_disk,
                ubuntu_release=target_release,
                config_path=config_path,
            )
            journal.advance("vm_initted")

        # Re-load config — init may have changed [vm_runtime] etc.
        cfg = cfg_mod.load(config_path)

        if not journal.is_phase_done("state_restored"):
            for name, _ in agent_specs:
                archives = sorted(staging_dir.glob(f"{name}-*.tar.zst"))
                if not archives:
                    raise RuntimeFailure(
                        f"no export archive found for agent {name!r} in {staging_dir}"
                    )
                # Use the most recent one.
                export_cmd.import_archive(name, archives[-1], config_path=config_path)
            journal.advance("state_restored")

        if not journal.is_phase_done("services_started"):
            for _, _ in agent_specs:
                # `docker compose up -d` for each service via compose_runner.
                # Iterate by service so a single broken agent doesn't take
                # the rest down.
                pass
            for name, agent_type in agent_specs:
                svc = agents_cmd.service_name(name, agent_type)
                compose_runner.up(cfg, svc)
            journal.advance("services_started")

        journal.complete()

    ui.emit(
        {
            "vm": cfg.vm.name,
            "rebuilt": True,
            "cpus": target_cpus,
            "memory_gb": target_memory,
            "disk_gb": target_disk,
            "ubuntu_release": target_release,
            "restored_agents": [n for n, _ in agent_specs],
            "staging_dir": str(staging_dir),
        },
        human=(
            f"VM {cfg.vm.name!r} rebuilt.\n"
            f"  cpus={target_cpus}, memory={target_memory}G, disk={target_disk}G, "
            f"ubuntu={target_release}\n"
            f"  agents restored: {', '.join(n for n, _ in agent_specs) or '(none)'}\n"
            f"  staging dir: {staging_dir}\n"
            f"  (safe to `rm -rf {staging_dir}` after `agents doctor` is green)"
        ),
    )


# ---------- destroy ----------


def destroy(*, yes: bool = False, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    if not yes and not ui.confirm(
        f"destroy VM {cfg.vm.name!r}? Host data-dir at {cfg.paths.host_data_dir} stays untouched.",
        default=False,
    ):
        raise UserError("aborted by user")
    mp.delete_vm(cfg.vm.name, purge=True)
    ui.emit({"vm": cfg.vm.name, "status": "destroyed"}, human=f"{cfg.vm.name}: destroyed")


# ---------- private helpers ----------


def _load_or_default_config(path: Path) -> AgentsConfig:
    if path.exists():
        return cfg_mod.load(path)
    # Defaults for everything except `paths`, which init will set right after.
    return AgentsConfig.model_construct(
        vm=VmConfig(),
        paths=PathsConfig.model_construct(
            host_data_dir=Path("/dev/null"),  # placeholder, overwritten immediately
            vm_agents_root=Path("/home/ubuntu/agents"),
            data_dir_in_vm=False,
        ),
        host=HostConfig(uid=os.getuid(), gid=os.getgid()),
        defaults=cfg_mod.DefaultsConfig(),
        export_cron=ExportCronConfig(),
        vm_runtime=cfg_mod.VmRuntimeConfig(),
    )


def _prepare_host_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    if any(data_dir.iterdir()) and not (data_dir / "docker-compose.yml").exists():
        # Existing files but no compose: refuse to mix with prior non-toolkit content.
        raise UserError(
            f"{data_dir} is non-empty and does not look like a previous toolkit data-dir. "
            "Pick an empty path or remove the existing files."
        )


def _select_ubuntu_image(requested: str) -> str:
    """If `multipass find` doesn't list `requested`, fall back to 24.04 LTS."""
    try:
        result = mp.find(show_unsupported=True)
    except RuntimeFailure:
        return requested
    if result.returncode != 0:
        return requested
    try:
        catalog = json.loads(result.stdout)
    except json.JSONDecodeError:
        return requested
    images = catalog.get("images", {})
    aliases: set[str] = set()
    for img in images.values():
        aliases.update(img.get("aliases_no_remote", []))
        aliases.update(img.get("aliases", []))
    if requested in aliases:
        return requested
    return "24.04" if "24.04" in aliases else requested


def _vm_exists(name: str) -> bool:
    try:
        result = mp.list_vms(fmt="json")
    except RuntimeFailure:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return any(v.get("name") == name for v in data.get("list", []))


def _ensure_mount(cfg: AgentsConfig) -> None:
    """Mount the host data-dir into the VM if it isn't already.

    Multipass ``--type native`` requires the VM stopped. If the VM is
    running we stop, mount, and restart — accepting the ~30s cost in
    exchange for a mount type that supports rootless Docker bind mounts
    (the default ``classic`` / fuse.sshfs does not — see ``mp.mount``).
    Mounts persist across reboots, so this only triggers when the mount
    is genuinely absent (fresh ``vm init`` or after an external umount).
    """
    if _has_mount(cfg):
        return

    was_running = _vm_running(cfg.vm.name)
    if was_running:
        mp.stop_vm(cfg.vm.name)
    mp.mount(
        cfg.paths.host_data_dir,
        cfg.vm.name,
        str(cfg.paths.vm_agents_root),
        host_uid=cfg.host.uid,
        host_gid=cfg.host.gid,
    )
    if was_running:
        mp.start_vm(cfg.vm.name)


def _has_mount(cfg: AgentsConfig) -> bool:
    result = mp.info_vm(cfg.vm.name, fmt="json")
    if result.returncode != 0:
        return False
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    mounts = info.get("info", {}).get(cfg.vm.name, {}).get("mounts", {})
    return str(cfg.paths.vm_agents_root) in mounts


def _vm_running(vm: str) -> bool:
    result = mp.info_vm(vm, fmt="json")
    if result.returncode != 0:
        return False
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    state = info.get("info", {}).get(vm, {}).get("state", "")
    return bool(state == "Running")


def _mount_probe(cfg: AgentsConfig) -> None:
    """Verify a write+read+unlink round-trip through the mount."""
    probe_name = f".mount-probe-{uuid.uuid4().hex[:8]}"
    probe_path = cfg.paths.host_data_dir / probe_name
    try:
        probe_path.write_text("ok\n", encoding="utf-8")
        result = mp.exec_in_vm(
            cfg.vm.name,
            "cat",
            f"{cfg.paths.vm_agents_root}/{probe_name}",
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "ok":
            raise HealthCheckFailure(
                f"mount probe failed: host wrote {probe_path}, VM cat returned "
                f"rc={result.returncode}, stdout={result.stdout!r}"
            )
    finally:
        probe_path.unlink(missing_ok=True)


def _run_bootstrap(vm_name: str) -> tuple[BootstrapStatus, str, str]:
    """Run the bootstrap script and return (status, stdout, stderr).

    Returning the raw streams lets the caller surface them when the
    handshake fails — the per-field `fail` flags by themselves rarely
    explain *why* something didn't work (network blocked, distro too
    new for Docker, etc.).
    """
    result = mp.exec_bash(vm_name, templates.VM_BOOTSTRAP_SCRIPT, timeout=900.0)
    status = mp.parse_bootstrap_ok(result.stdout)
    return status, result.stdout, result.stderr


def _seed_data_dir(cfg: AgentsConfig) -> None:
    """Write the initial compose + .env + skeleton dirs.

    Two paths because the layout differs by mode:

    - Host-mount mode (default): everything goes to the host data-dir,
      which the VM sees through the mount. Idempotent — never
      overwrites an existing compose or .env.

    - ``--data-dir-in-vm`` mode: agent state (compose, .env, .codex,
      backups, .upgrades) lives only in the VM. The host data-dir
      still holds the toolkit's lock + journals (host-filesystem
      primitives), so we create the minimal local layout for those.
    """
    # Always: minimal host scaffolding for the lock and journals.
    (cfg.paths.host_data_dir / ".journal").mkdir(exist_ok=True)

    if cfg.paths.data_dir_in_vm:
        _seed_vm_data_dir(cfg)
        return

    # Host-mount mode: everything on host, VM sees it through the mount.
    compose = cfg.paths.host_data_dir / "docker-compose.yml"
    env = cfg.paths.host_data_dir / ".env"
    if not compose.exists():
        compose.write_text(templates.initial_compose_content(), encoding="utf-8")
    if not env.exists():
        env.write_text(
            templates.initial_env_content(cfg.vm_runtime.ubuntu_uid, cfg.vm_runtime.ubuntu_gid),
            encoding="utf-8",
        )
    (cfg.paths.host_data_dir / ".codex").mkdir(exist_ok=True)
    (cfg.paths.host_data_dir / "backups").mkdir(exist_ok=True)
    (cfg.paths.host_data_dir / ".upgrades").mkdir(exist_ok=True)


def _seed_vm_data_dir(cfg: AgentsConfig) -> None:
    """Seed compose + .env + skeleton dirs inside the VM for
    ``--data-dir-in-vm`` mode. Only writes files that don't already exist.
    """
    root = cfg.paths.vm_agents_root
    compose_content = templates.initial_compose_content()
    env_content = templates.initial_env_content(
        cfg.vm_runtime.ubuntu_uid, cfg.vm_runtime.ubuntu_gid
    )
    script = (
        f"set -eu\n"
        f"mkdir -p {root}/.codex {root}/backups {root}/.upgrades\n"
        f"[ -f {root}/docker-compose.yml ] || cat > {root}/docker-compose.yml <<'__AGENTS_EOF__'\n"
        f"{compose_content}__AGENTS_EOF__\n"
        f"[ -f {root}/.env ] || cat > {root}/.env <<'__AGENTS_EOF__'\n"
        f"{env_content}__AGENTS_EOF__\n"
    )
    mp.exec_bash(cfg.vm.name, script)


def _unique_snapshot_name(vm: str, requested: str) -> str:
    """Append a monotonic counter if `<vm>.<requested>` already exists."""
    try:
        result = mp.info_vm(vm, fmt="json")
        if result.returncode != 0:
            return requested
        existing = json.loads(result.stdout).get("info", {}).get(vm, {}).get("snapshots", {})
    except (json.JSONDecodeError, RuntimeFailure):
        return requested
    if requested not in existing:
        return requested
    for n in range(2, 1000):
        candidate = f"{requested}-{n}"
        if candidate not in existing:
            return candidate
    raise RuntimeFailure(f"could not find a unique snapshot name for {requested}")


def _fetch_latest_tag(repo_key: str) -> str | None:
    """Best-effort fetch from GitHub Releases. 3-second timeout, no exceptions out."""
    url = GITHUB_LATEST_URLS.get(repo_key)
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name")
            return str(tag) if tag else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _fallback_tag(repo_key: str) -> str:
    if repo_key == "hermes":
        return templates.FALLBACK_HERMES_IMAGE_TAG
    if repo_key == "openclaw":
        return templates.FALLBACK_OPENCLAW_IMAGE_TAG
    raise ValueError(f"no fallback tag for repo_key={repo_key!r}")
