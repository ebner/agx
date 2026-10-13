"""Codex CLI integration: login / status / skill-enable / skill-disable.

The toolkit-scoped Codex account lives at ``<data-dir>/.codex/`` and is
distinct from the user's personal ``~/.codex/``. Authentication is done
on the host via the ``codex`` CLI with ``CODEX_HOME`` pointed at the
toolkit dir.

This module serves exactly ONE purpose: the **Codex coding skill**
inside Hermes' nested sandbox (the bundled ``autonomous-ai-agents/codex``
skill). Opt-in via ``agx codex skill-enable <hermes-agent>``: build a
derived sandbox image with Codex baked in, point the agent's
``terminal.docker_image`` at it, and add the Codex auth as a
sandbox-volume entry (``<vm_agents_root>/.codex:/codex:ro``) using
**VM-side** host paths.

It is deliberately NOT the way a Hermes agent authenticates its
``openai-codex`` **LLM provider**. Mounting ``<data-dir>/.codex`` into
the agent for ``hermes setup`` to seed a credential from would leave
two copies of ONE single-use OAuth refresh-token chain: any refresh of
the host file (e.g. by the Codex CLI itself) rotates the token under
the running agent — "Your authentication token has been invalidated" —
and the two copies can never both stay valid. Provider auth is done
inside the container, where Hermes owns the credential and its refresh
cycle::

    agx exec <name> -- hermes auth add openai-codex --type oauth --no-browser

(device-code flow: prints a URL + code to open on any browser that is
logged into the ChatGPT account; the credential persists in the agent's
``/opt/data/auth.json``). Because of the same single-use-chain rule,
nothing in the toolkit refreshes ``<data-dir>/.codex`` on the user's
behalf.

OpenClaw bundles its own Codex plugin — the toolkit refuses
``skill-enable`` on OpenClaw agents.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agx_cli import compose, compose_runner, hermes_config, mp, templates, ui
from agx_cli import config as cfg_mod
from agx_cli.agents_cmd import _detect_type_from_compose
from agx_cli.agents_cmd import service_name as _service_name_for
from agx_cli.config import AgentsConfig
from agx_cli.errors import RuntimeFailure, UserError

SANDBOX_IMAGE_PREFIX = "agents/hermes-sandbox-codex"
EXPIRY_WARNING_DAYS = 7


def _codex_home(cfg: AgentsConfig) -> Path:
    p = cfg.paths.host_data_dir / ".codex"
    p.mkdir(exist_ok=True)
    return p


def _require_host_codex() -> str:
    binary = shutil.which("codex")
    if binary is None:
        raise UserError("codex CLI not found on host. Install with `npm install -g @openai/codex`.")
    return binary


# ---------- login / status ----------


def login(
    *,
    device_auth: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Run the Codex CLI login flow with CODEX_HOME pinned to the
    toolkit-scoped dir.

    Default flow is browser+localhost-callback. On a headless / remote
    host the callback server's localhost URL isn't reachable from
    wherever the user has a browser; pass ``device_auth=True`` (CLI
    flag: ``--device-auth``) to get the device-code flow instead,
    where Codex prints a URL + code for the user to enter on any
    browser-equipped machine.
    """
    cfg = cfg_mod.load(config_path)
    codex_bin = _require_host_codex()
    env = dict(os.environ, CODEX_HOME=str(_codex_home(cfg)))
    argv = [codex_bin, "login"]
    if device_auth:
        argv.append("--device-auth")
    result = subprocess.run(argv, env=env, check=False)
    if result.returncode != 0:
        raise RuntimeFailure(f"codex login failed (rc={result.returncode})")
    # Under --data-dir-in-vm the sandbox skill volume points at
    # <vm_agents_root>/.codex, not the host. Sync the freshly-written
    # auth into the VM.
    if cfg.paths.data_dir_in_vm:
        _sync_host_codex_to_vm(cfg)
    ui.emit({"auth": "ok", "codex_home": env["CODEX_HOME"]}, human="codex auth ok")


