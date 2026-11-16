"""libvirt backend: argv-shape + GPU-detect coverage.

These tests don't exercise real libvirt — they mock subprocess.run
and verify the toolkit emits the right virsh / virt-install / scp /
ssh argv. End-to-end correctness needs a Linux host with libvirt
installed (see GH #2 for the smoke-test runbook).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _force_linux_host(mocker: Any) -> None:
    """The libvirt module refuses to import on non-Linux hosts.
    Stub platform.system() before the import path runs.
    """
    mocker.patch("platform.system", return_value="Linux")


@pytest.fixture
def libvirt(mocker: Any, tmp_path: Path) -> Any:
    """Import the libvirt backend with a configured data dir.
    Returns the module object so tests can call its functions
    and inspect module-level state.
    """
    from agx_cli.backends import libvirt as mod

    mod.configure(tmp_path)
    return mod


def _mock_subprocess(
    mocker: Any, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> Any:
    """Replace subprocess.run with a recording stub."""
    from unittest.mock import MagicMock

    proc = MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)
    return mocker.patch("subprocess.run", return_value=proc)


# ---------- _virsh ----------


def test_virsh_prepends_qemu_system_uri(libvirt: Any, mocker: Any) -> None:
    """Every virsh call must go through the system URI — that's the
    one that supports GPU passthrough (session URI doesn't).
    """
    run_mock = _mock_subprocess(mocker)
    libvirt._virsh("list", "--all", check=False)
    argv = run_mock.call_args.args[0]
    assert argv[:4] == ["virsh", "-c", "qemu:///system", "list"]


# ---------- launch ----------


def test_launch_rejects_non_release_image(libvirt: Any) -> None:
    """``image`` for libvirt is an Ubuntu release version, not a
    multipass image name. Reject anything that doesn't match
    \\d+\\.\\d+ rather than silently downloading a wrong URL.
    """
    from agx_cli.errors import UserError

    with pytest.raises(UserError, match="Ubuntu release"):
        libvirt.launch("agents", image="lts", cpus=2, memory_gb=4, disk_gb=20)


def test_launch_emits_virt_install_with_expected_args(
    libvirt: Any, mocker: Any, tmp_path: Path
) -> None:
    """Verify virt-install argv shape: --import, --noautoconsole,
    correct memory unit (MiB), correct disk format, correct
    network model.
    """
    # Pre-populate the image so launch skips the download path.
    img_dir = tmp_path / ".images"
    img_dir.mkdir(parents=True)
    (img_dir / "ubuntu-24.04-server-cloudimg-amd64.img").write_bytes(b"")
    # Pre-populate the SSH key so launch skips ssh-keygen.
    key = tmp_path / ".ssh" / "id_ed25519"
    key.parent.mkdir(parents=True)
    key.write_text("priv")
    key.with_suffix(".pub").write_text("ssh-ed25519 AAA test")

    mocker.patch("platform.machine", return_value="x86_64")
    run_mock = _mock_subprocess(mocker)
    libvirt.launch("agents", image="24.04", cpus=4, memory_gb=8, disk_gb=25)

    # Find the virt-install invocation (one of multiple subprocess.run calls)
    virt_calls = [c for c in run_mock.call_args_list if c.args[0][0] == "virt-install"]
    assert virt_calls, "expected a virt-install subprocess call"
    argv = virt_calls[0].args[0]
    assert "--name" in argv and argv[argv.index("--name") + 1] == "agents"
    # Memory in MiB (8 GiB = 8192).
    assert argv[argv.index("--memory") + 1] == "8192"
    assert argv[argv.index("--vcpus") + 1] == "4"
    # virtio network model — better perf than e1000 default.
    assert any("model=virtio" in a for a in argv)
    # --import + --noautoconsole + --wait 0 = non-interactive launch.
    assert "--import" in argv
    assert "--noautoconsole" in argv
    assert "--wait" in argv


# ---------- exec / transfer ----------


def test_exec_in_vm_builds_ssh_argv(libvirt: Any, mocker: Any, tmp_path: Path) -> None:
    """SSH command must include -i <key>, -o StrictHostKeyChecking=no,
    -o UserKnownHostsFile=/dev/null, and use ubuntu@<ip>.
    """
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("k")

    # Mock _vm_ip to return a fixed IP without going through virsh.
    mocker.patch("agx_cli.backends.libvirt._vm_ip", return_value="192.168.122.50")
    run_mock = _mock_subprocess(mocker)

    libvirt.exec_in_vm("agents", "uptime", check=False)
    argv = run_mock.call_args.args[0]
    assert argv[0] == "ssh"
    assert "ubuntu@192.168.122.50" in argv
    assert "-i" in argv
    assert "StrictHostKeyChecking=no" in argv
    assert "UserKnownHostsFile=/dev/null" in argv
    # The user command comes after the `--` separator.
    assert "uptime" in argv[argv.index("--") + 1 :]


def test_transfer_to_vm_uses_scp(libvirt: Any, mocker: Any, tmp_path: Path) -> None:
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("k")
    mocker.patch("agx_cli.backends.libvirt._vm_ip", return_value="192.168.122.50")
    run_mock = _mock_subprocess(mocker)

    libvirt.transfer_to_vm(tmp_path / "src.txt", "agents", "/dst/path.txt")
    argv = run_mock.call_args.args[0]
    assert argv[0] == "scp"
    assert any(a.endswith("src.txt") for a in argv)
    assert "ubuntu@192.168.122.50:/dst/path.txt" in argv


# ---------- snapshot ----------


def test_snapshot_creates_internal_revertible_snapshot(libvirt: Any, mocker: Any) -> None:
    """Internal (qcow2-internal) snapshots, NOT --disk-only.

    Earlier versions used --disk-only for external snapshots,
    but those CAN'T be reverted via snapshot-revert — they're
    backup points only. Worse, they mutate the disk backing chain
    and break VM restart afterwards (found in a real-world smoke
    test). Revertible semantics match what
    vm.upgrade's rollback flow assumes.
    """
    run_mock = _mock_subprocess(mocker)
    libvirt.snapshot("agents", "pre-upgrade-2026-05-15", comment="auto")
    argv = run_mock.call_args.args[0]
    assert "snapshot-create-as" in argv
    assert "--disk-only" not in argv  # explicitly NOT --disk-only
    assert "--description" in argv


def test_delete_snapshot_argv(libvirt: Any, mocker: Any) -> None:
    run_mock = _mock_subprocess(mocker)
    libvirt.delete_snapshot("agents", "pre-upgrade-coder-20260512T000000Z")
    argv = run_mock.call_args.args[0]
    assert argv[:3] == ["virsh", "-c", "qemu:///system"]
    assert argv[3:] == ["snapshot-delete", "agents", "pre-upgrade-coder-20260512T000000Z"]


def test_delete_snapshot_refuses_empty_name(libvirt: Any, mocker: Any) -> None:
    run_mock = _mock_subprocess(mocker)
    result = libvirt.delete_snapshot("agents", "")
    assert result.returncode != 0
    run_mock.assert_not_called()


def test_restore_requires_dot_separated_id(libvirt: Any, mocker: Any) -> None:
    """Multipass uses ``<vm>.<name>``; toolkit callers pass the same.
    Refuse anything else cleanly rather than calling virsh with
    nonsense args.
    """
    result = libvirt.restore("just-a-name")
    assert result.returncode != 0
    assert "must be" in result.stderr


# ---------- GPU detect ----------


def test_detect_gpus_parses_lspci_output(libvirt: Any, mocker: Any) -> None:
    """lspci -nn -D output should yield one entry per supported-
    vendor GPU. Verify vendor mapping (10de → NVIDIA, 1002 → AMD,
    8086 → Intel) and PCI ID extraction.
    """
    sample = (
        "0000:01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA102 [GeForce RTX 3090] [10de:2204]\n"
        "0000:01:00.1 Audio device [0403]: NVIDIA Corporation GA102 HDA [10de:1aef]\n"  # not a GPU
        "0000:09:00.0 3D controller [0302]: Advanced Micro Devices [AMD/ATI] Radeon Foo [1002:7340]\n"
        "0000:02:00.0 VGA compatible controller [0300]: Other Vendor [dead:beef]\n"  # filtered
    )
    mocker.patch("shutil.which", return_value="/usr/bin/lspci")
    _mock_subprocess(mocker, stdout=sample.encode())
    # iommu_group and vfio_bound checks read from /sys — stub to no.
    mocker.patch("pathlib.Path.exists", return_value=False)

    gpus = libvirt.detect_gpus()
    pci_ids = [g["pci_id"] for g in gpus]
    vendor_names = {g["vendor_name"] for g in gpus}
    assert "0000:01:00.0" in pci_ids
    assert "0000:09:00.0" in pci_ids
    # Audio device is filtered (not VGA/3D/Display)
    assert "0000:01:00.1" not in pci_ids
    # Unknown vendor filtered
    assert "0000:02:00.0" not in pci_ids
    assert vendor_names == {"NVIDIA", "AMD"}


def test_detect_gpus_returns_empty_when_lspci_missing(libvirt: Any, mocker: Any) -> None:
    """Hosts without lspci installed should yield [], not raise.
    Doctor + the autodetect codepath both gate on the empty list.
    """
    mocker.patch("shutil.which", return_value=None)
    assert libvirt.detect_gpus() == []


def test_attach_gpu_xml_validates_pci_format(libvirt: Any) -> None:
    """PCI IDs that don't match DDDD:BB:SS.F must be refused
    BEFORE building the XML — silently writing bad XML to a temp
    file and feeding it to virsh would give a less useful error.
    """
    result = libvirt.attach_gpu_xml("agents", "not-a-pci-id")
    assert result.returncode != 0
    assert "invalid PCI id" in result.stderr


# ---------- set_config dispatch ----------


def test_set_config_translates_local_cpus_to_setvcpus(libvirt: Any, mocker: Any) -> None:
    """multipass set ``local.agents.cpus=4`` should map to
    ``virsh setvcpus agents 4 --config``.
    """
    run_mock = _mock_subprocess(mocker)
    libvirt.set_config("local.agents.cpus", 4)
    argv = run_mock.call_args.args[0]
    assert "setvcpus" in argv
    assert "agents" in argv
    assert "4" in argv
    assert "--config" in argv


def test_set_config_translates_memory_to_setmaxmem_kib(libvirt: Any, mocker: Any) -> None:
    """``local.agents.memory=8G`` → both setmaxmem and setmem in KiB
    (8 GiB = 8388608 KiB). Both calls so the VM can actually use the
    new max.
    """
    run_mock = _mock_subprocess(mocker)
    libvirt.set_config("local.agents.memory", "8G")
    cmds = [c.args[0] for c in run_mock.call_args_list]
    assert any("setmaxmem" in c and "8388608" in c for c in cmds)
    assert any("setmem" in c and "8388608" in c for c in cmds)


def test_set_config_rejects_unknown_keys(libvirt: Any) -> None:
    result = libvirt.set_config("unrelated.key", "value")
    assert result.returncode != 0
    assert "unsupported" in result.stderr


# ---------- Linux-only enforcement ----------


def test_import_refuses_on_non_linux(mocker: Any) -> None:
    """libvirt.py raises UserError at import time on non-Linux to
    avoid letting cli.main proceed and fail later.
    """
    import importlib
    import sys

    from agx_cli.errors import UserError

    mocker.patch("platform.system", return_value="Darwin")
    # Force re-import.
    sys.modules.pop("agx_cli.backends.libvirt", None)
    with pytest.raises(UserError, match="Linux-only"):
        importlib.import_module("agx_cli.backends.libvirt")


def test_virtiofs_tag_fits_qemu_limit_and_keeps_legacy_short_tags() -> None:
    from agx_cli.backends import libvirt as libvirt_mod

    # The data-dir share keeps its readable tag (already attached on live VMs).
    assert libvirt_mod._virtiofs_tag("/home/ubuntu/agents") == "home_ubuntu_agents"
    # `agx mount` paths exceed QEMU's 36-byte cap in the legacy form.
    long_path = "/home/ubuntu/host-extras/itest-a45f00d1/0123abcd-somefolder"
    tag = libvirt_mod._virtiofs_tag(long_path)
    assert len(tag.encode()) <= libvirt_mod.VIRTIOFS_TAG_MAX
    assert tag == libvirt_mod._virtiofs_tag(long_path)  # mount/umount agree
    assert tag != libvirt_mod._virtiofs_tag(long_path + "x")


def test_filesystem_element_for_tag_carries_source_for_detach() -> None:
    from agx_cli.backends import libvirt as libvirt_mod

    domain = (
        "<domain><devices>"
        "<filesystem type='mount' accessmode='passthrough'><driver type='virtiofs'/>"
        "<source dir='/home/u/agents'/><target dir='home_ubuntu_agents'/></filesystem>"
        "<filesystem type='mount' accessmode='passthrough'><driver type='virtiofs'/>"
        "<source dir='/srv/shared'/><target dir='agx-abc'/></filesystem>"
        "</devices></domain>"
    )
    el = libvirt_mod._filesystem_element_for_tag(domain, "agx-abc")
    assert el is not None
    assert 'dir="/srv/shared"' in el and 'dir="agx-abc"' in el
    assert "home_ubuntu_agents" not in el
    assert libvirt_mod._filesystem_element_for_tag(domain, "missing") is None


# ---------- verify_host: preflight before vm init ----------


def _stub_host(
    mocker: Any,
    libvirt: Any,
    *,
    missing: tuple[str, ...] = (),
    virtiofsd: bool = True,
    kvm: bool = True,
    uri_rc: int = 0,
    net_active: bool = True,
) -> Any:
    from agx_cli.backends import MpResult

    mocker.patch("shutil.which", side_effect=lambda c: None if c in missing else f"/usr/bin/{c}")
    present = {p for p, ok in (("/usr/libexec/virtiofsd", virtiofsd), ("/dev/kvm", kvm)) if ok}
    mocker.patch("pathlib.Path.exists", lambda self: str(self) in present)

    def virsh(*args: str, check: bool = True, timeout: float | None = None) -> MpResult:
        if args[0] == "uri":
            err = (
                "" if uri_rc == 0 else "error: failed to connect\nerror: authentication unavailable"
            )
            return MpResult(stdout="qemu:///system\n", stderr=err, returncode=uri_rc)
        state = "yes" if net_active else "no"
        return MpResult(
            stdout=f"Name:  default\nActive:         {state}\n", stderr="", returncode=0
        )

    return mocker.patch.object(libvirt, "_virsh", side_effect=virsh)


def test_verify_host_passes_on_a_complete_host(mocker: Any, libvirt: Any) -> None:
    virsh = _stub_host(mocker, libvirt)
    libvirt.verify_host()
    assert [c.args[0] for c in virsh.call_args_list] == ["uri", "net-info"]


def test_verify_host_lists_every_gap_of_a_fresh_host(mocker: Any, libvirt: Any) -> None:
    from agx_cli.errors import UserError

    virsh = _stub_host(
        mocker,
        libvirt,
        missing=("virsh", "virt-install", "cloud-localds", "virtiofsd"),
        virtiofsd=False,
        kvm=False,
    )
    with pytest.raises(UserError) as exc:
        libvirt.verify_host()
    msg = str(exc.value)
    assert "commands not found: virsh, virt-install, cloud-localds" in msg
    assert "virtiofsd not found" in msg
    assert "/dev/kvm is missing" in msg
    assert "sudo apt install libvirt-clients" in msg and "virtiofsd" in msg
    virsh.assert_not_called()  # no daemon probe without virsh


def test_verify_host_names_the_libvirt_group_when_the_daemon_refuses(
    mocker: Any, libvirt: Any
) -> None:
    from agx_cli.errors import UserError

    _stub_host(mocker, libvirt, uri_rc=1)
    with pytest.raises(UserError, match="libvirt group") as exc:
        libvirt.verify_host()
    assert "failed to connect" in str(exc.value)


def test_verify_host_flags_an_inactive_default_network(mocker: Any, libvirt: Any) -> None:
    from agx_cli.errors import UserError

    _stub_host(mocker, libvirt, net_active=False)
    with pytest.raises(UserError, match="net-start default"):
        libvirt.verify_host()


@pytest.mark.parametrize(
    ("machine", "qemu"),
    [("x86_64", "qemu-system-x86 ovmf"), ("aarch64", "qemu-system-arm qemu-efi-aarch64")],
)
def test_host_packages_match_the_architecture(
    mocker: Any, libvirt: Any, machine: str, qemu: str
) -> None:
    mocker.patch("platform.machine", return_value=machine)
    assert qemu in libvirt._host_packages()
