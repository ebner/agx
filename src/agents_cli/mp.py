"""VM-backend dispatcher (historically the Multipass primitives module).

Every host↔VM call in the toolkit flows through here. The actual
implementation lives in :mod:`agents_cli.backends.multipass` (default)
or :mod:`agents_cli.backends.libvirt`; this module just delegates to
whichever one is active.

The dispatcher uses a module-level handle (:data:`_backend`) initialised
lazily to multipass. ``cli.main`` calls :func:`set_backend` early after
config load so day-2 commands see the right backend. Tests don't need
to set anything — the multipass default works unchanged, and tests
that patch ``agents_cli.mp.exec_in_vm`` replace the dispatcher
function itself (so the patch wins regardless of which backend would
otherwise have been active).

Value types (:class:`MpResult`, :class:`BootstrapStatus`,
:func:`parse_bootstrap_ok`) live in :mod:`agents_cli.backends` and
are re-exported here for backward compatibility with the many call
sites that do ``from agents_cli.mp import MpResult``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

# Re-export value types so the historical
# ``from agents_cli.mp import MpResult`` style keeps working.
from agents_cli.backends import (
    BootstrapStatus,
    MpResult,
    VmBackend,
    parse_bootstrap_ok,
)

__all__ = [
    "MULTIPASS_BIN",
    "BootstrapStatus",
    "MpResult",
    "delete_vm",
    "exec_bash",
    "exec_in_vm",
    "exec_in_vm_interactive",
    "exec_in_vm_with_stdin",
    "find",
    "info_vm",
    "launch",
    "list_vms",
    "mount",
    "mp",
    "parse_bootstrap_ok",
    "restore",
    "run",
    "set_backend",
    "set_config",
    "snapshot",
    "start_vm",
    "stop_vm",
    "transfer_from_vm",
    "transfer_to_vm",
    "umount",
]

# Re-export the low-level Multipass subprocess helpers so legacy
# imports (``from agents_cli.mp import mp, run``) keep working. These
# are Multipass-specific by design — they invoke the ``multipass``
# binary directly — and don't go through the backend dispatcher.
# Tests that exercise the Multipass implementation (``test_mp.py``)
# call them directly; the libvirt backend doesn't have an equivalent
# (its low-level helper is in ``backends/libvirt.py``).
from agents_cli.backends.multipass import (
    MULTIPASS_BIN,
    mp,
    run,
)

_backend: Any = None


def _b() -> VmBackend:
    """Return the active backend module. Lazily defaults to multipass
    so tests and pre-init code paths work without explicit setup.
    """
    global _backend
    if _backend is None:
        from agents_cli.backends import multipass

        _backend = multipass
    return cast(VmBackend, _backend)


def set_backend(backend_module: Any) -> None:
    """Swap in a different backend. Called by ``cli.main`` after config
    load to honour ``cfg.vm.backend``. Tests don't need to call this.
    """
    global _backend
    _backend = backend_module


# ---------- exec ----------


def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
    return _b().exec_in_vm(vm, *cmd, check=check)


def exec_bash(
    vm: str, script: str, *, check: bool = True, timeout: float | None = None
) -> MpResult:
    return _b().exec_bash(vm, script, check=check, timeout=timeout)


def exec_in_vm_with_stdin(vm: str, *cmd: str, input_bytes: bytes, check: bool = True) -> MpResult:
    return _b().exec_in_vm_with_stdin(vm, *cmd, input_bytes=input_bytes, check=check)


def exec_in_vm_interactive(vm: str, *cmd: str) -> int:
    return int(_b().exec_in_vm_interactive(vm, *cmd))


# ---------- file transfer ----------


def transfer_to_vm(host_path: Path, vm: str, vm_path: str) -> MpResult:
    return _b().transfer_to_vm(host_path, vm, vm_path)


def transfer_from_vm(vm: str, vm_path: str, host_path: Path) -> MpResult:
    return _b().transfer_from_vm(vm, vm_path, host_path)


# ---------- mounts ----------


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
    return _b().mount(
        host_path,
        vm,
        vm_path,
        host_uid=host_uid,
        host_gid=host_gid,
        vm_uid=vm_uid,
        vm_gid=vm_gid,
        mount_type=mount_type,
    )


def umount(vm: str, vm_path: str) -> MpResult:
    return _b().umount(vm, vm_path)


# ---------- lifecycle ----------


def launch(vm: str, *, image: str, cpus: int, memory_gb: int, disk_gb: int) -> MpResult:
    return _b().launch(vm, image=image, cpus=cpus, memory_gb=memory_gb, disk_gb=disk_gb)


def info_vm(vm: str, *, fmt: str = "json") -> MpResult:
    return _b().info_vm(vm, fmt=fmt)


def list_vms(*, fmt: str = "json") -> MpResult:
    return _b().list_vms(fmt=fmt)


def find(*, fmt: str = "json", show_unsupported: bool = False) -> MpResult:
    """Multipass-specific: list available Ubuntu cloud images. libvirt
    backend raises ``NotImplementedError`` — use ``cloud-images.ubuntu.com``
    references directly there.
    """
    return _b().find(fmt=fmt, show_unsupported=show_unsupported)


def start_vm(vm: str) -> MpResult:
    return _b().start_vm(vm)


def stop_vm(vm: str) -> MpResult:
    return _b().stop_vm(vm)


def set_config(key: str, value: str | int) -> MpResult:
    """Multipass-specific knob ``multipass set <key>=<value>``. libvirt
    backend translates the equivalent (``virsh setmaxmem`` /
    ``setvcpus`` / ``qemu-img resize``) internally when called from
    ``agents vm resize``.
    """
    return _b().set_config(key, value)


def delete_vm(vm: str, *, purge: bool = False) -> MpResult:
    return _b().delete_vm(vm, purge=purge)


def snapshot(vm: str, name: str, *, comment: str | None = None) -> MpResult:
    return _b().snapshot(vm, name, comment=comment)


def restore(snapshot_id: str, *, destructive: bool = False) -> MpResult:
    return _b().restore(snapshot_id, destructive=destructive)
