"""tailscale_cmd.py: VM-OS tailscaled management (install/join/status)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents_cli import tailscale_cmd
from agents_cli.config import (
    AgentsConfig,
    HostConfig,
    PathsConfig,
    VmConfig,
    VmRuntimeConfig,
)
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail(stderr: str = "boom", stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr=stderr, returncode=1)


def _make_cfg(tmp_path: Path, backend: str = "libvirt") -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return AgentsConfig(
        vm=VmConfig(backend=backend),
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    return _make_cfg(tmp_path)


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    from agents_cli import config as cfg_mod

    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


def _status_json(state: str = "Running") -> str:
    return json.dumps(
        {
            "BackendState": state,
            "Version": "1.86.2-t123",
            "Self": {"TailscaleIPs": ["100.64.0.7"], "DNSName": "agents.tail1234.ts.net."},
        }
    )


# ---------- backend gating ----------


def test_refuses_on_bare_metal(tmp_path: Path, mocker: Any) -> None:
    from agents_cli import config as cfg_mod

    cfg = _make_cfg(tmp_path, backend="bare_metal")
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    with pytest.raises(UserError, match="no VM"):
        tailscale_cmd.up(config_path=p)
    with pytest.raises(UserError, match="no VM"):
        tailscale_cmd.status(config_path=p)


# ---------- up ----------


def test_up_installs_joins_and_writes_state(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    bash = mocker.patch("agents_cli.mp.exec_bash", return_value=_ok("...\n1.86.2\n"))
    execs = mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=[
            _ok(),  # sudo tailscale up
            _ok(_status_json("Running")),  # tailscale status --json
        ],
    )
    tailscale_cmd.up(config_path=saved_config)

    assert "tailscale.com/install.sh" in bash.call_args.args[1]
    up_argv = execs.call_args_list[0].args
    assert up_argv[1:4] == ("sudo", "tailscale", "up")
    assert "--hostname=agents" in up_argv
    assert not any(a.startswith("--auth-key") for a in up_argv)
    state = tailscale_cmd.read_state(cfg)
    assert state["hostname"] == "agents"
    assert state["version"] == "1.86.2"


def test_up_auth_key_rides_stdin_never_argv(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """The auth key is a reusable credential and both backends echo the
    exec argv under --verbose — it must travel via stdin, not argv
    (codex review 2026-08-27)."""
    mocker.patch("agents_cli.mp.exec_bash", return_value=_ok("1.86.2\n"))
    stdin_exec = mocker.patch("agents_cli.mp.exec_in_vm_with_stdin", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(_status_json("Running")))
    tailscale_cmd.up(auth_key="tskey-abc", hostname="agents-lab", config_path=saved_config)
    call = stdin_exec.call_args
    assert all("tskey-abc" not in str(a) for a in call.args)
    assert call.kwargs["input_bytes"] == b"tskey-abc\n"
    assert any("--hostname=agents-lab" in str(a) for a in call.args)


def test_up_failed_rc_not_masked_by_already_running(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """An already-connected VM reports Running even when THIS `tailscale
    up` failed (bad hostname/key) — rc must gate the success path."""
    import pytest as _pytest

    from agents_cli.errors import RuntimeFailure as _RF

    mocker.patch("agents_cli.mp.exec_bash", return_value=_ok("1.86.2\n"))
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=[
            _fail(stderr="backend error: invalid hostname"),
            _ok(_status_json("Running")),
        ],
    )
    with _pytest.raises(_RF):
        tailscale_cmd.up(hostname="Bad_Host", config_path=saved_config)


def test_up_without_auth_emits_login_url(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    mocker.patch("agents_cli.mp.exec_bash", return_value=_ok("1.86.2\n"))
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=[
            _fail(stderr="To authenticate, visit:\n\nhttps://login.tailscale.com/a/abc123\n"),
            _ok(_status_json("NeedsLogin")),
        ],
    )
    emitted: list[dict[str, Any]] = []
    mocker.patch("agents_cli.ui.emit", side_effect=lambda p, human=None: emitted.append(dict(p)))
    tailscale_cmd.up(config_path=saved_config)
    assert emitted[0]["auth_url"] == "https://login.tailscale.com/a/abc123"
    assert tailscale_cmd.read_state(cfg).get("pending_auth") is True


def test_up_raises_on_install_failure(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    mocker.patch("agents_cli.mp.exec_bash", return_value=_fail("apt exploded"))
    with pytest.raises(RuntimeFailure, match="install failed"):
        tailscale_cmd.up(config_path=saved_config)


# ---------- status ----------


def test_status_parses_running_state(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(_status_json("Running")))
    payload = tailscale_cmd.status(config_path=saved_config)
    assert payload["backend_state"] == "Running"
    assert payload["ips"] == ["100.64.0.7"]
    assert payload["dns_name"] == "agents.tail1234.ts.net"
    assert payload["version"] == "1.86.2"


def test_status_reports_not_installed(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        return_value=_fail("bash: tailscale: command not found"),
    )
    payload = tailscale_cmd.status(config_path=saved_config)
    assert payload["installed"] is False


def test_status_lists_non_loopback_published_ports(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services:\n"
        "  hermes-coder:\n"
        "    ports:\n"
        '      - "9119:9119"\n'
        '      - "127.0.0.1:8642:8642"\n',
        encoding="utf-8",
    )
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(_status_json("Running")))
    payload = tailscale_cmd.status(config_path=saved_config)
    assert payload["published_ports"] == ["9119"]


# ---------- down / upgrade ----------


def test_down_invokes_tailscale_down(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    execs = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    tailscale_cmd.down(config_path=saved_config)
    assert execs.call_args.args[1:] == ("sudo", "tailscale", "down")


def test_upgrade_requires_prior_up(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    with pytest.raises(UserError, match="tailscale up"):
        tailscale_cmd.upgrade(config_path=saved_config)


def test_upgrade_runs_apt_only_upgrade(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    tailscale_cmd.write_state(cfg, {"hostname": "agents", "version": "1.80.0"})
    bash = mocker.patch("agents_cli.mp.exec_bash", return_value=_ok("1.86.2\n"))
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(_status_json("Running")))
    tailscale_cmd.upgrade(config_path=saved_config)
    assert "--only-upgrade tailscale" in bash.call_args.args[1]
    assert tailscale_cmd.read_state(cfg)["version"] == "1.86.2"


# ---------- doctor check ----------


def test_doctor_check_skips_without_state_file(cfg: AgentsConfig, mocker: Any) -> None:
    from agents_cli.ops import DoctorReport, _check_tailscale

    probe = mocker.patch("agents_cli.mp.exec_in_vm")
    report = DoctorReport()
    _check_tailscale(cfg, report)
    assert report.checks == []
    probe.assert_not_called()


def test_doctor_check_warns_when_disconnected(cfg: AgentsConfig, mocker: Any) -> None:
    from agents_cli.ops import DoctorReport, _check_tailscale

    tailscale_cmd.write_state(cfg, {"hostname": "agents"})
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(_status_json("NeedsLogin")))
    report = DoctorReport()
    _check_tailscale(cfg, report)
    [check] = report.checks
    assert check.name == "tailscale" and check.status == "warn"


def test_doctor_check_ok_when_running(cfg: AgentsConfig, mocker: Any) -> None:
    from agents_cli.ops import DoctorReport, _check_tailscale

    tailscale_cmd.write_state(cfg, {"hostname": "agents"})
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(_status_json("Running")))
    report = DoctorReport()
    _check_tailscale(cfg, report)
    [check] = report.checks
    assert check.status == "ok" and "100.64.0.7" in check.detail
