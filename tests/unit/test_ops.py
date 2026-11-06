"""ops.py: day-2 wrappers + doctor checks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agx_cli import compose, ops
from agx_cli import config as cfg_mod
from agx_cli.agents_cmd import create
from agx_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agx_cli.errors import HealthCheckFailure, UserError
from agx_cli.journal import Journal
from agx_cli.mp import MpResult


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
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


# ---------- start / stop / restart ----------


def test_start_calls_compose_runner_start(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agx_cli.compose_runner.start", return_value=_ok())
    ops.start("coder", config_path=with_coder)
    args, _ = spy.call_args
    assert args[1] == "hermes-coder"


def test_stop_calls_compose_runner_stop(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    ops.stop("coder", config_path=with_coder)
    args, _ = spy.call_args
    assert args[1] == "hermes-coder"


def test_restart_calls_compose_runner_restart(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())
    ops.restart("coder", config_path=with_coder)
    args, _ = spy.call_args
    assert args[1] == "hermes-coder"


def test_ops_for_unknown_agent_raises(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not found"):
        ops.start("ghost", config_path=saved_config)


# ---------- logs ----------


def test_logs_without_follow_dumps_stdout(with_coder: Path, mocker) -> None:
    mocker.patch("agx_cli.compose_runner.logs", return_value=_ok("hello\nworld\n"))
    spy_emit = mocker.spy(ops.ui, "emit")
    ops.logs("coder", follow=False, config_path=with_coder)
    spy_emit.assert_called_once()


def test_logs_follow_routes_through_backend_dispatcher(with_coder: Path, mocker) -> None:
    """`agx logs -f` used to os.execvp("multipass", ...), which broke
    under the libvirt and bare_metal backends. It must build the in-VM
    compose argv and stream via mp.exec_in_vm_interactive (backend-
    dispatched), exiting with the child's return code — same fix as
    run_in_agent.
    """
    interactive = mocker.patch("agx_cli.mp.exec_in_vm_interactive", return_value=3)
    with pytest.raises(SystemExit) as exc:
        ops.logs("coder", follow=True, since="10m", config_path=with_coder)
    assert exc.value.code == 3
    args, _ = interactive.call_args
    argv = list(args[1:])
    assert argv[:2] == ["docker", "compose"]
    assert "multipass" not in argv
    assert "logs" in argv and "-f" in argv
    assert "--since" in argv and "10m" in argv
    assert argv[-1] == "hermes-coder"


# ---------- doctor checks (table-driven) ----------


def _stub_minimal_doctor(mocker, vm_state: str = "Running") -> None:
    """Set up the bare minimum mocks so a doctor run can complete.

    The exec_in_vm stub responds differently to a few specific commands so the
    `systemctl --user is-active` check (which expects literally "active") and
    the mount-probe cat (which expects "ok") both pass.
    """
    mocker.patch(
        "agx_cli.mp.info_vm",
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
        # `df -Pk /` for the vm_disk check — return a half-full root fs.
        if cmd[:2] == ("df", "-Pk"):
            return _ok(
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/vda1 60000000 30000000 30000000 50% /\n"
            )
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=vm_command)
    # container_logs reads the daemon config via exec_bash; without this
    # the check shells out to the real `multipass` (absent in CI).
    mocker.patch("agx_cli.mp.exec_bash", return_value=_ok(""))


def test_doctor_reports_vm_not_running_as_fail(with_coder: Path, mocker) -> None:
    _stub_minimal_doctor(mocker, vm_state="Stopped")
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_warns_on_dangling_journal(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    _stub_minimal_doctor(mocker)
    mocker.patch("agx_cli.compose_runner.ps", return_value=_ok("[]"))
    j = Journal.begin(cfg.paths.host_data_dir, "create", "ghost", ["a"])
    import datetime

    old_payload = json.loads(j.path.read_text())
    old_payload["started_at"] = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)
    ).isoformat()
    j.path.write_text(json.dumps(old_payload))

    from agx_cli.ops import DoctorReport, _check_journals

    report = DoctorReport()
    _check_journals(cfg, report)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["journals"] == "warn"


def test_doctor_override_mirror_check(cfg: AgentsConfig, mocker) -> None:
    """--data-dir-in-vm: a host override with no in-VM mirror breaks every
    compose verb (the -f points at a missing file) — doctor must say so.
    Host-mount mode and no-override are skips."""
    from agx_cli.ops import DoctorReport, _check_override_mirror

    report = DoctorReport()
    _check_override_mirror(cfg, report)
    assert report.checks[-1].status == "ok"  # host-mount mode: skipped

    cfg.paths.data_dir_in_vm = True
    _check_override_mirror(cfg, report)
    assert report.checks[-1].status == "ok"  # no override file

    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=MpResult("", "", 1))
    _check_override_mirror(cfg, report)
    assert report.checks[-1].status == "fail"
    assert "agx vm start" in (report.checks[-1].detail or "")

    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok())
    _check_override_mirror(cfg, report)
    assert report.checks[-1].status == "ok"


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
    from agx_cli.ops import DoctorReport, _check_disk_space

    fake_usage = type("Usage", (), {"total": total, "free": free, "used": total - free})
    mocker.patch("shutil.disk_usage", return_value=fake_usage)
    report = DoctorReport()
    _check_disk_space(cfg, report)
    assert report.checks[0].status == expected


@pytest.mark.parametrize(
    "avail_kb,total_kb,expected",
    [
        (30_000_000, 60_000_000, "ok"),  # 50% — comfortable
        (6_000_000, 60_000_000, "warn"),  # 10% — under 15% threshold
        (0, 60_000_000, "fail"),  # 0% — full VM disk
    ],
)
def test_doctor_vm_disk_thresholds_classify(
    cfg: AgentsConfig, mocker, avail_kb: int, total_kb: int, expected: str
) -> None:
    from agx_cli.ops import DoctorReport, _check_vm_disk

    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        return_value=_ok(
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"/dev/vda1 {total_kb} {total_kb - avail_kb} {avail_kb} 50% /\n"
        ),
    )
    report = DoctorReport()
    _check_vm_disk(cfg, report)
    assert report.checks[0].status == expected


def test_doctor_vm_disk_degrades_to_warn_on_unparseable_df(cfg: AgentsConfig, mocker) -> None:
    from agx_cli.ops import DoctorReport, _check_vm_disk

    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok("garbage\n"))
    report = DoctorReport()
    _check_vm_disk(cfg, report)
    assert report.checks[0].status == "warn"


_ROTATING = '{"log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "3"}}'


@pytest.mark.parametrize(
    ("daemon_json", "rotates"),
    [
        (_ROTATING, True),
        ('{"log-driver": "local"}', True),
        ('{"log-driver": "journald"}', True),
        (None, False),
        ("", False),
        ('{"log-driver": "json-file"}', False),
        ('{"log-opts": {"max-file": "3"}}', False),
        ("{not json", False),
        ("[]", False),
    ],
)
def test_daemon_log_rotation_gap(daemon_json: str | None, rotates: bool) -> None:
    from agx_cli.ops import daemon_log_rotation_gap

    assert (daemon_log_rotation_gap(daemon_json) is None) is rotates


def _stub_log_rotation(
    mocker, *, daemon_json: str | None, inspect_lines: list[str]
) -> list[tuple[str, ...]]:
    """Stub the daemon.json read + project container listing/inspect.
    Returns the list the exec_in_vm calls are recorded into."""
    mocker.patch(
        "agx_cli.mp.exec_bash",
        return_value=_ok(daemon_json) if daemon_json is not None else _fail(""),
    )
    calls: list[tuple[str, ...]] = []

    def vm_command(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        calls.append(cmd)
        if cmd[:2] == ("docker", "ps"):
            return _ok("".join(f"id{i}\n" for i in range(len(inspect_lines))))
        if cmd[:2] == ("docker", "inspect"):
            return _ok("".join(f"{line}\n" for line in inspect_lines))
        return _fail()

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=vm_command)
    return calls


def test_doctor_container_logs_ok_when_daemon_and_containers_rotate(
    cfg: AgentsConfig, mocker
) -> None:
    from agx_cli.ops import DoctorReport, _check_container_logs

    calls = _stub_log_rotation(
        mocker,
        daemon_json=_ROTATING,
        inspect_lines=["/hermes-coder|json-file|50m", "/firecrawl-api|local|<no value>"],
    )
    report = DoctorReport()
    _check_container_logs(cfg, report)
    assert report.checks[0].status == "ok"
    # Scoped to this project's containers, never the whole daemon.
    ps = next(c for c in calls if c[:2] == ("docker", "ps"))
    assert f"label=com.docker.compose.project.working_dir={cfg.paths.vm_agents_root}" in ps


def test_doctor_container_logs_warns_on_missing_daemon_config(cfg: AgentsConfig, mocker) -> None:
    """Bare-metal's verify-only bootstrap never writes daemon.json — doctor
    is the standing guard against unrotated logs filling the disk."""
    from agx_cli.ops import DoctorReport, _check_container_logs

    cfg.vm.backend = "bare_metal"
    _stub_log_rotation(mocker, daemon_json=None, inspect_lines=[])
    report = DoctorReport()
    _check_container_logs(cfg, report)
    check = report.checks[0]
    assert check.status == "warn"
    assert "host root fs" in check.detail
    assert "max-size" in check.detail


def test_doctor_container_logs_names_containers_created_before_rotation(
    cfg: AgentsConfig, mocker
) -> None:
    """log-opts bind at create time: a fixed daemon still leaves older
    containers unbounded until they are recreated."""
    from agx_cli.ops import DoctorReport, _check_container_logs

    _stub_log_rotation(
        mocker,
        daemon_json=_ROTATING,
        inspect_lines=["/hermes-coder|json-file|<no value>", "/hermes-bob|json-file|50m"],
    )
    report = DoctorReport()
    _check_container_logs(cfg, report)
    check = report.checks[0]
    assert check.status == "warn"
    assert "hermes-coder" in check.detail
    assert "hermes-bob" not in check.detail
    assert "reseat" in check.detail


def test_doctor_port_conflict_detection(cfg: AgentsConfig, mocker) -> None:
    # used_ports first asks the VM's `docker compose config` — stub the
    # backend exec to fail so the check deterministically exercises the
    # host-side doc-parse fallback (and never invokes a real multipass).
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_fail())
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.add_service(doc, "hermes-a", {"ports": ["127.0.0.1:8642:8642"]})
    compose.add_service(doc, "hermes-b", {"ports": ["127.0.0.1:8642:8642"]})
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )

    from agx_cli.ops import DoctorReport, _check_port_conflicts

    report = DoctorReport()
    _check_port_conflicts(cfg, report)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["port_conflicts"] == "fail"


def test_doctor_target_missing_raises(saved_config: Path, mocker) -> None:
    _stub_minimal_doctor(mocker)
    with pytest.raises(UserError, match="not found"):
        ops.doctor(target="ghost", config_path=saved_config)


# ---------- doctor: profile gateways (GH #6 tier 1) ----------


def _profile_line(name: str, main: str, log: str, state: dict) -> str:
    return f"{name}\t{main}\t{log}\t{json.dumps(state)}"


def _run_profile_check(cfg, mocker, result: MpResult):
    from agx_cli.ops import DoctorReport, _check_profile_gateways

    mocker.patch("agx_cli.mp.exec_in_vm", return_value=result)
    report = DoctorReport()
    _check_profile_gateways(cfg, "hermes-coder", "coder", report)
    return report


def test_profile_gateways_quiet_without_profiles(cfg, mocker) -> None:
    # No secondary profiles (the common case) → the check adds nothing,
    # keeping doctor output stable for single-profile agents.
    report = _run_profile_check(cfg, mocker, _ok(""))
    assert report.checks == []


def test_profile_gateways_quiet_when_container_down(cfg, mocker) -> None:
    # container_running already reports a down container; this check
    # must not pile on.
    result = MpResult(stdout="", stderr='service "hermes-coder" is not running', returncode=1)
    report = _run_profile_check(cfg, mocker, result)
    assert report.checks == []


def test_profile_gateways_ok_when_supervised_and_up(cfg, mocker) -> None:
    line = _profile_line(
        "spike",
        "up (pid 336) 4242 seconds",
        "up (pid 289) 4242 seconds",
        {"desired_state": "running", "gateway_state": "running"},
    )
    report = _run_profile_check(cfg, mocker, _ok(line + "\n"))
    assert [c.status for c in report.checks] == ["ok"]
    assert "spike: up" in report.checks[0].detail


def test_profile_gateways_warns_when_wanted_but_down(cfg, mocker) -> None:
    line = _profile_line(
        "spike",
        "down (exitcode 0) 12 seconds, normally up, want up",
        "up (pid 289) 4242 seconds",
        {"desired_state": "running"},
    )
    report = _run_profile_check(cfg, mocker, _ok(line + "\n"))
    assert [c.status for c in report.checks] == ["warn"]
    assert "hermes -p spike gateway start" in report.checks[0].detail


def test_profile_gateways_warns_on_flapping_log_service(cfg, mocker) -> None:
    # The missing-log-dir class: gateway main service is up but s6-log
    # keeps dying — uptime of a few seconds at probe time.
    line = _profile_line(
        "spike",
        "up (pid 336) 4242 seconds",
        "up (pid 999) 2 seconds",
        {"desired_state": "running"},
    )
    report = _run_profile_check(cfg, mocker, _ok(line + "\n"))
    assert [c.status for c in report.checks] == ["warn"]
    assert "log service" in report.checks[0].detail
    assert "agx restart coder" in report.checks[0].detail


def test_profile_gateways_stays_ok_for_deliberate_stop(cfg, mocker) -> None:
    # `hermes gateway stop` records desired_state=stopped — a down
    # service then carries no signal.
    line = _profile_line(
        "spike",
        "down (exitcode 0) 99999 seconds, normally up",
        "up (pid 289) 99999 seconds",
        {"desired_state": "stopped", "gateway_state": "stopped"},
    )
    report = _run_profile_check(cfg, mocker, _ok(line + "\n"))
    assert [c.status for c in report.checks] == ["ok"]
    assert "stopped (deliberate)" in report.checks[0].detail


def test_profile_gateways_ignores_garbage_state_files(cfg, mocker) -> None:
    # Unparseable or non-object state files carry no assertable signal;
    # the check must not crash or false-alarm on them.
    lines = "\n".join(
        [
            "spike\tup (pid 1) 10 seconds\tup (pid 2) 10 seconds\tnot-json",
            'other\tup (pid 3) 10 seconds\tup (pid 4) 10 seconds\t"just-a-string"',
        ]
    )
    report = _run_profile_check(cfg, mocker, _ok(lines + "\n"))
    assert report.checks == []


def test_doctor_passes_on_healthy_setup(with_coder: Path, mocker) -> None:
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    assert not report.failed


def test_doctor_emit_false_suppresses_table_but_returns_full_report(
    with_coder: Path, mocker, capsys
) -> None:
    """Composite callers (`agx vm migrate-data`) own their command's
    single stdout object — with emit=False the table render (which goes
    to stdout, as a JSON array under --json) is suppressed entirely,
    while the returned report still carries every check for the caller
    to fold into its own payload."""
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder, emit=False)
    assert not report.failed
    assert len(report.checks) > 0  # the report itself is intact
    assert capsys.readouterr().out == ""  # nothing rendered to stdout


def test_doctor_emit_false_still_raises_on_failure(with_coder: Path, mocker, capsys) -> None:
    """Suppressing the render must not suppress the health verdict."""
    _stub_minimal_doctor(mocker, vm_state="Stopped")
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder, emit=False)
    assert capsys.readouterr().out == ""


def test_doctor_memory_check_warns_under_1g_available(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agx_cli.mp.info_vm",
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "vm_memory"]
    assert check.status == "warn"
    assert "available" in check.detail


def test_doctor_memory_check_fails_under_256m_available(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agx_cli.mp.info_vm",
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_load_check_warns_on_saturation(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agx_cli.mp.info_vm",
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "vm_load"]
    assert check.status == "warn"
    assert "load ratio" in check.detail


def test_doctor_load_check_fails_on_severe_oversub(with_coder: Path, mocker) -> None:
    mocker.patch(
        "agx_cli.mp.info_vm",
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
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
    from agx_cli import hermes_config

    cfg = cfg_mod.load(with_coder)
    doc = hermes_config.read_doc(cfg, "coder")
    if "terminal" not in doc:
        doc["terminal"] = {}
    doc["terminal"]["backend"] = "local"
    hermes_config.write_atomically(cfg, "coder", doc)

    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.docker_socket"]
    assert check.status == "ok"
    assert "skipped" in check.detail
    assert "local" in check.detail


def test_doctor_table_wraps_long_detail_in_human_mode(with_coder: Path, mocker, capsys) -> None:
    """A very long detail string would otherwise blow out the column
    width and break the separator. Confirm we wrap onto continuation
    lines (the first line fits in a sensible width, no single line
    exceeds the simulated terminal width).
    """
    import shutil

    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
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


def test_doctor_table_human_path_goes_through_ui_echo_human(mocker) -> None:
    """ops.py must not call click.echo directly — every output line goes
    through ui.py so --json/--yes stay auditable in one module. The
    human table renderer uses the ui.echo_human helper for its custom
    wrapped layout."""
    echo = mocker.patch("agx_cli.ui.echo_human")
    report = ops.DoctorReport()
    report.add("check_a", "ok", "fine")
    report.add("check_b", "fail", "broken")

    ops._emit_doctor_table(report)

    lines = [c.args[0] for c in echo.call_args_list]
    assert any("check_a" in ln for ln in lines)
    assert any("check_b" in ln for ln in lines)
    assert any(ln.startswith("check") for ln in lines)  # header line


def test_doctor_table_json_path_skips_echo_human(mocker) -> None:
    """Under --json the table goes through ui.table (single JSON array);
    the human renderer must not add stray lines around it."""
    mocker.patch("agx_cli.ui.is_json", return_value=True)
    echo = mocker.patch("agx_cli.ui.echo_human")
    table = mocker.patch("agx_cli.ui.table")
    report = ops.DoctorReport()
    report.add("check_a", "ok")

    ops._emit_doctor_table(report)

    echo.assert_not_called()
    table.assert_called_once()


def test_doctor_flags_data_dir_eacces(with_coder: Path, mocker) -> None:
    """Reproduce the canonical `agx exec` permission failure: stat
    /opt/data returns EACCES because the dir is owned by uid 10000 and
    the toolkit's RUNTIME_UID exec is uid 1000. The doctor check must
    fail and point the user at `agx fix-perms`.
    """
    mocker.patch(
        "agx_cli.mp.info_vm",
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_doctor_warns_on_data_dir_owner_mismatch(with_coder: Path, mocker) -> None:
    """stat succeeds but owner uid != RUNTIME_UID — warns (not fails) with
    a remediation hint pointing at `agx fix-perms`. The user can still
    function (e.g. running `agx exec --root`) but real exec at uid 1000
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder)
    matches = [c for c in report.checks if c.name == "coder.data_dir_access" and c.status == "warn"]
    assert matches, "expected a warn for the data_dir_access check"
    assert "fix-perms" in matches[0].detail


