"""ops.py: day-2 wrappers + doctor checks."""

from __future__ import annotations

import json
import os
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
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
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
        # `stat -c %a %u /opt/data` probe (data-dir access check) expects
        # "<mode> <uid>" — return a healthy mode/owner.
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("755 1000\n")
        # `cat /proc/meminfo` for the vm_memory check — return a healthy
        # snapshot (8 GiB total, 4 GiB available).
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        # `nproc` for the vm_load check.
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        # `cat /proc/loadavg` for the vm_load check — return idle load.
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.42 0.31 0.25 1/123 4567\n")
        # Default for the model_set probe: pretend auth.json doesn't
        # exist yet → check skips at "no auth.json yet". Tests that
        # exercise model_set replace this stub with a custom runner.
        if "cat" in cmd and "/opt/data/auth.json" in cmd:
            return MpResult(stdout="", stderr="No such file", returncode=1)
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


def test_doctor_memory_check_warns_under_1g_available(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "is-active" in " ".join(cmd):
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("755 1000\n")
        if cmd[:2] == ("cat", "/proc/meminfo"):
            # 8 GiB total, only 700 MiB available → warn (< 1 GiB)
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 716800 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.5 0.3 0.2 1/100 999\n")
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "vm_memory"]
    assert check.status == "warn"
    assert "available" in check.detail


