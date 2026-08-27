"""``agents tailscale {up,down,status,upgrade}`` — tailnet access to the VM.

Puts the *VM* on the user's tailnet so services published on the VM's
interfaces (the Hermes dashboard on 9119, per-agent API ports the user
chooses to publish) are reachable from other tailnet devices without SSH
tunnels or host-side proxying.

Unlike firecrawl/inference this is NOT a sibling container: rootless
Docker cannot own ``/dev/net/tun`` or ``NET_ADMIN``, so tailscaled runs
as a **VM-OS systemd service** installed via apt (Tailscale's official
install script adds the pkgs.tailscale.com stable repo). The toolkit
only ever mutates the VM's OS — never the host's — which is also why
the bare_metal backend refuses: there the "VM" *is* the host, and
installing a VPN on the user's workstation is not the toolkit's call
(install tailscale yourself; the dashboard port is already local).

Auth flow: ``agents tailscale up`` without ``--auth-key`` cannot finish
non-interactively — tailscaled prints a login URL and waits for the
browser handshake. The command captures and emits that URL (payload key
``auth_url``) and exits 0; after authenticating, ``agents tailscale
status`` confirms ``backend_state: Running``. With ``--auth-key`` (from
the Tailscale admin console) the join completes in one shot.

State: ``<data-dir>/.tailscale-state.json`` records that tailscale is
toolkit-managed (hostname + version + last up). Its presence gates the
doctor check — a user who never ran ``tailscale up`` gets no noise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agents_cli import compose, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError

# Matches the interactive-auth URL `tailscale up` prints while waiting.
_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")

_INSTALL_SCRIPT = """set -eu
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi
sudo systemctl enable --now tailscaled
tailscale version | head -1
"""


def _require_vm_backend(cfg: AgentsConfig) -> None:
    if cfg.vm.backend not in ("multipass", "libvirt"):
        raise UserError(
            "`agents tailscale` manages tailscaled inside the VM; on the "
            f"{cfg.vm.backend!r} backend there is no VM — install tailscale on "
            "this machine yourself (https://tailscale.com/download) and the "
            "locally-bound service ports are reachable over your tailnet already."
        )


def _state_path(cfg: AgentsConfig) -> Path:
    return cfg.paths.host_data_dir / ".tailscale-state.json"


def read_state(cfg: AgentsConfig) -> dict[str, Any]:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_state(cfg: AgentsConfig, payload: dict[str, Any]) -> None:
    p = _state_path(cfg)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def is_installed(cfg: AgentsConfig) -> bool:
    return _state_path(cfg).exists()


def _vm_status(cfg: AgentsConfig) -> dict[str, Any]:
    """``tailscale status --json`` inside the VM, reduced to what we use.

    Returns ``{"installed": False}`` when the binary is missing and
    ``{"error": ...}`` when the daemon is unreachable — callers render
    rather than raise, so ``status`` works on every broken half-state.
    """
    probe = mp.exec_in_vm(cfg.vm.name, "tailscale", "status", "--json", check=False)
    if probe.returncode != 0:
        err = (probe.stderr or probe.stdout).strip()
        if "not found" in err or "No such file" in err:
            return {"installed": False}
        return {"installed": True, "error": err or "tailscale status failed"}
    try:
        raw = json.loads(probe.stdout)
    except ValueError:
        return {"installed": True, "error": f"unparseable status output: {probe.stdout[:200]!r}"}
    self_info = raw.get("Self") or {}
    return {
        "installed": True,
        "backend_state": raw.get("BackendState", ""),
        "ips": list(self_info.get("TailscaleIPs") or []),
        "dns_name": str(self_info.get("DNSName") or "").rstrip("."),
        "version": str(raw.get("Version") or "").split("-", 1)[0],
    }


def _published_vm_ports(cfg: AgentsConfig) -> list[str]:
    """Container ports published on the VM's non-loopback interfaces —
    these are what tailnet peers can actually reach. Read from the
    override doc only (the toolkit's own fragments bind 127.0.0.1)."""
    ports: list[str] = []
    try:
        doc = compose.read_override_doc(cfg)
    except Exception:
        return ports
    for svc in (doc.get("services") or {}).values():
        for entry in svc.get("ports") or []:
            text = str(entry)
            # "9119:9119" (all interfaces) or an explicit non-loopback bind.
            if not text.startswith(("127.0.0.1:", "localhost:")):
                ports.append(text.split(":")[-2] if text.count(":") >= 1 else text)
    return ports


def up(
    *,
    auth_key: str | None = None,
    hostname: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Install (idempotent), start tailscaled, and join the tailnet."""
    cfg = cfg_mod.load(config_path)
    _require_vm_backend(cfg)
    ts_hostname = hostname or cfg.vm.name

    with compose.acquire_lock(cfg.paths.host_data_dir):
        ui.progress("ensuring tailscale is installed in the VM (apt, idempotent)...")
        install = mp.exec_bash(cfg.vm.name, _INSTALL_SCRIPT, check=False)
        if install.returncode != 0:
            raise RuntimeFailure(
                f"tailscale install failed: {(install.stderr or install.stdout).strip()[-400:]}"
            )
        version = install.stdout.strip().splitlines()[-1] if install.stdout.strip() else ""

        ui.progress(f"joining tailnet as {ts_hostname!r}...")
        if auth_key:
            # The key is a reusable credential — keep it out of the argv,
            # which both backends echo verbatim under --verbose. It rides
            # stdin into a bash read instead.
            script = (
                f'read -r TS_KEY && sudo tailscale up "--hostname={ts_hostname}" '
                f'--timeout=30s --auth-key="$TS_KEY"'
            )
            result = mp.exec_in_vm_with_stdin(
                cfg.vm.name,
                "bash",
                "-c",
                script,
                input_bytes=(auth_key + "\n").encode("utf-8"),
                check=False,
            )
        else:
            result = mp.exec_in_vm(
                cfg.vm.name,
                "sudo",
                "tailscale",
                "up",
                f"--hostname={ts_hostname}",
                "--timeout=30s",
                check=False,
            )

        status = _vm_status(cfg)
        # rc gate: a VM that was ALREADY connected reports Running even
        # when this invocation failed (bad hostname, rejected auth key) —
        # Running alone must not mask a failed change request.
        if result.returncode == 0 and status.get("backend_state") == "Running":
            write_state(
                cfg,
                {"hostname": ts_hostname, "version": status.get("version") or version},
            )
            ports = _published_vm_ports(cfg)
            ip = status["ips"][0] if status.get("ips") else "<tailnet-ip>"
            ui.emit(
                {
                    "backend_state": "Running",
                    "hostname": ts_hostname,
                    "ips": status.get("ips", []),
                    "dns_name": status.get("dns_name", ""),
                    "version": status.get("version", ""),
                    "published_ports": ports,
                },
                human=(
                    f"tailscale up: connected as {status.get('dns_name') or ts_hostname} "
                    f"({', '.join(status.get('ips', []))})\n"
                    + "".join(f"  reachable from your tailnet: http://{ip}:{p}\n" for p in ports)
                ).rstrip(),
            )
            return

        # Interactive auth pending: surface the login URL.
        combined = f"{result.stdout}\n{result.stderr}"
        m = _AUTH_URL_RE.search(combined)
        if m:
            write_state(cfg, {"hostname": ts_hostname, "version": version, "pending_auth": True})
            ui.emit(
                {
                    "backend_state": status.get("backend_state", "NeedsLogin"),
                    "auth_url": m.group(0),
                },
                human=(
                    f"authenticate this VM in your browser:\n\n  {m.group(0)}\n\n"
                    f"then run `agents tailscale status` to confirm it connected."
                ),
            )
            return
        raise RuntimeFailure(
            f"tailscale up did not reach Running and printed no auth URL: {combined.strip()[-400:]}"
        )


def down(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    """Disconnect from the tailnet (tailscaled stays installed)."""
    cfg = cfg_mod.load(config_path)
    _require_vm_backend(cfg)
    with compose.acquire_lock(cfg.paths.host_data_dir):
        result = mp.exec_in_vm(cfg.vm.name, "sudo", "tailscale", "down", check=False)
        if result.returncode != 0:
            raise RuntimeFailure(f"tailscale down failed: {result.stderr.strip()[-300:]}")
    ui.emit(
        {"backend_state": "Stopped"},
        human="tailscale: disconnected (re-join with `agents tailscale up`)",
    )


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Read-only tailnet status (no lock)."""
    cfg = cfg_mod.load(config_path)
    _require_vm_backend(cfg)
    payload: dict[str, Any] = {"managed": is_installed(cfg), **_vm_status(cfg)}
    if payload.get("backend_state") == "Running":
        payload["published_ports"] = _published_vm_ports(cfg)
    if not payload["installed"]:
        human = "tailscale: not installed in the VM (run `agents tailscale up`)"
    elif "error" in payload:
        human = f"tailscale: {payload['error']}"
    elif payload.get("backend_state") == "Running":
        ip = payload["ips"][0] if payload.get("ips") else "<tailnet-ip>"
        human = (
            f"tailscale: Running as {payload.get('dns_name') or '(no dns name)'} "
            f"({', '.join(payload.get('ips', []))})\n"
            + "".join(
                f"  reachable from your tailnet: http://{ip}:{p}\n"
                for p in payload.get("published_ports", [])
            )
        ).rstrip()
    else:
        human = (
            f"tailscale: {payload.get('backend_state', 'unknown')} "
            f"(run `agents tailscale up` to (re)connect)"
        )
    ui.emit(payload, human=human)
    return payload


def upgrade(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    """Upgrade the tailscale package inside the VM (apt)."""
    cfg = cfg_mod.load(config_path)
    _require_vm_backend(cfg)
    if not is_installed(cfg):
        raise UserError(
            "tailscale is not toolkit-managed here yet — run `agents tailscale up` first"
        )
    with compose.acquire_lock(cfg.paths.host_data_dir):
        before = _vm_status(cfg).get("version", "")
        ui.progress("upgrading tailscale via apt...")
        result = mp.exec_bash(
            cfg.vm.name,
            "set -eu\nsudo apt-get update -qq\n"
            "sudo apt-get install -y -qq --only-upgrade tailscale\n"
            "tailscale version | head -1\n",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeFailure(
                f"tailscale upgrade failed: {(result.stderr or result.stdout).strip()[-400:]}"
            )
        after = _vm_status(cfg)
        state = read_state(cfg)
        state["version"] = after.get("version", "")
        write_state(cfg, state)
    ui.emit(
        {"old_version": before, "new_version": after.get("version", "")},
        human=f"tailscale: {before or '?'} -> {after.get('version') or '?'}",
    )