def test_doctor_flags_hermes_doctor_failure(with_coder: Path, mocker) -> None:
    """Pass every check except the in-container `hermes doctor` call."""
    mocker.patch(
        "agx_cli.mp.info_vm",
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

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=conditional_runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    with pytest.raises(HealthCheckFailure):
        ops.doctor(config_path=with_coder)


def test_hermes_doctor_issue_report_is_warn_not_fail(cfg: AgentsConfig, mocker) -> None:
    """Hermes >= v0.21.6 exits 1 whenever `hermes doctor` finds ANY issue
    (incl. advisory WAL-on-9p notes) — older images exited 0 with the
    same findings. A completed run that reports issues is a `warn` with
    the flagged lines; only a run that couldn't happen is a `fail`.
    Mapping the former to fail rolled back a healthy upgrade."""
    from agx_cli.ops import DoctorReport, _check_hermes_doctor

    out = (
        "◆ Database\n"
        "  ⚠ state.db is in WAL mode on a cross-VM filesystem (virtiofs/9p, 168.0 KB)\n"
        "  ✗ ~/.local/bin/hermes not found\n"
        "  ✓ config.yaml ok\n"
        "Found 2 issue(s) to address:\n"
    )
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=MpResult(out, "", 1))
    report = DoctorReport()
    _check_hermes_doctor(cfg, "hermes-coder", "coder", report)
    [check] = report.checks
    assert check.status == "warn"
    assert "reported 2 issue(s)" in check.detail
    assert "WAL mode on a cross-VM filesystem" in check.detail
    assert "✓ config.yaml" not in check.detail
    assert "agx exec coder -- hermes doctor" in check.detail

    # Could not run at all → still a hard fail.
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        return_value=MpResult("", 'exec: "hermes": executable file not found in $PATH', 126),
    )
    report = DoctorReport()
    _check_hermes_doctor(cfg, "hermes-coder", "coder", report)
    assert report.checks[0].status == "fail"
    assert "executable file not found" in report.checks[0].detail


