"""bare_metal backend: thin-wrapper + verify-bootstrap coverage.

The backend mostly delegates to local subprocess + shutil, so the
tests focus on:

- Argv-shape correctness (we don't accidentally prepend ``multipass``
  or other VM-isms)
- Synthesized JSON payloads for ``info_vm`` / ``list_vms`` matching
  the multipass shape downstream consumers expect
- ``verify_bootstrap`` emitting a parseable ``BOOTSTRAP_OK`` line with
  per-field status flipping based on observable host state
- Non-Linux refusal (Linux-only per the locked design)
- Snapshot / restore refusal (host duplicity is the back-up story)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _force_linux_host(mocker: Any) -> None:
    mocker.patch("platform.system", return_value="Linux")


@pytest.fixture
def bare_metal(mocker: Any) -> Any:
    from agents_cli.backends import bare_metal as mod

    return mod


def _ok_proc(stdout: bytes = b"", stderr: bytes = b"") -> Any:
    return MagicMock(returncode=0, stdout=stdout, stderr=stderr)


def _fail_proc(rc: int = 1, stderr: bytes = b"") -> Any:
    return MagicMock(returncode=rc, stdout=b"", stderr=stderr)


# ---------- exec ----------


def test_exec_in_vm_runs_locally_ignoring_vm_arg(bare_metal: Any, mocker: Any) -> None:
    """The ``vm`` arg is part of the Protocol but bare-metal ignores
    it: the command must run on the host with no prefix.
    """
    run_mock = mocker.patch("subprocess.run", return_value=_ok_proc())
    bare_metal.exec_in_vm("ignored", "echo", "hi")
    argv = run_mock.call_args.args[0]
    assert argv == ["echo", "hi"]
    assert "multipass" not in argv and "ssh" not in argv


def test_exec_bash_pipes_script_to_bash_s(bare_metal: Any, mocker: Any) -> None:
    """Multi-line scripts must reach ``bash -s`` on stdin, never as
    a positional arg — otherwise we'd hit quoting hell.
    """
    run_mock = mocker.patch("subprocess.run", return_value=_ok_proc())
    bare_metal.exec_bash("ignored", "echo hi\n")
    call = run_mock.call_args
    assert call.args[0] == ["bash", "-s"]
    assert call.kwargs["input"] == b"echo hi\n"


def test_exec_in_vm_with_stdin_pipes_bytes(bare_metal: Any, mocker: Any) -> None:
    run_mock = mocker.patch("subprocess.run", return_value=_ok_proc())
    bare_metal.exec_in_vm_with_stdin("ignored", "tee", "/tmp/x", input_bytes=b"hello")
    assert run_mock.call_args.args[0] == ["tee", "/tmp/x"]
    assert run_mock.call_args.kwargs["input"] == b"hello"


def test_exec_in_vm_interactive_returns_rc_and_inherits_stdio(bare_metal: Any, mocker: Any) -> None:
    """Interactive variant must NOT capture stdio — otherwise wizards
    can't prompt. The signature returns the rc directly, not an MpResult.
    """
    run_mock = mocker.patch("subprocess.run", return_value=MagicMock(returncode=7))
    rc = bare_metal.exec_in_vm_interactive("ignored", "bash", "-l")
    assert rc == 7
    assert "capture_output" not in run_mock.call_args.kwargs


# ---------- transfers ----------


def test_transfer_to_vm_copies_file(bare_metal: Any, tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("payload", encoding="utf-8")
    dst = tmp_path / "subdir" / "dst.txt"  # parent doesn't exist yet
    result = bare_metal.transfer_to_vm(src, "ignored", str(dst))
    assert result.returncode == 0
    assert dst.read_text(encoding="utf-8") == "payload"


def test_transfer_from_vm_copies_file(bare_metal: Any, tmp_path: Path) -> None:
    src = tmp_path / "vm.txt"
    src.write_text("payload", encoding="utf-8")
    dst = tmp_path / "out" / "host.txt"
    result = bare_metal.transfer_from_vm("ignored", str(src), dst)
    assert result.returncode == 0
    assert dst.read_text(encoding="utf-8") == "payload"


# ---------- no-op mounts ----------


def test_mount_is_no_op_with_ok_rc(bare_metal: Any, tmp_path: Path) -> None:
    """The data-dir IS the host path in bare-metal mode, so the toolkit's
    ``_ensure_mount`` plumbing collapses to a no-op."""
    result = bare_metal.mount(tmp_path, "ignored", "/anything", host_uid=1000, host_gid=1000)
    assert result.returncode == 0


def test_umount_is_no_op_with_ok_rc(bare_metal: Any) -> None:
    assert bare_metal.umount("ignored", "/anything").returncode == 0


# ---------- lifecycle ----------


def test_launch_is_no_op(bare_metal: Any) -> None:
    """``vm.init`` branches around launch on bare-metal, but if anyone
    calls into it directly (e.g. via the Protocol), it must succeed
    without trying to invoke virt-install or similar."""
    result = bare_metal.launch("ignored", image="x", cpus=1, memory_gb=1, disk_gb=1)
    assert result.returncode == 0


def test_start_stop_set_autostart_are_no_ops(bare_metal: Any) -> None:
    assert bare_metal.start_vm("ignored").returncode == 0
    assert bare_metal.stop_vm("ignored").returncode == 0
    assert bare_metal.set_autostart("ignored", True).returncode == 0
    assert bare_metal.set_config("foo", "bar").returncode == 0


def test_delete_vm_is_a_pure_no_op(bare_metal: Any, mocker: Any) -> None:
    """No VM to delete and no side effects: ``vm.destroy`` branches early
    on bare-metal (compose-down happens there, via compose_runner), so
    this Protocol stub must not run anything — in particular no docker
    or compose commands.
    """
    run_mock = mocker.patch("subprocess.run")
    result = bare_metal.delete_vm("ignored", purge=True)
    assert result.returncode == 0
    run_mock.assert_not_called()


def test_info_vm_synthesizes_multipass_shape(bare_metal: Any) -> None:
    """``vm.status`` parses this JSON and expects ``info.<name>.state``
    + ``mounts``. The synthesized shape must satisfy that consumer
    or the bare-metal status output is broken.
    """
    result = bare_metal.info_vm("agents")
    payload = json.loads(result.stdout)
    assert "info" in payload
    entry = payload["info"]["agents"]
    assert entry["state"] == "Running"
    assert "mounts" in entry
    assert isinstance(entry.get("ipv4"), list)


def test_list_vms_returns_one_running_entry(bare_metal: Any) -> None:
    result = bare_metal.list_vms()
    payload = json.loads(result.stdout)
    names = [v["name"] for v in payload["list"]]
    assert names == ["agents"]
    assert payload["list"][0]["state"] == "Running"


def test_find_raises(bare_metal: Any) -> None:
    with pytest.raises(NotImplementedError):
        bare_metal.find()


def test_snapshot_refuses_with_helpful_hint(bare_metal: Any) -> None:
    from agents_cli.errors import UserError

    with pytest.raises(UserError, match="agents export"):
        bare_metal.snapshot("agents", "pre-upgrade")


def test_restore_refuses_with_helpful_hint(bare_metal: Any) -> None:
    from agents_cli.errors import UserError

    with pytest.raises(UserError, match="agents import"):
        bare_metal.restore("agents.pre-upgrade")


# ---------- non-Linux refusal ----------


def test_launch_refuses_on_non_linux(bare_metal: Any, mocker: Any) -> None:
    mocker.patch("platform.system", return_value="Darwin")
    from agents_cli.errors import UserError

    with pytest.raises(UserError, match="Linux-only"):
        bare_metal.launch("ignored", image="x", cpus=1, memory_gb=1, disk_gb=1)


def test_verify_bootstrap_refuses_on_non_linux(
    bare_metal: Any, mocker: Any, tmp_path: Path
) -> None:
    mocker.patch("platform.system", return_value="Darwin")
    from agents_cli.errors import UserError

    with pytest.raises(UserError, match="Linux-only"):
        bare_metal.verify_bootstrap(tmp_path)


# ---------- verify_bootstrap ----------


def _patch_probes(
    mocker: Any,
    *,
    has_newuidmap: bool = True,
    socket_exists: bool = True,
    docker_version_ok: bool = True,
    systemctl_user_active: bool = True,
    net_ok: bool = True,
    linger_yes: bool = True,
    compose_plugin_ok: bool = True,
) -> Any:
    """Stub every probe verify_bootstrap calls into. Returns the
    subprocess.run mock so tests can inspect argv if they need to.
    """
    mocker.patch(
        "shutil.which",
        side_effect=lambda name: (
            "/usr/bin/" + name if (name != "newuidmap" or has_newuidmap) else None
        ),
    )
    mocker.patch("pathlib.Path.is_dir", return_value=True)

    def _path_exists(self: Path) -> bool:
        # Rootless socket presence + rootful socket absence (we don't
        # want to trip the rootful-Docker refusal in these tests).
        if str(self).endswith("docker.sock") and "/run/user" in str(self):
            return socket_exists
        return str(self) != "/var/run/docker.sock"

    mocker.patch("pathlib.Path.exists", _path_exists)

    def run_side(argv: list[str], **kwargs: Any) -> Any:
        text = kwargs.get("text", False)
        if argv[:1] == ["systemctl"] and "--version" in argv:
            return _ok_proc()
        if argv[:1] == ["loginctl"]:
            stdout = "Linger=yes" if linger_yes else "Linger=no"
            return MagicMock(
                returncode=0,
                stdout=stdout if text else stdout.encode(),
                stderr="" if text else b"",
            )
        if argv[:3] == ["docker", "version", "--format"]:
            return _ok_proc() if docker_version_ok else _fail_proc()
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            stdout = "active" if systemctl_user_active else "inactive"
            return MagicMock(
                returncode=0 if systemctl_user_active else 3,
                stdout=stdout if text else stdout.encode(),
                stderr="" if text else b"",
            )
        if argv[:1] == ["curl"]:
            return _ok_proc() if net_ok else _fail_proc()
        if argv[:3] == ["docker", "compose", "version"]:
            return _ok_proc() if compose_plugin_ok else _fail_proc()
        return _ok_proc()

    return mocker.patch("subprocess.run", side_effect=run_side)


def test_verify_bootstrap_emits_parseable_ok_when_all_probes_pass(
    bare_metal: Any, mocker: Any, tmp_path: Path
) -> None:
    """Happy path: every probe succeeds → BOOTSTRAP_OK line has every
    field=ok and is_ok() returns True.
    """
    _patch_probes(mocker)
    status, stdout, _ = bare_metal.verify_bootstrap(tmp_path)
    assert "BOOTSTRAP_OK" in stdout
    assert status.is_ok(), f"reasons: {status.fail_reasons()}"
    # uid/gid/socket are populated from the real host user, but the
    # values must be present and non-zero.
    assert status.ubuntu_uid() > 0
    assert status.socket_path().startswith("/run/user/")


def test_verify_bootstrap_flags_failures_per_field(
    bare_metal: Any, mocker: Any, tmp_path: Path
) -> None:
    """When daemon/net fail, the handshake reports per-field 'fail'
    and stderr carries the apt-install hint."""
    _patch_probes(mocker, docker_version_ok=False, net_ok=False)
    status, _, _ = bare_metal.verify_bootstrap(tmp_path)
    assert not status.is_ok()
    reasons = status.fail_reasons()
    assert "daemon=fail" in reasons
    assert "net=fail" in reasons


def test_verify_bootstrap_hints_at_uidmap_install_when_missing(
    bare_metal: Any, mocker: Any, tmp_path: Path
) -> None:
    """Locked decision: verify-only — surface the exact `apt install`
    command instead of running sudo on the user's behalf.
    """
    _patch_probes(mocker, has_newuidmap=False)
    _, _, stderr = bare_metal.verify_bootstrap(tmp_path)
    assert "uidmap" in stderr
    assert "sudo apt install uidmap" in stderr


def test_verify_bootstrap_hints_at_rootless_install_when_socket_missing(
    bare_metal: Any, mocker: Any, tmp_path: Path
) -> None:
    """No rootless socket → the toolkit refuses to silently apt-install
    Docker. Surface the upstream install command and refuse.
    """
    _patch_probes(mocker, socket_exists=False, docker_version_ok=False)
    status, _, stderr = bare_metal.verify_bootstrap(tmp_path)
    assert "docker_host=fail" in status.fail_reasons()
    assert "get.docker.com/rootless" in stderr
