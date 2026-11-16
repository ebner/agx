"""VM lifecycle: init, start, stop, shell, status, snapshot, restore, destroy.

Orchestration only. Every multipass call goes through ``mp``, every config
read/write goes through ``config``. That way the unit tests mock at the
``mp`` boundary and never touch a real VM.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from agx_cli import config as cfg_mod
from agx_cli import mp, templates, ui
from agx_cli.config import AgentsConfig, ExportCronConfig, HostConfig, PathsConfig, VmConfig
from agx_cli.errors import CliError, HealthCheckFailure, RuntimeFailure, UserError
from agx_cli.mp import BootstrapStatus

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
    emit: bool = True,
) -> None:
    """Idempotently provision the agents VM.

    Steps:
      1. Validate / create the host data-dir.
      2. Build (or update) ~/.config/agx/config.toml with the chosen sizing.
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

    ``emit=False`` suppresses the final "ready" :func:`ui.emit` for
    composite callers (``vm rebuild``) that own the command's single
    stdout object; stage narration still goes to stderr via
    ``ui.progress``.
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
    from agx_cli import mp
    from agx_cli.backends import get_backend_module

    mp.set_backend(get_backend_module(backend))
    if backend == "libvirt":
        from agx_cli.backends import libvirt as libvirt_mod

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
        # Narration, not a result: stdout carries exactly one object per
        # command (the final "ready" payload below).
        ui.progress(f"launching VM {cfg.vm.name!r} ({image}) — this can take a few minutes...")
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
                from agx_cli.backends import libvirt as libvirt_mod

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
                from agx_cli.backends import libvirt as libvirt_mod

                libvirt_mod._cleanup_partial_domain(cfg.vm.name)
            raise

    if backend != "bare_metal" and not data_dir_in_vm:
        _ensure_mount(cfg)

    if backend == "bare_metal":
        from agx_cli.backends import bare_metal as bm

        status, boot_stdout, boot_stderr = bm.verify_bootstrap(cfg.paths.host_data_dir)
    else:
        ui.progress(
            "provisioning rootless Docker in the VM — several minutes on first run, "
            "seconds when already bootstrapped..."
        )
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
                "not include this release yet. Try `agx vm destroy --yes` and re-init with "
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
        _warn_bare_metal_log_rotation()
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
    # VM disk, image cache, seeds, staging: keep them out of host backups.
    from agx_cli import backup_tags

    backup_tags.tag_rebuildable_dirs(cfg)
    if emit:
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
    from agx_cli import backup_tags, compose

    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        # No VM lifecycle on bare-metal; nothing to wait for. Still sweep
        # stale staging files so the next mutation isn't tripped up by
        # a prior crashed write.
        compose.cleanup_stale_temps(cfg)
        backup_tags.tag_rebuildable_dirs(cfg)
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
        from agx_cli.backends import libvirt as libvirt_mod

        libvirt_mod.wait_for_ssh(cfg.vm.name, wait_cloud_init=False)
    if not cfg.paths.data_dir_in_vm:
        _ensure_mount(cfg)
        _mount_probe(cfg)
    else:
        # The host override file arms an extra `-f`; heal a missing in-VM
        # mirror (VM rebuilt by an older toolkit) so compose verbs work.
        compose.sync_override_to_vm(cfg, only_if_missing=True)
    # Sweep stale .compose.*.new in the VM and host-side compose-staging
    # files from prior crashes.
    compose.cleanup_stale_temps(cfg)
    backup_tags.tag_rebuildable_dirs(cfg)
    ui.emit({"vm": cfg.vm.name, "status": "running"}, human=f"{cfg.vm.name}: running")