def _sync_host_codex_to_vm(cfg: AgentsConfig) -> None:
    """Push the host-side toolkit Codex dir into the VM so the sandbox
    skill volume (which points at the VM-side ``<vm_agents_root>/.codex``)
    sees the same auth as ``agx codex login`` just wrote.

    Real directory sync: tars the host ``.codex/`` (including subdirs),
    transfers the archive, and extracts in the VM after ``rm -rf``ing
    the VM-side target so stale files are pruned.
    """
    import tarfile as _tarfile
    import tempfile
    import uuid as _uuid

    host_dir = _codex_home(cfg)
    if not host_dir.exists() or not any(host_dir.iterdir()):
        return

    ui.progress("syncing toolkit Codex auth into the VM...")
    with tempfile.NamedTemporaryFile(
        prefix="codex-sync-",
        suffix=".tar",
        dir=compose.host_staging_dir(cfg),
        delete=False,
    ) as tf:
        host_tar = Path(tf.name)
    try:
        with _tarfile.open(host_tar, "w") as tar:
            tar.add(host_dir, arcname=".")
        vm_tar = f"/tmp/agx-codex-sync-{os.getpid()}-{_uuid.uuid4().hex[:8]}.tar"
        mp.transfer_to_vm(host_tar, cfg.vm.name, vm_tar)
        target = f"{cfg.paths.vm_agents_root}/.codex"
        mp.exec_bash(
            cfg.vm.name,
            (
                f"set -eu\n"
                f"rm -rf {target}\n"
                f"mkdir -p {target}\n"
                f"tar -xf {vm_tar} -C {target}\n"
                f"rm -f {vm_tar}\n"
            ),
        )
    finally:
        host_tar.unlink(missing_ok=True)


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    codex_home = _codex_home(cfg)
    auth_path = codex_home / "auth.json"

    info: dict[str, Any] = {
        "codex_home": str(codex_home),
        "auth_present": auth_path.exists(),
        "vm_mount_ok": _vm_mount_resolves(cfg),
        "expires_at": None,
        "expires_in_days": None,
        "expiry_warning": False,
        "per_agent_skill": _per_agent_skill_status(cfg),
    }

    if auth_path.exists():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
            expires = _parse_expiry(data)
            if expires is not None:
                info["expires_at"] = expires.isoformat()
                delta = expires - datetime.datetime.now(datetime.UTC)
                info["expires_in_days"] = delta.days
                info["expiry_warning"] = delta.days < EXPIRY_WARNING_DAYS
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    human_lines = [
        f"auth: {'present' if info['auth_present'] else 'missing'} ({auth_path})",
        f"vm mount: {'ok' if info['vm_mount_ok'] else 'unreachable'} ({cfg.paths.vm_agents_root}/.codex)",
    ]
    if info["expires_at"] is not None:
        human_lines.append(
            f"expires: {info['expires_at']} ({info['expires_in_days']} day(s))"
            + ("  ⚠ less than 7 days remain" if info["expiry_warning"] else "")
        )
    for entry in info["per_agent_skill"]:
        human_lines.append(
            f"  {entry['name']}: skill={entry['skill_enabled']}, "
            f"sandbox_image={entry['sandbox_image'] or '(default)'}"
        )

    ui.emit(info, human="\n".join(human_lines))
    return info


def _vm_mount_resolves(cfg: AgentsConfig) -> bool:
    """Return True iff <vm_agents_root>/.codex exists in the VM and contains
    the auth file. Conservative on errors (returns False).
    """
    if not cfg.vm_runtime.is_populated():
        return False
    result = mp.exec_in_vm(
        cfg.vm.name,
        "test",
        "-r",
        f"{cfg.paths.vm_agents_root}/.codex/auth.json",
        check=False,
    )
    return result.returncode == 0


