"""VM backend primitives (Incus).

All host↔VM interaction flows through this module. The host never speaks
to the in-VM Docker daemon directly — it shells out to ``incus`` and
runs commands inside the VM.

Historical note: was previously implemented against ``multipass``. We
migrated to Incus to get **virtiofs** for the host data-dir share —
multipass's default fuse.sshfs mount can't be bind-mounted into a
rootless Docker container (kernel returns ``operation not supported``),
and its 9p ``--type native`` mount doesn't fully support the file ops
SQLite needs (Hermes' session store falls back to JSONL). Incus +
virtiofsd solves both. The module name is kept as ``mp`` for minimum
churn across the rest of the codebase — semantics are now Incus-backed.

Two design rules:

1. Argv-form only. No ``shell=True``. Quoting bugs are where security
   issues hide.
2. The ``BOOTSTRAP_OK`` parser is forward-compatible (extra fields are
   accepted) and strict on required fields, so a partial bootstrap is
   caught before the toolkit tries to use the VM.

The JSON returned by ``info_vm`` is normalised to a multipass-flavoured
shape (``{"info": {"<vm>": {"state": ..., "mounts": {...},
"snapshots": {...}, "ipv4": [...]}}}``) so the parsers in ``vm.py``,
``upgrades.py``, and ``ops.py`` did not need to change at migration time.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from agents_cli import ui
from agents_cli.errors import RuntimeFailure

INCUS_BIN: Final = "incus"

# Standard cloud-image user. Incus's ``images:ubuntu/24.04`` ships with
# this account preconfigured with passwordless sudo, matching the
# multipass default — so the existing bootstrap script (which uses
# ``sudo`` liberally) works unchanged.
VM_USER_UID: Final = 1000
VM_USER_GID: Final = 1000
VM_USER_HOME: Final = "/home/ubuntu"
# ``incus exec`` does NOT read ``/etc/environment`` (no PAM), so we
# must pass PATH explicitly. Includes ``/home/ubuntu/bin`` because
# rootless Docker installs its binaries there — without that on PATH
# the toolkit's ``docker ...`` invocations fail with ``command not
# found`` from inside the VM.
VM_USER_PATH: Final = (
    "PATH=/home/ubuntu/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)

# Device name for the host data-dir share on the instance. Stable so
# `_ensure_mount` and `umount` know what to remove / check for.
DATA_DEVICE_NAME: Final = "agents-data"

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


def _incus(
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> MpResult:
    return run([INCUS_BIN, *args], check=check, input_bytes=input_bytes, timeout=timeout)


# Back-compat alias for any external code that still imports ``mp.mp``.
mp = _incus


# ---------- exec / file transfer ----------


def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
    """Run a command in the VM as ``ubuntu`` (uid 1000), cwd /home/ubuntu.

    Passwordless sudo is available for elevated steps, matching the
    cloud-image default. Use ``exec_bash`` for piping scripts.
    """
    return _incus(
        "exec",
        "--user",
        str(VM_USER_UID),
        "--group",
        str(VM_USER_GID),
        "--cwd",
        VM_USER_HOME,
        "--env",
        f"HOME={VM_USER_HOME}",
        "--env",
        VM_USER_PATH,
        vm,
        "--",
        *cmd,
        check=check,
    )


def exec_bash(
    vm: str, script: str, *, check: bool = True, timeout: float | None = None
) -> MpResult:
    """Pipe a bash script into the VM. Avoids quoting hell."""
    return _incus(
        "exec",
        "--user",
        str(VM_USER_UID),
        "--group",
        str(VM_USER_GID),
        "--cwd",
        VM_USER_HOME,
        "--env",
        f"HOME={VM_USER_HOME}",
        "--env",
        VM_USER_PATH,
        vm,
        "--",
        "bash",
        "-s",
        check=check,
        input_bytes=script.encode(),
        timeout=timeout,
    )


def transfer_to_vm(host_path: Path, vm: str, vm_path: str) -> MpResult:
    """``incus file push`` — host file → VM path.

    Uses ``--create-dirs`` so callers can transfer into freshly-named
    targets without a separate mkdir step. UID/GID default to the
    ubuntu user so files round-trip the host share cleanly.
    """
    return _incus(
        "file",
        "push",
        "--create-dirs",
        "--uid",
        str(VM_USER_UID),
        "--gid",
        str(VM_USER_GID),
        str(host_path),
        f"{vm}{vm_path}",
    )


def transfer_from_vm(vm: str, vm_path: str, host_path: Path) -> MpResult:
    """``incus file pull`` — VM path → host file."""
    return _incus("file", "pull", f"{vm}{vm_path}", str(host_path))


# ---------- mount (virtiofs disk device on the instance) ----------


def mount(
    host_path: Path,
    vm: str,
    vm_path: str,
    *,
    host_uid: int,
    host_gid: int,
    vm_uid: int = VM_USER_UID,
    vm_gid: int = VM_USER_GID,
    device_name: str = DATA_DEVICE_NAME,
) -> MpResult:
    """Attach the host directory to the VM as a virtiofs share.

    Adds a disk device:
    ``incus config device add <vm> <device-name> disk source=<host>
    path=<vm-path>``. Incus auto-uses **virtiofs** for directory shares
    on VMs when ``virtiofsd`` is installed on the host (the package is a
    hard prereq, documented in the README). Without ``virtiofsd`` Incus
    falls back to 9p, which has the same SQLite ``disk I/O error`` quirk
    we hit on the multipass-era 9p mount.

    No ``raw.idmap`` is set — see the comment in ``launch`` for why.
    ``host_uid`` / ``host_gid`` are accepted for signature compatibility
    with the multipass-era caller.
    """
    del host_uid, host_gid, vm_uid, vm_gid
    return _incus(
        "config",
        "device",
        "add",
        vm,
        device_name,
        "disk",
        f"source={host_path}",
        f"path={vm_path}",
    )


def umount(vm: str, vm_path: str, *, device_name: str = DATA_DEVICE_NAME) -> MpResult:
    """Detach the host data-dir share from the VM.

    ``vm_path`` is unused at the Incus layer (devices are referenced by
    their device name, not their in-VM path) but kept in the signature
    for compatibility with the multipass-era callers.
    """
    del vm_path  # signature compat
    return _incus("config", "device", "remove", vm, device_name, check=False)


# ---------- lifecycle ----------


def launch(
    vm: str,
    *,
    image: str,
    cpus: int,
    memory_gb: int,
    disk_gb: int,
    host_data_dir: Path | None = None,
    vm_data_path: str | None = None,
    host_uid: int | None = None,
    host_gid: int | None = None,
    vm_uid: int = VM_USER_UID,
    vm_gid: int = VM_USER_GID,
) -> MpResult:
    """Create and start an Incus VM in one shot.

    A launch-time YAML config piped via stdin wires up sizing, idmap,
    and the host data-dir share so the VM comes up with the mount
    already attached — no stop-mount-start dance.

    ``image`` is an Incus image ref. The toolkit always passes
    ``images:ubuntu/<release>``; the ``images:`` remote is preconfigured
    by ``incus admin init`` and contains the community-maintained Ubuntu
    cloud images.

    ``host_data_dir`` + ``vm_data_path`` (with ``host_uid`` + ``host_gid``)
    are optional but together set up the virtiofs share at launch. If
    omitted, the VM comes up with no share and the caller must attach
    one with ``mount(...)``.
    """
    yaml_lines = [
        "config:",
        f'  limits.cpu: "{cpus}"',
        f"  limits.memory: {memory_gb}GiB",
    ]
    # NB: we intentionally do NOT set `raw.idmap` here.
    #
    # An idmap like `uid <host> 1000 / gid <host> 1000` would round-trip
    # host-owned files through virtiofs with the right ownership inside
    # the VM. But it breaks the Hermes container entrypoint: the
    # entrypoint chowns /opt/data to its `hermes` user, which under
    # rootless Docker resolves to a subuid (host uid 100000+ range).
    # virtiofsd then refuses the chown with `Invalid argument` because
    # that uid is outside the configured idmap, and the container falls
    # into a permission-denied loop. Incus 6.0's raw.idmap parser only
    # accepts single-id mappings (`uid <hostid> <nsid>`); it rejects the
    # 4-field range form (`uid <host> <ns> <count>`) needed to cover the
    # 65k-id rootless-docker subuid range. Without a workable extended
    # idmap, the least-bad option is no idmap: virtiofs passes uids
    # through 1:1, the chown succeeds, the container runs.
    #
    # The trade-off: files written by the container as the `hermes` user
    # (post privilege-drop) appear on the host owned by a subuid in the
    # 100000+ range — invisible to the regular user but still backed up
    # correctly by host-side root tools (duplicity, restic). Host-side
    # editing of in-container-written agent state requires sudo. Files
    # written by user-as-ubuntu via the toolkit (host data dir seed,
    # `agents export`, etc.) stay owned by the host user.
    del host_uid, host_gid, vm_uid, vm_gid  # accepted for signature compat
    yaml_lines += [
        "devices:",
        "  root:",
        "    type: disk",
        "    pool: default",
        "    path: /",
        f"    size: {disk_gb}GiB",
    ]
    if host_data_dir is not None and vm_data_path is not None:
        yaml_lines += [
            f"  {DATA_DEVICE_NAME}:",
            "    type: disk",
            f"    source: {host_data_dir}",
            f"    path: {vm_data_path}",
        ]
    yaml = "\n".join(yaml_lines) + "\n"
    return _incus(
        "launch",
        "-q",
        image,
        vm,
        "--vm",
        input_bytes=yaml.encode(),
        timeout=900.0,
    )


def start_vm(vm: str) -> MpResult:
    return _incus("start", vm)


def stop_vm(vm: str) -> MpResult:
    return _incus("stop", vm)


def delete_vm(vm: str, *, purge: bool = False) -> MpResult:
    """``incus delete --force`` (force is required to delete a running VM).

    ``purge`` is accepted for signature compat with the multipass era;
    Incus always purges (instance + root volume) on delete.
    """
    del purge  # signature compat
    return _incus("delete", "--force", vm)


# ---------- config / resize ----------


def set_cpus(vm: str, n: int) -> MpResult:
    return _incus("config", "set", vm, f"limits.cpu={n}")


def set_memory_gb(vm: str, gb: int) -> MpResult:
    return _incus("config", "set", vm, f"limits.memory={gb}GiB")


def set_disk_gb(vm: str, gb: int) -> MpResult:
    """Resize the root disk of the VM. The ``default`` storage pool
    needs to support online resize for this to work without a restart.
    """
    return _incus("config", "device", "set", vm, "root", f"size={gb}GiB")


# ---------- info / list / find ----------


def info_vm(vm: str, *, fmt: str = "json") -> MpResult:
    """Return instance info, normalised to the multipass shape.

    The historical callers in ``vm.py`` / ``upgrades.py`` / ``ops.py``
    parse ``{"info": {"<vm>": {"state": ..., "mounts": {...},
    "snapshots": {...}, "ipv4": [...]}}}``. We reshape Incus output
    here so they keep working unchanged. ``fmt`` is accepted for
    signature compat; only ``json`` is supported.
    """
    del fmt  # signature compat
    list_result = _incus("list", vm, "--format", "json", check=False)
    if list_result.returncode != 0:
        return list_result
    try:
        instances = json.loads(list_result.stdout)
    except json.JSONDecodeError:
        return MpResult(stdout="{}", stderr=list_result.stderr, returncode=1)
    if not isinstance(instances, list) or not instances:
        return MpResult(
            stdout=json.dumps({"info": {}}),
            stderr=list_result.stderr,
            returncode=1,
        )
    inst = instances[0]
    snap_result = _incus("snapshot", "list", vm, "--format", "json", check=False)
    snapshots: dict[str, dict[str, str]] = {}
    if snap_result.returncode == 0:
        try:
            for snap in json.loads(snap_result.stdout) or []:
                snapshots[snap["name"]] = {
                    "comment": snap.get("description", "") or "",
                    "created_at": snap.get("created_at", "") or "",
                }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    mounts: dict[str, dict[str, str]] = {}
    for dev_name, dev in (inst.get("devices") or {}).items():
        if dev.get("type") != "disk":
            continue
        if "source" not in dev or "path" not in dev:
            continue
        if dev["path"] == "/":  # root volume, not a host share
            continue
        mounts[str(dev["path"])] = {
            "source_path": str(dev["source"]),
            "device_name": dev_name,
        }

    ipv4 = []
    network = (inst.get("state") or {}).get("network") or {}
    for iface in network.values():
        for addr in iface.get("addresses") or []:
            if addr.get("family") == "inet" and addr.get("scope") == "global":
                ipv4.append(str(addr.get("address")))

    normalized = {
        "info": {
            vm: {
                "state": inst.get("status", "Unknown"),
                "mounts": mounts,
                "snapshots": snapshots,
                "ipv4": ipv4,
            }
        }
    }
    return MpResult(
        stdout=json.dumps(normalized),
        stderr=list_result.stderr,
        returncode=0,
    )


def list_vms(*, fmt: str = "json") -> MpResult:
    """Return instance list, normalised to the multipass shape.

    Multipass returned ``{"list": [{"name": ..., "state": ..., ...}]}``.
    Incus returns a raw array. We re-shape here.
    """
    del fmt  # signature compat
    result = _incus("list", "--format", "json", check=False)
    if result.returncode != 0:
        return result
    try:
        instances = json.loads(result.stdout)
    except json.JSONDecodeError:
        return result
    if not isinstance(instances, list):
        return result
    items = [
        {"name": inst.get("name", ""), "state": inst.get("status", "Unknown")}
        for inst in instances
    ]
    return MpResult(
        stdout=json.dumps({"list": items}),
        stderr=result.stderr,
        returncode=0,
    )


def find(*, fmt: str = "json", show_unsupported: bool = False) -> MpResult:
    """Return available Ubuntu images from the ``images:`` remote,
    normalised to the multipass ``find`` shape.

    Multipass returned ``{"images": {"<key>": {"aliases": [...], ...}}}``;
    callers iterate looking for a release alias (e.g. ``"24.04"``). We
    expose the same keys here so ``_select_ubuntu_image`` keeps working.
    """
    del fmt, show_unsupported  # signature compat
    result = _incus(
        "image", "list", "images:ubuntu", "--format", "json", check=False
    )
    if result.returncode != 0:
        return result
    try:
        images = json.loads(result.stdout)
    except json.JSONDecodeError:
        return result

    bucket: dict[str, dict[str, list[str]]] = {}
    for img in images or []:
        fp = img.get("fingerprint", "")
        aliases = [a.get("name", "") for a in (img.get("aliases") or []) if a.get("name")]
        clean = [a.split("/", 1)[1] for a in aliases if a.startswith("ubuntu/")]
        bucket[fp] = {
            "aliases": aliases,
            "aliases_no_remote": clean,
        }
    return MpResult(
        stdout=json.dumps({"images": bucket}),
        stderr=result.stderr,
        returncode=0,
    )


# ---------- snapshots ----------


def snapshot(vm: str, name: str, *, comment: str | None = None) -> MpResult:
    args = ["snapshot", "create", vm, name]
    return _incus(*args)


def restore(snapshot_id: str, *, destructive: bool = False) -> MpResult:
    """Restore a snapshot.

    ``snapshot_id`` is the multipass-era form ``<vm>.<snapshot-name>``;
    we split on the first dot and call ``incus snapshot restore``.
    ``destructive`` is accepted for signature compat (Incus is always
    destructive on restore — the current state is overwritten).
    """
    del destructive  # signature compat
    if "." not in snapshot_id:
        raise RuntimeFailure(
            f"snapshot id {snapshot_id!r} is not in the expected <vm>.<name> form"
        )
    vm, name = snapshot_id.split(".", 1)
    return _incus("snapshot", "restore", vm, name)


# ---------- BOOTSTRAP_OK parser (unchanged) ----------


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