def test_doctor_memory_check_fails_under_256m_available(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "is-active" in " ".join(cmd):
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("755 1000\n")
        if cmd[:2] == ("cat", "/proc/meminfo"):
            # 8 GiB total, 100 MiB available → fail
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 102400 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.5 0.3 0.2 1/100 999\n")
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_load_check_warns_on_saturation(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "is-active" in " ".join(cmd):
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("755 1000\n")
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            # load1=5.0 on 4 cpus → ratio 1.25 → warn
            return _ok("5.00 4.50 3.80 5/200 1234\n")
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "vm_load"]
    assert check.status == "warn"
    assert "load ratio" in check.detail


def test_doctor_load_check_fails_on_severe_oversub(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "is-active" in " ".join(cmd):
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("755 1000\n")
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            # load1=10 on 4 cpus → ratio 2.5 → fail
            return _ok("10.00 9.50 8.80 5/200 1234\n")
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_skips_docker_socket_when_backend_local(with_coder: Path, mocker) -> None:
    """When the agent's terminal.backend is `local` (Hermes default),
    the nested-sandbox docker socket is irrelevant — failing to
    reach it would be a false positive. The check must skip with
    a clear note instead.
    """
    from agents_cli import hermes_config

    cfg = cfg_mod.load(with_coder)
    doc = hermes_config.read_doc(cfg, "coder")
    if "terminal" not in doc:
        doc["terminal"] = {}
    doc["terminal"]["backend"] = "local"
    hermes_config.write_atomically(cfg, "coder", doc)

    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.docker_socket"]
    assert check.status == "ok"
    assert "skipped" in check.detail
    assert "local" in check.detail


def test_doctor_codex_mount_ok_when_persistent_auth_exists(with_coder: Path, mocker) -> None:
    """Post-import state: the bind-mounted /home/hermes/.codex/auth.json
    is unreadable to the runtime user (root-owned via rootless userns
    mapping), but Hermes already imported it to /opt/data/auth.json.
    This is the expected steady state — must not fail.
    """
    cfg = cfg_mod.load(with_coder)
    (cfg.paths.host_data_dir / ".codex").mkdir(exist_ok=True)
    (cfg.paths.host_data_dir / ".codex" / "auth.json").write_text(
        '{"expires_at": "2099-01-01T00:00:00Z"}', encoding="utf-8"
    )
    # Pretend /opt/data/auth.json exists in the container.
    persistent = cfg.paths.host_data_dir / "coder" / ".data" / "auth.json"
    persistent.parent.mkdir(parents=True, exist_ok=True)
    persistent.write_text("imported", encoding="utf-8")

    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        joined = " ".join(cmd)
        if "is-active" in joined:
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("755 1000\n")
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.5 0.3 0.2 1/100 999\n")
        # test -r /opt/data/auth.json (post-import probe): persistent exists → rc 0
        if "test" in cmd and "/opt/data/auth.json" in cmd:
            return _ok("")
        # test -r /home/hermes/.codex/auth.json (bridge probe): not reachable
        if "test" in cmd and "/home/hermes/.codex/auth.json" in cmd:
            return MpResult(stdout="", stderr="", returncode=1)
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.codex_mount"]
    assert check.status == "ok"
    assert "post-import" in check.detail


def test_doctor_table_wraps_long_detail_in_human_mode(with_coder: Path, mocker, capsys) -> None:
    """A very long detail string would otherwise blow out the column
    width and break the separator. Confirm we wrap onto continuation
    lines (the first line fits in a sensible width, no single line
    exceeds the simulated terminal width).
    """
    import shutil

    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    mocker.patch.object(shutil, "get_terminal_size", return_value=os_terminal_size(80))
    # Force a long detail by injecting a fail check into doctor's run.
    real_doctor = ops.doctor

    def patched_doctor(**kw):
        report = real_doctor(**kw)
        return report

    long_msg = "x " * 200
    # Easiest: invoke _emit_doctor_table directly with a fabricated report.
    report = ops.DoctorReport()
    report.add("a.long.check.name", "fail", long_msg.strip())
    ops._emit_doctor_table(report)
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert len(line) <= 80, f"line exceeds 80 cols: {line!r}"


def os_terminal_size(cols: int) -> os.terminal_size:
    """Helper: build an os.terminal_size-like object."""
    return os.terminal_size((cols, 24))


def test_doctor_flags_data_dir_eacces(with_coder: Path, mocker) -> None:
    """Reproduce the canonical `agents exec` permission failure: stat
    /opt/data returns EACCES because the dir is owned by uid 10000 and
    the toolkit's RUNTIME_UID exec is uid 1000. The doctor check must
    fail and point the user at `agents fix-perms`.
    """
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        joined = " ".join(cmd)
        if "is-active" in joined:
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return MpResult(
                stdout="",
                stderr="stat: cannot stat '/opt/data': Permission denied\n",
                returncode=1,
            )
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.42 0.31 0.25 1/123 4567\n")
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_warns_on_data_dir_owner_mismatch(with_coder: Path, mocker) -> None:
    """stat succeeds but owner uid != RUNTIME_UID — warns (not fails) with
    a remediation hint pointing at `agents fix-perms`. The user can still
    function (e.g. running `agents exec --root`) but real exec at uid 1000
    will hit issues, so flag it loudly.
    """
    _stub_minimal_doctor(mocker)
    # Override the stat probe to return a mismatched owner.

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        joined = " ".join(cmd)
        if "is-active" in joined:
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("700 10000\n")
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.42 0.31 0.25 1/123 4567\n")
        return _ok("ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    matches = [c for c in report.checks if c.name == "coder.data_dir_access" and c.status == "warn"]
    assert matches, "expected a warn for the data_dir_access check"
    assert "fix-perms" in matches[0].detail


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
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.42 0.31 0.25 1/123 4567\n")
        return _ok(stdout="ok\n")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=conditional_runner)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_arm64_emulation_warns_when_firecrawl_running_on_arm64(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """On arm64 hosts, the firecrawl nuq-postgres image runs under
    qemu emulation (upstream ships no arm64 tag). The doctor must
    flag this so users aren't surprised by slow Postgres init.
    """
    mocker.patch("agents_cli.firecrawl_cmd._docker_arch", return_value="arm64")
    mocker.patch("agents_cli.firecrawl_cmd.is_installed", return_value=True)
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "arm64_emulation"]
    assert check.status == "warn"
    assert "nuq-postgres" in check.detail


def test_doctor_arm64_emulation_silent_when_firecrawl_absent(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Don't warn on arm64 if firecrawl isn't installed — nothing
    is being emulated. Stays at ok with a 'not installed' detail.
    """
    mocker.patch("agents_cli.firecrawl_cmd._docker_arch", return_value="arm64")
    mocker.patch("agents_cli.firecrawl_cmd.is_installed", return_value=False)
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "arm64_emulation"]
    assert check.status == "ok"


def test_doctor_arm64_emulation_silent_on_amd64_host(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Most hosts are amd64 — the probe should be a quiet ok line,
    not a noisy warn.
    """
    mocker.patch("agents_cli.firecrawl_cmd._docker_arch", return_value="amd64")
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "arm64_emulation"]
    assert check.status == "ok"


def _seed_agent_config(cfg: AgentsConfig, agent: str, model: str) -> None:
    """Write the agent's config.yaml so ``hermes_config.read_doc`` can
    pick it up. Seeds the bare-string form of ``model:`` (Hermes also
    accepts a dict — covered by ``_seed_agent_config_dict_model``).
    """
    from agents_cli import hermes_config

    p = hermes_config.config_path(cfg, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"model: {model!r}\nproviders: {{}}\n", encoding="utf-8")


def _seed_agent_config_dict_model(cfg: AgentsConfig, agent: str, default: str) -> None:
    """Write the agent's config.yaml with the structured ``model:`` form
    the ``hermes model`` wizard produces:
    ``model: {default: <name>, provider: ..., base_url: ...}``.
    """
    from agents_cli import hermes_config

    p = hermes_config.config_path(cfg, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"model:\n  default: {default}\n  provider: openai-codex\n"
        f"  base_url: https://chatgpt.com/backend-api/codex\nproviders: {{}}\n",
        encoding="utf-8",
    )


def _doctor_runner_with_auth(auth_json: str | None, *, host_data_dir: Path | None = None):
    """Build a doctor exec_in_vm side_effect that returns ``auth_json``
    for ``cat /opt/data/auth.json`` (or rc=1 when None) and the usual
    healthy responses for everything else.

    If ``host_data_dir`` is given, ``cat /opt/data/config.yaml`` reads
    the host-side file at ``<host_data_dir>/<agent>/.data/config.yaml``
    so tests that seed config.yaml via the host filesystem continue to
    work after read_doc started routing through ``docker compose
    exec cat`` (2026-05-22 subuid-readability fix).
    """

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        joined = " ".join(cmd)
        if "is-active" in joined:
            return _ok("active\n")
        if "stat" in cmd and "/opt/data" in cmd:
            return _ok("755 1000\n")
        if cmd[:2] == ("cat", "/proc/meminfo"):
            return _ok("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        if cmd[:1] == ("nproc",):
            return _ok("4\n")
        if cmd[:2] == ("cat", "/proc/loadavg"):
            return _ok("0.42 0.31 0.25 1/123 4567\n")
        # The exec_in_vm argv passes through `docker compose ... exec <svc>
        # cat /opt/data/auth.json` — `cat` and the path sit at the tail,
        # not in cmd[:2]. Mirror the membership-check pattern used by
        # the codex_mount test.
        if "cat" in cmd and "/opt/data/auth.json" in cmd:
            if auth_json is None:
                return MpResult(stdout="", stderr="No such file", returncode=1)
            return _ok(auth_json)
        # Route config.yaml reads to the host-side seed when caller
        # provided host_data_dir. The container path /opt/data maps
        # to <data-dir>/<agent>/.data; we recover the agent name from
        # the service flag in the docker compose exec argv.
        if "cat" in cmd and "/opt/data/config.yaml" in cmd and host_data_dir is not None:
            agent: str | None = None
            for token in cmd:
                if token.startswith("hermes-"):
                    agent = token[len("hermes-") :]
                    break
            if agent:
                host_file = host_data_dir / agent / ".data" / "config.yaml"
                if host_file.exists():
                    return _ok(host_file.read_text(encoding="utf-8"))
            return MpResult(stdout="", stderr="No such file", returncode=1)
        return _ok("ok\n")

    return runner


def test_doctor_model_set_warns_when_provider_set_but_model_empty(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """Canonical misconfiguration: an active LLM provider is configured
    in auth.json but ``model:`` is empty in config.yaml. Interactive
    chat keeps working via the gateway's per-session runtime default,
    but headless aux calls (compression, cron, vision auto-fallback)
    silently fail with "<provider> request 'model' must be a non-empty
    string" — often masked downstream as "Bad file descriptor" when
    the stream cleanup hits an already-closed fd.
    """
    _seed_agent_config(cfg, "coder", "")

    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_doctor_runner_with_auth(
            '{"active_provider": "openai-codex"}', host_data_dir=cfg.paths.host_data_dir
        ),
    )
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.model_set"]
    assert check.status == "warn"
    assert "openai-codex" in check.detail
    assert "hermes model" in check.detail  # remediation hint


def test_doctor_model_set_ok_when_model_and_provider_both_set(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    _seed_agent_config(cfg, "coder", "gpt-5.5")

    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_doctor_runner_with_auth(
            '{"active_provider": "openai-codex"}', host_data_dir=cfg.paths.host_data_dir
        ),
    )
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.model_set"]
    assert check.status == "ok"
    assert "gpt-5.5" in check.detail
    assert "openai-codex" in check.detail


def test_doctor_model_set_ok_when_model_is_dict_form(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """The ``hermes model`` interactive wizard writes ``model:`` as a
    dict (``{default, provider, base_url}``), not a bare string. The
    check must treat ``model.default`` as the resolved name; otherwise
    we'd false-positive warn on the post-wizard happy path.
    """
    _seed_agent_config_dict_model(cfg, "coder", "gpt-5.5")

    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_doctor_runner_with_auth(
            '{"active_provider": "openai-codex"}', host_data_dir=cfg.paths.host_data_dir
        ),
    )
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.model_set"]
    assert check.status == "ok"
    assert "gpt-5.5" in check.detail


def test_doctor_model_set_skipped_when_no_active_provider(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """A fresh agent with no LLM provider configured yet shouldn't trip
    the model_set warn — no provider means no aux call would fire
    anyway. Skip silently so the doctor stays quiet during onboarding.
    """
    _seed_agent_config(cfg, "coder", "")

    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        # auth.json present but empty (no active_provider)
        side_effect=_doctor_runner_with_auth("{}", host_data_dir=cfg.paths.host_data_dir),
    )
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.model_set"]
    assert check.status == "ok"
    assert "no active provider" in check.detail


# ---------- runtime-user default for `docker compose exec` ----------


def test_compose_exec_argv_defaults_to_runtime_user(cfg: AgentsConfig) -> None:
    """`docker compose exec` defaults to the image USER (root for the Hermes
    image). Files created by a root exec are unreadable by the long-running
    gateway process (which is `gosu hermes` to uid 1000) — silently breaks
    things like `hermes pairing approve` because the approval file ends up
    root:root. Every toolkit exec must default to --user 1000:1000.
    """
    argv = ops.compose_exec_argv(cfg, "hermes-coder", "hermes", "pairing", "approve")
    assert "--user" in argv
    assert f"{ops.RUNTIME_UID}:{ops.RUNTIME_GID}" in argv
    # The --user pair must come BETWEEN "exec" and the service name, not
    # after the service name (which would be interpreted as args to the
    # in-container command).
    exec_idx = argv.index("exec")
    svc_idx = argv.index("hermes-coder")
    user_flag_idx = argv.index("--user")
    assert exec_idx < user_flag_idx < svc_idx


def test_compose_exec_argv_as_root_omits_user(cfg: AgentsConfig) -> None:
    """The escape hatch for chown / apt install / other admin work."""
    argv = ops.compose_exec_argv(
        cfg, "hermes-coder", "chown", "hermes:hermes", "/opt/data/x", as_root=True
    )
    assert "--user" not in argv


def test_in_vm_compose_exec_argv_threads_env_vars(cfg: AgentsConfig) -> None:
    """Validation paths (hermes config validate via HERMES_CONFIG=...) need
    env injection. Helper threads them via -e KEY=VALUE on the subcommand.
    """
    argv = ops._compose_exec_in_vm_argv(
        cfg,
        "hermes-coder",
        "hermes",
        "config",
        "validate",
        no_tty=True,
        env={"HERMES_CONFIG": "/opt/data/config.yaml.new"},
    )
    assert "-e" in argv
    assert "HERMES_CONFIG=/opt/data/config.yaml.new" in argv
    assert "-T" in argv
