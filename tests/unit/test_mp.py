"""mp.py: argv construction and BOOTSTRAP_OK parser coverage.

The module shells out to ``incus`` (was multipass pre-v0.2). Tests
assert exact argv shapes — argv quoting is where security issues hide
and where backend migrations break in subtle ways.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents_cli import mp
from agents_cli.errors import RuntimeFailure
from agents_cli.mp import BootstrapStatus, parse_bootstrap_ok

# ---------- argv quoting ----------


def _stub_run(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def test_exec_in_vm_runs_as_ubuntu(mocker) -> None:
    """exec must default to ubuntu/uid 1000 so the bootstrap script
    (which uses ``sudo`` for elevation) works unchanged, and so files
    written via the share round-trip with sensible ownership. PATH is
    pinned in the env because ``incus exec`` doesn't read
    ``/etc/environment`` — without it, the rootless docker binary at
    ``/home/ubuntu/bin/docker`` isn't found.
    """
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.exec_in_vm("agents", "docker", "ps", "-a")
    args, kwargs = subprocess.run.call_args
    assert args[0] == [
        "incus",
        "exec",
        "--user",
        "1000",
        "--group",
        "1000",
        "--cwd",
        "/home/ubuntu",
        "--env",
        "HOME=/home/ubuntu",
        "--env",
        mp.VM_USER_PATH,
        "agents",
        "--",
        "docker",
        "ps",
        "-a",
    ]
    assert "PATH=/home/ubuntu/bin:" in mp.VM_USER_PATH
    assert kwargs.get("input") is None


def test_exec_bash_pipes_script_via_stdin(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.exec_bash("agents", "echo hi && id")
    args, kwargs = subprocess.run.call_args
    # Same exec shape (ubuntu user, /home/ubuntu cwd, HOME + PATH env),
    # invoking `bash -s` so the script comes in over stdin.
    assert args[0][:13] == [
        "incus",
        "exec",
        "--user",
        "1000",
        "--group",
        "1000",
        "--cwd",
        "/home/ubuntu",
        "--env",
        "HOME=/home/ubuntu",
        "--env",
        mp.VM_USER_PATH,
        "agents",
    ]
    assert args[0][-3:] == ["--", "bash", "-s"]
    assert kwargs["input"] == b"echo hi && id"


def test_transfer_to_vm_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.transfer_to_vm(Path("/tmp/x"), "agents", "/home/ubuntu/x")
    args, _ = subprocess.run.call_args
    # Note: incus separates VM from path with no colon — `agents/home/ubuntu/x`
    # not `agents:/home/ubuntu/x` (which is the multipass syntax).
    # --create-dirs + --uid/--gid make file push idempotent under the
    # ubuntu user's ownership.
    assert args[0] == [
        "incus",
        "file",
        "push",
        "--create-dirs",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "/tmp/x",
        "agents/home/ubuntu/x",
    ]


def test_transfer_from_vm_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.transfer_from_vm("agents", "/home/ubuntu/x", Path("/tmp/x"))
    args, _ = subprocess.run.call_args
    assert args[0] == [
        "incus",
        "file",
        "pull",
        "agents/home/ubuntu/x",
        "/tmp/x",
    ]


def test_mount_attaches_disk_device(mocker) -> None:
    """mount adds a virtiofs-backed disk device (Incus auto-selects
    virtiofs when ``virtiofsd`` is installed — a hard prereq).

    No raw.idmap is set — see the comment in ``mp.launch`` for why.
    host_uid/gid are accepted in the signature for forward-compat.
    """
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.mount(
        Path("/data/agents"),
        "agents",
        "/home/ubuntu/agents",
        host_uid=1001,
        host_gid=1001,
        vm_uid=1000,
        vm_gid=1000,
    )
    calls = subprocess.run.call_args_list
    assert len(calls) == 1, "no raw.idmap call should fire"
    assert calls[0].args[0] == [
        "incus",
        "config",
        "device",
        "add",
        "agents",
        "agents-data",
        "disk",
        "source=/data/agents",
        "path=/home/ubuntu/agents",
    ]


def test_umount_removes_device(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.umount("agents", "/home/ubuntu/agents")
    args, _ = subprocess.run.call_args
    assert args[0] == ["incus", "config", "device", "remove", "agents", "agents-data"]


def test_launch_no_mount_argv(mocker) -> None:
    """Without host-share args the YAML config only sets sizing — caller
    is expected to attach a mount later via ``mp.mount``.
    """
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.launch("agents", image="images:ubuntu/24.04", cpus=4, memory_gb=8, disk_gb=25)
    args, kwargs = subprocess.run.call_args
    assert args[0] == [
        "incus",
        "launch",
        "-q",
        "images:ubuntu/24.04",
        "agents",
        "--vm",
    ]
    yaml = kwargs["input"].decode()
    assert 'limits.cpu: "4"' in yaml
    assert "limits.memory: 8GiB" in yaml
    assert "size: 25GiB" in yaml
    assert "raw.idmap" not in yaml
    assert "agents-data" not in yaml


def test_launch_with_share_wires_disk_device_in_yaml(mocker) -> None:
    """When host_data_dir + host_uid/gid are passed, the launch config
    includes a disk device entry so the VM comes up with the share
    already attached (no stop-mount-start dance).

    raw.idmap is deliberately NOT emitted — see the long-form comment in
    mp.launch for why. host_uid/gid are accepted in the signature for
    forward-compat (if Incus ever gains range-idmap support and we want
    to re-enable idmapping).
    """
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.launch(
        "agents",
        image="images:ubuntu/24.04",
        cpus=4,
        memory_gb=8,
        disk_gb=25,
        host_data_dir=Path("/home/user/agents"),
        vm_data_path="/home/ubuntu/agents",
        host_uid=1000,
        host_gid=1000,
    )
    _, kwargs = subprocess.run.call_args
    yaml = kwargs["input"].decode()
    assert "raw.idmap" not in yaml
    assert "agents-data:" in yaml
    assert "source: /home/user/agents" in yaml
    assert "path: /home/ubuntu/agents" in yaml


def test_snapshot_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.snapshot("agents", "pre-upgrade-coder-20260512", comment="upgrade rollback point")
    args, _ = subprocess.run.call_args
    # Incus snapshot has no comment field at the CLI; comment is
    # accepted by the helper for compat but quietly dropped.
    assert args[0] == [
        "incus",
        "snapshot",
        "create",
        "agents",
        "pre-upgrade-coder-20260512",
    ]


def test_restore_splits_multipass_style_snapshot_id(mocker) -> None:
    """Caller passes ``<vm>.<name>`` (the historical multipass form);
    we split internally and call ``incus snapshot restore <vm> <name>``.
    """
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.restore("agents.pre-upgrade-coder-20260512", destructive=True)
    args, _ = subprocess.run.call_args
    assert args[0] == [
        "incus",
        "snapshot",
        "restore",
        "agents",
        "pre-upgrade-coder-20260512",
    ]


def test_restore_rejects_unsplittable_id() -> None:
    with pytest.raises(RuntimeFailure, match=r"<vm>\.<name>"):
        mp.restore("no-dot-here")


def test_delete_argv(mocker) -> None:
    """``--force`` is required because the VM may be running. ``purge``
    is accepted for signature compat but is implicit on Incus delete.
    """
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.delete_vm("agents", purge=True)
    args, _ = subprocess.run.call_args
    assert args[0] == ["incus", "delete", "--force", "agents"]


def test_set_cpus_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.set_cpus("agents", 8)
    args, _ = subprocess.run.call_args
    assert args[0] == ["incus", "config", "set", "agents", "limits.cpu=8"]


def test_set_memory_gb_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.set_memory_gb("agents", 16)
    args, _ = subprocess.run.call_args
    assert args[0] == ["incus", "config", "set", "agents", "limits.memory=16GiB"]


def test_set_disk_gb_uses_device_set(mocker) -> None:
    """Disk sizing on Incus is ``config device set <vm> root size=...``,
    NOT ``config set``. Easy to get wrong.
    """
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.set_disk_gb("agents", 50)
    args, _ = subprocess.run.call_args
    assert args[0] == ["incus", "config", "device", "set", "agents", "root", "size=50GiB"]


# ---------- info_vm normalisation ----------


def test_info_vm_normalises_to_multipass_shape(mocker) -> None:
    """The legacy callers in vm.py / upgrades.py / ops.py parse the
    multipass shape (``{"info": {"<vm>": {"state": ..., "mounts": {...},
    "snapshots": {...}, "ipv4": [...]}}}``). The Incus rewrite reshapes
    server-side so those parsers don't change.
    """
    import json as _json

    incus_list_payload = _json.dumps(
        [
            {
                "name": "agents",
                "status": "Running",
                "devices": {
                    "root": {"type": "disk", "path": "/", "pool": "default"},
                    "agents-data": {
                        "type": "disk",
                        "source": "/home/user/agents",
                        "path": "/home/ubuntu/agents",
                    },
                },
                "state": {
                    "network": {
                        "eth0": {
                            "addresses": [
                                {"family": "inet", "scope": "global", "address": "10.0.0.5"},
                                {"family": "inet6", "scope": "link", "address": "fe80::1"},
                            ]
                        }
                    }
                },
            }
        ]
    )
    incus_snap_payload = _json.dumps(
        [{"name": "baseline", "description": "first snapshot", "created_at": "now"}]
    )

    call_outputs = [
        _stub_run(stdout=incus_list_payload.encode()),
        _stub_run(stdout=incus_snap_payload.encode()),
    ]
    mocker.patch("subprocess.run", side_effect=call_outputs)

    result = mp.info_vm("agents")
    normalised = _json.loads(result.stdout)
    vm_info = normalised["info"]["agents"]
    assert vm_info["state"] == "Running"
    # root volume is filtered out of the mounts dict (it's not a host share).
    assert "/home/ubuntu/agents" in vm_info["mounts"]
    assert "/" not in vm_info["mounts"]
    assert vm_info["mounts"]["/home/ubuntu/agents"]["source_path"] == "/home/user/agents"
    assert "baseline" in vm_info["snapshots"]
    # Only global-scope IPv4 addresses are surfaced.
    assert vm_info["ipv4"] == ["10.0.0.5"]


def test_list_vms_normalises_to_multipass_shape(mocker) -> None:
    import json as _json

    payload = _json.dumps(
        [
            {"name": "agents", "status": "Running"},
            {"name": "gh-runner", "status": "Stopped"},
        ]
    )
    mocker.patch("subprocess.run", return_value=_stub_run(stdout=payload.encode()))

    result = mp.list_vms()
    normalised = _json.loads(result.stdout)
    assert normalised == {
        "list": [
            {"name": "agents", "state": "Running"},
            {"name": "gh-runner", "state": "Stopped"},
        ]
    }


def test_find_returns_ubuntu_image_aliases_in_multipass_shape(mocker) -> None:
    """`_select_ubuntu_image` in vm.py iterates the multipass-flavoured
    ``{"images": {<fp>: {"aliases_no_remote": [...], "aliases": [...]}}}``
    shape looking for a release alias like ``"24.04"``. We synthesise
    that shape from incus's image list output.
    """
    import json as _json

    payload = _json.dumps(
        [
            {
                "fingerprint": "abc123",
                "aliases": [{"name": "ubuntu/24.04"}, {"name": "ubuntu/24.04/cloud"}],
            },
            {
                "fingerprint": "def456",
                "aliases": [{"name": "ubuntu/26.04"}],
            },
        ]
    )
    mocker.patch("subprocess.run", return_value=_stub_run(stdout=payload.encode()))

    result = mp.find()
    catalog = _json.loads(result.stdout)
    aliases: set[str] = set()
    for img in catalog["images"].values():
        aliases.update(img["aliases_no_remote"])
    assert "24.04" in aliases
    assert "26.04" in aliases


# ---------- subprocess failure handling ----------


def test_nonzero_exit_raises_runtime_failure(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run(returncode=1, stderr=b"boom"))
    with pytest.raises(RuntimeFailure, match="boom"):
        mp._incus("nope")


def test_nonzero_exit_with_check_false_returns_result(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run(returncode=2, stderr=b"x"))
    result = mp._incus("nope", check=False)
    assert result.returncode == 2
    assert result.stderr == "x"


def test_command_not_found_raises_runtime_failure(mocker) -> None:
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("no such file: incus"))
    with pytest.raises(RuntimeFailure, match="command not found"):
        mp._incus("info", "agents")


def test_timeout_raises_runtime_failure(mocker) -> None:
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["incus"], timeout=1.0),
    )
    with pytest.raises(RuntimeFailure, match="timed out"):
        mp._incus("info", "agents", timeout=1.0)


def test_non_utf8_stdout_decoded_with_replacement(mocker) -> None:
    bad = b"\xff\xfe ok bytes"
    mocker.patch("subprocess.run", return_value=_stub_run(stdout=bad))
    result = mp._incus("anything")
    assert "�" in result.stdout


# ---------- BOOTSTRAP_OK parser ----------


_GOOD_LINE = (
    "BOOTSTRAP_OK uidmap=ok dbus=ok lingering=ok docker_host=ok daemon=ok "
    "user_socket_active=ok net=ok ubuntu_uid=1000 ubuntu_gid=1000 "
    "socket_path=/run/user/1000/docker.sock"
)


def test_parse_good_line() -> None:
    status = parse_bootstrap_ok(f"some preamble\n{_GOOD_LINE}\n")
    assert status.is_ok()
    assert status.fail_reasons() == []
    assert status.socket_path() == "/run/user/1000/docker.sock"
    assert status.ubuntu_uid() == 1000
    assert status.ubuntu_gid() == 1000


def test_parse_missing_line_raises() -> None:
    with pytest.raises(RuntimeFailure, match="BOOTSTRAP_OK"):
        parse_bootstrap_ok("nothing useful here\n")


def test_parse_malformed_line_yields_empty_status() -> None:
    """A bare `BOOTSTRAP_OK` without fields is parsed but is_ok is False."""
    status = parse_bootstrap_ok("BOOTSTRAP_OK\n")
    assert not status.is_ok()
    assert "uidmap missing" in status.fail_reasons()


def test_parse_missing_required_field() -> None:
    line = _GOOD_LINE.replace(" lingering=ok", "")
    status = parse_bootstrap_ok(line)
    assert not status.is_ok()
    assert "lingering missing" in status.fail_reasons()


def test_parse_fail_field() -> None:
    line = _GOOD_LINE.replace("docker_host=ok", "docker_host=fail")
    status = parse_bootstrap_ok(line)
    assert not status.is_ok()
    assert "docker_host=fail" in status.fail_reasons()


def test_parse_extra_fields_are_forward_compatible() -> None:
    line = _GOOD_LINE + " future_check=ok another=stuff"
    status = parse_bootstrap_ok(line)
    assert status.is_ok()
    assert status.fields["future_check"] == "ok"
    assert status.fields["another"] == "stuff"


def test_parse_trailing_whitespace_stripped() -> None:
    status = parse_bootstrap_ok(f"   {_GOOD_LINE}   \n")
    assert status.is_ok()


def test_parse_non_utf8_garbage_around_line() -> None:
    # decode-with-replacement turns bad bytes into U+FFFD; we just need to
    # find the well-formed BOOTSTRAP_OK line on its own row.
    garbage = "� binary noise �\n"
    status = parse_bootstrap_ok(garbage + _GOOD_LINE + "\n")
    assert status.is_ok()


def test_parse_last_line_wins() -> None:
    earlier = _GOOD_LINE.replace("uidmap=ok", "uidmap=fail")
    status = parse_bootstrap_ok(earlier + "\n" + _GOOD_LINE + "\n")
    assert status.is_ok()  # the later good line takes precedence


def test_bootstrap_status_default_is_not_ok() -> None:
    assert not BootstrapStatus().is_ok()