def stop(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        ui.emit(
            {"vm": cfg.vm.name, "status": "running", "backend": "bare_metal"},
            human=(
                f"{cfg.vm.name}: no-op (bare_metal — host doesn't stop). "
                f"Use `agx stop <name>` to stop individual agents."
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
            "If you really need a smaller disk: `agx vm destroy` then re-run "
            "`agx vm init --data-dir <path> --disk <size>` — your host data-dir "
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
    compose ...`` and ``agx exec ...`` just work.
    """
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        from agx_cli.backends import bare_metal as bm

        rc = bm.open_shell(cfg.paths.host_data_dir, cfg.vm_runtime.docker_socket_path)
        if rc != 0:
            raise RuntimeFailure(f"subshell exited with status {rc}")
        return
    if cfg.vm.backend == "libvirt":
        from agx_cli.backends import libvirt as libvirt_mod

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
    emit: bool = True,
) -> str:
    """Take a VM snapshot.

    Multipass snapshots can take a running VM directly. libvirt with
    an active virtiofs mount cannot (vhost-user backend lacks
    ``VHOST_USER_PROTOCOL_F_LOG_SHMFD``), so on that backend we
    prompt for confirmation and run a transactional stop → snapshot
    → start cycle. The VM is briefly down (~30s); any containers
    inside get a clean shutdown via the libvirt ACPI signal +
    restart afterwards.

    ``yes=True`` skips the confirmation prompt. ``emit=False``
    suppresses the final :func:`ui.emit` — used by ``agx upgrade``,
    which already narrates its snapshot phase via ``ui.progress`` and
    must keep stdout to exactly one object of its own.
    """
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "VM snapshots are unavailable on the bare_metal backend (no VM disk to "
            "snapshot). Use `agx export <name>` for data-dir backups, and a host-"
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
                from agx_cli.backends import libvirt as libvirt_mod

                libvirt_mod.wait_for_ssh(cfg.vm.name, wait_cloud_init=False)

    if emit:
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
    emit: bool = True,
) -> None:
    """Revert the VM to a prior snapshot.

    Same downtime trade-off as :func:`take_snapshot`: libvirt with
    an active virtiofs mount can't snapshot-revert a running VM,
    so we prompt for confirmation and stop the VM first. After
    revert the VM is restarted so the user lands on a running VM
    again (matching multipass-restore semantics).

    ``yes=True`` skips the confirmation prompt. ``destructive=True``
    bypasses multipass's safety check (no-op on libvirt). ``emit=False``
    suppresses the final :func:`ui.emit` — used by ``agx rollback``,
    which narrates via ``ui.progress`` and emits its own single object.
    """
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "snapshot restore is unavailable on the bare_metal backend. Use "
            "`agx import <name> <archive>` to restore an `agx export` archive."
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
        result = mp.restore(snapshot_id, destructive=destructive)
        if result.returncode != 0:
            raise RuntimeFailure(f"restore failed: {(result.stderr or 'unknown error').strip()}")
    finally:
        if was_running:
            ui.info(f"restarting {cfg.vm.name!r}...")
            mp.start_vm(cfg.vm.name)
            if cfg.vm.backend == "libvirt":
                from agx_cli.backends import libvirt as libvirt_mod

                libvirt_mod.wait_for_ssh(cfg.vm.name, wait_cloud_init=False)

    if emit:
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
    "sidecars_started",
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

    1. **staged_exports** — ``agx export --all`` writes ``.tar.zst``
       archives into ``<data-dir>/.rebuild-staging/``. In
       ``--data-dir-in-vm`` mode the sidecar compose fragments (with
       their digest pins) are staged there too — the compose file lives
       on the doomed VM disk in that mode. The sidecars' bulk state
       (firecrawl's postgres, model weights) is NOT staged; the user
       must consent to losing it up front.
    2. **vm_destroyed** — ``mp.delete_vm`` removes the VM. Host data-dir
       stays put (in host-mount mode all the agent state is here
       already; in ``--data-dir-in-vm`` only the staged archives are).
    3. **vm_initted** — fresh ``vm init`` with the (possibly overridden)
       sizing. ``cpus``/``memory_gb``/``disk_gb``/``ubuntu_release``
       default to the existing config; pass any to override.
    4. **state_restored** — ``agx import`` each archive back into its
       agent's data dir (plus, in ``--data-dir-in-vm`` mode, the staged
       sidecar fragments back into the freshly seeded compose).
    5. **services_started** — bring every agent's container up via
       ``docker compose up -d``.
    6. **sidecars_started** — re-up the project-scoped sidecar stacks
       (firecrawl, ollama, llama-cpp model services). Their compose
       blocks and digest pins survive on the host data-dir, but the
       containers lived only in the old VM's Docker engine — without
       this they'd sit silently stranded. Best-effort: a sidecar that
       fails to come back warns (with the manual command) and never
       fails the rebuild.

    Journaled — each phase advances the on-disk journal so a crashed
    rebuild can resume by re-running the command. Phases are
    independently idempotent (each checks whether its work is already
    done before redoing it).

    The staging dir is left in place for inspection; you can ``rm -rf
    <data-dir>/.rebuild-staging/`` after confirming everything's back
    up.
    """
    from agx_cli import agents_cmd, compose, compose_runner, export_cmd
    from agx_cli.journal import Journal

    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "vm rebuild doesn't apply on the bare_metal backend (there's no VM to "
            "rebuild). For data restore: `agx import`; for full reset: `agx "
            "vm destroy --yes` then `agx vm init --backend bare_metal`."
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

    # Sidecar service definitions, also captured BEFORE destroy. In
    # --data-dir-in-vm mode the compose file itself lives on the VM disk
    # about to be destroyed, so these fragments (which carry the digest
    # pins) would be lost with it — stage them host-side and restore
    # after init. Their BULK state (firecrawl's postgres data, model
    # weights) is deliberately NOT staged this wave — multi-GB copies
    # through multipass transfer aren't worth it — so the user must
    # explicitly consent to that loss. (.firecrawl-state.json lives on
    # the HOST data-dir in both modes and survives on its own.)
    sidecar_fragments = _sidecar_service_fragments(doc)
    if cfg.paths.data_dir_in_vm and sidecar_fragments:
        lost = ", ".join(sorted(sidecar_fragments))
        if not yes and not ui.confirm(
            f"--data-dir-in-vm: sidecar services ({lost}) keep their state on "
            f"the VM disk, which this rebuild destroys. Permanently lost:\n"
            f"  - firecrawl's postgres data (scrape/job history)\n"
            f"  - downloaded model weights (ollama models, llama-cpp GGUFs)\n"
            f"The service definitions and digest pins are staged and restored, "
            f"and the stacks re-upped after the rebuild — but postgres starts "
            f"empty and every model must be re-pulled.\n"
            f"Proceed?",
            default=False,
        ):
            raise UserError(
                "aborted: rebuild would destroy in-VM sidecar state "
                f"({lost} — firecrawl postgres data, downloaded model "
                "weights). Re-run and confirm (or pass --yes) to accept "
                "the loss."
            )

    staging_dir = cfg.paths.host_data_dir / ".rebuild-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        journal = Journal.find_resumable(
            cfg.paths.host_data_dir, "rebuild", cfg.vm.name
        ) or Journal.begin(cfg.paths.host_data_dir, "rebuild", cfg.vm.name, REBUILD_PHASES)

        # Sub-operations run with emit=False: rebuild owns this command's
        # single stdout object (the summary at the end) and narrates the
        # slow stages on stderr instead.
        if not journal.is_phase_done("staged_exports"):
            stage_path = staging_dir / _SIDECAR_STAGE_FILENAME
            if cfg.paths.data_dir_in_vm and sidecar_fragments:
                ui.progress(
                    "staging sidecar service definitions (fragments + digest "
                    "pins survive; postgres data and model weights do not)..."
                )
                _stage_sidecar_fragments(stage_path, sidecar_fragments)
            else:
                # Drop stale staging from a prior rebuild so it can't be
                # replayed into a run with no (or different) sidecars.
                stage_path.unlink(missing_ok=True)
            for name, _ in agent_specs:
                ui.progress(f"exporting {name!r} to {staging_dir}...")
                export_cmd.export(name, to=staging_dir, config_path=config_path, emit=False)
            journal.advance("staged_exports")

        if not journal.is_phase_done("vm_destroyed"):
            if _vm_exists(cfg.vm.name):
                ui.progress(f"destroying VM {cfg.vm.name!r} (host data-dir stays untouched)...")
                mp.delete_vm(cfg.vm.name, purge=True)
            journal.advance("vm_destroyed")

        if not journal.is_phase_done("vm_initted"):
            # Re-init from scratch with the chosen sizing. init() persists
            # the new values back to config.toml. Backend + GPU passthrough
            # are carried over explicitly: init() defaults to multipass and
            # writes cfg.vm.backend unconditionally, so omitting them would
            # silently migrate a libvirt user to multipass and drop GPUs.
            ui.progress("re-initializing the VM (launch + bootstrap — takes minutes)...")
            init(
                data_dir=cfg.paths.host_data_dir,
                data_dir_in_vm=cfg.paths.data_dir_in_vm,
                cpus=target_cpus,
                memory_gb=target_memory,
                disk_gb=target_disk,
                ubuntu_release=target_release,
                backend=cfg.vm.backend,
                gpu=list(cfg.vm.gpu_passthrough) or None,
                config_path=config_path,
                emit=False,
            )
            journal.advance("vm_initted")

        # Re-load config — init may have changed [vm_runtime] etc.
        cfg = cfg_mod.load(config_path)

        if not journal.is_phase_done("state_restored"):
            if cfg.paths.data_dir_in_vm:
                # The fresh VM's compose was seeded empty by init — put
                # the staged sidecar fragments back so the re-up phase
                # below finds (and pins) the same services as before.
                _restore_staged_sidecar_fragments(cfg, staging_dir)
            for name, _ in agent_specs:
                archives = sorted(staging_dir.glob(f"{name}-*.tar.zst"))
                if not archives:
                    raise RuntimeFailure(
                        f"no export archive found for agent {name!r} in {staging_dir}"
                    )
                # Use the most recent one.
                ui.progress(f"importing {name!r} from {archives[-1].name}...")
                export_cmd.import_archive(name, archives[-1], config_path=config_path, emit=False)
            journal.advance("state_restored")

        if not journal.is_phase_done("services_started"):
            # `docker compose up -d` per service via compose_runner, so a
            # single broken agent doesn't take the rest down.
            for name, agent_type in agent_specs:
                svc = agents_cmd.service_name(name, agent_type)
                ui.progress(f"starting {svc}...")
                compose_runner.up(cfg, svc)
            journal.advance("services_started")

    # Sidecar stacks (firecrawl, in-VM inference) also lived only in the
    # old VM's Docker engine: their compose blocks, digest pins, and env
    # wiring all survive on the host data-dir, but no container crosses
    # to the fresh VM. Without this step everything LOOKS restored (e.g.
    # after a multipass → libvirt migration) while the firecrawl stack
    # sits silently stranded. Runs OUTSIDE the rebuild
    # lock: firecrawl_cmd.up / inference_cmd.up acquire `.agents.lock`
    # themselves, and a second flock on the same file blocks even within
    # one process (flock is per open-file-description).
    if journal.is_phase_done("sidecars_started"):
        sidecars: dict[str, Any] = {"skipped": True}
    else:
        sidecars = _reup_sidecar_stacks(cfg, config_path=config_path)
        if "sidecars_started" in journal.phases:
            # Journals written before this phase existed (a crashed
            # rebuild resumed across a toolkit upgrade) don't declare
            # it — Journal.advance refuses undeclared phases.
            journal.advance("sidecars_started")
    journal.complete()

    lc = sidecars.get("llamacpp") or {"started": [], "failed": []}
    sidecar_human = (
        "(already re-upped by an earlier resumed run)"
        if sidecars.get("skipped")
        else (
            f"firecrawl={sidecars['firecrawl']}, ollama={sidecars['ollama']}, "
            f"llama-cpp models started={len(lc['started'])} failed={len(lc['failed'])}"
        )
    )
    ui.emit(
        {
            "vm": cfg.vm.name,
            "rebuilt": True,
            "cpus": target_cpus,
            "memory_gb": target_memory,
            "disk_gb": target_disk,
            "ubuntu_release": target_release,
            "restored_agents": [n for n, _ in agent_specs],
            "sidecars": sidecars,
            "staging_dir": str(staging_dir),
        },
        human=(
            f"VM {cfg.vm.name!r} rebuilt.\n"
            f"  cpus={target_cpus}, memory={target_memory}G, disk={target_disk}G, "
            f"ubuntu={target_release}\n"
            f"  agents restored: {', '.join(n for n, _ in agent_specs) or '(none)'}\n"
            f"  sidecars: {sidecar_human}\n"
            f"  staging dir: {staging_dir}\n"
            f"  (safe to `rm -rf {staging_dir}` after `agx doctor` is green)"
        ),
    )


def _reup_sidecar_stacks(cfg: AgentsConfig, *, config_path: Path) -> dict[str, Any]:
    """Re-up the project-scoped sidecar stacks after a VM rebuild.

    The rebuilt VM has a fresh Docker engine: the agent containers are
    restored by the phases above, but firecrawl / ollama / llama-cpp
    model services would otherwise stay absent until someone notices
    scrapes failing connection-refused. ``firecrawl_cmd.up`` self-heals
    a lost ``.firecrawl-state.json`` from the compose digest pins, so
    this is safe even after a migration that dropped the state file (no
    unintended upgrade).

    Best-effort by design: each sidecar failure warns with the exact
    manual command and moves on — the restored agents matter more than
    the sidecars, and every recovery path stays available afterwards.

    Returns the summary folded into rebuild's single stdout object:
    ``{"firecrawl": "absent"|"running"|"failed", "ollama": ...,
    "llamacpp": {"started": [...], "failed": [...]}}``.
    """
    from agx_cli import compose, firecrawl_cmd, inference_cmd

    summary: dict[str, Any] = {
        "firecrawl": "absent",
        "ollama": "absent",
        "llamacpp": {"started": [], "failed": []},
    }
    try:
        doc = compose.read_compose_doc(cfg)
        services = list((doc.get("services") or {}).keys())
    except (RuntimeFailure, UserError):
        services = []

    if any(compose.is_firecrawl_service(s) for s in services):
        ui.progress("re-starting the firecrawl stack in the fresh VM...")
        try:
            firecrawl_cmd.up(config_path=config_path, emit=False)
            summary["firecrawl"] = "running"
        except CliError as e:
            summary["firecrawl"] = "failed"
            ui.warn(
                f"firecrawl did not come back up: {e}\n"
                "The agents themselves are restored; bring the stack back "
                "with `agx firecrawl up`."
            )

    # Gate on the flag OR compose presence: the flag can lag reality —
    # `inference up ollama` persists ollama_enabled only after a
    # successful compose up, so a process that died between the start
    # and that final config save leaves a live compose block with the
    # flag still false. Compose presence is equally authoritative, and
    # a successful re-up self-heals the flag.
    if cfg.inference.ollama_enabled or compose.OLLAMA_SERVICE_NAME in services:
        ui.progress("re-starting the ollama inference engine in the fresh VM...")
        try:
            inference_cmd.up("ollama", config_path=config_path, emit=False)
            summary["ollama"] = "running"
        except CliError as e:
            summary["ollama"] = "failed"
            ui.warn(
                f"ollama did not come back up: {e}\nBring it back with `agx inference up ollama`."
            )

    if any(s.startswith(compose.LLAMACPP_SERVICE_PREFIX) for s in services):
        ui.progress("re-starting llama-cpp model services in the fresh VM...")
        # Warns per failed service internally (with the `agx
        # inference load` recovery command); never raises for a
        # service that won't start.
        summary["llamacpp"] = inference_cmd.reup_llamacpp_services(cfg)

    return summary


# Host-side staging file for sidecar compose fragments during a
# --data-dir-in-vm rebuild (the compose file itself lives on the VM disk
# in that mode and dies with it).
_SIDECAR_STAGE_FILENAME = "sidecar-services.json"


def _sidecar_service_fragments(doc: Any) -> dict[str, Any]:
    """Extract the sidecar service blocks (firecrawl-*, ollama,
    llama-cpp-*) from a compose doc, keyed by service name.
    """
    from agx_cli import compose

    out: dict[str, Any] = {}
    for name, svc in (doc.get("services") or {}).items():
        if (
            compose.is_firecrawl_service(name)
            or name == compose.OLLAMA_SERVICE_NAME
            or name.startswith(compose.LLAMACPP_SERVICE_PREFIX)
        ):
            out[name] = svc
    return out


def _stage_sidecar_fragments(stage_path: Path, fragments: dict[str, Any]) -> None:
    """Write the sidecar service blocks to the host-side staging file so
    a ``--data-dir-in-vm`` rebuild can restore them after the VM (and
    with it the compose file) is destroyed and re-created.
    """
    stage_path.write_text(json.dumps(fragments, indent=2, default=str), encoding="utf-8")


def _restore_staged_sidecar_fragments(cfg: AgentsConfig, staging_dir: Path) -> None:
    """Re-insert staged sidecar service blocks into the (freshly seeded,
    empty) compose file after a ``--data-dir-in-vm`` rebuild's init.

    No-op when nothing was staged. Each fragment's image ref is
    allowlist-validated before it re-enters compose (rule #5 — the
    staging file sits on the host and could have been doctored while the
    rebuild was interrupted); invalid entries are warned about and
    skipped, leaving the re-up phase to surface the now-partial stack.
    Idempotent on resume: fragments already present are left alone.
    """
    from agx_cli import compose

    stage_path = staging_dir / _SIDECAR_STAGE_FILENAME
    if not stage_path.exists():
        return
    try:
        fragments: dict[str, Any] = json.loads(stage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        ui.warn(
            f"staged sidecar service definitions unreadable ({e}); skipping "
            "their restore — re-add the sidecars manually (`agx firecrawl "
            "up`, `agx inference up ollama`, `agx inference load ...`)."
        )
        return
    doc = compose.read_compose_doc(cfg)
    services = doc.setdefault("services", {})
    restored: list[str] = []
    for name, fragment in fragments.items():
        image = fragment.get("image") if isinstance(fragment, dict) else None
        if not isinstance(image, str) or not image:
            ui.warn(f"skipping staged sidecar service {name!r}: no image ref")
            continue
        try:
            compose.validate_image_ref(image)
        except UserError as e:
            ui.warn(f"skipping staged sidecar service {name!r}: {e}")
            continue
        if name not in services:
            services[name] = fragment
            restored.append(name)
    if restored:
        compose.write_compose_atomically(cfg, doc)
        ui.progress(f"restored sidecar service definitions: {', '.join(restored)}")
        ui.warn(
            "sidecar service definitions were restored after the "
            "data-dir-in-VM rebuild, but their in-VM state did not survive: "
            "firecrawl's postgres starts empty and model weights must be "
            "re-pulled (`agx model pull ...`)."
        )


# ---------- destroy ----------


def destroy(*, yes: bool = False, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    if cfg.vm.backend == "bare_metal":
        # On bare-metal there's no VM to delete; instead, bring the
        # compose stack down. Host data-dir is untouched (matches the
        # multipass/libvirt invariant).
        from agx_cli import compose_runner

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


def _warn_bare_metal_log_rotation() -> None:
    """The VM bootstrap writes container-log rotation into the daemon's
    ``daemon.json``; the verify-only bare-metal bootstrap must not touch
    the user's daemon config, so it only points out the gap (doctor's
    ``container_logs`` check keeps flagging it afterwards).
    """
    from agx_cli import ops

    conf_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    daemon_json = conf_dir / "docker" / "daemon.json"
    try:
        text: str | None = daemon_json.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except OSError as e:
        ui.warn(f"could not read {daemon_json} to check container-log rotation: {e}")
        return
    gap = ops.daemon_log_rotation_gap(text)
    if gap:
        ui.warn(
            f"container-log rotation: {gap}; one chatty container can fill the host "
            f"root fs. Write {ops.DAEMON_JSON_LOG_ROTATION} to {daemon_json}, then "
            "`systemctl --user restart docker` BEFORE creating agents."
        )


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
    bootstrap with "no route to host" — observed in testing), only
    stop+start on multipass.

    Mounts persist across reboots, so this only triggers when the
    mount is genuinely absent (fresh ``vm init`` or after an external
    umount).
    """
    if _has_mount(cfg):
        return

    requires_stop = cfg.vm.backend == "multipass"
    was_running = _vm_running(cfg.vm.name)
    if was_running and requires_stop:
        ui.progress(
            f"stopping {cfg.vm.name!r} to add the data-dir mount "
            f"(native mounts need the VM stopped)..."
        )
        mp.stop_vm(cfg.vm.name)
    ui.progress(f"mounting {cfg.paths.host_data_dir} -> {cfg.paths.vm_agents_root} (in VM)...")
    mp.mount(
        cfg.paths.host_data_dir,
        cfg.vm.name,
        str(cfg.paths.vm_agents_root),
        host_uid=cfg.host.uid,
        host_gid=cfg.host.gid,
    )
    if was_running and requires_stop:
        # multipass-only branch (requires_stop): mp.start_vm blocks until
        # the VM is reachable again. libvirt never stops for a mount
        # (virtiofs attaches --live --config), so no SSH re-wait is needed.
        ui.progress(f"restarting {cfg.vm.name!r}...")
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
    ui.progress("seeding data-dir (compose file, .env, skeleton dirs)...")
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

    Also re-pushes the host-side ``docker-compose.override.yml`` when one
    exists: it survives a ``vm rebuild`` on the host (and keeps the extra
    ``-f`` armed via ``compose.override_file_args``) while its in-VM
    mirror dies with the old VM disk — without this, every compose verb
    in the fresh VM failed on the missing file.
    """
    from agx_cli import compose

    root = cfg.paths.vm_agents_root
    compose_content = templates.initial_compose_content()
    env_content = templates.initial_env_content(
        cfg.vm_runtime.ubuntu_uid, cfg.vm_runtime.ubuntu_gid
    )
    # The one allowed heredoc through the backend exec: first-time
    # seeding of static template content, guarded by `[ -f ] ||` so it
    # never overwrites a live file. Every later compose/.env write goes
    # through the staged-transfer + validate + atomic-rename path.
    script = (
        f"set -eu\n"
        f"mkdir -p {root}/.codex {root}/backups {root}/.upgrades\n"
        f"[ -f {root}/docker-compose.yml ] || cat > {root}/docker-compose.yml <<'__AGENTS_EOF__'\n"
        f"{compose_content}__AGENTS_EOF__\n"
        f"[ -f {root}/.env ] || cat > {root}/.env <<'__AGENTS_EOF__'\n"
        f"{env_content}__AGENTS_EOF__\n"
    )
    mp.exec_bash(cfg.vm.name, script)
    compose.sync_override_to_vm(cfg)


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
    """Latest upstream IMAGE tag (``outdated_cmd.fetch_latest_tag``);
    ``None`` when GitHub is unreachable or the release has no image under
    that tag (the caller then falls back). No exceptions out."""
    from agx_cli import outdated_cmd

    latest = outdated_cmd.fetch_latest_tag(repo_key)
    if latest and outdated_cmd.image_tag_published(repo_key, latest) is False:
        ui.warn(f"latest {repo_key} release {latest!r} has no published image tag; using fallback")
        return None
    return latest


def _resolve_gpu_list(requested: list[str]) -> list[str]:
    """Map the ``--gpu`` CLI input to a concrete list of PCI IDs.

    ``["auto"]`` triggers vendor-aware detection (NVIDIA / AMD / Intel)
    via :func:`agx_cli.backends.libvirt.detect_gpus`. Anything else
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
        from agx_cli.backends import libvirt as libvirt_mod

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
