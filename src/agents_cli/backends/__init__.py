"""VM backend abstraction.

The toolkit's host↔VM primitives historically lived in ``mp.py`` and
assumed Multipass. This package extracts that surface into a backend-
agnostic interface so libvirt (see GH issue #2 — GPU passthrough) can
slot in without touching the dozens of callers that go through
``mp.exec_in_vm(...)`` and friends.

Architecture:

- ``backends/multipass.py`` — the historical Multipass implementation.
- ``backends/libvirt.py``   — KVM via virsh + virt-install + ssh; the
  motivation for this whole abstraction (GPU passthrough that
  Multipass doesn't support).
- ``mp.py`` — thin dispatcher. Defaults to multipass; ``set_backend``
  swaps in libvirt when ``cfg.vm.backend == "libvirt"``. Tests work
  unchanged because patches on ``mp.exec_in_vm`` replace the
  dispatcher at the call site.

Value types (``MpResult``, ``BootstrapStatus``) are shared — both
backends return the same shape. ``BOOTSTRAP_OK`` parsing is also
shared because the in-VM bootstrap script is identical regardless of
hypervisor (the script knows nothing about how it got there).

The Protocol defined below is for documentation + type checking;
backend modules are duck-typed module objects rather than class
instances, mirroring the existing ``mp.py`` shape (free functions,
not methods). The dispatcher in ``mp.py`` doesn't enforce the
Protocol at runtime — that's the type checker's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from agents_cli.errors import RuntimeFailure

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
    """Result of a VM-targeted shell invocation. Named for historical
    reasons (Multipass-prefixed); the shape is universal across
    backends.
    """

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


def parse_bootstrap_ok(output: str) -> BootstrapStatus:
    """Find the last ``BOOTSTRAP_OK`` line in ``output`` and parse its fields.

    Same parser regardless of how the bootstrap script was invoked
    (multipass exec, virsh console, libvirt-guest-agent, etc.). The
    parser is permissive about extra/unknown fields (forward compat)
    but :meth:`BootstrapStatus.is_ok` is strict about required ones.
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


class VmBackend(Protocol):
    """Universal VM lifecycle + exec interface.

    Each method's docstring lives on the implementing module's function
    (this Protocol is mostly here for documentation of the contract).
    Backend modules expose these as free functions, not methods, so
    callers do ``backend.exec_in_vm(...)``  — mirrors the existing
    ``mp.exec_in_vm(...)`` shape and means dispatch is a one-liner.
    """

    def exec_in_vm(self, vm: str, *cmd: str, check: bool = True) -> MpResult: ...

    def exec_bash(
        self, vm: str, script: str, *, check: bool = True, timeout: float | None = None
    ) -> MpResult: ...

    def exec_in_vm_with_stdin(
        self, vm: str, *cmd: str, input_bytes: bytes, check: bool = True
    ) -> MpResult: ...

    def exec_in_vm_interactive(self, vm: str, *cmd: str) -> int: ...

    def transfer_to_vm(self, host_path: Path, vm: str, vm_path: str) -> MpResult: ...

    def transfer_from_vm(self, vm: str, vm_path: str, host_path: Path) -> MpResult: ...

    def mount(
        self,
        host_path: Path,
        vm: str,
        vm_path: str,
        *,
        host_uid: int,
        host_gid: int,
        vm_uid: int = 1000,
        vm_gid: int = 1000,
        mount_type: str = "native",
    ) -> MpResult: ...

    def umount(self, vm: str, vm_path: str) -> MpResult: ...

    def launch(
        self, vm: str, *, image: str, cpus: int, memory_gb: int, disk_gb: int
    ) -> MpResult: ...

    def info_vm(self, vm: str, *, fmt: str = "json") -> MpResult: ...

    def list_vms(self, *, fmt: str = "json") -> MpResult: ...

    def find(self, *, fmt: str = "json", show_unsupported: bool = False) -> MpResult: ...

    def start_vm(self, vm: str) -> MpResult: ...

    def stop_vm(self, vm: str) -> MpResult: ...

    def set_autostart(self, vm: str, enabled: bool) -> MpResult: ...

    def set_config(self, key: str, value: str | int) -> MpResult: ...

    def delete_vm(self, vm: str, *, purge: bool = False) -> MpResult: ...

    def snapshot(self, vm: str, name: str, *, comment: str | None = None) -> MpResult: ...

    def restore(self, snapshot_id: str, *, destructive: bool = False) -> MpResult: ...


def get_backend_module(backend_name: str) -> object:
    """Resolve a backend name to its module object.

    Used by ``mp.py``'s dispatcher to set up the active backend after
    config load. Backend modules are duck-typed (free functions); we
    return the module itself rather than a class instance.

    Raises :class:`UserError` for unknown backend names.
    """
    from agents_cli.errors import UserError

    if backend_name == "multipass":
        from agents_cli.backends import multipass

        return multipass
    if backend_name == "libvirt":
        from agents_cli.backends import libvirt

        return libvirt
    raise UserError(
        f"unknown vm backend {backend_name!r}; supported: multipass, libvirt (Linux only)"
    )
