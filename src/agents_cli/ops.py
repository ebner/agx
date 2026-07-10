"""Day-2 ops: start, stop, restart, logs, exec, setup, doctor."""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import click

from agents_cli import compose, compose_runner, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import (
    _agent_type_from_service,
    _detect_type_from_compose,
    _name_from_service,
)
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.config import AgentsConfig
from agents_cli.errors import HealthCheckFailure, RuntimeFailure, UserError
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
    as_root: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """``agents exec <name> [--root] [--] <cmd...>``.

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
) -> DoctorReport:
    cfg = cfg_mod.load(config_path)
    report = DoctorReport()

    _check_vm_reachable(cfg, report)
    if cfg.paths.host_data_dir.exists():
        _check_mount_round_trip(cfg, report)
        _check_user_docker_service(cfg, report)
        _check_disk_space(cfg, report)
        _check_vm_memory(cfg, report)
        _check_vm_load(cfg, report)
        _check_journals(cfg, report)
        _check_port_conflicts(cfg, report)
        _check_codex_auth(cfg, report)
        _check_arm64_emulation(cfg, report)
        _check_firecrawl_running(cfg, report)
        _check_inference_health(cfg, report)
    else:
        report.add("data_dir_exists", "fail", f"{cfg.paths.host_data_dir} missing")

    for svc, agent_type, agent_name in _services_to_check(cfg, target):
        _check_service_container(cfg, svc, agent_name, agent_type, report)
        _check_agent_version(cfg, svc, agent_name, agent_type, report)
        _check_host_access(cfg, agent_name, agent_type, report)
        if agent_type == "hermes":
            _check_hermes_data_dir_accessible(cfg, svc, agent_name, report)
            _check_hermes_doctor(cfg, svc, agent_name, report)
            _check_hermes_codex_mount(cfg, svc, agent_name, report)
            _check_hermes_model_set(cfg, svc, agent_name, report)

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
# `agents exec coder hermes pairing approve ...` runs as root, the
# files it creates are owned by root — but the gateway runs as uid 1000
# and can't read them. Every interactive / wizard exec MUST default to
# the runtime user. Admin operations that need root can opt in via the
# CLI `--root` flag.
RUNTIME_UID: int = 1000
RUNTIME_GID: int = 1000


def compose_exec_argv(
    cfg: AgentsConfig,
    svc: str,
    *cmd: str,
    no_tty: bool = False,
    as_root: bool = False,
) -> list[str]:
    """Build the full argv (incl. ``multipass exec ...``) for a
    ``docker compose exec`` inside the VM. Use this for ``os.execvp``
    paths (interactive shells, wizards).
    """
    return [
        *_multipass_compose_argv(cfg),
        "exec",
        *([] if as_root else ["--user", f"{RUNTIME_UID}:{RUNTIME_GID}"]),
        *(["-T"] if no_tty else []),
        svc,
        *cmd,
    ]


def _compose_exec_in_vm_argv(
    cfg: AgentsConfig,
    svc: str,
    *cmd: str,
    no_tty: bool = False,
    as_root: bool = False,
    env: dict[str, str] | None = None,
) -> list[str]:
    """The in-VM half of ``compose_exec_argv`` — drops the ``multipass
    exec <vm> --`` prefix so it can be passed as ``*cmd`` to
    ``mp.exec_in_vm``. Same user/tty semantics.

    ``env`` maps environment variables to inject via ``-e KEY=VALUE``
    on the ``compose exec`` subcommand.
    """
    root = str(cfg.paths.vm_agents_root)
    env_args: list[str] = []
    for key, value in (env or {}).items():
        env_args += ["-e", f"{key}={value}"]
    return [
        "docker",
        "compose",
        "--project-directory",
        root,
        "-f",
        f"{root}/docker-compose.yml",
        "--env-file",
        f"{root}/.env",
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
    from agents_cli import hermes_config

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
    new pin in ``agents list`` but the agent is still on the old code.
    Surface that as a ``warn`` so the user knows to ``agents restart``.

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

    if running_tag == pinned_tag:
        report.add(f"{agent_name}.version", "ok", running_tag)
    else:
        report.add(
            f"{agent_name}.version",
            "warn",
            (
                f"container running {running_tag} but compose pins {pinned_tag} — "
                f"run `agents restart {agent_name}` to apply the pinned version"
            ),
        )


def _check_host_access(
    cfg: AgentsConfig, agent_name: str, agent_type: str, report: DoctorReport
) -> None:
    """Warn if the host user can't reach the agent's data dir via the
    filesystem (i.e. no POSIX ACL granting access, and the dir is
    owned by a foreign uid — typically the rootless-Docker subuid).

    Emits ``warn`` not ``fail`` because the toolkit itself works fine
    without host access — it's a user-ergonomics check. The hint
    points at ``agents access grant`` for the fix.

    Skipped on bare-metal (container uid maps to host user; files are
    already accessible) and in ``--data-dir-in-vm`` mode (the dirs
    aren't on the host at all).
    """
    if cfg.vm.backend == "bare_metal" or cfg.paths.data_dir_in_vm:
        return
    from agents_cli import access_cmd, permissions

    try:
        dirs = access_cmd._agent_dirs(cfg, agent_name, agent_type)
    except (UserError, Exception):
        return
    for d in dirs:
        status = permissions.assess_host_access(d, cfg.host.uid)
        if status == "missing":
            report.add(
                f"{agent_name}.host_access",
                "warn",
                f"{d} is owned by a foreign uid with no ACL for host user "
                f"{cfg.host.uid} — run `agents access grant {agent_name}` to fix.",
            )
            return
        if status == "masked":
            report.add(
                f"{agent_name}.host_access",
                "warn",
                f"{d} has an ACL entry for host user {cfg.host.uid} but it's "
                f"masked to ---. Re-run `agents access grant {agent_name}` to "
                f"apply the mask reset and unblock reads.",
            )
            return


def _check_hermes_data_dir_accessible(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Probe whether ``agents exec`` (running as :data:`RUNTIME_UID`) can
    actually stat ``/opt/data``.

    The recurring failure mode: image entrypoint didn't ``usermod`` the
    in-container ``hermes`` user to ``HERMES_UID``, so ``/opt/data`` is
    owned by uid 10000 with mode 700, and ``agents exec`` (which runs
    as uid 1000) hits ``EACCES`` on every traversal. Symptom is
    ``hermes <anything>`` crashing on import with
    ``PermissionError: '/opt/data/.env'``.

    This probe mimics what an ordinary exec would do — runs as the
    runtime user (no ``as_root``), tries to ``stat /opt/data``. If it
    fails, the remediation is ``agents fix-perms <agent>``.
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
                    f"`agents exec {agent_name} hermes ...` may hit EACCES — "
                    f"run `agents fix-perms {agent_name}`.",
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
        f"Run `agents fix-perms {agent_name}`.",
    )


def _check_hermes_doctor(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "hermes", "doctor", no_tty=True),
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


def _check_codex_auth(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Verify the toolkit-scoped Codex auth is present and not near expiry.

    Reports OK if no auth file exists yet (Codex usage is optional);
    warns when the token expires in less than 7 days; fails only on
    malformed JSON or parse-but-already-expired.
    """
    import datetime
    import json as _json

    from agents_cli.codex import EXPIRY_WARNING_DAYS, _parse_expiry

    auth_path = cfg.paths.host_data_dir / ".codex" / "auth.json"
    if not auth_path.exists():
        report.add("codex_auth", "ok", "not configured (skip `agents codex login`)")
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


def _check_hermes_codex_mount(
    cfg: AgentsConfig, svc: str, agent_name: str, report: DoctorReport
) -> None:
    """Verify the Hermes container has the toolkit-scoped Codex auth bound
    at the container user's ``~/.codex`` so the LLM-provider import path
    works on FIRST setup.

    The mount is a one-time import bridge. After ``hermes setup`` runs,
    persistent auth lives at ``/opt/data/auth.json`` (hermes-owned)
    and the bind-mounted ``/home/hermes/.codex/auth.json`` is no longer
    consulted. Critically, under rootless Docker the host-uid-1000-owned
    auth file appears as root-owned mode 600 inside the container, which
    is NOT readable by the runtime hermes user — so after setup, the
    mount being unreadable is expected, not a failure.

    Decision tree:

    - No host auth → skip (Codex not configured).
    - Host auth + persistent in-container auth → ok (post-import).
    - Host auth + no persistent + mount readable → ok (pre-import,
      bridge available).
    - Host auth + no persistent + mount unreadable → fail (setup
      can't import; investigate).
    """
    auth_path = cfg.paths.host_data_dir / ".codex" / "auth.json"
    if not auth_path.exists():
        report.add(f"{agent_name}.codex_mount", "ok", "skipped (no host auth)")
        return

    # Did Hermes already import the auth into its persistent home?
    post_import = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "test", "-r", "/opt/data/auth.json", no_tty=True),
        check=False,
    )
    if post_import.returncode == 0:
        report.add(
            f"{agent_name}.codex_mount",
            "ok",
            "post-import; persistent auth at /opt/data/auth.json",
        )
        return

    # Pre-setup state: the mount must be readable so `hermes setup`
    # can import it. Probe through the container.
    bridge = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "test", "-r", "/home/hermes/.codex/auth.json", no_tty=True
        ),
        check=False,
    )
    if bridge.returncode == 0:
        report.add(f"{agent_name}.codex_mount", "ok", "pre-import; bridge mount readable")
    elif bridge.returncode in (124, 125, 126):
        report.add(f"{agent_name}.codex_mount", "warn", "container not up; skipping")
    else:
        report.add(
            f"{agent_name}.codex_mount",
            "fail",
            f"/home/hermes/.codex/auth.json unreadable AND no persistent "
            f"/opt/data/auth.json — `hermes setup` won't be able to import "
            f"(rc={bridge.returncode})",
        )


