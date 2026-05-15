"""libvirt / KVM backend (Phase A stub — see GH #2 for implementation roadmap).

This module satisfies the :class:`agents_cli.backends.VmBackend`
Protocol interface so the dispatcher in ``mp.py`` can route to it
when ``cfg.vm.backend == "libvirt"``. Every method currently raises
:class:`NotImplementedError` with a hint pointing at the issue.

Phase B will fill these in:

- ``launch`` — virt-install with cloud-init seed ISO, Ubuntu cloud
  image cached under ``<data-dir>/.images/`` (per ``platform.machine()``
  for amd64 vs arm64).
- ``exec_*`` — ssh into the VM (key-based auth via the cloud-init
  user-data injected at launch).
- ``transfer_*`` — scp over the same ssh connection.
- ``mount`` — virtiofs (preferred) or 9p, attached via the domain XML.
- ``snapshot`` / ``restore`` — ``virsh snapshot-create-as`` /
  ``snapshot-revert``.
- GPU passthrough — vfio-pci device detection (NVIDIA / AMD / Intel),
  IOMMU-group validation, hostdev XML attach.

Refuses to import on non-Linux hosts — :func:`_require_linux_host`
runs at module import time so ``cfg.vm.backend == "libvirt"`` fails
loud at ``cli.main`` startup rather than during the first VM
operation.
"""

from __future__ import annotations

import platform
from pathlib import Path

from agents_cli.backends import MpResult
from agents_cli.errors import RuntimeFailure, UserError


def _require_linux_host() -> None:
    """libvirt is Linux-only (per GH #2 / runbook). macOS users on
    Apple Silicon should stick with Multipass; Windows users get
    Multipass via Hyper-V.

    Raised at import time (via the module-level call below) AND at
    every method entry as a belt-and-suspenders check — module
    re-imports across test scopes occasionally bypass module-level
    raise. The duplicate is cheap.
    """
    if platform.system() != "Linux":
        raise UserError(
            "libvirt backend is Linux-only. Use --backend multipass on "
            f"{platform.system()}; see docs/runbook.md for the cross-platform matrix."
        )


_require_linux_host()


def _stub(name: str) -> RuntimeFailure:
    return RuntimeFailure(
        f"libvirt backend {name!r} is not implemented yet — see GitHub issue #2. "
        f"Use --backend multipass for now."
    )


# ---------- exec ----------


def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
    raise _stub("exec_in_vm")


def exec_bash(
    vm: str, script: str, *, check: bool = True, timeout: float | None = None
) -> MpResult:
    raise _stub("exec_bash")


def exec_in_vm_with_stdin(vm: str, *cmd: str, input_bytes: bytes, check: bool = True) -> MpResult:
    raise _stub("exec_in_vm_with_stdin")


def exec_in_vm_interactive(vm: str, *cmd: str) -> int:
    raise _stub("exec_in_vm_interactive")


# ---------- file transfer ----------


def transfer_to_vm(host_path: Path, vm: str, vm_path: str) -> MpResult:
    raise _stub("transfer_to_vm")


def transfer_from_vm(vm: str, vm_path: str, host_path: Path) -> MpResult:
    raise _stub("transfer_from_vm")


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
    raise _stub("mount")


def umount(vm: str, vm_path: str) -> MpResult:
    raise _stub("umount")


# ---------- lifecycle ----------


def launch(vm: str, *, image: str, cpus: int, memory_gb: int, disk_gb: int) -> MpResult:
    raise _stub("launch")


def info_vm(vm: str, *, fmt: str = "json") -> MpResult:
    raise _stub("info_vm")


def list_vms(*, fmt: str = "json") -> MpResult:
    raise _stub("list_vms")


def find(*, fmt: str = "json", show_unsupported: bool = False) -> MpResult:
    """libvirt has no equivalent of ``multipass find`` — Ubuntu cloud
    images live at https://cloud-images.ubuntu.com/<release>/current/.
    The libvirt backend's ``launch`` will pull them directly when
    implemented; until then this just refuses.
    """
    raise _stub("find")


def start_vm(vm: str) -> MpResult:
    raise _stub("start_vm")


def stop_vm(vm: str) -> MpResult:
    raise _stub("stop_vm")


def set_config(key: str, value: str | int) -> MpResult:
    """No direct equivalent — libvirt resizing goes through
    ``virsh setmaxmem`` / ``setvcpus`` / ``qemu-img resize``. ``agents
    vm resize`` will dispatch to a libvirt-specific helper in Phase B.
    """
    raise _stub("set_config")


def delete_vm(vm: str, *, purge: bool = False) -> MpResult:
    raise _stub("delete_vm")


def snapshot(vm: str, name: str, *, comment: str | None = None) -> MpResult:
    raise _stub("snapshot")


def restore(snapshot_id: str, *, destructive: bool = False) -> MpResult:
    raise _stub("restore")