def test_hermes_doctor_detail_lists_numbered_issues_one_per_line(cfg: AgentsConfig, mocker) -> None:
    """Hermes' own "Found N issue(s) to address:" list is what the detail
    shows (one ``- item`` line each) — not the much longer ⚠ list, where
    every unconfigured optional integration is a line."""
    from agx_cli.ops import DoctorReport, _check_hermes_doctor

    out = (
        "  ⚠ discord.py (optional, not installed)\n"
        "  ⚠ Nous Portal auth (not logged in)\n"
        "\n"
        "  Found 2 issue(s) to address:\n"
        "\n"
        "  1. Run 'hermes setup' to configure missing API keys for full tool access\n"
        "  2. WhatsApp bridge has 3 npm vulnerabilities; wait for an upstream lockfile fix\n"
        "\n"
        "  Tip: run 'hermes doctor --fix' to auto-fix what's possible.\n"
    )
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=MpResult(out, "", 1))
    report = DoctorReport()
    _check_hermes_doctor(cfg, "hermes-coder", "coder", report)
    [check] = report.checks
    assert check.status == "warn"
    assert check.detail.splitlines() == [
        "`hermes doctor` reported 2 issue(s):",
        "- Run 'hermes setup' to configure missing API keys for full tool access",
        "- WhatsApp bridge has 3 npm vulnerabilities; wait for an upstream lockfile fix",
        "full list: `agx exec coder -- hermes doctor`",
    ]