def _check_inference_health(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Probe in-VM inference engines that the toolkit owns.

    Ollama: hits ``/api/tags`` via ``docker exec`` (no network probe
    needed since we're already in the daemon's domain). llama-cpp's
    per-model services are checked individually when present.

    Skips silently when no engine is enabled — most users won't have
    local inference up.
    """
    if not cfg.inference.ollama_enabled:
        report.add(
            "inference_ollama",
            "ok",
            "skipped (not enabled; `agents inference up ollama` to start)",
        )
        return
    from agents_cli.compose import OLLAMA_SERVICE_NAME

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
            f"Try `agents restart-service ollama` or `agents inference down ollama && agents inference up ollama`.",
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
    from agents_cli import firecrawl_cmd

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


def _check_firecrawl_running(cfg: AgentsConfig, report: DoctorReport) -> None:
    """Flag a firecrawl stack that's defined in compose but not running
    while agents are still wired to use it.

    This is the exact state a VM-backend migration leaves behind. The
    host data dir survives wholesale (compose service blocks, project
    ``.env``, postgres data, each agent's ``env_passthrough`` wiring),
    but the sidecar containers live only in the *old* VM's Docker engine
    and never cross over — so ``firecrawl status`` reports them ``absent``
    and a wired agent's scrape calls fail with connection-refused,
    silently, until someone re-runs ``agents firecrawl up``.

    Nothing else surfaces it: ``firecrawl_cmd.is_installed`` (the gate on
    the arm64 check) additionally requires ``.firecrawl-state.json``, the
    digest-pin record — which is *also* lost in the migration — so the
    is_installed-gated checks skip too. This check deliberately gates on
    compose-presence alone so it still fires with the state file gone.

    Gating the warning on still-wired agents is what separates a
    migration-loss from a deliberate ``agents firecrawl down``: ``down``
    drops the wiring, so a stack that's stopped *and* unwired is a clean,
    intentional stop and stays quiet here. Severity is ``warn`` (not
    ``fail``) — firecrawl is an optional sidecar; a wired agent's chat
    keeps working, only scraping is degraded, so it shouldn't trip
    doctor's exit-3 / break cron that gates on it.
    """
    from agents_cli import firecrawl_cmd

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
            "installed but stopped; no agents wired "
            "(looks like a deliberate `agents firecrawl down`)",
        )
        return
    is_are = "is" if len(wired) == 1 else "are"
    report.add(
        "firecrawl_running",
        "warn",
        f"defined in compose and {', '.join(wired)} {is_are} wired to it, "
        "but the containers aren't running — a VM rebuild/migration drops "
        "the in-VM Docker state. Scrape calls fail until you run "
        "`agents firecrawl up` (reuses the pinned digests).",
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
    from agents_cli import hermes_config

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
        f"runtime default. Fix: `agents exec {agent_name} -- hermes model` "
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

    click.echo(f"{'check':<{check_w}}{sep}{'status':<{status_w}}{sep}{'detail':<{detail_w}}")
    click.echo(f"{'-' * check_w}{sep}{'-' * status_w}{sep}{'-' * detail_w}")
    for r in rows:
        for line in row_lines(r["check"], r["status"], r["detail"]):
            click.echo(line)