def _parse_expiry(auth_data: dict[str, Any]) -> datetime.datetime | None:
    """Best-effort parse of an OAuth expiry from a Codex auth.json.

    Tries the common shapes: ``expires_at`` (iso or epoch), ``tokens.expires_at``,
    and a ``last_refresh`` + ``expires_in`` combo. Returns ``None`` if nothing
    parseable is found.
    """
    candidates: list[Any] = [
        auth_data.get("expires_at"),
        auth_data.get("tokens", {}).get("expires_at")
        if isinstance(auth_data.get("tokens"), dict)
        else None,
    ]
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, (int, float)):
            try:
                return datetime.datetime.fromtimestamp(c, datetime.UTC)
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(c, str):
            try:
                return datetime.datetime.fromisoformat(c.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _per_agent_skill_status(cfg: AgentsConfig) -> list[dict[str, Any]]:
    """For each Hermes agent: skill-enable state + derived sandbox image."""
    from agx_cli import compose as compose_mod

    doc = compose_mod.read_compose_doc(cfg)
    out: list[dict[str, Any]] = []
    for svc_name in doc.get("services") or {}:
        if not svc_name.startswith("hermes-"):
            continue
        name = svc_name[len("hermes-") :]
        state_path = _skill_state_path(cfg, name)
        out.append(
            {
                "name": name,
                "skill_enabled": state_path.exists(),
                "sandbox_image": (
                    json.loads(state_path.read_text())["derived_image"]
                    if state_path.exists()
                    else None
                ),
            }
        )
    return out


# ---------- skill-enable / skill-disable ----------


def _skill_state_path(cfg: AgentsConfig, name: str) -> Path:
    """Host-side state file location.

    Lives under the host data-dir (always present for journals/lock,
    works in both host-mount and --data-dir-in-vm modes) rather than
    inside the agent's data dir, which doesn't exist on host under
    --data-dir-in-vm.
    """
    return cfg.paths.host_data_dir / ".codex-skill" / f"{name}.json"


def _sandbox_image_tag(base_image: str) -> str:
    """Slug the derived image tag so user-supplied bases with `/` and `:`
    produce a valid Docker reference. sha256 prefix is plenty here.
    """
    digest = hashlib.sha256(base_image.encode("utf-8")).hexdigest()[:12]
    return f"{SANDBOX_IMAGE_PREFIX}:{digest}"


def _sandbox_volume_mapping(cfg: AgentsConfig) -> str:
    """Hermes' ``terminal.docker_volumes`` entries are interpreted by the **VM
    Docker daemon**, so we name VM-side host paths here (NOT paths inside
    the Hermes runtime container).

    The mount target inside the sandbox is ``/codex`` rather than
    ``<user-home>/.codex``: the derived sandbox image sets
    ``CODEX_HOME=/codex`` so the Codex CLI reads auth from there
    regardless of which user the base image runs as.
    """
    return f"{cfg.paths.vm_agents_root}/.codex:/codex:ro"


def skill_enable(name: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, str]:
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type != "hermes":
        raise UserError(
            "agx codex skill-enable only applies to Hermes agents — "
            "OpenClaw bundles its own Codex plugin (run "
            "`openclaw models auth login --provider openai-codex` inside the container)"
        )

    with compose.acquire_lock(cfg.paths.host_data_dir):
        doc = hermes_config.read_doc(cfg, name)
        current_image = (
            hermes_config.get_terminal_docker_image(doc) or templates.HERMES_DEFAULT_SANDBOX_IMAGE
        )
        state_path = _skill_state_path(cfg, name)

        if current_image.startswith(SANDBOX_IMAGE_PREFIX):
            # Already running our derived image; idempotent re-enable —
            # but ONLY if the state file still knows the true base.
            # Recording the derived image as "original" would make the
            # base unrecoverable and wedge every enable/disable cycle on
            # the derived image forever.
            if not state_path.exists():
                raise UserError(
                    f"terminal.docker_image for {name!r} is already the derived sandbox "
                    f"image ({current_image}) but the skill state file recording the "
                    f"original base image is missing ({state_path}). Refusing to enable — "
                    f"the base image would be unrecoverable. Set terminal.docker_image in "
                    f"the agent's config.yaml back to the intended base image first, then "
                    f"re-run `agx codex skill-enable {name}`."
                )
            derived = current_image
        else:
            derived = _sandbox_image_tag(current_image)
            _build_sandbox_image_in_vm(cfg, derived, current_image)
            # Persist the original image so skill-disable can restore it.
            # Skip rewrite when the state file exists, to keep re-enable
            # a true no-op.
            if not state_path.exists():
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(
                    json.dumps(
                        {"original_docker_image": current_image, "derived_image": derived},
                        indent=2,
                    ),
                    encoding="utf-8",
                )

        hermes_config.set_terminal_docker_image(doc, derived)
        hermes_config.add_docker_volume(doc, _sandbox_volume_mapping(cfg))
        hermes_config.write_atomically(cfg, name, doc)

        svc = _service_name_for(name, "hermes")
        # `up -d` only creates the container when it's missing (restart
        # cannot — e.g. after a VM rebuild or `docker compose rm`). The
        # edit above is config.yaml-only, so up alone is a NO-OP for an
        # already-running container and the agent would keep the old
        # sandbox image; the restart forces the config.yaml re-read.
        compose_runner.up(cfg, svc)
        compose_runner.restart(cfg, svc)

    ui.emit(
        {"name": name, "derived_image": derived},
        human=(
            f"codex skill enabled for {name!r}\n"
            f"  sandbox image: {derived}\n"
            f"  base image:    {current_image}\n"
            f"  container restarted"
        ),
    )
    return {"name": name, "derived_image": derived}


def skill_disable(name: str, *, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type != "hermes":
        raise UserError("codex skill-disable only applies to Hermes agents")
    state_path = _skill_state_path(cfg, name)
    if not state_path.exists():
        raise UserError(f"codex skill is not enabled for {name!r}; no .codex-skill.json found")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        original_image = state["original_docker_image"]

        doc = hermes_config.read_doc(cfg, name)
        hermes_config.set_terminal_docker_image(doc, original_image)
        hermes_config.remove_docker_volume(doc, _sandbox_volume_mapping(cfg))
        hermes_config.write_atomically(cfg, name, doc)

        state_path.unlink()

        svc = _service_name_for(name, "hermes")
        # Same up-then-restart as skill_enable: up creates the container
        # if absent; restart forces the config.yaml re-read that a no-op
        # `up` wouldn't trigger.
        compose_runner.up(cfg, svc)
        compose_runner.restart(cfg, svc)

    ui.emit(
        {"name": name, "restored_image": original_image},
        human=(
            f"codex skill disabled for {name!r}; sandbox image restored to "
            f"{original_image}; container restarted"
        ),
    )


def _build_sandbox_image_in_vm(cfg: AgentsConfig, image_tag: str, base_image: str) -> None:
    """Write the Dockerfile to a host temp, ``multipass transfer`` it into
    the VM project dir, then ``docker build`` against the staged file.

    Works in both modes uniformly. In host-mount mode the transfer ends
    up on the host too (via the mount); in ``--data-dir-in-vm`` mode it
    only exists in the VM.
    """
    import uuid as _uuid

    dockerfile_content = templates.render_codex_sandbox_dockerfile(base_image)
    # uuid suffix on top of the deterministic hash so concurrent skill-enables
    # (different processes targeting the same VM root) can't trample each
    # other's staged Dockerfile.
    suffix = f"{hashlib.sha256(image_tag.encode()).hexdigest()[:8]}-{os.getpid()}-{_uuid.uuid4().hex[:8]}"
    dockerfile_name = f".codex-sandbox-{suffix}.Dockerfile"
    host_temp = compose.host_staging_dir(cfg) / dockerfile_name
    vm_dockerfile = f"{cfg.paths.vm_agents_root}/{dockerfile_name}"
    try:
        host_temp.write_text(dockerfile_content, encoding="utf-8")
        mp.transfer_to_vm(host_temp, cfg.vm.name, vm_dockerfile)
        ui.progress(
            f"building derived sandbox image {image_tag} from {base_image} — can take minutes..."
        )
        result = mp.exec_in_vm(
            cfg.vm.name,
            "docker",
            "build",
            "-t",
            image_tag,
            "-f",
            vm_dockerfile,
            str(cfg.paths.vm_agents_root),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeFailure(f"docker build failed for {image_tag}: {result.stderr.strip()}")
        ui.progress(f"built {image_tag}")
    finally:
        host_temp.unlink(missing_ok=True)
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_dockerfile, check=False)