def test_doctor_arm64_emulation_warns_when_firecrawl_running_on_arm64(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """On arm64 hosts, the firecrawl nuq-postgres image runs under
    qemu emulation (upstream ships no arm64 tag). The doctor must
    flag this so users aren't surprised by slow Postgres init.
    """
    mocker.patch("agx_cli.firecrawl_cmd._docker_arch", return_value="arm64")
    mocker.patch("agx_cli.firecrawl_cmd.is_installed", return_value=True)
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
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
    mocker.patch("agx_cli.firecrawl_cmd._docker_arch", return_value="arm64")
    mocker.patch("agx_cli.firecrawl_cmd.is_installed", return_value=False)
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
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
    mocker.patch("agx_cli.firecrawl_cmd._docker_arch", return_value="amd64")
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "arm64_emulation"]
    assert check.status == "ok"


def test_doctor_firecrawl_warns_when_installed_and_wired_but_down(
    cfg: AgentsConfig, mocker
) -> None:
    """The migration-loss gap: compose still defines the firecrawl
    services and an agent is still wired to them, but the containers
    aren't running (the in-VM Docker state didn't survive the VM
    rebuild). Must warn so the silent scrape failures get surfaced.
    """
    from agx_cli.ops import DoctorReport, _check_firecrawl_running

    mocker.patch(
        "agx_cli.firecrawl_cmd._firecrawl_services_in_compose",
        return_value=["firecrawl-api", "firecrawl-redis"],
    )
    mocker.patch("agx_cli.firecrawl_cmd._service_running", return_value=False)
    mocker.patch("agx_cli.firecrawl_cmd._list_wired_agents", return_value=["coder"])

    report = DoctorReport()
    _check_firecrawl_running(cfg, report)
    [check] = [c for c in report.checks if c.name == "firecrawl_running"]
    assert check.status == "warn"
    assert "coder" in check.detail
    assert "agx firecrawl up" in check.detail


