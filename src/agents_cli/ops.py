"""Day-2 ops: start, stop, restart, logs, exec, setup, doctor."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from agents_cli import compose, compose_runner, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import (
    _agent_type_from_service,
    _detect_type_from_compose,
    _name_from_service,
)
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.config import AgentsConfig
from agents_cli.errors import HealthCheckFailure, UserError
from agents_cli.journal import Journal


def _resolve_service(cfg: AgentsConfig, name: str) -> tuple[str, str]:
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not found in compose")
    return _service_name_for(name, agent_type), agent_type


# ---------- start / stop / restart ----------


def start(name: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    svc, _ = _resolve_service(cfg, name)
    compose_runner.start(cfg, svc)
    ui.emit({"name": name, "status": "started"}, human=f"{name}: started")


def stop(name: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    svc, _ = _resolve_service(cfg, name)
    compose_runner.stop(cfg, svc)
    ui.emit({"name": name, "status": "stopped"}, human=f"{name}: stopped")


def restart(name: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    svc, _ = _resolve_service(cfg, name)
    compose_runner.restart(cfg, svc)
    ui.emit({"name": name, "status": "restarted"}, human=f"{name}: restarted")


# ---------- logs ----------


def logs(
    name: str,
    *,
    follow: bool = False,
    since: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    cfg = cfg_mod.load(config_path)
    svc, _ = _resolve_service(cfg, name)
    if follow:
        # Replace the process so the user gets a clean Ctrl-C exit.
        argv = [*_multipass_compose_argv(cfg), "logs", "-f"]
        if since:
            argv += ["--since", since]
        argv.append(svc)
        os.execvp("multipass", argv)
    result = compose_runner.logs(cfg, svc, follow=False, since=since)
    ui.emit({"name": name, "logs": result.stdout}, human=result.stdout.rstrip())


# ---------- exec ----------


def run_in_agent(
    name: str,
    cmd: tuple[str, ...] = (),
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """`agents exec <name> [--] <cmd...>`. Drops into a TTY shell when ``cmd`` is empty."""
    cfg = cfg_mod.load(config_path)
    svc, _ = _resolve_service(cfg, name)
    argv = [*_multipass_compose_argv(cfg), "exec", svc, *(cmd or ("bash",))]
    os.execvp("multipass", argv)


# ---------- setup wizard ----------


def setup(name: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_service(cfg, name)
    binary = "hermes" if agent_type == "hermes" else "openclaw"
    sub = "setup" if agent_type == "hermes" else "onboard"
    argv = [*_multipass_compose_argv(cfg), "exec", svc, binary, sub]
    os.execvp("multipass", argv)


# ---------- doctor ----------


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, status=status, detail=detail))

    @property
    def failed(self) -> bool:
        return any(c.status == "fail" for c in self.checks)


def doctor(
    *,
    target: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> DoctorReport:
    cfg = cfg_mod.load(config_path)
    report = DoctorReport()

    _check_vm_reachable(cfg, report)
    if cfg.paths.host_data_dir.exists():
        _check_mount_round_trip(cfg, report)
        _check_user_docker_service(cfg, report)
        _check_disk_space(cfg, report)
        _check_journals(cfg, report)
        _check_port_conflicts(cfg, report)
    else:
        report.add("data_dir_exists", "fail", f"{cfg.paths.host_data_dir} missing")

    for svc, agent_type, agent_name in _services_to_check(cfg, target):
        _check_service_container(cfg, svc, agent_name, agent_type, report)
        if agent_type == "hermes":
            _check_hermes_doctor(cfg, svc, agent_name, report)

    _emit_doctor_table(report)
    if report.failed:
        raise HealthCheckFailure(
            f"{sum(1 for c in report.checks if c.status == 'fail')} doctor check(s) failed"
        )
    return report


def _multipass_compose_argv(cfg: AgentsConfig) -> list[str]:
    root = str(cfg.paths.vm_agents_root)
    return [
        "multipass",
        "exec",
        cfg.vm.name,
        "--",
        "docker",
        "compose",
        "--project-directory",
        root,
        "-f",
        f"{root}/docker-compose.yml",
        "--env-file",
        f"{root}/.env",
    ]


def _services_to_check(cfg: AgentsConfig, target: str | None) -> list[tuple[str, str, str]]:
    """List of (service_name, agent_type, agent_name) to doctor over."""
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    out: list[tuple[str, str, str]] = []
    for svc_name in services:
        agent_type = _agent_type_from_service(svc_name)
        if agent_type is None:
            continue
        agent_name = _name_from_service(svc_name, agent_type)
        if target is not None and agent_name != target:
            continue
        out.append((svc_name, agent_type, agent_name))
    if target is not None and not out:
        raise UserError(f"agent {target!r} not found in compose")
    return out


def _check_vm_reachable(cfg: AgentsConfig, report: DoctorReport) -> None:
    result = mp.info_vm(cfg.vm.name, fmt="json")
    if result.returncode != 0:
        report.add("vm_reachable", "fail", result.stderr.strip())
        return
    try:
        info = json.loads(result.stdout)
        state = info.get("info", {}).get(cfg.vm.name, {}).get("state", "?")
    except json.JSONDecodeError:
        state = "?"
    report.add("vm_reachable", "ok" if state == "Running" else "fail", f"state={state}")


def _check_mount_round_trip(cfg: AgentsConfig, report: DoctorReport) -> None:
    if cfg.paths.data_dir_in_vm:
        report.add("mount_round_trip", "ok", "skipped (data_dir_in_vm)")
        return
    import uuid

    probe = cfg.paths.host_data_dir / f".doctor-probe-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("ok\n", encoding="utf-8")
        result = mp.exec_in_vm(
            cfg.vm.name,
            "cat",
            f"{cfg.paths.vm_agents_root}/{probe.name}",
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "ok":
            report.add("mount_round_trip", "ok")
        else:
            report.add(
                "mount_round_trip",
                "fail",
                f"VM cat rc={result.returncode} stdout={result.stdout!r}",
            )
    finally:
        probe.unlink(missing_ok=True)


def _check_user_docker_service(cfg: AgentsConfig, report: DoctorReport) -> None:
    result = mp.exec_in_vm(
        cfg.vm.name,
        "systemctl",
        "--user",
        "is-active",
        "docker.service",
        check=False,
    )
    state = result.stdout.strip()
    if state == "active":
        report.add("user_docker_service", "ok")
    else:
        report.add(
            "user_docker_service",
            "fail",
            f"systemctl --user is-active = {state!r}; check `loginctl enable-linger ubuntu`",
        )


def _check_disk_space(cfg: AgentsConfig, report: DoctorReport) -> None:
    usage = shutil.disk_usage(cfg.paths.host_data_dir)
    free_pct = usage.free / usage.total * 100
    detail = f"{free_pct:.1f}% free on {cfg.paths.host_data_dir}"
    if free_pct < 5:
        report.add("disk_space", "fail", detail)
    elif free_pct < 15:
        report.add("disk_space", "warn", detail)
    else:
        report.add("disk_space", "ok", detail)


def _check_journals(cfg: AgentsConfig, report: DoctorReport) -> None:
    dangling = Journal.find_dangling(cfg.paths.host_data_dir)
    if not dangling:
        report.add("journals", "ok")
        return
    names = ", ".join(f"{j.op}/{j.name}" for j in dangling)
    report.add("journals", "warn", f"dangling > 24h: {names}")


def _check_port_conflicts(cfg: AgentsConfig, report: DoctorReport) -> None:
    """No two services bind the same host port."""
    used = compose.used_ports(cfg)
    # used_ports returns a set, so duplicates are already collapsed. Re-derive
    # the list and look for repeats by counting via the compose doc.
    doc = compose.read_compose_doc(cfg)
    counts: dict[int, int] = {}
    for svc in (doc.get("services") or {}).values():
        for entry in svc.get("ports", []) or []:
            if isinstance(entry, str):
                # `127.0.0.1:8642:8642` — middle value is the host port.
                parts = entry.split(":")
                if len(parts) >= 2:
                    try:
                        host_port = int(parts[-2])
                        counts[host_port] = counts.get(host_port, 0) + 1
                    except ValueError:
                        pass
    dupes = sorted(p for p, n in counts.items() if n > 1)
    if dupes:
        report.add("port_conflicts", "fail", f"duplicate host ports: {dupes}")
    else:
        report.add(
            "port_conflicts",
            "ok",
            f"{len(used)} port(s) in use" if used else "no ports allocated",
        )


def _check_service_container(
    cfg: AgentsConfig,
    svc: str,
    agent_name: str,
    agent_type: str,
    report: DoctorReport,
) -> None:
    result = compose_runner.ps(cfg, fmt="json")
    if result.returncode != 0:
        report.add(f"{agent_name}.container", "fail", "docker compose ps failed")
        return
    state = _container_state_for(result.stdout, svc)
    if state == "running":
        report.add(f"{agent_name}.container", "ok")
    else:
        report.add(f"{agent_name}.container", "fail", f"state={state}")
        return

    if agent_type == "hermes":
        # Verify the mounted rootless socket + DOCKER_HOST env actually let
        # the Hermes container talk to the in-VM Docker daemon. Required for
        # the nested sandbox to ever come up.
        docker_check = mp.exec_in_vm(
            cfg.vm.name,
            "docker",
            "compose",
            "--project-directory",
            str(cfg.paths.vm_agents_root),
            "-f",
            f"{cfg.paths.vm_agents_root}/docker-compose.yml",
            "--env-file",
            f"{cfg.paths.vm_agents_root}/.env",
            "exec",
            "-T",
            svc,
            "docker",
            "ps",
            check=False,
        )
        if docker_check.returncode == 0:
            report.add(f"{agent_name}.docker_socket", "ok")
        else:
            report.add(
                f"{agent_name}.docker_socket",
                "fail",
                f"docker ps inside {svc!r} failed: {docker_check.stderr.strip()}",
            )


def _container_state_for(ps_json: str, service: str) -> str:
    """Parse `docker compose ps --format json` output; handle both per-line
    JSONL and bare-array forms (docker compose ≤ 2.20 vs ≥ 2.21)."""
    try:
        parsed = json.loads(ps_json)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        entries = []
        for line in ps_json.splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("Service") == service or entry.get("Name", "").endswith(f"-{service}"):
            return str(entry.get("State", "?"))
    return "absent"


def _check_hermes_doctor(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "compose",
        "--project-directory",
        str(cfg.paths.vm_agents_root),
        "-f",
        f"{cfg.paths.vm_agents_root}/docker-compose.yml",
        "--env-file",
        f"{cfg.paths.vm_agents_root}/.env",
        "exec",
        "-T",
        svc,
        "hermes",
        "doctor",
        check=False,
    )
    if result.returncode == 0:
        report.add(f"{agent_name}.hermes_doctor", "ok")
    else:
        report.add(
            f"{agent_name}.hermes_doctor",
            "fail",
            result.stderr.strip() or result.stdout.strip(),
        )


def _emit_doctor_table(report: DoctorReport) -> None:
    rows = [{"check": c.name, "status": c.status, "detail": c.detail} for c in report.checks]
    ui.table(rows, ["check", "status", "detail"])
