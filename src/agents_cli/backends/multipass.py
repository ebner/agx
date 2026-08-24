"""Multipass implementation of the VmBackend interface.

Historically these functions lived in ``agents_cli.mp``. They moved
here when libvirt joined as an alternative backend; ``mp.py`` is now
a thin dispatcher that delegates to whichever backend module
``cfg.vm.backend`` selects (default: multipass).

Two design rules carried over from the original mp.py:

1. Argv-form only. No ``shell=True``. Quoting bugs are where security
   issues hide.
2. ``parse_bootstrap_ok`` lives in ``backends/__init__.py`` as a
   shared value-type helper — the script is identical regardless of
   hypervisor and runs in-VM either way.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from agents_cli import ui
from agents_cli.backends import MpResult
from agents_cli.errors import RuntimeFailure

MULTIPASS_BIN: Final = "multipass"


def run(
    argv: Sequence[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> MpResult:
    """Run an arbitrary subprocess. argv-form only.

    Decoding is forgiving (`errors="replace"`) so non-UTF-8 bytes don't crash
    parsers downstream — they show up as `\\ufffd` and naturally fail any
    structured match (which is exactly what we want for BOOTSTRAP_OK).
    """
    ui.info(f"$ {' '.join(argv)}")
    try:
        proc = subprocess.run(
            list(argv),
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeFailure(f"command not found: {argv[0]} ({e})") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeFailure(f"{' '.join(argv)} timed out after {timeout}s") from e

    out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    result = MpResult(stdout=out, stderr=err, returncode=proc.returncode)

    if check and proc.returncode != 0:
        raise RuntimeFailure(
            f"command failed (exit {proc.returncode}): {' '.join(argv)}\nstderr: {err.strip()}"
        )
    return result


def mp(
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> MpResult:
    return run([MULTIPASS_BIN, *args], check=check, input_bytes=input_bytes, timeout=timeout)


def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
    """Run a command in the VM, argv-form. Use `exec_bash` for scripts."""
    return mp("exec", vm, "--", *cmd, check=check)


def exec_bash(
    vm: str, script: str, *, check: bool = True, timeout: float | None = None
) -> MpResult:
    """Pipe a bash script into the VM. Avoids quoting hell."""
    return mp(
        "exec", vm, "--", "bash", "-s", check=check, input_bytes=script.encode(), timeout=timeout
    )


def exec_in_vm_with_stdin(vm: str, *cmd: str, input_bytes: bytes, check: bool = True) -> MpResult:
    """Run a VM-side command with the given bytes piped to its stdin.

    Used for "write a file from inside a container" — we send the file
    content via ``docker compose exec -T <svc> sh -c 'cat > <path>'``.
    Going through the container's user namespace is the only way to
    write to a path whose underlying directory is owned by a
    rootless-Docker subuid (the common case in ``--data-dir-in-vm``
    mode, where multipass-SFTP runs as ``ubuntu`` and can't touch
    subuid-owned dirs).
    """
    return mp("exec", vm, "--", *cmd, check=check, input_bytes=input_bytes)


def exec_in_vm_interactive(vm: str, *cmd: str) -> int:
    """Run a command in the VM with stdin/stdout/stderr inherited from
    the parent process — necessary for interactive wizards (``hermes
    setup``, ``openclaw onboard``) so the user can actually see prompts
    and respond.

    Unlike ``exec_in_vm`` (which uses ``subprocess.run`` with
    ``capture_output=True`` and would swallow the wizard's prompts),
    this returns the raw exit code and produces no ``MpResult``. The
    Python process stays alive after the wizard finishes — caller can
    still emit success messages or advance a phase journal.
    """
    argv = [MULTIPASS_BIN, "exec", vm, "--", *cmd]
    ui.info(f"$ {' '.join(argv)}")
    try:
        result = subprocess.run(argv, check=False)
    except FileNotFoundError as e:
        raise RuntimeFailure(f"command not found: {argv[0]} ({e})") from e
    return result.returncode


def transfer_to_vm(host_path: Path, vm: str, vm_path: str) -> MpResult:
    return mp("transfer", str(host_path), f"{vm}:{vm_path}")


def transfer_from_vm(vm: str, vm_path: str, host_path: Path) -> MpResult:
    return mp("transfer", f"{vm}:{vm_path}", str(host_path))


def mount(
    host_path: Path,
    vm: str,
    vm_path: str,
    *,
    host_uid: int,
    host_gid: int,
    vm_uid: int = 1000,
    vm_gid: int = 1000,
    mount_type: str = "native",
) -> MpResult:
    """Mount a host directory into the VM.

    ``mount_type`` defaults to ``"native"`` (hypervisor-backed: 9p on
    Linux/QEMU, virtiofs on macOS/HyperKit) rather than multipass's
    historical default of ``"classic"`` (fuse.sshfs). Reason: rootless
    Docker cannot bind-mount paths from a fuse.sshfs filesystem into a
    container — the kernel returns ``operation not supported`` and every
    Hermes/OpenClaw container fails on first start. 9p/virtiofs is fine.

    Multipass requires the VM to be stopped to add a native mount;
    classic mounts can be added on a running VM. The orchestration
    (stop, mount, start) is done by ``vm._ensure_mount``.
    """
    return mp(
        "mount",
        "--type",
        mount_type,
        "-u",
        f"{host_uid}:{vm_uid}",
        "-g",
        f"{host_gid}:{vm_gid}",
        str(host_path),
        f"{vm}:{vm_path}",
    )


def umount(vm: str, vm_path: str) -> MpResult:
    return mp("umount", f"{vm}:{vm_path}")


def launch(vm: str, *, image: str, cpus: int, memory_gb: int, disk_gb: int) -> MpResult:
    return mp(
        "launch",
        "--name",
        vm,
        "--cpus",
        str(cpus),
        "--memory",
        f"{memory_gb}G",
        "--disk",
        f"{disk_gb}G",
        image,
        timeout=900.0,
    )


def info_vm(vm: str, *, fmt: str = "json") -> MpResult:
    return mp("info", vm, "--format", fmt, check=False)


def list_vms(*, fmt: str = "json") -> MpResult:
    return mp("list", "--format", fmt)


def find(*, fmt: str = "json", show_unsupported: bool = False) -> MpResult:
    """Multipass-specific: list available Ubuntu cloud images via
    ``multipass find``. libvirt has no equivalent — it pulls images
    from Canonical's cloud-images mirror directly when needed.
    """
    args = ["find", "--format", fmt]
    if show_unsupported:
        args.append("--show-unsupported")
    return mp(*args)


def start_vm(vm: str) -> MpResult:
    return mp("start", vm)


def stop_vm(vm: str) -> MpResult:
    return mp("stop", vm)


def set_autostart(vm: str, enabled: bool) -> MpResult:
    """No-op on Multipass: snap-confined multipassd auto-resumes all
    defined VMs on host boot by default, so explicit autostart wiring
    isn't needed. Returns a synthetic ok result so callers don't need
    to branch.
    """
    return MpResult(stdout="", stderr="", returncode=0)


def set_config(key: str, value: str | int) -> MpResult:
    """Multipass-specific: ``multipass set <key>=<value>``. Used by
    ``agents vm resize`` to reallocate per-VM cpus/memory/disk on a
    stopped VM. libvirt achieves the same via ``virsh setmaxmem`` /
    ``virsh setvcpus`` / ``qemu-img resize``.
    """
    return mp("set", f"{key}={value}")


def delete_vm(vm: str, *, purge: bool = False) -> MpResult:
    args = ["delete", vm]
    if purge:
        args.append("--purge")
    return mp(*args)


def snapshot(vm: str, name: str, *, comment: str | None = None) -> MpResult:
    args = ["snapshot", vm, "--name", name]
    if comment:
        args += ["--comment", comment]
    return mp(*args)


def restore(snapshot_id: str, *, destructive: bool = False) -> MpResult:
    """``snapshot_id`` is the multipass form ``<vm>.<name>``."""
    args = ["restore", snapshot_id]
    if destructive:
        args.append("--destructive")
    return mp(*args)


def delete_snapshot(vm: str, name: str) -> MpResult:
    """Permanently delete one snapshot (``--purge`` skips the recycle bin).

    The dotted ``<vm>.<name>`` form is load-bearing: ``multipass delete
    --purge <vm>`` with no snapshot part would destroy the whole VM, so
    an empty name is refused here rather than passed through.
    """
    if not name:
        return MpResult(stdout="", stderr="refusing to delete: empty snapshot name", returncode=1)
    # check=False: callers (prune) treat deletion as best-effort and
    # inspect the MpResult — a raise here would abort the rest of a
    # cleanup pass on the first undeletable snapshot.
    return mp("delete", "--purge", f"{vm}.{name}", check=False)