def test_doctor_firecrawl_ok_when_running(cfg: AgentsConfig, mocker) -> None:
    """Healthy stack — quiet ok line."""
    from agx_cli.ops import DoctorReport, _check_firecrawl_running

    mocker.patch(
        "agx_cli.firecrawl_cmd._firecrawl_services_in_compose",
        return_value=["firecrawl-api"],
    )
    mocker.patch("agx_cli.firecrawl_cmd._service_running", return_value=True)

    report = DoctorReport()
    _check_firecrawl_running(cfg, report)
    [check] = [c for c in report.checks if c.name == "firecrawl_running"]
    assert check.status == "ok"
    assert check.detail == "running"


def test_doctor_firecrawl_ok_when_not_installed(cfg: AgentsConfig, mocker) -> None:
    """No firecrawl services in compose — nothing to flag."""
    from agx_cli.ops import DoctorReport, _check_firecrawl_running

    mocker.patch("agx_cli.firecrawl_cmd._firecrawl_services_in_compose", return_value=[])

    report = DoctorReport()
    _check_firecrawl_running(cfg, report)
    [check] = [c for c in report.checks if c.name == "firecrawl_running"]
    assert check.status == "ok"
    assert "not installed" in check.detail


def test_doctor_firecrawl_quiet_after_deliberate_down(cfg: AgentsConfig, mocker) -> None:
    """``agx firecrawl down`` leaves the compose blocks but drops the
    agent wiring. A stopped + unwired stack is an intentional stop, not
    a regression — stay at ok so doctor doesn't nag.
    """
    from agx_cli.ops import DoctorReport, _check_firecrawl_running

    mocker.patch(
        "agx_cli.firecrawl_cmd._firecrawl_services_in_compose",
        return_value=["firecrawl-api", "firecrawl-redis"],
    )
    mocker.patch("agx_cli.firecrawl_cmd._service_running", return_value=False)
    mocker.patch("agx_cli.firecrawl_cmd._list_wired_agents", return_value=[])

    report = DoctorReport()
    _check_firecrawl_running(cfg, report)
    [check] = [c for c in report.checks if c.name == "firecrawl_running"]
    assert check.status == "ok"
    assert "down" in check.detail


