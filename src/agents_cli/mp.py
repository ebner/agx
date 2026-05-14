"""Multipass primitives.

All host↔VM interaction flows through this module. The host never speaks
to the in-VM Docker daemon directly — it shells out to ``multipass`` and
runs commands inside the VM.

Two design rules:

1. Argv-form only. No ``shell=True``. Quoting bugs are where security
   issues hide.
2. The ``BOOTSTRAP_OK`` parser is forward-compatible (extra fields are
   accepted) and strict on required fields, so a partial bootstrap is
   caught before the toolkit tries to use the VM.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from agents_cli import ui
from agents_cli.errors import RuntimeFailure

MULTIPASS_BIN: Final = "multipass"

REQUIRED_OK_FIELDS: Final = frozenset(
    {
        "uidmap",
        "dbus",
        "lingering",
        "docker_host",
        "daemon",
        "user_socket_active",
        "net",
    }
)
REQUIRED_VALUE_FIELDS: Final = frozenset({"ubuntu_uid", "ubuntu_gid", "socket_path"})


@dataclass(frozen=True)
class MpResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class BootstrapStatus:
    fields: dict[str, str] = field(default_factory=dict)

    def is_ok(self) -> bool:
        for k in REQUIRED_OK_FIELDS:
            if self.fields.get(k) != "ok":
                return False
        return all(k in self.fields for k in REQUIRED_VALUE_FIELDS)

    def fail_reasons(self) -> list[str]:
        reasons = []
        for k in REQUIRED_OK_FIELDS:
            v = self.fields.get(k)
            if v == "fail":
                reasons.append(f"{k}=fail")
            elif v is None:
                reasons.append(f"{k} missing")
        for k in REQUIRED_VALUE_FIELDS:
            if k not in self.fields:
                reasons.append(f"{k} missing")
        return reasons

    def socket_path(self) -> str:
        return self.fields["socket_path"]

    def ubuntu_uid(self) -> int:
        return int(self.fields["ubuntu_uid"])

    def ubuntu_gid(self) -> int:
        return int(self.fields["ubuntu_gid"])


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
    args = ["find", "--format", fmt]
    if show_unsupported:
        args.append("--show-unsupported")
    return mp(*args)


def start_vm(vm: str) -> MpResult:
    return mp("start", vm)


def stop_vm(vm: str) -> MpResult:
    return mp("stop", vm)


def set_config(key: str, value: str | int) -> MpResult:
    """``multipass set <key>=<value>``. Used by ``agents vm resize`` to
    reallocate per-VM cpus/memory/disk on a stopped VM.
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


def parse_bootstrap_ok(output: str) -> BootstrapStatus:
    """Find the last ``BOOTSTRAP_OK`` line in ``output`` and parse its fields.

    Raises ``RuntimeFailure`` if no such line is present. The parser is
    permissive about extra/unknown fields (forward compat) but
    ``BootstrapStatus.is_ok`` is strict about the required ones.
    """
    for raw_line in reversed(output.splitlines()):
        line = raw_line.strip()
        if not line.startswith("BOOTSTRAP_OK"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            return BootstrapStatus(fields={})
        fields: dict[str, str] = {}
        for tok in parts[1].split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                fields[k] = v
        return BootstrapStatus(fields=fields)
    raise RuntimeFailure("BOOTSTRAP_OK line not found in bootstrap script output")
