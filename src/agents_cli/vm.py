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
    backend: str = "multipass",
    gpu: list[str] | None = None,
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

    ``backend`` selects the hypervisor (``"multipass"`` default,
    ``"libvirt"`` experimental Linux-only). ``gpu`` is a list of PCI
    IDs to pass through on libvirt — ``["auto"]`` triggers vendor-
    aware autodetect (NVIDIA / AMD / Intel) and fills the config with
    every GPU bound to vfio-pci.
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
    cfg.vm.backend = backend
    if backend == "libvirt":
        cfg.vm.gpu_passthrough = _resolve_gpu_list(gpu or [])
    elif gpu:
        raise UserError("--gpu requires --backend libvirt")

    # Bare-metal aligns vm_agents_root with the real host data-dir so
    # compose_runner's `--project-directory` / `-f` / `--env-file` flags
    # produce correct host paths without per-call special-casing.
    if backend == "bare_metal":
        import platform as _platform

        if _platform.system() != "Linux":
            raise UserError(
                f"--backend bare_metal is Linux-only (host: {_platform.system()}). "
                "Use --backend multipass on macOS/Windows."
            )
        if data_dir_in_vm:
            raise UserError("--data-dir-in-vm is meaningless with --backend bare_metal")
        cfg.paths = PathsConfig.model_validate(
            {
                "host_data_dir": str(cfg.paths.host_data_dir),
                "vm_agents_root": str(cfg.paths.host_data_dir),
                "data_dir_in_vm": False,
            }
        )

    # Switch the dispatcher to the chosen backend BEFORE any of the
    # mp.* primitives below run, otherwise we'd talk to multipass
    # when the user asked for libvirt.
    from agents_cli import mp
    from agents_cli.backends import get_backend_module

    mp.set_backend(get_backend_module(backend))
    if backend == "libvirt":
        from agents_cli.backends import libvirt as libvirt_mod

        libvirt_mod.configure(cfg.paths.host_data_dir)

    cfg.host = HostConfig(uid=os.getuid(), gid=os.getgid())

    _prepare_host_data_dir(cfg.paths.host_data_dir)

    if backend == "multipass":
        image = _select_ubuntu_image(cfg.vm.ubuntu_release)
        if image != cfg.vm.ubuntu_release:
            ui.warn(
                f"Ubuntu {cfg.vm.ubuntu_release} not listed by `multipass find`; "
                f"falling back to {image}."
            )
            cfg.vm.ubuntu_release = image
    elif backend == "libvirt":
        # libvirt backend interprets `image` as the Ubuntu release
        # version (e.g. "24.04") and downloads the matching cloud
        # image directly — no catalog probe needed.
        image = cfg.vm.ubuntu_release
    else:
        # bare_metal: no VM, no image. Keep the string so the journal
        # event below has something printable.
        image = "(host)"

    cfg_mod.save(cfg, config_path)

    if backend != "bare_metal" and not _vm_exists(cfg.vm.name):
        ui.emit(
            {"action": "vm_launch", "vm": cfg.vm.name, "image": image},
            human=f"launching VM {cfg.vm.name!r} ({image}) — this can take a few minutes...",
        )
        try:
            mp.launch(
                cfg.vm.name,
                image=image,
                cpus=cfg.vm.cpus,
                memory_gb=cfg.vm.memory_gb,
                disk_gb=cfg.vm.disk_gb,
            )
            # libvirt: wait for SSH (cloud-init) before bootstrap; attach
            # any configured GPUs while the VM is stopped, then restart.
            if backend == "libvirt":
                from agents_cli.backends import libvirt as libvirt_mod

                libvirt_mod.wait_for_ssh(cfg.vm.name)
                if cfg.vm.gpu_passthrough:
                    mp.stop_vm(cfg.vm.name)
                    for pci in cfg.vm.gpu_passthrough:
                        ui.info(f"attaching GPU {pci} to {cfg.vm.name}")
                        libvirt_mod.attach_gpu_xml(cfg.vm.name, pci)
                    mp.start_vm(cfg.vm.name)
                    libvirt_mod.wait_for_ssh(cfg.vm.name)
        except BaseException:
            # Transactional cleanup (libvirt only). Multipass's `launch`
            # is atomic — either the VM exists fully or not at all —
            # but libvirt can leave a partially-created domain that
            # references missing disk files, blocking retries until
            # `virsh undefine --remove-all-storage`. See GH #3.
            if backend == "libvirt":
                ui.warn(
                    "vm init failed after launch — cleaning up partial libvirt domain "
                    "so retry isn't blocked..."
                )
                from agents_cli.backends import libvirt as libvirt_mod

                libvirt_mod._cleanup_partial_domain(cfg.vm.name)
            raise

    if backend != "bare_metal" and not data_dir_in_vm:
        _ensure_mount(cfg)

    if backend == "bare_metal":
        from agents_cli.backends import bare_metal as bm

        status, boot_stdout, boot_stderr = bm.verify_bootstrap(cfg.paths.host_data_dir)
    else:
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

    # Make the VM auto-resume on host boot so the chain Docker →
    # containers → agents recovers without manual intervention.
    # No-op on multipass (snap daemon auto-resumes everything),
    # essential on libvirt (defaults to Autostart=disable), no-op on
    # bare_metal (no VM; container `restart: unless-stopped` covers it).
    if backend != "bare_metal":
        autostart_result = mp.set_autostart(cfg.vm.name, enabled=True)
        if autostart_result.returncode != 0:
            ui.warn(
                f"could not enable autostart for {cfg.vm.name!r}: "
                f"{(autostart_result.stderr or '').strip() or 'unknown error'}. "
                f"VM won't auto-start on host reboot; enable manually via the "
                f"backend's native tool if needed."
            )

    cfg_mod.save(cfg, config_path)
    _seed_data_dir(cfg)

    if backend == "bare_metal":
        ui.warn(
            "bare_metal backend: the VM trust boundary is forfeited. A compromised "
            "agent that escapes its container reaches the host directly. Only use this "
            "on single-user dev workstations where the host is already trusted."
        )
        human_ready = (
            f"bare_metal runtime ready (no VM).\n"
            f"  data-dir: {cfg.paths.host_data_dir}\n"
            f"  docker socket: {cfg.vm_runtime.docker_socket_path}\n"
            f"  hermes image: nousresearch/hermes-agent:{cfg.defaults.hermes_image_tag}\n"
            f"  openclaw image: openclaw/openclaw:{cfg.defaults.openclaw_image_tag}"
        )
    else:
        human_ready = (
            f"VM {cfg.vm.name!r} ready.\n"
            f"  data-dir: {cfg.paths.host_data_dir}\n"
            f"  docker socket: {cfg.vm_runtime.docker_socket_path}\n"
            f"  hermes image: nousresearch/hermes-agent:{cfg.defaults.hermes_image_tag}\n"
            f"  openclaw image: openclaw/openclaw:{cfg.defaults.openclaw_image_tag}"
        )
    ui.emit(
        {
            "vm": cfg.vm.name,
            "backend": backend,
            "data_dir": str(cfg.paths.host_data_dir),
            "docker_socket": cfg.vm_runtime.docker_socket_path,
            "hermes_image_tag": cfg.defaults.hermes_image_tag,
            "openclaw_image_tag": cfg.defaults.openclaw_image_tag,
        },
        human=human_ready,
    )