# ---------- doctor: llama-cpp per-model services ----------


def _seed_llamacpp_service(cfg: AgentsConfig, *slugs: str) -> None:
    body = "services:\n"
    for slug in slugs:
        body += f"  llama-cpp-{slug}:\n    image: x\n"
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(body, encoding="utf-8")


def test_doctor_llamacpp_warns_when_defined_but_absent(cfg: AgentsConfig, mocker) -> None:
    """The VM-rebuild stranding class: the compose blocks survive the
    migration, the containers don't. Same gap _check_firecrawl_running
    covers — gate on compose presence, warn with the recovery hint.
    """
    from agx_cli.ops import DoctorReport, _check_inference_llamacpp

    _seed_llamacpp_service(cfg, "qwen")
    mocker.patch("agx_cli.compose_runner.ps", return_value=_ok("[]"))

    report = DoctorReport()
    _check_inference_llamacpp(cfg, report)
    [check] = [c for c in report.checks if c.name == "inference_llamacpp"]
    assert check.status == "warn"
    assert "llama-cpp-qwen" in check.detail
    assert "not running" in check.detail
    # The recovery hint must name the REAL command — loading lives under
    # the `inference` group (`agx inference load ...`); the `model`
    # group has no `load` subcommand.
    assert "agx inference load llama-cpp" in check.detail
    assert "agx model load" not in check.detail


def test_doctor_llamacpp_ok_when_running(cfg: AgentsConfig, mocker) -> None:
    from agx_cli.ops import DoctorReport, _check_inference_llamacpp

    _seed_llamacpp_service(cfg, "qwen")
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "llama-cpp-qwen", "State": "running"}])),
    )

    report = DoctorReport()
    _check_inference_llamacpp(cfg, report)
    [check] = [c for c in report.checks if c.name == "inference_llamacpp"]
    assert check.status == "ok"


def test_doctor_llamacpp_warns_on_partial_stranding(cfg: AgentsConfig, mocker) -> None:
    """Two services defined, one exited — only the stranded one is named."""
    from agx_cli.ops import DoctorReport, _check_inference_llamacpp

    _seed_llamacpp_service(cfg, "qwen", "phi")
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(
            json.dumps(
                [
                    {"Service": "llama-cpp-qwen", "State": "running"},
                    {"Service": "llama-cpp-phi", "State": "exited"},
                ]
            )
        ),
    )

    report = DoctorReport()
    _check_inference_llamacpp(cfg, report)
    [check] = [c for c in report.checks if c.name == "inference_llamacpp"]
    assert check.status == "warn"
    assert "llama-cpp-phi" in check.detail
    assert "llama-cpp-qwen" not in check.detail


def test_doctor_llamacpp_skips_when_none_defined(cfg: AgentsConfig, mocker) -> None:
    """Most users won't run local inference — no compose entries means
    an ok/skip, not a probe.
    """
    from agx_cli.ops import DoctorReport, _check_inference_llamacpp

    ps_spy = mocker.patch("agx_cli.compose_runner.ps", return_value=_ok("[]"))

    report = DoctorReport()
    _check_inference_llamacpp(cfg, report)
    [check] = [c for c in report.checks if c.name == "inference_llamacpp"]
    assert check.status == "ok"
    assert "skipped" in check.detail
    ps_spy.assert_not_called()


def test_doctor_inference_health_covers_both_engines(cfg: AgentsConfig, mocker) -> None:
    """_check_inference_health fans out to BOTH the ollama probe and the
    per-model llama-cpp probes — the llama-cpp half was the doctor
    blind spot (docstring claimed it, code skipped it).
    """
    from agx_cli.ops import DoctorReport, _check_inference_health

    _seed_llamacpp_service(cfg, "qwen")
    mocker.patch("agx_cli.compose_runner.ps", return_value=_ok("[]"))

    report = DoctorReport()
    _check_inference_health(cfg, report)
    names = {c.name for c in report.checks}
    assert "inference_ollama" in names
    assert "inference_llamacpp" in names


