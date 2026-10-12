"""Day-2 ops: start, stop, restart, logs, exec, setup, doctor."""

from __future__ import annotations

import json
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from agx_cli import compose, compose_runner, mp, ui
from agx_cli import config as cfg_mod
from agx_cli.agents_cmd import (
    _agent_type_from_service,
    _detect_type_from_compose,
    _name_from_service,
)
from agx_cli.agents_cmd import service_name as _service_name_for
from agx_cli.config import AgentsConfig
from agx_cli.errors import HealthCheckFailure, RuntimeFailure, UserError
from agx_cli.journal import Journal


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
        # Stream through the backend dispatcher so `-f` works under
        # libvirt (SSH-based) and bare_metal too. Previously did
        # os.execvp("multipass", ...) which bypassed the dispatcher and
        # broke under any non-multipass backend (same fix as run_in_agent).
        argv = [*compose_runner._base_argv(cfg), "logs", "-f"]
        if since:
            argv += ["--since", since]
        argv.append(svc)
        sys.exit(mp.exec_in_vm_interactive(cfg.vm.name, *argv))
    result = compose_runner.logs(cfg, svc, follow=False, since=since)
    ui.emit({"name": name, "logs": result.stdout}, human=result.stdout.rstrip())


# ---------- exec ----------


def run_in_agent(
    name: str,
    cmd: tuple[str, ...] = (),
    *,
    as_root: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """``agx exec <name> [--root] [--] <cmd...>``.

    Defaults to running as the agent's runtime user (uid 1000), the
    same uid the long-running gateway process drops to — so files
    created here share ownership with what the gateway writes. Drops
    into a TTY shell when ``cmd`` is empty.

    Pass ``as_root=True`` (CLI: ``--root``) for admin operations like
    ``chown`` or installing apt packages inside the container.
    """
    cfg = cfg_mod.load(config_path)
    svc, _ = _resolve_service(cfg, name)
    # Dispatch via the backend so interactive exec works under
    # libvirt (SSH-based) as well as multipass. Previously did
    # os.execvp("multipass", ...) which bypassed the dispatcher and
    # broke under any non-multipass backend.
    in_vm = _compose_exec_in_vm_argv(cfg, svc, *(cmd or ("bash",)), as_root=as_root)
    sys.exit(mp.exec_in_vm_interactive(cfg.vm.name, *in_vm))


# ---------- setup wizard ----------


def setup(name: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_service(cfg, name)
    binary = "hermes" if agent_type == "hermes" else "openclaw"
    sub = "setup" if agent_type == "hermes" else "onboard"
    in_vm = _compose_exec_in_vm_argv(cfg, svc, binary, sub)
    sys.exit(mp.exec_in_vm_interactive(cfg.vm.name, *in_vm))


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
    emit: bool = True,
    raise_on_fail: bool = True,
) -> DoctorReport:
    """Run the health checks; render the report table unless ``emit=False``.

    ``raise_on_fail=False`` returns the report even when a check failed
    — for ``agx doctor-cron``, which reduces the report to a
    notification and must not turn a red doctor into a non-zero cron
    exit (cron would then mail the raw log through its own MAILTO).

    ``emit=False`` is for composite callers (``agx vm migrate-data``)
    that own their command's single stdout object — the table renders to
    stdout (``ui.table`` under ``--json``), which would violate the
    one-object contract. The returned :class:`DoctorReport` carries every
    check for the caller to fold into its own payload; a failing report
    still raises :class:`HealthCheckFailure` either way.
    """
    cfg = cfg_mod.load(config_path)
    report = DoctorReport()

    _check_vm_reachable(cfg, report)
    if cfg.paths.host_data_dir.exists():
        _check_mount_round_trip(cfg, report)
        _check_override_mirror(cfg, report)
        _check_user_docker_service(cfg, report)
        _check_disk_space(cfg, report)
        _check_vm_disk(cfg, report)
        _check_container_logs(cfg, report)
        _check_vm_memory(cfg, report)
        _check_vm_load(cfg, report)
        _check_journals(cfg, report)
        _check_port_conflicts(cfg, report)
        _check_codex_auth(cfg, report)
        _check_arm64_emulation(cfg, report)
        _check_firecrawl_running(cfg, report)
        _check_inference_health(cfg, report)
        _check_tailscale(cfg, report)
        _check_access_views(cfg, report)
    else:
        report.add("data_dir_exists", "fail", f"{cfg.paths.host_data_dir} missing")

    for svc, agent_type, agent_name in _services_to_check(cfg, target):
        _check_service_container(cfg, svc, agent_name, agent_type, report)
        _check_agent_version(cfg, svc, agent_name, agent_type, report)
        if agent_type == "hermes":
            _check_hermes_data_dir_accessible(cfg, svc, agent_name, report)
            _check_hermes_doctor(cfg, svc, agent_name, report)
            _check_hermes_model_set(cfg, svc, agent_name, report)
            _check_hermes_credential_pool(cfg, svc, agent_name, report)
            _check_profile_gateways(cfg, svc, agent_name, report)
            _check_profile_dashboard_bindings(cfg, svc, agent_name, report)

    if emit:
        _emit_doctor_table(report)
    if report.failed and raise_on_fail:
        raise HealthCheckFailure(
            f"{sum(1 for c in report.checks if c.status == 'fail')} doctor check(s) failed"
        )
    return report


# Container UID/GID for the agent's runtime user.
#
# Hermes' entrypoint reads HERMES_UID/HERMES_GID from env and remaps its
# internal `hermes` user to that uid before `gosu hermes`, so the long-
# running gateway process is uid 1000 inside the container. OpenClaw's
# default `node` user is also 1000. We hardcode 1000:1000 for both,
# matching the .env values the toolkit writes.
#
# Why this matters: `docker compose exec` defaults to the image's
# configured USER, which for the Hermes image is `root` (uid 0). If
# `agx exec coder hermes pairing approve ...` runs as root, the
# files it creates are owned by root — but the gateway runs as uid 1000
# and can't read them. Every interactive / wizard exec MUST default to
# the runtime user. Admin operations that need root can opt in via the
# CLI `--root` flag.
RUNTIME_UID: int = 1000
RUNTIME_GID: int = 1000


def _compose_exec_in_vm_argv(
    cfg: AgentsConfig,
    svc: str,
    *cmd: str,
    no_tty: bool = False,
    as_root: bool = False,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Build the in-VM argv for a ``docker compose exec`` against
    ``svc`` — pass it as ``*cmd`` to ``mp.exec_in_vm`` /
    ``mp.exec_in_vm_interactive``. Defaults to the runtime user (see
    :data:`RUNTIME_UID` above); ``as_root=True`` opts out.

    ``env`` maps environment variables to inject via ``-e KEY=VALUE``
    on the ``compose exec`` subcommand.
    """
    env_args: list[str] = []
    for key, value in (env or {}).items():
        env_args += ["-e", f"{key}={value}"]
    return [
        *compose_runner._base_argv(cfg),
        "exec",
        *([] if as_root else ["--user", f"{RUNTIME_UID}:{RUNTIME_GID}"]),
        *(["-T"] if no_tty else []),
        *env_args,
        svc,
        *cmd,
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
    if cfg.vm.backend == "bare_metal":
        report.add("mount_round_trip", "ok", "skipped (bare_metal — data-dir is host-native)")
        return
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


def _check_override_mirror(cfg: AgentsConfig, report: DoctorReport) -> None:
    """``--data-dir-in-vm`` only: the host-side override file arms the
    extra ``-f`` (``compose.override_file_args``), so its in-VM mirror
    must exist or every compose verb fails on a missing file. The mirror
    used to be lost on ``vm rebuild`` (fixed in the in-VM seeding); this
    catches a VM rebuilt by an older toolkit or a hand-deleted mirror.
    """
    from agx_cli import compose

    if not cfg.paths.data_dir_in_vm:
        report.add("override_mirror", "ok", "skipped (host-mount mode)")
        return
    if not compose.override_path(cfg).exists():
        report.add("override_mirror", "ok", "no override file")
        return
    vm_copy = f"{cfg.paths.vm_agents_root}/{compose.OVERRIDE_FILENAME}"
    result = mp.exec_in_vm(cfg.vm.name, "test", "-f", vm_copy, check=False)
    if result.returncode == 0:
        report.add("override_mirror", "ok")
    else:
        report.add(
            "override_mirror",
            "fail",
            f"host {compose.OVERRIDE_FILENAME} has no in-VM mirror at {vm_copy}; "
            "every compose verb will fail — re-push it with `agx vm start`",
        )


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


def _check_vm_disk(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Free space on the root filesystem holding the Docker engine + images.

    Complements ``disk_space`` (host data dir): under the VM backends the
    OS, Docker engine, and image cache live on the VM's root disk, which
    ``shutil.disk_usage`` on the host can't see. A full VM disk breaks
    agents obscurely — container overlay-fs writes fail, so Hermes can't
    persist session storage and s6 log services hot-loop — long before
    anything host-side notices. Repeated ``agx upgrade`` runs are the
    usual growth driver: each pull leaves the superseded image tag behind.
    """
    result = mp.exec_in_vm(cfg.vm.name, "df", "-Pk", "/", check=False)
    if result.returncode != 0:
        report.add("vm_disk", "warn", "could not run `df -Pk /`")
        return
    lines = result.stdout.strip().splitlines()
    fields = lines[1].split() if len(lines) >= 2 else []
    try:
        total_kb, avail_kb = int(fields[1]), int(fields[3])
    except (IndexError, ValueError):
        report.add("vm_disk", "warn", f"unparseable df output: {result.stdout!r}")
        return
    if total_kb <= 0:
        report.add("vm_disk", "warn", f"df reports zero-size root fs: {result.stdout!r}")
        return
    free_pct = avail_kb / total_kb * 100
    where = "host root fs" if cfg.vm.backend == "bare_metal" else "VM root disk"
    detail = (
        f"{free_pct:.1f}% free on {where} "
        f"({avail_kb / 1024 / 1024:.1f} of {total_kb / 1024 / 1024:.1f} GiB available)"
    )
    if free_pct < 5:
        report.add(
            "vm_disk",
            "fail",
            f"{detail}; superseded agent image tags from past upgrades are the"
            " usual culprit — `docker rmi` the old tags",
        )
    elif free_pct < 15:
        report.add(
            "vm_disk",
            "warn",
            f"{detail}; superseded agent image tags from past upgrades are the"
            " usual culprit — `docker rmi` the old tags",
        )
    else:
        report.add("vm_disk", "ok", detail)


# What the VM bootstrap writes (templates step 3b2); the remediation hint
# prints it verbatim so bare-metal users get the identical setting.
DAEMON_JSON_LOG_ROTATION = (
    '{"log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "3"}}'
)


def daemon_log_rotation_gap(daemon_json: str | None) -> str | None:
    """``None`` when a rootless daemon config rotates container logs,
    else a one-line reason. ``daemon_json`` is the file's text, or
    ``None`` when it doesn't exist.

    Only the ``json-file`` driver (Docker's default) grows unbounded
    without ``max-size``; ``local``, ``journald`` and friends rotate or
    hand off on their own.
    """
    if daemon_json is None or not daemon_json.strip():
        return "no daemon.json, so the default json-file log driver never rotates"
    try:
        conf = json.loads(daemon_json)
    except json.JSONDecodeError as e:
        return f"daemon.json is not valid JSON ({e.msg})"
    if not isinstance(conf, dict):
        return "daemon.json is not a JSON object"
    if conf.get("log-driver", "json-file") != "json-file":
        return None
    opts = conf.get("log-opts")
    if isinstance(opts, dict) and opts.get("max-size"):
        return None
    return "daemon.json sets no log-opts.max-size, so the json-file log driver never rotates"


def _check_container_logs(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Container-log rotation — the 2026-08 disk-full outage class.

    Rootless Docker ships no log rotation: one chatty container
    (firecrawl-postgres: 2.8 GB in 5 days) can fill the disk the engine
    lives on. Two layers, because log-opts apply at container CREATE
    time: (1) the daemon config — the VM bootstrap writes it, the
    verify-only bare-metal bootstrap can't, so this is the only
    standing guard there; (2) the project's existing containers, which
    keep unbounded logs until recreated even after the daemon is fixed.
    """
    bare_metal = cfg.vm.backend == "bare_metal"
    conf = mp.exec_bash(
        cfg.vm.name,
        'cat "${XDG_CONFIG_HOME:-$HOME/.config}/docker/daemon.json" 2>/dev/null',
        check=False,
    )
    problems: list[str] = []
    daemon_gap = daemon_log_rotation_gap(conf.stdout if conf.returncode == 0 else None)
    if daemon_gap:
        where = (
            "~/.config/docker/daemon.json"
            if bare_metal
            else "the VM's ~/.config/docker/daemon.json"
        )
        problems.append(
            f"{daemon_gap} — new containers can fill the "
            f"{'host root fs' if bare_metal else 'VM root disk'}. Write {DAEMON_JSON_LOG_ROTATION} "
            f"to {where}, then `systemctl --user restart docker`"
        )

    ids = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "ps",
        "-aq",
        "--filter",
        f"label=com.docker.compose.project.working_dir={cfg.paths.vm_agents_root}",
        check=False,
    )
    unbounded: list[str] = []
    if ids.returncode == 0 and ids.stdout.split():
        inspect = mp.exec_in_vm(
            cfg.vm.name,
            "docker",
            "inspect",
            "--format",
            '{{.Name}}|{{.HostConfig.LogConfig.Type}}|{{index .HostConfig.LogConfig.Config "max-size"}}',
            *ids.stdout.split(),
            check=False,
        )
        for line in inspect.stdout.splitlines() if inspect.returncode == 0 else []:
            name, _, rest = line.partition("|")
            driver, _, max_size = rest.partition("|")
            if driver == "json-file" and max_size.strip() in ("", "<no value>"):
                unbounded.append(name.lstrip("/"))
    if unbounded:
        problems.append(
            f"{len(unbounded)} container(s) log without rotation ({', '.join(sorted(unbounded))}) "
            "— log-opts apply at create time, so recreate them: agents via "
            "`agx reseat <name>`, other services via `docker compose up -d "
            "--force-recreate <service>` from the project dir (`agx vm shell`, "
            f"then `cd {cfg.paths.vm_agents_root}`)"
        )

    if problems:
        report.add("container_logs", "warn", "; ".join(problems))
    else:
        report.add("container_logs", "ok", "daemon and project containers rotate their logs")


def _check_vm_memory(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Probe the VM's available RAM and warn / fail on tight headroom.

    Uses ``MemAvailable`` from ``/proc/meminfo`` — kernel's accounting of
    "memory that can be allocated without swapping or reclaim from disk".
    More accurate than ``free - used`` because it accounts for reclaimable
    page cache. Thresholds:

    - fail when < 256 MiB available (OOM imminent)
    - warn when < 1 GiB available (squeezed; firecrawl + multiple
      Hermes agents will start contesting)
    """
    result = mp.exec_in_vm(cfg.vm.name, "cat", "/proc/meminfo", check=False)
    if result.returncode != 0:
        report.add("vm_memory", "warn", "could not read /proc/meminfo")
        return
    info: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        parts = v.strip().split()
        if parts and parts[0].isdigit():
            info[k.strip()] = int(parts[0])  # kB
    total_kb = info.get("MemTotal", 0)
    avail_kb = info.get("MemAvailable", 0)
    if total_kb == 0:
        report.add("vm_memory", "warn", "MemTotal=0 in /proc/meminfo")
        return
    avail_mib = avail_kb / 1024
    total_gib = total_kb / 1024 / 1024
    used_pct = (1 - avail_kb / total_kb) * 100
    detail = f"{avail_mib / 1024:.1f} GiB available of {total_gib:.1f} GiB ({used_pct:.0f}% used)"
    if avail_mib < 256:
        report.add("vm_memory", "fail", detail)
    elif avail_mib < 1024:
        report.add("vm_memory", "warn", detail)
    else:
        report.add("vm_memory", "ok", detail)


def _check_vm_load(cfg: AgentsConfig, report: DoctorReport) -> None:
    """1-minute load average vs CPU count.

    Load ratio (load1 / nproc):

    - fail when > 2.0 — sustained 2x oversubscription, things will be
      noticeably slow.
    - warn when > 1.0 — fully saturated; one extra task and you queue.
    """
    nproc = mp.exec_in_vm(cfg.vm.name, "nproc", check=False)
    cpus = int(nproc.stdout.strip()) if nproc.returncode == 0 and nproc.stdout.strip() else 1
    loadavg = mp.exec_in_vm(cfg.vm.name, "cat", "/proc/loadavg", check=False)
    if loadavg.returncode != 0:
        report.add("vm_load", "warn", "could not read /proc/loadavg")
        return
    parts = loadavg.stdout.strip().split()
    if not parts:
        report.add("vm_load", "warn", f"unparseable loadavg: {loadavg.stdout!r}")
        return
    try:
        load1 = float(parts[0])
    except ValueError:
        report.add("vm_load", "warn", f"unparseable loadavg: {loadavg.stdout!r}")
        return
    ratio = load1 / cpus
    detail = f"{load1:.2f} (1m) on {cpus} CPU{'s' if cpus != 1 else ''}; load ratio {ratio:.2f}"
    if ratio > 2.0:
        report.add("vm_load", "fail", detail)
    elif ratio > 1.0:
        report.add("vm_load", "warn", detail)
    else:
        report.add("vm_load", "ok", detail)


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
        # The nested-sandbox docker socket only matters when the agent's
        # ``terminal.backend`` is ``docker``. Default is ``local`` post
        # Hermes setup; for that case the in-container ``docker ps``
        # would fail (rootless socket isn't reachable from a non-root
        # container user under userns), but that's not a real problem —
        # Hermes won't try to use it. Skip with a clear note.
        backend = _hermes_terminal_backend(cfg, agent_name)
        if backend != "docker":
            report.add(
                f"{agent_name}.docker_socket",
                "ok",
                f"skipped (terminal.backend={backend!r}; nested sandbox not in use)",
            )
            return
        docker_check = mp.exec_in_vm(
            cfg.vm.name,
            *_compose_exec_in_vm_argv(cfg, svc, "docker", "ps", no_tty=True),
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


def _hermes_terminal_backend(cfg: AgentsConfig, agent_name: str) -> str:
    """Read ``terminal.backend`` from the agent's config.yaml. Falls
    back to ``"local"`` (Hermes' default after setup) when the field
    is absent or the config can't be read.
    """
    from agx_cli import hermes_config

    try:
        doc = hermes_config.read_doc(cfg, agent_name)
    except (RuntimeFailure, UserError):
        return "local"
    terminal = doc.get("terminal")
    if not isinstance(terminal, dict):
        return "local"
    backend = terminal.get("backend", "local")
    return str(backend) if backend else "local"


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


def _check_agent_version(
    cfg: AgentsConfig,
    svc: str,
    agent_name: str,
    agent_type: str,
    report: DoctorReport,
) -> None:
    """Report the agent's current image tag + flag pin/runtime drift.

    Two sources of truth, both interesting:
    - **Compose pin** (``services.<svc>.image`` in docker-compose.yml) —
      the version that would be used on the next ``compose up``.
    - **Live container image** (``docker inspect``'s ``Config.Image``) —
      what the running container is actually executing right now.

    Normally identical. They diverge when somebody hand-edits the
    compose file without restarting the container — the user sees the
    new pin in ``agx list`` but the agent is still on the old code.
    Surface that as a ``warn`` so the user knows to ``agx restart``.

    Universal across agent types: both Hermes and OpenClaw record their
    image tag in the same compose field; no agent-specific code path.

    Skipped (``ok``) if the container isn't running — the
    ``<agent>.container`` check above already fails in that case, and
    reporting "pinned at X, container down" twice is noise.
    """
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return
    pinned_image = (doc.get("services") or {}).get(svc, {}).get("image", "")
    pinned_tag = pinned_image.split(":", 1)[1] if ":" in pinned_image else ""
    if not pinned_tag:
        report.add(f"{agent_name}.version", "warn", "no image tag in compose")
        return

    container_name = svc  # toolkit sets container_name = svc in the compose fragment
    inspect = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "inspect",
        container_name,
        "--format",
        "{{.Config.Image}}",
        check=False,
    )
    if inspect.returncode != 0:
        # Container isn't running (or doesn't exist yet). _check_service_container
        # already surfaced that; skip the drift comparison and just report the pin.
        report.add(
            f"{agent_name}.version",
            "ok",
            f"pinned at {pinned_tag} (container not running; container check has the details)",
        )
        return
    running_image = inspect.stdout.strip()
    running_tag = running_image.split(":", 1)[1] if ":" in running_image else "?"

    # Hermes carries a dual versioning scheme: the image tags / GitHub
    # release tags are calver (v2026.7.7.2) while the app itself also
    # has a semver identity (v0.18.2). `hermes --version` prints both —
    # "Hermes Agent v0.18.2 (2026.7.7.2) · upstream 9de9c25f" — so
    # surface the semver alongside the tag. Best-effort: any exec or
    # parse failure (older image, container mid-restart) just omits it.
    semver = ""
    if agent_type == "hermes":
        ver = mp.exec_in_vm(
            cfg.vm.name,
            *_compose_exec_in_vm_argv(cfg, svc, "hermes", "--version", no_tty=True),
            check=False,
        )
        if ver.returncode == 0:
            # Since v0.21.6 the release tag IS the semver and the calver
            # parenthetical may be absent, so the "(" is optional.
            m = re.search(r"Hermes Agent (v\d+\.\d+(?:\.\d+)?)", ver.stdout)
            if m:
                semver = m.group(1)
    suffix = f" (app {semver})" if semver and semver != running_tag else ""

    if running_tag == pinned_tag:
        report.add(f"{agent_name}.version", "ok", f"{running_tag}{suffix}")
    else:
        report.add(
            f"{agent_name}.version",
            "warn",
            (
                f"container running {running_tag}{suffix} but compose pins {pinned_tag} — "
                f"run `agx restart {agent_name}` to apply the pinned version"
            ),
        )


def _check_access_views(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Registered `agx access mount` views that aren't (correctly)
    mounted — typically a host reboot without the fstab line, or a view
    left behind pointing at a deleted directory.

    Opt-in: silent when no view was ever registered. ``warn`` not
    ``fail`` — views are a host-side convenience, the agents don't
    depend on them. Mount state comes from ``/proc/self/mountinfo``
    (``os.path.ismount`` can't see a same-filesystem bind mount).
    """
    if cfg.paths.data_dir_in_vm:
        return
    from agx_cli import access_cmd

    try:
        rows = access_cmd.view_rows(cfg)
    except (OSError, UserError):
        return
    for row in rows:
        if row["status"] == "mounted":
            continue
        hint = (
            "add its fstab line (`agx access list --json` shows it) or re-run "
            f"`agx access mount {row['agent']}`"
            if row["status"] == "not-mounted"
            else f"run `agx access unmount {row['agent']}`, then mount again"
        )
        report.add(
            f"{row['agent']}.host_view",
            "warn",
            f"host view {row['view']} is {row['status']} — {hint}.",
        )


def _check_hermes_data_dir_accessible(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Probe whether ``agx exec`` (running as :data:`RUNTIME_UID`) can
    actually stat ``/opt/data``.

    The recurring failure mode: image entrypoint didn't ``usermod`` the
    in-container ``hermes`` user to ``HERMES_UID``, so ``/opt/data`` is
    owned by uid 10000 with mode 700, and ``agx exec`` (which runs
    as uid 1000) hits ``EACCES`` on every traversal. Symptom is
    ``hermes <anything>`` crashing on import with
    ``PermissionError: '/opt/data/.env'``.

    This probe mimics what an ordinary exec would do — runs as the
    runtime user (no ``as_root``), tries to ``stat /opt/data``. If it
    fails, the remediation is ``agx fix-perms <agent>``.
    """
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "stat", "-c", "%a %u", "/opt/data", no_tty=True),
        check=False,
    )
    if result.returncode == 0:
        parts = result.stdout.strip().split()
        if len(parts) == 2:
            mode, uid = parts
            try:
                uid_int = int(uid)
            except ValueError:
                uid_int = -1
            detail = f"mode={mode} owner={uid}"
            if uid_int == RUNTIME_UID:
                report.add(f"{agent_name}.data_dir_access", "ok", detail)
            else:
                report.add(
                    f"{agent_name}.data_dir_access",
                    "warn",
                    f"{detail}; toolkit exec runs as uid {RUNTIME_UID}. "
                    f"`agx exec {agent_name} hermes ...` may hit EACCES — "
                    f"run `agx fix-perms {agent_name}`.",
                )
            return
    err = (result.stderr or "").lower()
    if "is not running" in err or "no such" in err:
        report.add(f"{agent_name}.data_dir_access", "warn", "container not up")
        return
    report.add(
        f"{agent_name}.data_dir_access",
        "fail",
        f"`stat /opt/data` as uid {RUNTIME_UID} failed: "
        f"{(result.stderr or '').strip() or 'rc=' + str(result.returncode)}. "
        f"Run `agx fix-perms {agent_name}`.",
    )


_HERMES_DOCTOR_ISSUES_RE = re.compile(r"Found (\d+) issue\(s\)")
_HERMES_DOCTOR_MAX_DETAIL_LINES: Final = 4


def _check_hermes_doctor(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Run ``hermes doctor`` in the container and classify the outcome.

    Two very different non-zero exits must not be conflated:

    - ``hermes doctor`` RAN and reported issues. Hermes ≥ v0.21.6 exits 1
      whenever it finds any issue — including advisory ``⚠`` items such
      as "state.db is in WAL mode on a cross-VM filesystem" — whereas
      ≤ v2026.9.x exited 0 with the same findings. Mapping that to
      ``fail`` made ``agx upgrade`` roll back a perfectly healthy
      v0.21.6 (2026-10-10). It is a ``warn`` carrying the issue lines.
    - ``hermes doctor`` could NOT run (binary missing, import crash,
      container down): ``fail`` — that is the "upgrade broke the agent"
      signal this check exists for.
    """
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "hermes", "doctor", no_tty=True),
        check=False,
    )
    if result.returncode == 0:
        report.add(f"{agent_name}.hermes_doctor", "ok")
        return
    output = f"{result.stdout}\n{result.stderr}"
    issues = _HERMES_DOCTOR_ISSUES_RE.search(output)
    if issues is None:
        report.add(
            f"{agent_name}.hermes_doctor",
            "fail",
            result.stderr.strip() or result.stdout.strip(),
        )
        return
    flagged = [line.strip() for line in output.splitlines() if line.strip().startswith(("✗", "⚠"))]
    shown = "; ".join(line[:160] for line in flagged[:_HERMES_DOCTOR_MAX_DETAIL_LINES])
    more = len(flagged) - _HERMES_DOCTOR_MAX_DETAIL_LINES
    detail = f"`hermes doctor` reported {issues.group(1)} issue(s)"
    if shown:
        detail += f": {shown}" + (f" (+{more} more)" if more > 0 else "")
    detail += f" — full list: `agx exec {agent_name} -- hermes doctor`"
    report.add(f"{agent_name}.hermes_doctor", "warn", detail)


# One in-container pass over every secondary Hermes profile: emit one
# "<name>\t<svstat main>\t<svstat log>\t<gateway_state.json>" line per
# profile that has a gateway_state.json. Quiet no-op (rc 0, no output)
# when /opt/data/profiles doesn't exist. Runs as container-root:
# s6-svstat reads the supervise/ control dir, which the runtime uid
# can't. Tabs/newlines are stripped from the JSON so the line count and
# the field count stay stable.
_PROFILE_GATEWAY_PROBE = (
    "for d in /opt/data/profiles/*/; do "
    's="$d/gateway_state.json"; [ -f "$s" ] || continue; '
    'n=$(basename "$d"); '
    'main=$(/command/s6-svstat "/run/service/gateway-$n" 2>&1 | tr -d "\\n"); '
    'logsvc=$(/command/s6-svstat "/run/service/gateway-$n/log" 2>&1 | tr -d "\\n"); '
    'state=$(tr -d "\\n\\r\\t" < "$s" 2>/dev/null); '
    'printf \'%s\\t%s\\t%s\\t%s\\n\' "$n" "$main" "$logsvc" "$state"; '
    "done"
)

_SVSTAT_SECONDS_RE = re.compile(r"(\d+) seconds")


def _svstat_seconds(stat: str) -> int | None:
    m = _SVSTAT_SECONDS_RE.search(stat)
    return int(m.group(1)) if m else None


# Scans every profile home (default + named) for a dashboard.address
# outside the loopback set. Toolkit writes are refused at write time
# (hermes_config.validate_dashboard_binding), but hand edits inside the
# container and imported profile distributions bypass that — and a
# non-loopback bind in ANY profile is reachable from sibling containers
# on the compose network even without a published port (GH #6 tier 2).
_PROFILE_DASHBOARD_PROBE = """\
import glob, json
try:
    import yaml
except Exception:
    print(json.dumps({"error": "no yaml"}))
    raise SystemExit(0)
rows = []
paths = ["/opt/data/config.yaml"] + sorted(glob.glob("/opt/data/profiles/*/config.yaml"))
for path in paths:
    name = "default" if path == "/opt/data/config.yaml" else path.split("/")[-2]
    try:
        doc = yaml.safe_load(open(path)) or {}
    except Exception:
        continue
    addr = ((doc.get("dashboard") or {}).get("address"))
    if addr is not None:
        rows.append({"profile": name, "address": str(addr)})
print(json.dumps({"rows": rows}))
"""


def _check_profile_dashboard_bindings(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Warn when any profile's config binds the dashboard off-loopback.

    Complements the write-time refusal in ``hermes_config`` — that guard
    only covers toolkit-driven writes to the profile being edited; this
    check sweeps every profile home in the container, catching hand
    edits and imported profile distributions.
    """
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "python3", "-c", _PROFILE_DASHBOARD_PROBE, no_tty=True, as_root=True
        ),
        check=False,
    )
    if result.returncode != 0:
        return  # container down — container_running already reports it
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    offenders = [
        r
        for r in data.get("rows", [])
        if isinstance(r, dict) and r.get("address") not in ("127.0.0.1", "localhost", "::1")
    ]
    if offenders:
        detail = ", ".join(f"{r['profile']}={r['address']!r}" for r in offenders)
        report.add(
            f"{agent_name}.profile_dashboards",
            "warn",
            f"non-loopback dashboard.address in profile config(s): {detail} — "
            f"reachable from sibling containers on the compose network; "
            f"bind 127.0.0.1 and publish via the override file instead",
        )


def _check_profile_gateways(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Warn when a secondary Hermes profile's gateway should be running
    but its s6 service is down or flapping (GH #6 tier 1).

    Profile gateways are hand-managed (``hermes -p <name> gateway
    start``) and otherwise invisible to the toolkit — this check is the
    only toolkit-side eye on them. "Should be running" comes from the
    profile's ``gateway_state.json`` (``desired_state``; upstream's boot
    reconciler uses the same record, and only an explicit ``gateway
    stop`` writes ``stopped``). A log service that is down or keeps
    restarting is the missing-log-dir hot-loop class: the s6-log leaf
    under ``/opt/data/logs/gateways/<profile>`` can't be created by the
    image's root-side mkdir on a 9p/virtiofs mount, and a restored or
    imported data dir arrives without it. A container restart re-runs
    the cont-init hook, which pre-creates the leaf for every existing
    profile.

    Adds no check at all for the common cases doctor already covers or
    that carry no signal: container not up, no secondary profiles, or a
    state file that isn't valid JSON.
    """
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "sh", "-c", _PROFILE_GATEWAY_PROBE, no_tty=True, as_root=True
        ),
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or "").lower()
        if "is not running" in err or "no such" in err:
            return  # container not up — container_running already reports it
        report.add(
            f"{agent_name}.profile_gateways",
            "warn",
            f"probe failed: {(result.stderr or '').strip() or 'rc=' + str(result.returncode)}",
        )
        return
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        return  # no secondary profiles — the common case
    healthy: list[str] = []
    problems: list[str] = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, main_stat, log_stat, raw_state = parts
        try:
            state = json.loads(raw_state)
        except ValueError:
            continue
        if not isinstance(state, dict):
            continue
        desired = state.get("desired_state") or state.get("gateway_state")
        if desired != "running":
            healthy.append(f"{name}: stopped (deliberate)")
            continue
        if not main_stat.startswith("up "):
            problems.append(
                f"{name}: wants running but s6 reports {main_stat!r} — "
                f"`agx exec {agent_name} hermes -p {name} gateway start`"
            )
            continue
        log_secs = _svstat_seconds(log_stat)
        if not log_stat.startswith("up ") or (log_secs is not None and log_secs <= 5):
            problems.append(
                f"{name}: gateway up but its log service reports {log_stat!r} — "
                f"possibly the missing-log-dir hot-loop; `agx restart {agent_name}` "
                "re-runs the cont-init pre-create"
            )
            continue
        healthy.append(f"{name}: up")
    if problems:
        report.add(f"{agent_name}.profile_gateways", "warn", "; ".join(problems))
    elif healthy:
        report.add(f"{agent_name}.profile_gateways", "ok", "; ".join(healthy))


def _check_codex_auth(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Verify the toolkit-scoped Codex auth (used ONLY by the Codex
    sandbox skill, ``agx codex skill-enable``) is present and not
    near expiry. Hermes agents' ``openai-codex`` LLM-provider auth is
    NOT this file — see ``_check_hermes_credential_pool``.

    Reports OK if no auth file exists yet (the skill is optional);
    warns when the token expires in less than 7 days; fails only on
    malformed JSON or parse-but-already-expired.
    """
    import datetime
    import json as _json

    from agx_cli.codex import EXPIRY_WARNING_DAYS, _parse_expiry

    auth_path = cfg.paths.host_data_dir / ".codex" / "auth.json"
    if not auth_path.exists():
        report.add("codex_auth", "ok", "not configured (only the Codex sandbox skill needs it)")
        return
    try:
        data = _json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as e:
        report.add("codex_auth", "fail", f"auth.json unreadable: {e}")
        return

    expires = _parse_expiry(data)
    if expires is None:
        report.add("codex_auth", "ok", "present (expiry unknown)")
        return
    now = datetime.datetime.now(datetime.UTC)
    delta = expires - now
    if delta.days < 0:
        report.add("codex_auth", "fail", f"expired {(-delta.days)} day(s) ago")
    elif delta.days < EXPIRY_WARNING_DAYS:
        report.add("codex_auth", "warn", f"expires in {delta.days} day(s)")
    else:
        report.add("codex_auth", "ok", f"expires in {delta.days} day(s)")


_CREDENTIAL_BAD_STATUSES: Final = frozenset({"exhausted", "dead"})


def _check_hermes_credential_pool(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Fail when every pooled credential of the agent's PRIMARY provider
    is ``exhausted``/``dead`` in Hermes' own auth store.

    This is the "agent answers *Provider authentication failed*" class:
    a ChatGPT-subscription Codex OAuth chain dies without warning (Hermes
    upstream #65346, unresolved) and there is no reliable pre-warning —
    the access token is refreshed continuously and says nothing about
    when the refresh chain will stop working. What IS possible is prompt
    detection: Hermes marks the credential the moment a request fails
    (``last_status`` = ``exhausted`` for retryable errors, ``dead`` for
    terminal ``token_invalidated``/``token_revoked``). ``agx
    doctor-cron`` polls this check and mails on state change.

    Reads ``/opt/data/auth.json`` through the container as root
    (mode-600, hermes-owned) and never surfaces token values — only
    status/reason fields. Provider = ``model.provider`` in config.yaml,
    falling back to ``active_provider`` in auth.json.
    """
    from agx_cli import hermes_config

    cat = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "cat", "/opt/data/auth.json", no_tty=True, as_root=True
        ),
        check=False,
    )
    if cat.returncode != 0:
        err = (cat.stderr or "").lower()
        if "is not running" in err or "no such" in err:
            report.add(f"{agent_name}.credential_pool", "warn", "container not up")
        else:
            report.add(f"{agent_name}.credential_pool", "ok", "skipped (no auth.json yet)")
        return
    try:
        auth = json.loads(cat.stdout)
    except json.JSONDecodeError:
        report.add(f"{agent_name}.credential_pool", "warn", "auth.json unparseable")
        return

    provider = ""
    try:
        doc = hermes_config.read_doc(cfg, agent_name)
        raw_model = doc.get("model")
        if isinstance(raw_model, dict):
            provider = str(raw_model.get("provider") or "")
    except (RuntimeFailure, UserError):
        pass
    if not provider or provider == "auto":
        provider = str(auth.get("active_provider") or "")
    if not provider:
        report.add(f"{agent_name}.credential_pool", "ok", "skipped (no primary provider set)")
        return

    pool = auth.get("credential_pool") or {}
    entries = pool.get(provider) if isinstance(pool, dict) else None
    if not isinstance(entries, list) or not entries:
        report.add(
            f"{agent_name}.credential_pool",
            "warn",
            f"no pooled credential for primary provider {provider!r}",
        )
        return

    bad = [
        e
        for e in entries
        if isinstance(e, dict) and str(e.get("last_status") or "") in _CREDENTIAL_BAD_STATUSES
    ]
    if not bad:
        report.add(
            f"{agent_name}.credential_pool",
            "ok",
            f"{provider}: {len(entries)} credential(s) healthy",
        )
        return

    reasons = sorted(
        {
            f"{e.get('last_status')}"
            + (f"/{e.get('last_error_reason')}" if e.get("last_error_reason") else "")
            for e in bad
        }
    )
    detail = f"{provider}: {len(bad)}/{len(entries)} credential(s) {', '.join(reasons)}"
    if provider == "openai-codex":
        detail += (
            f" — re-auth: `agx exec {agent_name} -- hermes auth add openai-codex "
            f"--type oauth --no-browser`"
        )
    report.add(
        f"{agent_name}.credential_pool",
        "fail" if len(bad) == len(entries) else "warn",
        detail,
    )


def _check_inference_health(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Probe in-VM inference engines that the toolkit owns.

    Ollama (``inference_ollama``): gated on ``inference.ollama_enabled``;
    hits ``ollama list`` via ``docker exec`` (no network probe needed
    since we're already in the daemon's domain).

    llama-cpp (``inference_llamacpp``): every ``llama-cpp-*`` service
    defined in compose gets a container-state probe. Defined-but-not-
    running is the VM-rebuild stranding class (same failure mode
    ``_check_firecrawl_running`` covers — the compose blocks survive a
    VM migration, the containers don't), so it warns with the recovery
    hint. Gated on compose presence, not on any config flag, so it
    still fires when config was lost alongside the VM.

    Skips (``ok``) when no engine is enabled/defined — most users won't
    have local inference up.
    """
    _check_inference_ollama(cfg, report)
    _check_inference_llamacpp(cfg, report)


def _check_inference_ollama(cfg: AgentsConfig, report: DoctorReport) -> None:
    if not cfg.inference.ollama_enabled:
        report.add(
            "inference_ollama",
            "ok",
            "skipped (not enabled; `agx inference up ollama` to start)",
        )
        return
    from agx_cli.compose import OLLAMA_SERVICE_NAME

    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        OLLAMA_SERVICE_NAME,
        "ollama",
        "list",
        check=False,
    )
    if result.returncode == 0:
        model_count = max(0, len(result.stdout.strip().splitlines()) - 1)
        report.add(
            "inference_ollama",
            "ok",
            f"healthy at http://{OLLAMA_SERVICE_NAME}:11434/v1 ({model_count} model(s) loaded)",
        )
    else:
        err = (result.stderr or "").strip()
        report.add(
            "inference_ollama",
            "fail",
            f"`ollama list` failed inside container: {err or 'rc=' + str(result.returncode)}. "
            f"Try `agents restart-service ollama` or `agx inference down ollama && agx inference up ollama`.",
        )


def _check_inference_llamacpp(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Warn on per-model llama-cpp services that are defined in compose
    but whose containers aren't running — mirrors
    ``_check_firecrawl_running``'s compose-presence gating.
    """
    from agx_cli.compose import LLAMACPP_SERVICE_PREFIX

    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        report.add("inference_llamacpp", "warn", "compose file unreadable")
        return
    services = sorted(
        s for s in (doc.get("services") or {}) if s.startswith(LLAMACPP_SERVICE_PREFIX)
    )
    if not services:
        report.add("inference_llamacpp", "ok", "skipped (no per-model services defined)")
        return

    result = compose_runner.ps(cfg)
    if result.returncode != 0:
        report.add("inference_llamacpp", "warn", "docker compose ps failed; cannot probe")
        return
    stranded = [
        s
        for s in services
        if _container_state_for(result.stdout, s).lower() not in ("running", "healthy")
    ]
    if stranded:
        report.add(
            "inference_llamacpp",
            "warn",
            f"defined in compose but not running: {', '.join(stranded)}. "
            "A VM rebuild/migration drops in-VM container state; re-run "
            "`agx inference load llama-cpp <slug> --gguf <path>` (the "
            "compose blocks and GGUFs are preserved) or `agx inference "
            "down llama-cpp` if no longer wanted.",
        )
    else:
        report.add(
            "inference_llamacpp",
            "ok",
            f"{len(services)} per-model service(s) running",
        )


def _check_arm64_emulation(cfg: AgentsConfig, report: DoctorReport) -> None:
    """On arm64 hosts, surface which active project-level images will be
    running under qemu-user emulation because upstream doesn't ship an
    arm64 variant.

    Currently the only one is Firecrawl's ``nuq-postgres`` — works under
    emulation, but slower at init time and on heavy queries. The check
    only fires when firecrawl is actually installed (otherwise there's
    nothing to emulate).

    Not a failure — just visibility. Users who hit performance issues
    on RPi5 / arm64 Linux can swap in a stock ``postgres:16`` via
    ``docker-compose.override.yml`` and run Firecrawl's schema
    migrations against it.
    """
    from agx_cli import firecrawl_cmd

    if firecrawl_cmd._docker_arch() != "arm64":
        report.add("arm64_emulation", "ok", "host is amd64; no emulation in play")
        return
    if not firecrawl_cmd.is_installed(cfg):
        report.add("arm64_emulation", "ok", "host arm64; firecrawl not installed")
        return
    report.add(
        "arm64_emulation",
        "warn",
        "host is arm64. Running under qemu-user emulation: "
        + "; ".join(firecrawl_cmd.ARM64_EMULATED_IMAGES)
        + ". Works but is slower; swap via docker-compose.override.yml if it bites.",
    )


def _check_tailscale(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Warn when toolkit-managed tailscale isn't connected.

    Gated on the state file: a deployment that never ran `agx
    tailscale up` gets no check at all (opt-in feature, like firecrawl).
    Skips on bare_metal, where the command family refuses anyway.
    """
    from agx_cli import tailscale_cmd

    if cfg.vm.backend not in ("multipass", "libvirt") or not tailscale_cmd.is_installed(cfg):
        return
    vm_state = tailscale_cmd._vm_status(cfg)
    if not vm_state.get("installed"):
        report.add(
            "tailscale",
            "warn",
            "state file present but tailscale binary missing in the VM (rebuilt VM?) — "
            "re-run `agx tailscale up`",
        )
        return
    backend_state = vm_state.get("backend_state", "")
    if backend_state == "Running":
        ips = ", ".join(vm_state.get("ips", []))
        report.add("tailscale", "ok", f"Running ({ips})")
    else:
        detail = vm_state.get("error") or f"backend_state={backend_state or 'unknown'}"
        report.add("tailscale", "warn", f"{detail} — `agx tailscale up` to (re)connect")


def _check_firecrawl_running(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Flag a firecrawl stack that's defined in compose but not running
    while agents are still wired to use it.

    This is the exact state a VM-backend migration leaves behind. The
    host data dir survives wholesale (compose service blocks, project
    ``.env``, postgres data, each agent's ``env_passthrough`` wiring),
    but the sidecar containers live only in the *old* VM's Docker engine
    and never cross over — so ``firecrawl status`` reports them ``absent``
    and a wired agent's scrape calls fail with connection-refused,
    silently, until someone re-runs ``agx firecrawl up``.

    Nothing else surfaces it: ``firecrawl_cmd.is_installed`` (the gate on
    the arm64 check) additionally requires ``.firecrawl-state.json``, the
    digest-pin record — which is *also* lost in the migration — so the
    is_installed-gated checks skip too. This check deliberately gates on
    compose-presence alone so it still fires with the state file gone.

    Gating the warning on still-wired agents is what separates a
    migration-loss from a deliberate ``agx firecrawl down``: ``down``
    drops the wiring, so a stack that's stopped *and* unwired is a clean,
    intentional stop and stays quiet here. Severity is ``warn`` (not
    ``fail``) — firecrawl is an optional sidecar; a wired agent's chat
    keeps working, only scraping is degraded, so it shouldn't trip
    doctor's exit-3 / break cron that gates on it.
    """
    from agx_cli import firecrawl_cmd

    services = firecrawl_cmd._firecrawl_services_in_compose(cfg)
    if not services:
        report.add("firecrawl_running", "ok", "not installed")
        return
    if firecrawl_cmd._service_running(cfg, "firecrawl-api"):
        report.add("firecrawl_running", "ok", "running")
        return
    wired = firecrawl_cmd._list_wired_agents(cfg)
    if not wired:
        report.add(
            "firecrawl_running",
            "ok",
            "installed but stopped; no agents wired (looks like a deliberate `agx firecrawl down`)",
        )
        return
    is_are = "is" if len(wired) == 1 else "are"
    report.add(
        "firecrawl_running",
        "warn",
        f"defined in compose and {', '.join(wired)} {is_are} wired to it, "
        "but the containers aren't running — a VM rebuild/migration drops "
        "the in-VM Docker state. Scrape calls fail until you run "
        "`agx firecrawl up` (reuses the pinned digests).",
    )


def _check_hermes_model_set(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Warn when an active provider is configured in ``auth.json`` but
    the top-level ``model:`` in ``config.yaml`` is empty.

    Symptom of this misconfiguration: headless aux calls (compression,
    cron jobs, vision in ``provider: auto`` mode) silently fail with
    ``RuntimeError: <Provider> Responses request 'model' must be a
    non-empty string``. Interactive chat keeps working because the
    gateway resolves a per-session model default at session-create
    time, but that fallback isn't applied to aux code paths.

    Often masked downstream as ``Compression summary failed: [Errno 9]
    Bad file descriptor`` — when the empty-model ``RuntimeError`` fires
    mid-stream, the compression cleanup tries to close an already-broken
    HTTP stream and the EBADF wrapper hides the real cause.
    """
    from agx_cli import hermes_config

    try:
        doc = hermes_config.read_doc(cfg, agent_name)
    except (RuntimeFailure, UserError) as e:
        report.add(f"{agent_name}.model_set", "warn", f"config.yaml unreadable: {e}")
        return

    # Hermes accepts ``model:`` in two shapes:
    #   - bare string:  model: gpt-5.5
    #   - structured:   model: {default: gpt-5.5, provider: ..., base_url: ...}
    # The wizard (`hermes model`) writes the structured form; older
    # configs and `hermes config set model X` write the bare form.
    # Both are valid; "is set" means the resolved name is non-empty.
    raw_model = doc.get("model")
    if isinstance(raw_model, dict):
        model_name = raw_model.get("default") or raw_model.get("name") or ""
    elif isinstance(raw_model, str):
        model_name = raw_model
    else:
        model_name = ""

    cat = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "cat", "/opt/data/auth.json", no_tty=True, as_root=True
        ),
        check=False,
    )
    if cat.returncode != 0:
        # Container down or no auth.json yet — nothing to compare against.
        err = (cat.stderr or "").lower()
        if "is not running" in err or "no such" in err:
            report.add(f"{agent_name}.model_set", "warn", "container not up")
        else:
            report.add(f"{agent_name}.model_set", "ok", "skipped (no auth.json yet)")
        return
    try:
        auth = json.loads(cat.stdout)
    except json.JSONDecodeError:
        report.add(f"{agent_name}.model_set", "warn", "auth.json unparseable")
        return
    active = auth.get("active_provider") if isinstance(auth, dict) else None
    if not active:
        report.add(f"{agent_name}.model_set", "ok", "skipped (no active provider)")
        return

    if model_name.strip():
        report.add(
            f"{agent_name}.model_set",
            "ok",
            f"model={model_name!r}, provider={active!r}",
        )
        return

    report.add(
        f"{agent_name}.model_set",
        "warn",
        f"config.yaml `model:` is empty but auth.json active_provider={active!r}. "
        f"Headless aux calls (compression, cron, vision auto-fallback) will silently "
        f"fail with \"<provider> request 'model' must be a non-empty string\" — "
        f"interactive chat works only because the gateway resolves a per-session "
        f"runtime default. Fix: `agx exec {agent_name} -- hermes model` "
        f"(interactive wizard).",
    )


def _emit_doctor_table(report: DoctorReport) -> None:
    """Render the doctor results, wrapping the ``detail`` column to the
    terminal width so a long detail message doesn't push the column
    headers + separator off the right edge.

    The first two columns (check name, status) have bounded widths
    derived from the rows; the third (detail) takes whatever's left of
    the terminal width and wraps onto continuation lines indented under
    the detail column.

    Under ``--json`` falls through to ``ui.table`` which emits the
    full unwrapped JSON form for machine consumers.
    """
    rows = [{"check": c.name, "status": c.status, "detail": c.detail} for c in report.checks]
    if ui.is_json():
        ui.table(rows, ["check", "status", "detail"])
        return

    term_width = shutil.get_terminal_size((120, 20)).columns
    check_w = max(len("check"), max((len(r["check"]) for r in rows), default=0))
    status_w = max(len("status"), max((len(r["status"]) for r in rows), default=0))
    # 2-space gutters between columns. Floor at 20 so wrapping doesn't
    # produce a comically narrow detail column on tiny terminals.
    detail_w = max(20, term_width - check_w - status_w - 4)

    sep = "  "
    indent = f"{'':<{check_w}}{sep}{'':<{status_w}}{sep}"

    def row_lines(check: str, status: str, detail: str) -> list[str]:
        wrapped = textwrap.wrap(detail or "", width=detail_w) or [""]
        first = f"{check:<{check_w}}{sep}{status:<{status_w}}{sep}{wrapped[0]}"
        return [first, *(f"{indent}{cont}" for cont in wrapped[1:])]

    ui.echo_human(f"{'check':<{check_w}}{sep}{'status':<{status_w}}{sep}{'detail':<{detail_w}}")
    ui.echo_human(f"{'-' * check_w}{sep}{'-' * status_w}{sep}{'-' * detail_w}")
    for r in rows:
        for line in row_lines(r["check"], r["status"], r["detail"]):
            ui.echo_human(line)
