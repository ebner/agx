"""mp.py: argv construction and BOOTSTRAP_OK parser coverage."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agx_cli import mp
from agx_cli.errors import RuntimeFailure
from agx_cli.mp import BootstrapStatus, parse_bootstrap_ok

# ---------- argv quoting ----------


def _stub_run(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def test_exec_in_vm_uses_dashdash_separator(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.exec_in_vm("agents", "docker", "ps", "-a")
    args, kwargs = subprocess.run.call_args
    assert args[0] == ["multipass", "exec", "agents", "--", "docker", "ps", "-a"]
    assert kwargs.get("input") is None


def test_exec_bash_pipes_script_via_stdin(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.exec_bash("agents", "echo hi && id")
    args, kwargs = subprocess.run.call_args
    assert args[0] == ["multipass", "exec", "agents", "--", "bash", "-s"]
    assert kwargs["input"] == b"echo hi && id"


def test_transfer_to_vm_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.transfer_to_vm(Path("/tmp/x"), "agents", "/home/ubuntu/x")
    args, _ = subprocess.run.call_args
    assert args[0] == ["multipass", "transfer", "/tmp/x", "agents:/home/ubuntu/x"]


def test_transfer_from_vm_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.transfer_from_vm("agents", "/home/ubuntu/x", Path("/tmp/x"))
    args, _ = subprocess.run.call_args
    assert args[0] == ["multipass", "transfer", "agents:/home/ubuntu/x", "/tmp/x"]


def test_mount_includes_uid_gid_mapping_and_native_type(mocker) -> None:
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
    args, _ = subprocess.run.call_args
    # `--type native` is load-bearing: classic / fuse.sshfs mounts cannot
    # be bind-mounted into a rootless Docker container (operation not
    # supported), which breaks every agent on first start.
    assert args[0] == [
        "multipass",
        "mount",
        "--type",
        "native",
        "-u",
        "1001:1000",
        "-g",
        "1001:1000",
        "/data/agents",
        "agents:/home/ubuntu/agents",
    ]


def test_mount_type_is_overridable(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.mount(
        Path("/data/agents"),
        "agents",
        "/home/ubuntu/agents",
        host_uid=1000,
        host_gid=1000,
        mount_type="classic",
    )
    args, _ = subprocess.run.call_args
    assert "classic" in args[0]


def test_launch_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.launch("agents", image="26.04", cpus=4, memory_gb=8, disk_gb=25)
    args, _ = subprocess.run.call_args
    assert args[0] == [
        "multipass",
        "launch",
        "--name",
        "agents",
        "--cpus",
        "4",
        "--memory",
        "8G",
        "--disk",
        "25G",
        "26.04",
    ]


def test_snapshot_with_comment(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.snapshot("agents", "pre-upgrade-coder-20260512", comment="upgrade rollback point")
    args, _ = subprocess.run.call_args
    assert args[0] == [
        "multipass",
        "snapshot",
        "agents",
        "--name",
        "pre-upgrade-coder-20260512",
        "--comment",
        "upgrade rollback point",
    ]


def test_restore_argv(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.restore("agents.pre-upgrade-coder-20260512", destructive=True)
    args, _ = subprocess.run.call_args
    assert args[0] == [
        "multipass",
        "restore",
        "agents.pre-upgrade-coder-20260512",
        "--destructive",
    ]


def test_delete_with_purge(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.delete_vm("agents", purge=True)
    args, _ = subprocess.run.call_args
    assert args[0] == ["multipass", "delete", "agents", "--purge"]


def test_delete_snapshot_argv_uses_dotted_form(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.delete_snapshot("agents", "pre-upgrade-coder-20260512")
    args, _ = subprocess.run.call_args
    assert args[0] == ["multipass", "delete", "--purge", "agents.pre-upgrade-coder-20260512"]


def test_delete_snapshot_failure_returns_result_instead_of_raising(mocker) -> None:
    """Prune treats snapshot deletion as best-effort and inspects the
    MpResult — a check=True raise on the first undeletable snapshot
    would abort the rest of the cleanup pass (codex review 2026-08-24)."""
    mocker.patch("subprocess.run", return_value=_stub_run(returncode=1, stderr=b"not found"))
    result = mp.delete_snapshot("agents", "ghost-snap")
    assert result.returncode == 1


def test_delete_snapshot_refuses_empty_name(mocker) -> None:
    """`multipass delete --purge agents` (no snapshot part) would destroy
    the VM — an empty name must never reach the binary."""
    run = mocker.patch("subprocess.run", return_value=_stub_run())
    result = mp.delete_snapshot("agents", "")
    assert result.returncode != 0
    run.assert_not_called()


def test_find_with_show_unsupported(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run())
    mp.find(show_unsupported=True)
    args, _ = subprocess.run.call_args
    assert args[0] == ["multipass", "find", "--format", "json", "--show-unsupported"]


# ---------- subprocess failure handling ----------


def test_nonzero_exit_raises_runtime_failure(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run(returncode=1, stderr=b"boom"))
    with pytest.raises(RuntimeFailure, match="boom"):
        mp.mp("nope")


def test_nonzero_exit_with_check_false_returns_result(mocker) -> None:
    mocker.patch("subprocess.run", return_value=_stub_run(returncode=2, stderr=b"x"))
    result = mp.mp("nope", check=False)
    assert result.returncode == 2
    assert result.stderr == "x"


def test_command_not_found_raises_runtime_failure(mocker) -> None:
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("no such file: multipass"))
    with pytest.raises(RuntimeFailure, match="command not found"):
        mp.mp("info", "agents")


def test_timeout_raises_runtime_failure(mocker) -> None:
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["multipass"], timeout=1.0),
    )
    with pytest.raises(RuntimeFailure, match="timed out"):
        mp.mp("info", "agents", timeout=1.0)


def test_non_utf8_stdout_decoded_with_replacement(mocker) -> None:
    bad = b"\xff\xfe ok bytes"
    mocker.patch("subprocess.run", return_value=_stub_run(stdout=bad))
    result = mp.mp("anything")
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