def test_doctor_version_check_reports_tag_when_running(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """``<agent>.version`` MUST show the tag the container is
    actually running — the most useful "what version am I on?" answer
    in the doctor output. Works for both Hermes and OpenClaw because
    the compose ``image:`` field is universal."""
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    # Override the default exec_in_vm to serve `docker inspect` with
    # an image that matches the compose pin (cfg fixture pins v1.0.0).
    def runner(vm_name: str, *cmd: str, check: bool = True):
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
        if cmd[:3] == ("docker", "inspect", "hermes-coder") and "{{.Config.Image}}" in cmd:
            return _ok("nousresearch/hermes-agent:v1.0.0\n")
        return _ok("ok\n")

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.version"]
    assert check.status == "ok"
    assert check.detail == "v1.0.0"


def test_doctor_version_check_shows_hermes_semver_alongside_tag(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """Hermes has a dual versioning scheme — calver image tags
    (v2026.7.7.2) and an app semver (v0.18.2). `hermes --version`
    prints both; the doctor detail surfaces the semver next to the
    tag so the user can correlate with upstream release notes."""
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True):
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
        if cmd[:3] == ("docker", "inspect", "hermes-coder") and "{{.Config.Image}}" in cmd:
            return _ok("nousresearch/hermes-agent:v1.0.0\n")
        if "hermes" in cmd and "--version" in cmd:
            return _ok("Hermes Agent v0.18.2 (2026.7.7.2) · upstream 9de9c25f\n")
        return _ok("ok\n")

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.version"]
    assert check.status == "ok"
    assert check.detail == "v1.0.0 (app v0.18.2)"


def test_doctor_version_check_omits_semver_when_probe_fails(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """Older images (or a container mid-restart) may not answer
    `hermes --version` — the detail must degrade to the plain tag,
    never block or garble the check."""
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True):
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
        if cmd[:3] == ("docker", "inspect", "hermes-coder") and "{{.Config.Image}}" in cmd:
            return _ok("nousresearch/hermes-agent:v1.0.0\n")
        if "hermes" in cmd and "--version" in cmd:
            return _fail("no such option: --version")
        return _ok("ok\n")

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.version"]
    assert check.status == "ok"
    assert check.detail == "v1.0.0"


def test_doctor_version_check_warns_on_pin_vs_runtime_drift(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """Hand-edited compose with no follow-up restart leaves the
    container running an older tag than what's pinned. Surface as
    ``warn`` with the exact ``agx restart <name>`` hint — without
    this the user only sees the new pin in ``agx list`` and assumes
    the upgrade took.
    """
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    def runner(vm_name: str, *cmd: str, check: bool = True):
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
        if cmd[:3] == ("docker", "inspect", "hermes-coder") and "{{.Config.Image}}" in cmd:
            # Container running v0.9.0; compose fixture pins v1.0.0.
            return _ok("nousresearch/hermes-agent:v0.9.0\n")
        return _ok("ok\n")

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.version"]
    assert check.status == "warn"
    assert "v0.9.0" in check.detail
    assert "v1.0.0" in check.detail
    assert "agx restart coder" in check.detail


def test_doctor_version_check_falls_back_when_container_down(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """If the container isn't running, ``docker inspect`` errors —
    the version check should still surface the compose pin (the
    user wants "what version am I on?" even when the container is
    down) and defer the actual "container down" complaint to the
    parallel ``<agent>.container`` check (avoid duplicate noise).
    """
    _stub_minimal_doctor(mocker)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "exited"}])),
    )

    base = _doctor_runner_with_auth(None, host_data_dir=cfg.paths.host_data_dir)

    def runner(vm_name: str, *cmd: str, check: bool = True):
        if cmd[:3] == ("docker", "inspect", "hermes-coder"):
            return MpResult(stdout="", stderr="No such container", returncode=1)
        return base(vm_name, *cmd, check=check)

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)

    # The container-state failure makes doctor raise HealthCheckFailure
    # after emitting the full report. We exercise the version-check
    # helper directly so we don't have to also placate every other
    # check just to read one row.
    from agx_cli.ops import DoctorReport, _check_agent_version

    report = DoctorReport()
    _check_agent_version(cfg, "hermes-coder", "coder", "hermes", report)
    version_checks = [c for c in report.checks if c.name == "coder.version"]
    assert version_checks
    # Pin still reported; status ok (the parallel .container check owns
    # the "container down" complaint).
    assert version_checks[0].status == "ok"
    assert "v1.0.0" in version_checks[0].detail
    assert "container not running" in version_checks[0].detail


def _seed_agent_config(cfg: AgentsConfig, agent: str, model: str) -> None:
    """Write the agent's config.yaml so ``hermes_config.read_doc`` can
    pick it up. Seeds the bare-string form of ``model:`` (Hermes also
    accepts a dict — covered by ``_seed_agent_config_dict_model``).
    """
    from agx_cli import hermes_config

    p = hermes_config.config_path(cfg, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"model: {model!r}\nproviders: {{}}\n", encoding="utf-8")


def _seed_agent_config_dict_model(cfg: AgentsConfig, agent: str, default: str) -> None:
    """Write the agent's config.yaml with the structured ``model:`` form
    the ``hermes model`` wizard produces:
    ``model: {default: <name>, provider: ..., base_url: ...}``.
    """
    from agx_cli import hermes_config

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
    work although read_doc routes through ``docker compose exec cat``
    (the host user can't read the subuid-owned file).
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
        "agx_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_doctor_runner_with_auth(
            '{"active_provider": "openai-codex"}', host_data_dir=cfg.paths.host_data_dir
        ),
    )
    mocker.patch(
        "agx_cli.compose_runner.ps",
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
        "agx_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_doctor_runner_with_auth(
            '{"active_provider": "openai-codex"}', host_data_dir=cfg.paths.host_data_dir
        ),
    )
    mocker.patch(
        "agx_cli.compose_runner.ps",
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
        "agx_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_doctor_runner_with_auth(
            '{"active_provider": "openai-codex"}', host_data_dir=cfg.paths.host_data_dir
        ),
    )
    mocker.patch(
        "agx_cli.compose_runner.ps",
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
        "agx_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        # auth.json present but empty (no active_provider)
        side_effect=_doctor_runner_with_auth("{}", host_data_dir=cfg.paths.host_data_dir),
    )
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )

    report = ops.doctor(config_path=with_coder)
    [check] = [c for c in report.checks if c.name == "coder.model_set"]
    assert check.status == "ok"
    assert "no active provider" in check.detail


