"""ops.py: day-2 wrappers + doctor checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents_cli import compose, ops
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import create
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import HealthCheckFailure, UserError
from agents_cli.journal import Journal
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail(stderr: str = "boom") -> MpResult:
    return MpResult(stdout="", stderr=stderr, returncode=1)


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (data_dir / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
        defaults=DefaultsConfig(
            hermes_image_tag="v1.0.0",
            openclaw_image_tag="0.1.0",
        ),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


@pytest.fixture
def with_coder(cfg: AgentsConfig, saved_config: Path, mocker) -> Path:
    """Seed a compose with one Hermes agent named 'coder' and stub runners."""
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


# ---------- start / stop / restart ----------


def test_start_calls_compose_runner_start(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agents_cli.compose_runner.start", return_value=_ok())
    ops.start("coder", config_path=with_coder)
    args, _ = spy.call_args
    assert args[1] == "hermes-coder"


def test_stop_calls_compose_runner_stop(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    ops.stop("coder", config_path=with_coder)
    args, _ = spy.call_args
    assert args[1] == "hermes-coder"


def test_restart_calls_compose_runner_restart(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agents_cli.compose_runner.restart", return_value=_ok())
    ops.restart("coder", config_path=with_coder)
    args, _ = spy.call_args
    assert args[1] == "hermes-coder"


def test_ops_for_unknown_agent_raises(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not found"):
        ops.start("ghost", config_path=saved_config)


# ---------- logs ----------


def test_logs_without_follow_dumps_stdout(with_coder: Path, mocker) -> None:
    mocker.patch("agents_cli.compose_runner.logs", return_value=_ok("hello\nworld\n"))
    spy_emit = mocker.spy(ops.ui, "emit")
    ops.logs("coder", follow=False, config_path=with_coder)
    spy_emit.assert_called_once()


# ---------- doctor checks (table-driven) ----------


def _stub_minimal_doctor(mocker, vm_state: str = "Running") -> None:
    """Set up the bare minimum mocks so a doctor run can complete.

    The exec_in_vm stub responds differently to a few specific commands so the
    `systemctl --user is-active` check (which expects literally "active") and
    the mount-probe cat (which expects "ok") both pass.
    """
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": vm_state}}})),
    )

    def vm_command(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        joined = " ".join(cmd)
        if "is-active" in joined:
            return _ok("active\n")
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=vm_command)


def test_doctor_reports_vm_not_running_as_fail(with_coder: Path, mocker) -> None:
    _stub_minimal_doctor(mocker, vm_state="Stopped")
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_warns_on_dangling_journal(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    _stub_minimal_doctor(mocker)
    mocker.patch("agents_cli.compose_runner.ps", return_value=_ok("[]"))
    j = Journal.begin(cfg.paths.host_data_dir, "create", "ghost", ["a"])
    import datetime

    old_payload = json.loads(j.path.read_text())
    old_payload["started_at"] = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)
    ).isoformat()
    j.path.write_text(json.dumps(old_payload))

    from agents_cli.ops import DoctorReport, _check_journals

    report = DoctorReport()
    _check_journals(cfg, report)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["journals"] == "warn"


@pytest.mark.parametrize(
    "free,total,expected",
    [
        (300, 1000, "ok"),  # 30% — comfortable
        (100, 1000, "warn"),  # 10% — under 15% threshold
        (30, 1000, "fail"),  # 3% — under 5% threshold
    ],
)
def test_doctor_disk_space_thresholds_classify(
    cfg: AgentsConfig, mocker, free: int, total: int, expected: str
) -> None:
    from agents_cli.ops import DoctorReport, _check_disk_space

    fake_usage = type("Usage", (), {"total": total, "free": free, "used": total - free})
    mocker.patch("shutil.disk_usage", return_value=fake_usage)
    report = DoctorReport()
    _check_disk_space(cfg, report)
    assert report.checks[0].status == expected


def test_doctor_port_conflict_detection(cfg: AgentsConfig) -> None:
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.add_service(doc, "hermes-a", {"ports": ["127.0.0.1:8642:8642"]})
    compose.add_service(doc, "hermes-b", {"ports": ["127.0.0.1:8642:8642"]})
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )

    from agents_cli.ops import DoctorReport, _check_port_conflicts

    report = DoctorReport()
    _check_port_conflicts(cfg, report)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["port_conflicts"] == "fail"


def test_doctor_target_missing_raises(saved_config: Path, mocker) -> None:
    _stub_minimal_doctor(mocker)
    with pytest.raises(UserError, match="not found"):
        ops.doctor(target="ghost", config_path=saved_config)


def test_doctor_passes_on_healthy_setup(with_coder: Path, mocker) -> None:
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    assert not report.failed


def test_doctor_flags_hermes_doctor_failure(with_coder: Path, mocker) -> None:
    """Pass every check except the in-container `hermes doctor` call."""
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )

    def conditional_runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        joined = " ".join(cmd)
        if "hermes" in joined and "doctor" in joined:
            return _fail("doctor says no")
        return _ok(stdout="ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=conditional_runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)
