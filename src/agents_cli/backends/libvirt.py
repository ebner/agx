"""libvirt / KVM backend (Phase B — EXPERIMENTAL, see GH #2).

⚠️  Untested on real hardware in the merging session — every code
path here is docs-driven. Smoke-test on a Linux + libvirt host
before relying on it; expect minor quirks around virsh output
formats, cloud-init seed ISO, and GPU IOMMU-group handling that
will surface only when actually exercised.

Architecture:

- VMs are provisioned via ``virt-install --import`` against an
  Ubuntu cloud image (cached at ``<data-dir>/.images/``) with a
  cloud-init seed ISO that injects an SSH key pair the toolkit
  generates at ``vm init`` time (stored under
  ``<data-dir>/.ssh/``). All subsequent exec/transfer goes through
  that SSH connection — no libvirt-guest-agent dependency.
- The libvirt URI is ``qemu:///system`` (required for GPU
  passthrough; user must be in the ``libvirt`` group on the host).
- Mounts use virtiofs attached via domain-XML ``<filesystem>``
  elements. 9p is documented as a manual fallback if virtiofsd is
  unavailable.
- Snapshots use ``virsh snapshot-create-as`` ``--disk-only``
  (external snapshots / qcow2 backing chain) so the disk isn't
  paused during creation.
- GPU passthrough: PCI device detected via ``lspci -nn``
  (vendor IDs ``10de`` NVIDIA, ``1002`` AMD, ``8086`` Intel),
  IOMMU groups validated under ``/sys/kernel/iommu_groups/``,
  attached via ``<hostdev>`` XML.

Linux-only: import refuses on macOS/Windows. For non-Linux hosts
use ``--backend multipass``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from agents_cli import ui
from agents_cli.backends import MpResult
from agents_cli.errors import RuntimeFailure, UserError


def _require_linux_host() -> None:
    if platform.system() != "Linux":
        raise UserError(
            "libvirt backend is Linux-only. Use --backend multipass on "
            f"{platform.system()}; see docs/runbook.md for the cross-platform matrix."
        )


_require_linux_host()


# ---------- constants ----------

LIBVIRT_URI: Final = "qemu:///system"
VIRSH: Final = "virsh"
VIRT_INSTALL: Final = "virt-install"
SSH: Final = "ssh"
SCP: Final = "scp"
QEMU_IMG: Final = "qemu-img"
CLOUD_LOCALDS: Final = "cloud-localds"
LSPCI: Final = "lspci"

CLOUD_IMAGE_URL_FMT: Final = (
    "https://cloud-images.ubuntu.com/releases/{release}/release/"
    "ubuntu-{release}-server-cloudimg-{arch}.img"
)

# Set by configure(); helpers raise loud if used pre-init.
_CACHE_ROOT: Path | None = None
_SSH_KEY: Path | None = None


def _data_dir() -> Path:
    if _CACHE_ROOT is None:
        raise RuntimeFailure(
            "libvirt backend not initialised — call configure(data_dir) "
            "first. cli.main does this when cfg.vm.backend == 'libvirt'."
        )
    return _CACHE_ROOT


def _ssh_key_path() -> Path:
    if _SSH_KEY is None:
        raise RuntimeFailure("libvirt SSH key not initialised — call configure() first.")
    return _SSH_KEY


def configure(data_dir: Path) -> None:
    """Populate module-level cache paths. Called by cli.main when
    libvirt is the active backend, before any primitive runs.
    """
    global _CACHE_ROOT, _SSH_KEY
    _CACHE_ROOT = data_dir
    _SSH_KEY = data_dir / ".ssh" / "id_ed25519"


# ---------- low-level subprocess + virsh helpers ----------


def _run(
    argv: list[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> MpResult:
    """Argv-form subprocess wrapper — same shape as multipass.run."""
    ui.info(f"$ {' '.join(argv)}")
    try:
        proc = subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeFailure(
            f"command not found: {argv[0]} ({e}). "
            f"Install libvirt: sudo apt install libvirt-clients libvirt-daemon-system "
            f"virtinst genisoimage cloud-image-utils openssh-client qemu-utils."
        ) from e
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


def _virsh(*args: str, check: bool = True, timeout: float | None = 60.0) -> MpResult:
    return _run([VIRSH, "-c", LIBVIRT_URI, *args], check=check, timeout=timeout)


# ---------- SSH layer ----------


def _ssh_opts() -> list[str]:
    """Throwaway-VM SSH defaults: no host-key prompt, no
    known-hosts churn (key changes after vm rebuild).
    """
    return [
        "-i",
        str(_ssh_key_path()),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=10",
    ]


def _vm_ip(vm: str) -> str:
    """``virsh domifaddr`` → IPv4 lease. Re-queried each call —
    libvirt DHCP can re-issue.
    """
    result = _virsh("domifaddr", vm, "--source", "lease", check=False)
    if result.returncode != 0:
        raise RuntimeFailure(f"could not get IP for libvirt VM {vm!r}: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        m = re.search(r"\b(\d+\.\d+\.\d+\.\d+)/\d+", line)
        if m:
            return m.group(1)
    raise RuntimeFailure(
        f"libvirt VM {vm!r} has no IPv4 lease yet (is it running and on the default network?)"
    )


def _ssh(vm: str, *cmd: str, check: bool = True, input_bytes: bytes | None = None) -> MpResult:
    """Run ``cmd`` argv on the libvirt VM via SSH.

    ssh concatenates all args after the destination with spaces and
    passes that string to the remote login shell, which re-parses it.
    That means embedded spaces or shell metacharacters in any ``cmd``
    element get lost — ``sh -c "docker ps -a"`` ends up running
    ``docker`` with three positional args and confused docker.

    Fix: shell-quote each argv element with :func:`shlex.quote`
    before passing to ssh. Joined back together by ssh, the remote
    shell parses it as the original argv shape.
    """
    import shlex

    ip = _vm_ip(vm)
    quoted = [shlex.quote(c) for c in cmd]
    argv = [SSH, *_ssh_opts(), f"ubuntu@{ip}", "--", *quoted]
    return _run(argv, check=check, input_bytes=input_bytes)


# ---------- cloud-init + disk provisioning ----------


def _docker_arch() -> str:
    m = platform.machine().lower()
    return "arm64" if m in ("aarch64", "arm64") else "amd64"


def _ensure_cloud_image(release: str) -> Path:
    """Cache an Ubuntu cloud image under ``<data-dir>/.images/``.
    Idempotent across launches.
    """
    arch = _docker_arch()
    cache = _data_dir() / ".images"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"ubuntu-{release}-server-cloudimg-{arch}.img"
    if target.exists():
        return target
    url = CLOUD_IMAGE_URL_FMT.format(release=release, arch=arch)
    ui.info(f"downloading {url} → {target}")
    _run(["curl", "-fSL", "-o", str(target), url], timeout=900.0)
    return target


def _ensure_ssh_key() -> Path:
    """Generate a toolkit-owned ed25519 key on first launch."""
    key = _ssh_key_path()
    if key.exists():
        return key
    key.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "agents-libvirt",
            "-f",
            str(key),
        ]
    )
    os.chmod(key, 0o600)
    return key


def _build_cloud_init(vm: str, ssh_pubkey: str) -> tuple[Path, Path]:
    """Write user-data + meta-data YAML to a tempdir."""
    tmp = Path(tempfile.mkdtemp(prefix=f"agents-libvirt-{vm}-"))
    (tmp / "meta-data").write_text(f"instance-id: {vm}\nlocal-hostname: {vm}\n", encoding="utf-8")
    (tmp / "user-data").write_text(
        "#cloud-config\n"
        "users:\n"
        "  - name: ubuntu\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    ssh_authorized_keys:\n"
        f"      - {ssh_pubkey.strip()}\n"
        "package_update: true\n"
        "packages:\n"
        "  - openssh-server\n"
        "  - qemu-guest-agent\n"
        "runcmd:\n"
        "  - systemctl enable --now qemu-guest-agent\n",
        encoding="utf-8",
    )
    return tmp / "user-data", tmp / "meta-data"


def _make_seed_iso(vm: str, user_data: Path, meta_data: Path) -> Path:
    """cloud-localds packs user-data + meta-data into the NoCloud
    ISO9660 format cloud-init looks for on first boot.
    """
    iso = _data_dir() / ".seeds" / f"{vm}-seed.iso"
    iso.parent.mkdir(parents=True, exist_ok=True)
    _run([CLOUD_LOCALDS, str(iso), str(user_data), str(meta_data)])
    return iso


def wait_for_ssh(vm: str, timeout_s: int = 180, *, wait_cloud_init: bool = True) -> None:
    """Block until SSH responds AND (by default) cloud-init has
    finished. Cloud-init takes 60-180s on a fresh boot and holds the
    apt dpkg lock the whole time; running the toolkit's bootstrap
    apt-install during that window silently no-ops because of the
    lock.

    Two-phase wait:

    1. Poll ``ssh ubuntu@<vm> true`` until two successive probes
       succeed (single success can race sshd startup and the next
       SSH call hits ``kex_exchange_identification: Connection reset``).
    2. Run ``cloud-init status --wait`` inside the VM, which blocks
       until first-boot is done (apt lock released, packages
       installed).

    Set ``wait_cloud_init=False`` for follow-up reboots where
    cloud-init has long since finished — saves the 2-second status
    check round-trip.
    """
    import contextlib
    import time

    start = time.time()
    successes = 0
    while time.time() - start < timeout_s:
        with contextlib.suppress(RuntimeFailure):
            result = _ssh(vm, "true", check=False)
            if result.returncode == 0:
                successes += 1
                if successes >= 2:
                    time.sleep(2)
                    if wait_cloud_init:
                        # `cloud-init status --wait` blocks until
                        # first-boot completes; check=False because
                        # rc=2 means "completed with errors" which
                        # we'd rather surface via the bootstrap
                        # output than mask here.
                        _ssh(vm, "cloud-init", "status", "--wait", check=False)
                        # Brief settle so the dpkg lock is fully
                        # released before bootstrap fires.
                        time.sleep(2)
                    return
                time.sleep(3)
                continue
        successes = 0
        time.sleep(5)
    raise RuntimeFailure(f"libvirt VM {vm!r} did not become SSH-reachable within {timeout_s}s")


# ---------- VmBackend Protocol implementation ----------


def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
    _require_linux_host()
    return _ssh(vm, *cmd, check=check)


def exec_bash(
    vm: str, script: str, *, check: bool = True, timeout: float | None = None
) -> MpResult:
    _require_linux_host()
    ip = _vm_ip(vm)
    argv = [SSH, *_ssh_opts(), f"ubuntu@{ip}", "bash", "-s"]
    # One automatic retry on "kex_exchange" / "Connection reset by
    # peer" — sshd post-cloud-init can briefly drop connections
    # while it reconfigures. ``wait_for_ssh`` already cushions this
    # with a double-probe + settle delay, but heavy bootstrap
    # scripts (the toolkit's is ~500 lines) sometimes still race.
    import time

    for attempt in range(2):
        result = _run(argv, check=False, input_bytes=script.encode(), timeout=timeout)
        if result.returncode == 0:
            return result
        err = (result.stderr or "").lower()
        transient = (
            "kex_exchange" in err or "connection reset" in err or "connection refused" in err
        )
        if not transient or attempt == 1:
            if check and result.returncode != 0:
                raise RuntimeFailure(
                    f"command failed (exit {result.returncode}): {' '.join(argv)}\n"
                    f"stderr: {(result.stderr or '').strip()}"
                )
            return result
        time.sleep(5)
    return result


def exec_in_vm_with_stdin(vm: str, *cmd: str, input_bytes: bytes, check: bool = True) -> MpResult:
    _require_linux_host()
    return _ssh(vm, *cmd, check=check, input_bytes=input_bytes)


def exec_in_vm_interactive(vm: str, *cmd: str) -> int:
    """Interactive SSH — inherits terminal so wizards can prompt.

    Same shlex.quote treatment as :func:`_ssh`: ssh joins post-`--`
    argv with spaces and the remote shell re-parses, so embedded
    spaces / shell metacharacters in any element need to round-trip
    via shell-quoting.
    """
    import shlex

    _require_linux_host()
    ip = _vm_ip(vm)
    quoted = [shlex.quote(c) for c in cmd]
    argv = [SSH, "-t", *_ssh_opts(), f"ubuntu@{ip}", "--", *quoted]
    ui.info(f"$ {' '.join(argv)}")
    try:
        result = subprocess.run(argv, check=False)
    except FileNotFoundError as e:
        raise RuntimeFailure(f"command not found: {argv[0]} ({e})") from e
    return result.returncode


def transfer_to_vm(host_path: Path, vm: str, vm_path: str) -> MpResult:
    _require_linux_host()
    ip = _vm_ip(vm)
    return _run([SCP, *_ssh_opts(), str(host_path), f"ubuntu@{ip}:{vm_path}"])


def transfer_from_vm(vm: str, vm_path: str, host_path: Path) -> MpResult:
    _require_linux_host()
    ip = _vm_ip(vm)
    return _run([SCP, *_ssh_opts(), f"ubuntu@{ip}:{vm_path}", str(host_path)])


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
    """Attach a virtiofs share + mount it in-guest.

    Three things have to be true for virtiofs to work:

    1. Domain XML has ``<memoryBacking source.type='memfd'
       access.mode='shared'/>`` — set at ``launch`` time.
    2. ``<filesystem type='mount'><driver type='virtiofs'/>...``
       attached. We use ``--live --config`` so it takes effect
       both immediately on a running VM and persists across reboots.
    3. In-guest: ``mount -t virtiofs <tag> <vm_path>`` plus an
       fstab entry for boot.

    Host-side prereq: libvirt-qemu must traverse ``host_path``'s
    parent dirs. Falls back gracefully on perm errors.

    9p fallback isn't implemented — virtiofsd is the supported path.
    If virtiofs is wedged, edit the domain XML manually via
    ``virsh edit <vm>``.
    """
    _require_linux_host()
    import contextlib

    tag = vm_path.replace("/", "_").strip("_") or "share"
    # Make sure libvirt-qemu can traverse the host path.
    with contextlib.suppress(OSError):
        os.chmod(host_path, 0o755)
    xml = (
        "<filesystem type='mount' accessmode='passthrough'>"
        "  <driver type='virtiofs'/>"
        f"  <source dir='{host_path}'/>"
        f"  <target dir='{tag}'/>"
        "</filesystem>"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml)
        xml_path = f.name
    try:
        # --live attaches now; --config persists across reboots.
        # Falls back to --config-only if the VM is stopped.
        result = _virsh("attach-device", vm, xml_path, "--live", "--config", check=False)
        if result.returncode != 0 and "not running" in (result.stderr or "").lower():
            result = _virsh("attach-device", vm, xml_path, "--config", check=False)
    finally:
        os.unlink(xml_path)
    if result.returncode != 0:
        return result
    # In-guest mount + fstab entry. Best-effort: if VM is stopped,
    # this is skipped and the fstab will be picked up at next boot.
    import contextlib

    fstab_line = f"{tag} {vm_path} virtiofs rw,_netdev 0 0"
    with contextlib.suppress(RuntimeFailure):
        exec_bash(
            vm,
            f"sudo mkdir -p {vm_path}\n"
            f"grep -qF '{fstab_line}' /etc/fstab || "
            f"echo '{fstab_line}' | sudo tee -a /etc/fstab >/dev/null\n"
            f"mountpoint -q {vm_path} || sudo mount -t virtiofs {tag} {vm_path}\n",
            check=False,
        )
    return result


def umount(vm: str, vm_path: str) -> MpResult:
    """Detach the virtiofs share via domain XML, located by target tag."""
    _require_linux_host()
    tag = vm_path.replace("/", "_").strip("_") or "share"
    xml = (
        "<filesystem type='mount' accessmode='passthrough'>"
        "  <driver type='virtiofs'/>"
        f"  <target dir='{tag}'/>"
        "</filesystem>"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml)
        xml_path = f.name
    try:
        return _virsh("detach-device", vm, xml_path, "--config", check=False)
    finally:
        os.unlink(xml_path)


def launch(vm: str, *, image: str, cpus: int, memory_gb: int, disk_gb: int) -> MpResult:
    """Provision a libvirt VM via ``virt-install --import``.

    ``image`` is the Ubuntu release version (e.g. ``"24.04"``); the
    multipass-style short form maps directly to a cloud-image
    filename. Anything else is rejected up-front to avoid silent
    wrong-image guesses.
    """
    _require_linux_host()
    if not re.fullmatch(r"\d+\.\d+", image):
        raise UserError(f"libvirt backend expects an Ubuntu release (e.g. '24.04'), got {image!r}")

    base_image = _ensure_cloud_image(image)
    pub_key = _ensure_ssh_key().with_suffix(".pub").read_text(encoding="utf-8")
    user_data, meta_data = _build_cloud_init(vm, pub_key)
    seed_iso = _make_seed_iso(vm, user_data, meta_data)

    disk = _data_dir() / ".disks" / f"{vm}.qcow2"
    disk.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            QEMU_IMG,
            "create",
            "-f",
            "qcow2",
            "-b",
            str(base_image),
            "-F",
            "qcow2",
            str(disk),
            f"{disk_gb}G",
        ]
    )
    # libvirt-qemu must be able to traverse the data-dir to reach
    # disk + seed. Default mkdir mode on /tmp is 700, which blocks
    # libvirt-qemu. Make all relevant parent dirs world-traversable.
    # Disk + seed files themselves stay at default mode 600 (libvirt
    # opens them via the QEMU helper which has the right caps).
    import contextlib

    for d in (disk.parent, base_image.parent, seed_iso.parent, _data_dir()):
        with contextlib.suppress(OSError):
            os.chmod(d, 0o755)

    # virt-install accepts `ubuntu<MAJOR>.<MINOR>` (with the dot) per
    # `--osinfo list`. The toolkit's `image` arg is already that shape
    # (e.g. "24.04") so we just prepend the vendor prefix.
    osvariant = f"ubuntu{image}"
    return _run(
        [
            VIRT_INSTALL,
            "--connect",
            LIBVIRT_URI,
            "--name",
            vm,
            "--memory",
            str(memory_gb * 1024),
            # Required for virtiofs mounts to work later. Without
            # shared memfd backing, attach_device of a <filesystem
            # type=virtiofs> block succeeds but the guest mount hits
            # "Transport endpoint is not connected". See GH #4.
            "--memorybacking",
            "source.type=memfd,access.mode=shared",
            "--vcpus",
            str(cpus),
            "--disk",
            f"path={disk},format=qcow2,bus=virtio",
            "--disk",
            f"path={seed_iso},device=cdrom",
            "--os-variant",
            osvariant,
            "--virt-type",
            "kvm",
            "--graphics",
            "none",
            "--network",
            "network=default,model=virtio",
            "--import",
            "--noautoconsole",
            "--wait",
            "0",
        ],
        timeout=300.0,
    )


def _cleanup_partial_domain(vm: str) -> None:
    """Tear down a half-provisioned libvirt domain so a retry of
    ``vm init`` doesn't trip over the orphan. Safe to call when the
    domain doesn't exist; each step is best-effort.

    GH #3: virt-install can leave a defined-but-unstartable domain
    if e.g. ``wait_for_ssh`` times out after the domain is
    registered. Caller (``vm.init``'s libvirt branch) wraps the
    launch+wait+gpu sequence and invokes this on failure.
    """
    _virsh("destroy", vm, check=False)
    # Snapshot metadata blocks undefine — wipe it first.
    snap_list = _virsh("snapshot-list", vm, "--name", check=False)
    if snap_list.returncode == 0:
        for snap_name in snap_list.stdout.splitlines():
            snap_name = snap_name.strip()
            if snap_name:
                _virsh("snapshot-delete", vm, snap_name, "--metadata", check=False)
    _virsh(
        "undefine",
        vm,
        "--nvram",
        "--managed-save",
        "--remove-all-storage",
        check=False,
    )


def info_vm(vm: str, *, fmt: str = "json") -> MpResult:
    """Run ``virsh dominfo`` and reshape the output to the JSON form
    multipass produces, so callers (``vm.status``, ``vm._vm_running``)
    don't need to branch on backend.

    multipass returns ``{"errors": [], "info": {"<vm>": {"state": ...,
    "release": ..., ...}}}``; we fill in the same outer shape with the
    fields virsh actually surfaces (state, OS type, CPU count, memory).
    """
    _require_linux_host()
    raw = _virsh("dominfo", vm, check=False)
    if raw.returncode != 0:
        return raw
    fields: dict[str, str] = {}
    for line in raw.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fields[key.strip()] = val.strip()
    # Map virsh "State" values to multipass-compatible labels for the
    # vm.status / _vm_running consumers.
    state_map = {
        "running": "Running",
        "shut off": "Stopped",
        "paused": "Suspended",
        "in shutdown": "Stopping",
    }
    state = state_map.get(fields.get("State", "").lower(), fields.get("State", "Unknown"))
    # Enumerate snapshots so ``vm.list_snapshots`` (which reads
    # ``info[vm]["snapshots"]``) finds them. virsh snapshot-list emits
    # a tabular form; --name shrinks it to one snapshot-name per line.
    snap_result = _virsh("snapshot-list", vm, "--name", check=False)
    snapshots: dict[str, dict[str, str]] = {}
    if snap_result.returncode == 0:
        for snap_name in snap_result.stdout.splitlines():
            snap_name = snap_name.strip()
            if snap_name:
                snapshots[snap_name] = {}
    info = {
        "state": state,
        "release": fields.get("OS Type", "kvm"),
        "cpu_count": fields.get("CPU(s)", ""),
        "memory": fields.get("Max memory", ""),
        "id": fields.get("Id", ""),
        "uuid": fields.get("UUID", ""),
        "snapshots": snapshots,
    }
    payload = {"errors": [], "info": {vm: info}}
    return MpResult(stdout=json.dumps(payload), stderr="", returncode=0)


def list_vms(*, fmt: str = "json") -> MpResult:
    """Best-effort multipass-list-compatible JSON payload."""
    _require_linux_host()
    result = _virsh("list", "--all", "--name", check=False)
    if result.returncode != 0:
        return result
    names = [n for n in result.stdout.splitlines() if n.strip()]
    payload = {"list": [{"name": n} for n in names]}
    return MpResult(stdout=json.dumps(payload), stderr="", returncode=0)


def find(*, fmt: str = "json", show_unsupported: bool = False) -> MpResult:
    """Return what's in the local cache. libvirt has no ``multipass
    find`` equivalent; cloud images are fetched directly from
    cloud-images.ubuntu.com on first launch.
    """
    _require_linux_host()
    cache = _data_dir() / ".images"
    items: list[dict[str, str]] = []
    if cache.exists():
        for p in cache.glob("ubuntu-*-server-cloudimg-*.img"):
            items.append({"name": p.name, "path": str(p)})
    return MpResult(stdout=json.dumps({"cached_images": items}), stderr="", returncode=0)


def start_vm(vm: str) -> MpResult:
    _require_linux_host()
    return _virsh("start", vm, check=False)


def stop_vm(vm: str) -> MpResult:
    """ACPI shutdown. libvirt waits ~30s for graceful exit; caller
    should retry with destroy if it hangs.
    """
    _require_linux_host()
    return _virsh("shutdown", vm, check=False)


def set_config(key: str, value: str | int) -> MpResult:
    """Translate multipass-style ``local.<vm>.<knob>`` keys into
    virsh / qemu-img equivalents. ``agents vm resize`` is the only
    known caller.
    """
    _require_linux_host()
    m = re.match(r"local\.(?P<vm>[^.]+)\.(?P<knob>cpus|memory|disk)", key)
    if not m:
        return MpResult(
            stdout="", stderr=f"unsupported libvirt set_config key: {key}", returncode=1
        )
    vm = m.group("vm")
    knob = m.group("knob")
    if knob == "cpus":
        return _virsh("setvcpus", vm, str(value), "--config", check=False)
    if knob == "memory":
        gb_match = re.match(r"(\d+)", str(value))
        if not gb_match:
            return MpResult(stdout="", stderr=f"unparseable memory value {value!r}", returncode=1)
        kib = int(gb_match.group(1)) * 1024 * 1024
        out = _virsh("setmaxmem", vm, str(kib), "--config", check=False)
        if out.returncode != 0:
            return out
        return _virsh("setmem", vm, str(kib), "--config", check=False)
    # disk
    gb_match = re.match(r"(\d+)", str(value))
    if not gb_match:
        return MpResult(stdout="", stderr=f"unparseable disk value {value!r}", returncode=1)
    disk = _data_dir() / ".disks" / f"{vm}.qcow2"
    return _run([QEMU_IMG, "resize", str(disk), f"{gb_match.group(1)}G"], check=False)


def delete_vm(vm: str, *, purge: bool = False) -> MpResult:
    """destroy + undefine. With ``purge``, also removes the qcow2
    disk and seed ISO under ``<data-dir>/.disks/`` and ``.seeds/``.
    """
    _require_linux_host()
    _virsh("destroy", vm, check=False)
    args = ["undefine", vm, "--nvram"]
    if purge:
        args.append("--remove-all-storage")
    result = _virsh(*args, check=False)
    if purge:
        disk = _data_dir() / ".disks" / f"{vm}.qcow2"
        if disk.exists():
            disk.unlink()
        seed = _data_dir() / ".seeds" / f"{vm}-seed.iso"
        if seed.exists():
            seed.unlink()
    return result


def snapshot(vm: str, name: str, *, comment: str | None = None) -> MpResult:
    """Create an internal qcow2 snapshot (revertible).

    Earlier versions used ``--disk-only`` for external (backing-chain)
    snapshots, but those CAN'T be reverted with ``snapshot-revert``
    — they're backup points only. Worse, they modify the disk chain
    so future VM starts may hit perm errors on the base image. The
    revertible-by-default semantics match Multipass's snapshot
    behaviour and what ``vm.upgrade``'s rollback flow assumes.

    Internal snapshots accumulate inside the qcow2; periodic
    ``virsh snapshot-delete`` cleanup may be needed on long-running
    VMs that snapshot frequently.

    **Incompatible with virtiofs on a running VM**. libvirt refuses
    ``snapshot-create-as`` on a domain with active virtiofs (vhost-
    user) filesystems because the backend lacks ``VHOST_USER_PROTOCOL
    _F_LOG_SHMFD``. The high-level ``vm.take_snapshot`` prompts the
    user and stops the VM around the snapshot to avoid this. Direct
    callers of this primitive that hit the error get the virsh error
    message verbatim — the workaround is to stop the VM first.
    """
    _require_linux_host()
    args = ["snapshot-create-as", vm, name]
    if comment:
        args.extend(["--description", comment])
    return _virsh(*args, check=False)


def restore(snapshot_id: str, *, destructive: bool = False) -> MpResult:
    """``snapshot_id`` form ``<vm>.<name>`` (same as multipass)."""
    _require_linux_host()
    if "." not in snapshot_id:
        return MpResult(
            stdout="",
            stderr=f"libvirt snapshot id must be '<vm>.<name>', got {snapshot_id!r}",
            returncode=1,
        )
    vm, name = snapshot_id.split(".", 1)
    args = ["snapshot-revert", vm, name]
    if destructive:
        args.append("--force")
    return _virsh(*args, check=False)


# ---------- GPU passthrough ----------

GPU_VENDORS: Final = {
    "10de": "NVIDIA",
    "1002": "AMD",
    "8086": "Intel",
}


def detect_gpus() -> list[dict[str, str]]:
    """Enumerate PCI VGA/3D/Display controllers from supported
    vendors via ``lspci -nn -D``. Returns ``[]`` when lspci is
    missing — callers decide whether that's a hard failure.

    Each entry: ``{pci_id, vendor, vendor_name, device, description,
    iommu_group, vfio_bound}``. The ``iommu_group`` and ``vfio_bound``
    fields drive doctor + GPU-attach validation.
    """
    if not shutil.which(LSPCI):
        return []
    result = _run([LSPCI, "-nn", "-D"], check=False)
    if result.returncode != 0:
        return []
    gpus: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        m = re.match(
            r"(?P<pci>\S+)\s+(?:VGA compatible|3D|Display)\s+controller\s+\[\w+\]:\s+(?P<desc>.+?)\s+\[(?P<vid>[0-9a-f]{4}):(?P<did>[0-9a-f]{4})\]",
            line,
        )
        if not m:
            continue
        vid = m.group("vid").lower()
        if vid not in GPU_VENDORS:
            continue
        pci = m.group("pci")
        gpus.append(
            {
                "pci_id": pci,
                "vendor": vid,
                "vendor_name": GPU_VENDORS[vid],
                "device": m.group("did"),
                "description": m.group("desc"),
                "iommu_group": _iommu_group_for(pci),
                "vfio_bound": "true" if _is_vfio_bound(pci) else "false",
            }
        )
    return gpus


def _iommu_group_for(pci: str) -> str:
    """Read ``/sys/bus/pci/devices/<pci>/iommu_group/``. Returns empty
    string when IOMMU isn't enabled or the device isn't in a group.
    """
    link = Path(f"/sys/bus/pci/devices/{pci}/iommu_group")
    if not link.exists():
        return ""
    try:
        return Path(os.readlink(link)).name
    except OSError:
        return ""


def _is_vfio_bound(pci: str) -> bool:
    driver_link = Path(f"/sys/bus/pci/devices/{pci}/driver")
    if not driver_link.exists():
        return False
    try:
        return Path(os.readlink(driver_link)).name == "vfio-pci"
    except OSError:
        return False


def attach_gpu_xml(vm: str, pci_id: str) -> MpResult:
    """Build and attach the ``<hostdev>`` XML for a PCI passthrough
    device. Requires the device bound to vfio-pci and the VM stopped.

    ``pci_id`` form ``0000:01:00.0`` (full domain:bus:slot.function).
    """
    _require_linux_host()
    m = re.match(
        r"(?P<dom>[0-9a-f]{4}):(?P<bus>[0-9a-f]{2}):(?P<slot>[0-9a-f]{2})\.(?P<func>[0-7])",
        pci_id,
    )
    if not m:
        return MpResult(
            stdout="",
            stderr=f"invalid PCI id {pci_id!r}; expected DDDD:BB:SS.F",
            returncode=1,
        )
    xml = (
        "<hostdev mode='subsystem' type='pci' managed='yes'>"
        "  <source>"
        f"    <address domain='0x{m.group('dom')}' bus='0x{m.group('bus')}'"
        f"     slot='0x{m.group('slot')}' function='0x{m.group('func')}'/>"
        "  </source>"
        "</hostdev>"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml)
        xml_path = f.name
    try:
        return _virsh("attach-device", vm, xml_path, "--config", check=False)
    finally:
        os.unlink(xml_path)