# ---------- runtime-user default for `docker compose exec` ----------


def test_in_vm_compose_exec_argv_defaults_to_runtime_user(cfg: AgentsConfig) -> None:
    """`docker compose exec` defaults to the image USER (root for the Hermes
    image). Files created by a root exec are unreadable by the long-running
    gateway process (which is `gosu hermes` to uid 1000) — silently breaks
    things like `hermes pairing approve` because the approval file ends up
    root:root. Every toolkit exec must default to --user 1000:1000.
    """
    argv = ops._compose_exec_in_vm_argv(cfg, "hermes-coder", "hermes", "pairing", "approve")
    assert "--user" in argv
    assert f"{ops.RUNTIME_UID}:{ops.RUNTIME_GID}" in argv
    # The --user pair must come BETWEEN "exec" and the service name, not
    # after the service name (which would be interpreted as args to the
    # in-container command).
    exec_idx = argv.index("exec")
    svc_idx = argv.index("hermes-coder")
    user_flag_idx = argv.index("--user")
    assert exec_idx < user_flag_idx < svc_idx


def test_in_vm_compose_exec_argv_as_root_omits_user(cfg: AgentsConfig) -> None:
    """The escape hatch for chown / apt install / other admin work."""
    argv = ops._compose_exec_in_vm_argv(
        cfg, "hermes-coder", "chown", "hermes:hermes", "/opt/data/x", as_root=True
    )
    assert "--user" not in argv


def test_in_vm_compose_exec_argv_includes_override_iff_present(cfg: AgentsConfig) -> None:
    """Exec paths must see the same merged compose view as the runner
    verbs — otherwise `docker compose exec` can't resolve a service whose
    definition depends on the override layer."""
    argv = ops._compose_exec_in_vm_argv(cfg, "hermes-coder", "true")
    assert argv.count("-f") == 1

    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    argv = ops._compose_exec_in_vm_argv(cfg, "hermes-coder", "true")
    f_idx = [i for i, a in enumerate(argv) if a == "-f"]
    assert len(f_idx) == 2
    assert argv[f_idx[0] + 1].endswith("/docker-compose.yml")
    assert argv[f_idx[1] + 1].endswith("/docker-compose.override.yml")


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


# ---------- credential pool ----------


def _run_credential_pool_check(cfg: AgentsConfig, with_coder: Path, mocker, auth_json: str):
    _seed_agent_config_dict_model(cfg, "coder", "gpt-6-astra")
    mocker.patch(
        "agx_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running"}}})),
    )
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_doctor_runner_with_auth(auth_json, host_data_dir=cfg.paths.host_data_dir),
    )
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "hermes-coder", "State": "running"}])),
    )
    report = ops.doctor(config_path=with_coder, emit=False, raise_on_fail=False)
    [check] = [c for c in report.checks if c.name == "coder.credential_pool"]
    return check


def _pool(*entries: dict) -> str:
    return json.dumps(
        {"active_provider": "openai-codex", "credential_pool": {"openai-codex": list(entries)}}
    )


def test_doctor_credential_pool_fails_when_every_credential_is_dead(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """The "Provider authentication failed" class: Hermes marked the only
    Codex credential exhausted/token_expired. Detail must carry the
    reason and the in-container re-auth one-liner, never a token."""
    check = _run_credential_pool_check(
        cfg,
        with_coder,
        mocker,
        _pool(
            {
                "label": "openai-codex-oauth-1",
                "last_status": "exhausted",
                "last_error_reason": "token_expired",
                "access_token": "SECRET-ACCESS",
                "refresh_token": "SECRET-REFRESH",
            }
        ),
    )
    assert check.status == "fail"
    assert "exhausted/token_expired" in check.detail
    assert "hermes auth add openai-codex --type oauth --no-browser" in check.detail
    assert "SECRET" not in check.detail


def test_doctor_credential_pool_warns_when_some_credentials_remain(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    check = _run_credential_pool_check(
        cfg,
        with_coder,
        mocker,
        _pool(
            {"label": "a", "last_status": "dead", "last_error_reason": "token_invalidated"},
            {"label": "b", "last_status": "ok"},
        ),
    )
    assert check.status == "warn"
    assert "1/2" in check.detail


def test_doctor_credential_pool_ok_and_edge_cases(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    check = _run_credential_pool_check(
        cfg, with_coder, mocker, _pool({"label": "a", "last_status": "ok"}, {"label": "b"})
    )
    assert check.status == "ok" and "2 credential(s) healthy" in check.detail

    # Provider configured but nothing pooled for it → warn, not fail.
    check = _run_credential_pool_check(
        cfg, with_coder, mocker, json.dumps({"active_provider": "x", "credential_pool": {}})
    )
    assert check.status == "warn" and "no pooled credential" in check.detail