# ---------- start / stop / shell / status ----------


def start(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    from agents_cli import compose

    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        # No VM lifecycle on bare-metal; nothing to wait for. Still sweep
        # stale staging files so the next mutation isn't tripped up by
        # a prior crashed write.
        compose.cleanup_stale_temps(cfg)
        ui.emit(
            {"vm": cfg.vm.name, "status": "running", "backend": "bare_metal"},
            human=f"{cfg.vm.name}: running (bare_metal — host is always 'on')",
        )
        return
    mp.start_vm(cfg.vm.name)
    # libvirt's ``virsh start`` returns as soon as QEMU is up; SSH is
    # only reachable after cloud-init runs the network bringup. The
    # mount probe below uses exec_in_vm (= SSH on libvirt) and would
    # race DHCP with rc=255 (ssh: connection refused). Wait_for_ssh
    # cushions this. Same pattern as vm.init's libvirt branch.
    # Multipass doesn't need it (mp.start_vm blocks until SSH-able).
    if cfg.vm.backend == "libvirt":
        from agents_cli.backends import libvirt as libvirt_mod

        libvirt_mod.wait_for_ssh(cfg.vm.name, wait_cloud_init=False)
    if not cfg.paths.data_dir_in_vm:
        _ensure_mount(cfg)
        _mount_probe(cfg)
    # Sweep stale .compose.*.new in the VM and host-side compose-staging
    # files from prior crashes.
    compose.cleanup_stale_temps(cfg)
    ui.emit({"vm": cfg.vm.name, "status": "running"}, human=f"{cfg.vm.name}: running")


def stop(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        ui.emit(
            {"vm": cfg.vm.name, "status": "running", "backend": "bare_metal"},
            human=(
                f"{cfg.vm.name}: no-op (bare_metal — host doesn't stop). "
                f"Use `agents stop <name>` to stop individual agents."
            ),
        )
        return
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
    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "vm resize doesn't apply on the bare_metal backend (host sizing is "
            "real hardware). Adjust host resources directly."
        )

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
    """Drop into a shell with the toolkit's env pre-applied.

    Multipass / libvirt: hand off to the backend's native shell tool.
    Bare-metal: spawn a subshell on the host with ``CWD=<data-dir>`` and
    ``DOCKER_HOST=unix://<rootless socket>`` exported, so ``docker
    compose ...`` and ``agents exec ...`` just work.
    """
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        from agents_cli.backends import bare_metal as bm

        rc = bm.open_shell(cfg.paths.host_data_dir, cfg.vm_runtime.docker_socket_path)
        if rc != 0:
            raise RuntimeFailure(f"subshell exited with status {rc}")
        return
    if cfg.vm.backend == "libvirt":
        from agents_cli.backends import libvirt as libvirt_mod

        rc = libvirt_mod.exec_in_vm_interactive(cfg.vm.name, "bash", "-l")
        if rc != 0:
            raise RuntimeFailure(f"shell exited with status {rc}")
        return
    os.execvp("multipass", ["multipass", "shell", cfg.vm.name])


# ---------- snapshots ----------


def take_snapshot(
    name: str,
    *,
    comment: str | None = None,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> str:
    """Take a VM snapshot.

    Multipass snapshots can take a running VM directly. libvirt with
    an active virtiofs mount cannot (vhost-user backend lacks
    ``VHOST_USER_PROTOCOL_F_LOG_SHMFD``), so on that backend we
    prompt for confirmation and run a transactional stop → snapshot
    → start cycle. The VM is briefly down (~30s); any containers
    inside get a clean shutdown via the libvirt ACPI signal +
    restart afterwards.

    ``yes=True`` skips the confirmation prompt.
    """
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "VM snapshots are unavailable on the bare_metal backend (no VM disk to "
            "snapshot). Use `agents export <name>` for data-dir backups, and a host-"
            "level snapshot tool (duplicity, restic, btrfs/zfs snapshots) for the "
            "rest."
        )
    full_name = _unique_snapshot_name(cfg.vm.name, name)

    # libvirt + virtiofs needs the VM stopped for snapshot. Other
    # combinations (multipass, libvirt with --data-dir-in-vm) work
    # directly on a running VM.
    requires_downtime = cfg.vm.backend == "libvirt" and not cfg.paths.data_dir_in_vm
    was_running = requires_downtime and _vm_running(cfg.vm.name)

    if (
        was_running
        and not yes
        and not ui.confirm(
            (
                "Snapshotting a libvirt VM with an active virtiofs mount "
                "requires briefly stopping the VM (~30s downtime). Any "
                "running Docker containers (Hermes agents, Firecrawl, "
                "Ollama, etc.) will be cleanly stopped by libvirt's ACPI "
                "shutdown and restarted afterwards.\n"
                "Proceed?"
            ),
            default=False,
        )
    ):
        raise UserError("aborted by user")

    if was_running:
        ui.info(f"stopping {cfg.vm.name!r} for snapshot...")
        mp.stop_vm(cfg.vm.name)
        # Wait for shutdown to complete (ACPI can take ~20s on a
        # busy Ubuntu cloud image).
        _wait_for_vm_stopped(cfg.vm.name, timeout_s=60)

    try:
        result = mp.snapshot(cfg.vm.name, full_name, comment=comment)
        if result.returncode != 0:
            raise RuntimeFailure(f"snapshot failed: {(result.stderr or 'unknown error').strip()}")
    finally:
        if was_running:
            ui.info(f"restarting {cfg.vm.name!r}...")
            mp.start_vm(cfg.vm.name)
            if cfg.vm.backend == "libvirt":
                from agents_cli.backends import libvirt as libvirt_mod

                libvirt_mod.wait_for_ssh(cfg.vm.name, wait_cloud_init=False)

    ui.emit({"vm": cfg.vm.name, "snapshot": full_name}, human=f"snapshot: {full_name}")
    return full_name


def _wait_for_vm_stopped(vm_name: str, *, timeout_s: int = 60) -> None:
    """Poll info_vm until the VM reports a non-running state.

    libvirt's ``shutdown`` is asynchronous (sends ACPI then returns);
    calling snapshot-create-as immediately after may race against
    qemu still flushing. Multipass's ``stop`` blocks until done so
    this is a libvirt-shaped helper, but harmless on multipass.
    """
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _vm_running(vm_name):
            return
        time.sleep(2)
    raise RuntimeFailure(
        f"VM {vm_name!r} did not shut down within {timeout_s}s — "
        f"force with `virsh destroy {vm_name}` and retry."
    )


def restore_snapshot(
    snapshot_name: str,
    *,
    destructive: bool = False,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Revert the VM to a prior snapshot.

    Same downtime trade-off as :func:`take_snapshot`: libvirt with
    an active virtiofs mount can't snapshot-revert a running VM,
    so we prompt for confirmation and stop the VM first. After
    revert the VM is restarted so the user lands on a running VM
    again (matching multipass-restore semantics).

    ``yes=True`` skips the confirmation prompt. ``destructive=True``
    bypasses multipass's safety check (no-op on libvirt).
    """
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "snapshot restore is unavailable on the bare_metal backend. Use "
            "`agents import <name> <archive>` to restore an `agents export` archive."
        )
    snapshot_id = f"{cfg.vm.name}.{snapshot_name}"

    requires_downtime = cfg.vm.backend == "libvirt" and not cfg.paths.data_dir_in_vm
    was_running = requires_downtime and _vm_running(cfg.vm.name)

    if (
        was_running
        and not yes
        and not ui.confirm(
            (
                f"Restoring snapshot {snapshot_name!r} on a libvirt VM with "
                f"an active virtiofs mount requires briefly stopping the "
                f"VM (~30s). Any running Docker containers will be cleanly "
                f"stopped and restarted afterwards. The restore itself is "
                f"destructive — state since the snapshot will be lost.\n"
                f"Proceed?"
            ),
            default=False,
        )
    ):
        raise UserError("aborted by user")

    if was_running:
        ui.info(f"stopping {cfg.vm.name!r} for restore...")
        mp.stop_vm(cfg.vm.name)
        _wait_for_vm_stopped(cfg.vm.name, timeout_s=60)

    try:
        mp.restore(snapshot_id, destructive=destructive)
    finally:
        if was_running:
            ui.info(f"restarting {cfg.vm.name!r}...")
            mp.start_vm(cfg.vm.name)
            if cfg.vm.backend == "libvirt":
                from agents_cli.backends import libvirt as libvirt_mod

                libvirt_mod.wait_for_ssh(cfg.vm.name, wait_cloud_init=False)

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
    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "vm rebuild doesn't apply on the bare_metal backend (there's no VM to "
            "rebuild). For data restore: `agents import`; for full reset: `agents "
            "vm destroy --yes` then `agents vm init --backend bare_metal`."
        )

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
    if cfg.vm.backend == "bare_metal":
        # On bare-metal there's no VM to delete; instead, bring the
        # compose stack down. Host data-dir is untouched (matches the
        # multipass/libvirt invariant).
        from agents_cli import compose_runner

        if not yes and not ui.confirm(
            (
                f"bare_metal: stop and remove all toolkit-managed containers? "
                f"Host data-dir at {cfg.paths.host_data_dir} stays untouched."
            ),
            default=False,
        ):
            raise UserError("aborted by user")
        compose_runner.down(cfg, remove_orphans=True)
        ui.emit(
            {"vm": cfg.vm.name, "status": "destroyed", "backend": "bare_metal"},
            human=f"{cfg.vm.name}: compose stack down (bare_metal — no VM to delete)",
        )
        return
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

    Multipass ``--type native`` requires the VM stopped. The libvirt
    backend's virtiofs mount works ``--live --config`` (no stop
    needed). To avoid an unnecessary stop/start cycle (which loses
    DHCP lease + briefly wedges the host's ARP cache, then breaks
    bootstrap with "no route to host" — observed during the
    2026-05-16 smoke test), only stop+start on multipass.

    Mounts persist across reboots, so this only triggers when the
    mount is genuinely absent (fresh ``vm init`` or after an external
    umount).
    """
    if _has_mount(cfg):
        return

    requires_stop = cfg.vm.backend == "multipass"
    was_running = _vm_running(cfg.vm.name)
    if was_running and requires_stop:
        mp.stop_vm(cfg.vm.name)
    mp.mount(
        cfg.paths.host_data_dir,
        cfg.vm.name,
        str(cfg.paths.vm_agents_root),
        host_uid=cfg.host.uid,
        host_gid=cfg.host.gid,
    )
    if was_running and requires_stop:
        mp.start_vm(cfg.vm.name)
        if cfg.vm.backend == "libvirt":
            from agents_cli.backends import libvirt as libvirt_mod

            libvirt_mod.wait_for_ssh(cfg.vm.name)


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


def _resolve_gpu_list(requested: list[str]) -> list[str]:
    """Map the ``--gpu`` CLI input to a concrete list of PCI IDs.

    ``["auto"]`` triggers vendor-aware detection (NVIDIA / AMD / Intel)
    via :func:`agents_cli.backends.libvirt.detect_gpus`. Anything else
    is treated as a literal PCI-ID list and validated against the
    DDDD:BB:SS.F regex. Empty list returns empty.

    Detected GPUs are filtered to those bound to vfio-pci — silently
    skipping host-driver-bound devices (which would fail to attach
    anyway). If autodetect finds zero vfio-bound GPUs, raises
    UserError with a setup hint rather than silently succeeding with
    no passthrough.
    """
    if not requested:
        return []
    if requested == ["auto"]:
        from agents_cli.backends import libvirt as libvirt_mod

        gpus = libvirt_mod.detect_gpus()
        vfio = [g["pci_id"] for g in gpus if g["vfio_bound"] == "true"]
        if not vfio:
            raise UserError(
                "no vfio-pci-bound GPUs found. Bind the GPU to vfio-pci first: "
                "see https://wiki.archlinux.org/title/PCI_passthrough_via_OVMF"
                " for the standard kernel-cmdline + vfio-pci.ids setup."
            )
        return vfio
    # Explicit list; pydantic validator catches malformed entries.
    return list(requested)


def _fallback_tag(repo_key: str) -> str:
    if repo_key == "hermes":
        return templates.FALLBACK_HERMES_IMAGE_TAG
    if repo_key == "openclaw":
        return templates.FALLBACK_OPENCLAW_IMAGE_TAG
    raise ValueError(f"no fallback tag for repo_key={repo_key!r}")
