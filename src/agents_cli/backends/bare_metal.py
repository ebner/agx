"""Bare-metal "backend": run agents directly on the host, no VM.

Why this exists: the multipass/libvirt VM is the toolkit's defense-in-
depth boundary, but on a single-user dev workstation or a dedicated
LLM box (e.g. Strix Halo) the VM is overhead without benefit and
blocks straightforward GPU access. Bare-metal is the explicit opt-out;
the host becomes the trust boundary. See GH #5.

Implementation shape: this backend is mostly thin wrappers around
local ``subprocess``. The toolkit's call sites all go through
``mp.exec_in_vm(vm, *cmd)`` and friends — the ``vm`` argument is just
ignored here, and the command runs on the host. VM-lifecycle methods
(``launch``, ``start_vm``, ``snapshot``, etc.) are no-ops or explicit
refusals. Compose paths are interpreted as host paths because
``vm.init`` sets ``cfg.paths.vm_agents_root = cfg.paths.host_data_dir``
on bare-metal so the unchanged ``compose_runner`` produces correct
argv.

The bootstrap handshake is preserved: ``verify_bootstrap`` (called
from ``vm.init`` in lieu of the install script) probes the same
fields the script would and emits a ``BOOTSTRAP_OK`` line that
``parse_bootstrap_ok`` reads. Per the locked decision in issue #5
the bootstrap is *verify-only*: if rootless Docker isn't installed,
we surface the exact commands and refuse — no surprise sudo on the
user's host.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from agents_cli import ui
from agents_cli.backends import BootstrapStatus, MpResult, parse_bootstrap_ok
from agents_cli.errors import RuntimeFailure, UserError

# Rootless socket every toolkit ``docker`` call targets. Set by
# :func:`configure` from ``[vm_runtime].docker_socket_path``; before
# ``vm init`` has captured it, the rootless default (the same path
# ``verify_bootstrap`` probes and records) applies.
_docker_socket: str | None = None


def configure(docker_socket_path: str) -> None:
    """Pin the rootless socket for this process. ``cli.main`` calls this
    when bare_metal is the active backend (mirrors ``libvirt.configure``).
    """
    global _docker_socket
    _docker_socket = docker_socket_path or None


def _docker_env() -> dict[str, str]:
    """Host env with ``DOCKER_HOST`` forced to the rootless socket.

    In the VM the only daemon is the rootless one. On the host, the
    caller's ambient ``DOCKER_HOST`` / docker context is whatever the
    user's shell (or cron's minimal env) happens to have — possibly a
    rootful daemon, which the security model refuses (see the check in
    :func:`verify_bootstrap`). ``DOCKER_CONTEXT`` is dropped so it
    can't contend with the pinned host.
    """
    env = os.environ.copy()
    env.pop("DOCKER_CONTEXT", None)
    socket_path = _docker_socket or f"/run/user/{os.getuid()}/docker.sock"
    env["DOCKER_HOST"] = f"unix://{socket_path}"
    return env


def _refuse_non_linux() -> None:
    if platform.system() != "Linux":
        raise UserError(
            f"bare_metal backend is Linux-only (host OS: {platform.system()}). "
            "Use --backend multipass on macOS/Windows."
        )


def _run(
    argv: Sequence[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> MpResult:
    ui.info(f"$ {' '.join(argv)}")
    try:
        proc = subprocess.run(
            list(argv),
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=_docker_env(),
        )
    except FileNotFoundError as e:
        raise RuntimeFailure(f"command not found: {argv[0]} ({e})") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeFailure(f"{' '.join(argv)} timed out after {timeout}s") from e
    out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    result = MpResult(stdout=out, stderr=err, returncode=proc.returncode)
    if check and proc.returncode != 0:
        raise RuntimeFailure(
            f"command failed (exit {proc.returncode}): {' '.join(argv)}\nstderr: {err.strip()}"
        )
    return result


# ---------- exec ----------


def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
    return _run(list(cmd), check=check)


def exec_bash(
    vm: str, script: str, *, check: bool = True, timeout: float | None = None
) -> MpResult:
    return _run(["bash", "-s"], check=check, input_bytes=script.encode(), timeout=timeout)


def exec_in_vm_with_stdin(vm: str, *cmd: str, input_bytes: bytes, check: bool = True) -> MpResult:
    return _run(list(cmd), check=check, input_bytes=input_bytes)


def exec_in_vm_interactive(vm: str, *cmd: str) -> int:
    """Inherit stdio so interactive wizards (``hermes setup``, ``openclaw
    onboard``) can prompt the user. Same contract as the multipass/libvirt
    counterparts.
    """
    argv = list(cmd)
    ui.info(f"$ {' '.join(argv)}")
    try:
        result = subprocess.run(argv, check=False, env=_docker_env())
    except FileNotFoundError as e:
        raise RuntimeFailure(f"command not found: {argv[0]} ({e})") from e
    return result.returncode


# ---------- file transfer ----------


def transfer_to_vm(host_path: Path, vm: str, vm_path: str) -> MpResult:
    """Host→VM transfer collapses to a host file copy. ``shutil.copy``
    preserves mode but not ownership — that's fine; the target lives
    under the user's data-dir which is already host-owned.
    """
    try:
        dest = Path(vm_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(host_path, dest)
    except OSError as e:
        return MpResult(stdout="", stderr=str(e), returncode=1)
    return MpResult(stdout="", stderr="", returncode=0)


def transfer_from_vm(vm: str, vm_path: str, host_path: Path) -> MpResult:
    try:
        host_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(vm_path, host_path)
    except OSError as e:
        return MpResult(stdout="", stderr=str(e), returncode=1)
    return MpResult(stdout="", stderr="", returncode=0)


# ---------- mounts ----------


def mount(
    host_path: Path,
    vm: str,
    vm_path: str,
    *,
    host_uid: int,
    host_gid: int,
    vm_uid: int = 1000,
    vm_gid: int = 1000,
    mount_type: str = "native",
) -> MpResult:
    """No-op: the data-dir is the host path. The toolkit invariant is
    that paths under ``cfg.paths.vm_agents_root`` resolve to real on-
    host paths, and on bare-metal we set ``vm_agents_root ==
    host_data_dir`` so there is literally nothing to mount.
    """
    return MpResult(stdout="", stderr="", returncode=0)


def umount(vm: str, vm_path: str) -> MpResult:
    return MpResult(stdout="", stderr="", returncode=0)


# ---------- lifecycle ----------


def launch(vm: str, *, image: str, cpus: int, memory_gb: int, disk_gb: int) -> MpResult:
    """No-op on bare-metal — there's no VM to launch. ``vm.init``
    branches around this for bare-metal and calls :func:`verify_bootstrap`
    instead.
    """
    _refuse_non_linux()
    return MpResult(stdout="", stderr="", returncode=0)


def info_vm(vm: str, *, fmt: str = "json") -> MpResult:
    """Synthesize a multipass-shaped ``info`` payload so the rest of
    the toolkit (vm.status, ops.doctor) doesn't need to special-case.

    Reports the host kernel + a fake "Running" state + the data-dir
    as the single "mount" + the loopback IP (consistent with the
    multipass NAT story where the VM has its own IP).
    """
    uname = platform.uname()
    payload = {
        "info": {
            vm: {
                "state": "Running",
                "release": f"{uname.system} {uname.release}",
                "image_release": uname.release,
                "image_hash": "",
                "ipv4": ["127.0.0.1"],
                "mounts": {},
                "memory_total": "",
                "memory_used": "",
                "disk_total": "",
                "disk_used": "",
            }
        }
    }
    return MpResult(stdout=json.dumps(payload), stderr="", returncode=0)


def list_vms(*, fmt: str = "json") -> MpResult:
    payload = {
        "list": [
            {
                "name": "agents",
                "state": "Running",
                "ipv4": ["127.0.0.1"],
                "release": platform.system(),
            }
        ]
    }
    return MpResult(stdout=json.dumps(payload), stderr="", returncode=0)


def find(*, fmt: str = "json", show_unsupported: bool = False) -> MpResult:
    """No image catalog on bare-metal — the host OS is whatever the
    user is running. ``vm.init`` doesn't call this for bare-metal.
    """
    raise NotImplementedError("find() is multipass-specific; not applicable on bare_metal")


def start_vm(vm: str) -> MpResult:
    return MpResult(
        stdout="", stderr="bare_metal: no VM to start (host is always 'on')\n", returncode=0
    )


def stop_vm(vm: str) -> MpResult:
    return MpResult(
        stdout="", stderr="bare_metal: no VM to stop (host is always 'on')\n", returncode=0
    )


def set_autostart(vm: str, enabled: bool) -> MpResult:
    """No-op: agent containers use ``restart: unless-stopped`` so they
    come back after host reboot via the Docker daemon's own restart
    logic; the rootless daemon is a systemd --user unit that restarts
    via the user's enable-linger setting (asserted in bootstrap).
    """
    return MpResult(stdout="", stderr="", returncode=0)


def set_config(key: str, value: str | int) -> MpResult:
    """No-op: host sizing is real hardware. ``agents vm resize`` and
    ``rebuild`` both refuse on bare-metal at the orchestration layer.
    """
    return MpResult(stdout="", stderr="bare_metal: host sizing is real hardware\n", returncode=0)


def delete_vm(vm: str, *, purge: bool = False) -> MpResult:
    """No-op: there is no VM to delete. ``vm.destroy`` never reaches this
    on bare-metal — it branches early and brings the compose project down
    via ``compose_runner.down`` itself — so this stub exists only to
    satisfy the ``VmBackend`` Protocol. The host data-dir is untouched.
    """
    return MpResult(stdout="", stderr="", returncode=0)


def snapshot(vm: str, name: str, *, comment: str | None = None) -> MpResult:
    raise UserError(
        "snapshots are unavailable on the bare_metal backend (there's no VM disk to snapshot). "
        "Use `agents export` for data-dir backups + a host-level tool (duplicity, restic, rsnapshot) "
        "for full-host snapshots."
    )


def restore(snapshot_id: str, *, destructive: bool = False) -> MpResult:
    raise UserError(
        "snapshot restore is unavailable on the bare_metal backend. Use `agents import` to restore "
        "an `agents export` archive."
    )


def delete_snapshot(vm: str, name: str) -> MpResult:
    """No snapshots exist on bare-metal, so there is never anything to
    delete. Returns a non-zero MpResult (not a raise) so best-effort
    callers like ``agents prune`` degrade quietly instead of aborting."""
    return MpResult(
        stdout="", stderr="snapshots are unavailable on the bare_metal backend", returncode=1
    )


# ---------- bootstrap verification ----------


def verify_bootstrap(data_dir: Path) -> tuple[BootstrapStatus, str, str]:
    """Probe the host for the same fields the in-VM bootstrap script
    checks, and emit a ``BOOTSTRAP_OK`` line so ``parse_bootstrap_ok``
    can consume the result unchanged.

    Per locked decision #4 in issue #5: verify-only. If a check fails,
    the line still gets emitted with ``<field>=fail`` AND a hint line
    is added pointing at the exact command the user needs to run. The
    caller (``vm.init``) will refuse to proceed when any field is
    ``fail``.

    Probed fields (must match REQUIRED_OK_FIELDS / REQUIRED_VALUE_FIELDS
    in :mod:`agents_cli.backends`):

    - ``uidmap`` — ``newuidmap`` on PATH (provided by Ubuntu's
      ``uidmap`` package)
    - ``dbus`` — ``/run/user`` exists and systemd reachable
    - ``lingering`` — ``loginctl show-user`` reports ``Linger=yes``
    - ``docker_host`` — rootless Docker socket present
    - ``daemon`` — ``docker version`` succeeds
    - ``user_socket_active`` — ``systemctl --user is-active docker.service``
    - ``net`` — network reach to ``download.docker.com`` (matches
      the in-VM script's probe)
    - ``ubuntu_uid`` / ``ubuntu_gid`` — values are the *host user's*
      uid/gid (the bootstrap script picks the in-VM ``ubuntu`` user;
      here the host user IS the runtime user)
    - ``socket_path`` — ``/run/user/<uid>/docker.sock``

    Returns ``(status, stdout, stderr)`` for symmetry with the in-VM
    flow so the caller can dump the same diagnostic tail on failure.
    """
    _refuse_non_linux()

    lines: list[str] = []
    hints: list[str] = []

    def line(msg: str) -> None:
        lines.append(msg)

    line("[bare_metal bootstrap] verify-only mode (no installation)")

    uid = os.getuid()
    gid = os.getgid()
    socket_path = f"/run/user/{uid}/docker.sock"

    # uidmap
    uidmap_ok = "fail"
    if shutil.which("newuidmap"):
        uidmap_ok = "ok"
    else:
        hints.append("  sudo apt install uidmap")
    line(f"  newuidmap: {uidmap_ok}")

    # dbus / systemd user session
    dbus_ok = "fail"
    if Path("/run/user").is_dir():
        try:
            r_systemctl_v = subprocess.run(
                ["systemctl", "--version"], capture_output=True, check=False
            )
            if r_systemctl_v.returncode == 0:
                dbus_ok = "ok"
        except FileNotFoundError:
            pass
    if dbus_ok != "ok":
        hints.append("  sudo apt install dbus-user-session systemd")
    line(f"  dbus/systemd: {dbus_ok}")

    # lingering
    lingering_ok = "fail"
    try:
        r_linger = subprocess.run(
            ["loginctl", "show-user", os.environ.get("USER", str(uid)), "--property=Linger"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r_linger.returncode == 0 and "Linger=yes" in r_linger.stdout:
            lingering_ok = "ok"
    except FileNotFoundError:
        pass
    if lingering_ok != "ok":
        hints.append(f"  sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
    line(f"  linger: {lingering_ok}")

    # docker_host: rootless socket exists
    docker_host_ok = "fail"
    if Path(socket_path).exists():
        docker_host_ok = "ok"
    else:
        hints.append("  curl -fsSL https://get.docker.com/rootless | sh")
    line(f"  rootless docker socket: {docker_host_ok}")

    # daemon: docker version succeeds via the rootless socket
    daemon_ok = "fail"
    if docker_host_ok == "ok":
        env = os.environ.copy()
        env["DOCKER_HOST"] = f"unix://{socket_path}"
        try:
            r_docker_v = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                env=env,
                capture_output=True,
                check=False,
            )
            if r_docker_v.returncode == 0:
                daemon_ok = "ok"
        except FileNotFoundError:
            pass
    line(f"  docker daemon: {daemon_ok}")

    # user_socket_active: systemctl --user is-active docker.service
    user_active_ok = "fail"
    try:
        r_active = subprocess.run(
            ["systemctl", "--user", "is-active", "docker.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r_active.returncode == 0 and r_active.stdout.strip() == "active":
            user_active_ok = "ok"
    except FileNotFoundError:
        pass
    if user_active_ok != "ok":
        hints.append("  systemctl --user enable --now docker.service")
    line(f"  systemctl --user docker.service: {user_active_ok}")

    # net: same target as the in-VM script
    net_ok = "fail"
    try:
        r_net = subprocess.run(
            [
                "curl",
                "-sf",
                "-o",
                "/dev/null",
                "--max-time",
                "10",
                "https://download.docker.com/linux/ubuntu/gpg",
            ],
            capture_output=True,
            check=False,
        )
        if r_net.returncode == 0:
            net_ok = "ok"
    except FileNotFoundError:
        pass
    line(f"  network reach (download.docker.com): {net_ok}")

    # docker-compose-plugin
    compose_ok = True
    if daemon_ok == "ok":
        env = os.environ.copy()
        env["DOCKER_HOST"] = f"unix://{socket_path}"
        try:
            r_compose = subprocess.run(
                ["docker", "compose", "version"], env=env, capture_output=True, check=False
            )
            compose_ok = r_compose.returncode == 0
        except FileNotFoundError:
            compose_ok = False
    if not compose_ok:
        # Not a separate handshake field, but we still want to surface it
        # because rootless docker's install path doesn't ship the plugin.
        hints.append("  sudo apt install docker-compose-plugin")
        line("  docker compose plugin: fail")
    else:
        line("  docker compose plugin: ok")

    # Refuse root-only Docker. If the user is running with DOCKER_HOST
    # pointing at the rootful socket (e.g. they're in the docker group),
    # complain — the toolkit's security model needs rootless.
    if Path("/var/run/docker.sock").exists() and docker_host_ok != "ok":
        hints.append(
            "  rootful Docker at /var/run/docker.sock detected; the toolkit refuses to run on top of it. "
            "Install rootless instead: dockerd-rootless-setuptool.sh install"
        )

    # Emit the handshake line. parse_bootstrap_ok looks for it in
    # arbitrary stdout, so the helpful preamble above is fine to keep.
    handshake = (
        "BOOTSTRAP_OK"
        f" uidmap={uidmap_ok}"
        f" dbus={dbus_ok}"
        f" lingering={lingering_ok}"
        f" docker_host={docker_host_ok}"
        f" daemon={daemon_ok}"
        f" user_socket_active={user_active_ok}"
        f" net={net_ok}"
        f" ubuntu_uid={uid}"
        f" ubuntu_gid={gid}"
        f" socket_path={socket_path}"
    )
    line(handshake)

    stdout = "\n".join(lines) + "\n"
    stderr = ""
    if hints:
        stderr = (
            "missing prerequisites — run these and retry `agents vm init`:\n"
            + "\n".join(hints)
            + "\n"
        )

    status = parse_bootstrap_ok(stdout)
    return status, stdout, stderr


# ---------- subshell for `vm shell` ----------


def open_shell(data_dir: Path, socket_path: str) -> int:
    """Spawn an interactive subshell with the toolkit's working env.

    Matches the spirit of ``multipass shell`` / ``virsh console``: drop
    the user into a context where ``docker compose ...`` and ``agents
    exec ...`` Just Work — CWD on the data-dir, ``DOCKER_HOST`` set to
    the rootless socket. The user's regular login shell is used so
    their prompt, aliases, history all carry over.
    """
    env = os.environ.copy()
    env["DOCKER_HOST"] = f"unix://{socket_path}"
    env["AGENTS_BARE_METAL"] = "1"
    shell_bin = os.environ.get("SHELL") or shutil.which("bash") or "/bin/bash"
    ui.info(f"$ cd {shlex.quote(str(data_dir))} && DOCKER_HOST=unix://{socket_path} {shell_bin}")
    try:
        result = subprocess.run([shell_bin], cwd=str(data_dir), env=env, check=False)
    except FileNotFoundError as e:
        raise RuntimeFailure(f"shell not found: {shell_bin} ({e})") from e
    return result.returncode
