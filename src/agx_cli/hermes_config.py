"""Round-trip edits to each Hermes agent's config.yaml.

Each agent has its own ``<data-dir>/<name>/.data/config.yaml`` (the
file the Hermes container sees at ``$HERMES_HOME/config.yaml``). The
file is hand-editable, so we round-trip with ruamel.yaml to preserve
the user's comments and key order.

Write pattern (different from ``compose.write_compose_atomically`` —
this one goes THROUGH the container):

1. Mutate in memory.
2. ``docker compose exec -T <svc> sh -c 'cat > <staged>'`` with the
   YAML content piped via stdin. Running as the container's runtime
   user (which owns ``/opt/data`` under rootless Docker) is the only
   way to write here in ``--data-dir-in-vm`` mode, where the dir is
   subuid-owned and multipass-SFTP runs as ``ubuntu`` (which isn't
   the owner).
3. Validate via ``hermes config validate`` exec'd in the same
   container.
4. Atomic same-fs rename inside the container: ``mv <staged> config.yaml``.
5. RM the staged file on any error path.

The CLI refuses any change that would bind the dashboard to anything
other than ``127.0.0.1`` / ``localhost`` / ``::1`` — one of the toolkit's
locked security invariants (see AGENTS.md and docs/threat-model.md).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from agx_cli import mp
from agx_cli.config import AgentsConfig
from agx_cli.errors import UserError

ALLOWED_DASHBOARD_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# What ``read_doc`` saw, per returned doc: id(doc) -> (doc, (agent,
# profile), sha256 of the file text or None when absent). The doc itself
# is held so its id can't be recycled. ``write_atomically`` compares the
# file against this right before its rename — the agents edit their own
# config.yaml (Hermes migrations, `hermes config set` from a tool call,
# the curator), and a toolkit read-modify-write must not silently revert
# such a change that landed in between.
_READ_STATE: dict[int, tuple[Any, tuple[str, str | None], str | None]] = {}

# Hermes stores named profiles lowercase under ``$HERMES_HOME/profiles/<id>/``,
# each a full HERMES_HOME of its own (config.yaml, .env, state.db, ...).
# The toolkit addresses them with a ``profile`` parameter threaded through
# this module (GH #6 tier 2); ``None`` and ``"default"`` both mean the
# container's primary home at ``/opt/data``. The pattern is deliberately
# strict — these names are interpolated into container paths, so anything
# outside it (slashes, dots-only, uppercase) is refused rather than
# normalized.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def normalize_profile(profile: str | None) -> str | None:
    """``None``/``"default"``/empty → None (primary home); else validate.

    Raises :class:`UserError` on names that could escape the profiles
    dir or that Hermes itself would refuse.
    """
    if profile is None:
        return None
    p = profile.strip().lower()
    if p in ("", "default"):
        return None
    if not _PROFILE_NAME_RE.fullmatch(p) or ".." in p:
        raise UserError(
            f"invalid profile name {profile!r} — expected lowercase "
            f"letters/digits/dot/dash/underscore (Hermes stores profiles "
            f"lowercase under profiles/<id>/)"
        )
    return p


def profile_home(profile: str | None = None) -> str:
    """In-container HERMES_HOME for the given profile."""
    p = normalize_profile(profile)
    return "/opt/data" if p is None else f"/opt/data/profiles/{p}"


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def config_path(cfg: AgentsConfig, agent_name: str, profile: str | None = None) -> Path:
    """The host-side path of a Hermes agent's config.yaml (visible through
    the mount in host-mount mode; only exists in the VM under
    ``--data-dir-in-vm``). ``profile`` targets a named profile's config.
    """
    base = cfg.paths.host_data_dir / agent_name / ".data"
    p = normalize_profile(profile)
    if p is not None:
        base = base / "profiles" / p
    return base / "config.yaml"


def container_config_path(profile: str | None = None) -> str:
    """In-container path of the agent's config.yaml — what processes
    inside the container see at ``$HERMES_HOME/config.yaml``. Used by
    write paths that go through ``docker compose exec``.
    """
    return f"{profile_home(profile)}/config.yaml"


def load(path: Path) -> Any:
    """Round-trip read; returns an empty ruamel mapping if the file is missing."""
    y = _yaml()
    if not path.exists():
        return y.load("{}\n")
    return y.load(path.read_text(encoding="utf-8"))


def read_doc(cfg: AgentsConfig, agent_name: str, profile: str | None = None) -> Any:
    """Read the agent's config.yaml via ``docker compose exec cat``.

    Routed through the container regardless of ``data_dir_in_vm``
    setting. Three reasons:

    1. ``data_dir_in_vm`` mode: ``<vm_agents_root>/<name>/.data/`` is
       inside the VM, the toolkit runs on the host — direct read is
       impossible.
    2. Host-mount mode: the dir is on the host but owned by a
       rootless-Docker subuid (e.g. host uid 100999 for container uid
       1000) under mode 700. A host-side ``load(config_path(...))``
       call hits EACCES, and host ACLs can't fix that durably
       (restrictive creates are born with a masked ACL). The runtime
       user inside the container always owns and can read these
       files.
    3. Routing through one code path keeps the test simulator (which
       already maps ``docker compose exec ... cat /opt/data/X`` to
       the host-side file) consistent across modes.

    Returns an empty doc if the container isn't running (e.g. during
    ``agx create``'s ``data_dir_created`` phase before
    ``service_up``) or the file doesn't exist yet — same fallback the
    old SFTP path had.
    """
    text = _read_text(cfg, agent_name, profile)
    parsed = _yaml().load(text) if text is not None else None
    # Defensive: ``cat`` may return non-YAML output if the file is
    # empty or the stub in tests returns a generic OK. Callers index
    # ``doc.get("model")`` etc. which would AttributeError on a str.
    # Match the rc != 0 fallback shape.
    if not isinstance(parsed, dict):
        parsed = _yaml().load("{}\n")
    _READ_STATE[id(parsed)] = (parsed, (agent_name, normalize_profile(profile)), _digest(text))
    return parsed


def _read_text(cfg: AgentsConfig, agent_name: str, profile: str | None) -> str | None:
    """config.yaml's text via ``docker compose exec cat``; ``None`` when
    unreadable (container down, file not created yet)."""
    from agx_cli.ops import _compose_exec_in_vm_argv

    svc = f"hermes-{agent_name}"
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "cat", container_config_path(profile), no_tty=True),
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _digest(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _refuse_if_changed_since_read(
    cfg: AgentsConfig, agent_name: str, doc: Any, profile: str | None
) -> None:
    """Raise when config.yaml no longer matches what ``read_doc`` returned
    for ``doc``. Docs not obtained from ``read_doc`` are not checked."""
    seen = _READ_STATE.get(id(doc))
    if seen is None or seen[0] is not doc or seen[1] != (agent_name, normalize_profile(profile)):
        return
    if _digest(_read_text(cfg, agent_name, profile)) != seen[2]:
        raise UserError(
            f"{agent_name}'s config.yaml changed while the toolkit was editing it "
            "(the agent or another process wrote it) — nothing was written, so that "
            "change is kept. Re-run the command to apply yours on top of it."
        )


def dump(doc: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def write_atomically(
    cfg: AgentsConfig, agent_name: str, doc: Any, profile: str | None = None
) -> None:
    """Atomic write via ``docker compose exec`` inside the running container.

    Writes the new content to a staged path inside ``/opt/data`` via
    ``cat > <staged>`` exec'd as the container's runtime user, validates
    via ``hermes config validate`` (also exec'd), then ``mv`` to
    ``config.yaml`` — all within the same filesystem, so the rename is
    atomic.

    Why not ``multipass transfer`` like ``compose.write_compose_atomically``
    does for the project-level compose file? Because under rootless
    Docker, ``/opt/data`` is owned by a subuid (e.g. host uid 100999
    for container uid 1000) — not by ``ubuntu``. Multipass-SFTP runs
    as the ``ubuntu`` user and silently can't write to subuid-owned
    paths (the failure surfaces only on first attempt: ``cannot open
    remote file ...: Permission denied``). Going through
    ``docker compose exec`` puts the writer inside the container's
    user namespace where it owns the dir.

    The container MUST be running. ``agx create`` brings it up
    during the ``service_up`` phase before the wizard runs, and every
    later caller (``codex skill-enable``, memory plugin enable,
    ``agx secret set``) runs against an already-running container.
    """
    validate_dashboard_binding(doc)

    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime fields not populated; run `agx vm init` first")

    from agx_cli.ops import _compose_exec_in_vm_argv

    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    target = container_config_path(profile)
    stage = f"{target}.{pid}.{suffix}.new"
    svc = f"hermes-{agent_name}"
    content = dump(doc).encode("utf-8")

    # Write the staged file from inside the container. `sh -c 'cat > FILE'`
    # is the simplest way to get arbitrary content onto disk as the runtime
    # user; the content is piped via stdin so it never appears on the argv
    # (avoiding both quoting hell and process-listing leaks for sensitive
    # config like API keys).
    # The stage path is a positional parameter, not shell text.
    write_argv = _compose_exec_in_vm_argv(
        cfg, svc, "sh", "-c", 'cat > "$1"', "sh", stage, no_tty=True
    )
    write = mp.exec_in_vm_with_stdin(cfg.vm.name, *write_argv, input_bytes=content, check=False)
    if write.returncode != 0:
        err = (write.stderr or "").lower()
        if any(m in err for m in ("not running", "no such service", "no such container")):
            raise UserError(
                f"cannot write {agent_name}'s config.yaml — container "
                f"{svc!r} is not running. Start it with "
                f"`agx start {agent_name}` first."
            )
        p = normalize_profile(profile)
        if p is not None and ("no such file" in err or "directory" in err):
            raise UserError(
                f"profile {p!r} does not exist in agent {agent_name!r} — "
                f"create it first with `agx exec {agent_name} hermes profile create {p}`"
            )
        raise UserError(
            f"failed to stage {agent_name}'s config.yaml inside {svc!r}: "
            f"{(write.stderr or '').strip() or 'rc=' + str(write.returncode)}"
        )

    try:
        _validate_in_container(cfg, agent_name, stage, profile=profile)
        # Last moment before the rename (validation can take seconds):
        # optimistic concurrency against the agent's own edits.
        _refuse_if_changed_since_read(cfg, agent_name, doc, profile)
        mv_argv = _compose_exec_in_vm_argv(cfg, svc, "mv", stage, target, no_tty=True)
        mp.exec_in_vm(cfg.vm.name, *mv_argv)
    except Exception:
        rm_argv = _compose_exec_in_vm_argv(cfg, svc, "rm", "-f", stage, no_tty=True)
        mp.exec_in_vm(cfg.vm.name, *rm_argv, check=False)
        raise
    # The file now holds this doc: a later write of the same doc in this
    # process compares against what WE wrote.
    if id(doc) in _READ_STATE:
        _READ_STATE[id(doc)] = (
            doc,
            (agent_name, normalize_profile(profile)),
            _digest(content.decode("utf-8")),
        )


def _validate_in_container(
    cfg: AgentsConfig, agent_name: str, vm_path: str, profile: str | None = None
) -> None:
    """Best-effort validation of the staged config file inside the container.

    Hermes' CLI evolved here — older versions had ``hermes config
    validate``, newer ones use ``hermes config check``. We try each;
    if NEITHER is available the structural ``validate_dashboard_binding``
    upstream is the only guard, which is acceptable: every writer
    goes through this module's helpers that produce well-formed YAML.

    Skipped silently when the container isn't running.
    """
    from agx_cli.ops import _compose_exec_in_vm_argv

    svc = f"hermes-{agent_name}"
    home = profile_home(profile)
    container_stage = f"{home}/{Path(vm_path).name}"
    # For a named profile, point HERMES_HOME at the profile's own home so
    # validation resolves .env / includes against the profile being
    # edited — the same mechanism Hermes' generated profile aliases use.
    env = {"HERMES_CONFIG": container_stage}
    if normalize_profile(profile) is not None:
        env["HERMES_HOME"] = home

    for sub in ("check", "validate"):
        result = mp.exec_in_vm(
            cfg.vm.name,
            *_compose_exec_in_vm_argv(
                cfg,
                svc,
                "hermes",
                "config",
                sub,
                no_tty=True,
                env=env,
            ),
            check=False,
        )
        if result.returncode in (0, None):
            return
        err = (result.stderr or "").lower()
        if any(
            marker in err
            for marker in (
                "not running",
                "no such service",
                "no such container",
                "is not running",
            )
        ):
            return
        # Subcommand not recognised → try the next one.
        if "invalid choice" in err or "usage: hermes config" in err:
            continue
        raise UserError(
            f"hermes config {sub} failed for {agent_name!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def validate_dashboard_binding(doc: Any) -> None:
    """Refuse any dashboard.address that isn't localhost — the dashboard
    is meant to be accessible only via SSH tunnel.
    """
    dashboard = doc.get("dashboard") or {}
    addr = dashboard.get("address")
    if addr is None:
        return
    if addr not in ALLOWED_DASHBOARD_HOSTS:
        raise UserError(
            f"refusing to set dashboard.address={addr!r}; allowed: "
            f"{sorted(ALLOWED_DASHBOARD_HOSTS)}"
        )


# ---------- terminal.docker_image (used by codex skill-enable) ----------


def get_terminal_docker_image(doc: Any) -> str | None:
    terminal = doc.get("terminal") or {}
    image = terminal.get("docker_image")
    return str(image) if image else None


def set_terminal_docker_image(doc: Any, image: str) -> None:
    terminal = doc.setdefault("terminal", {})
    terminal["docker_image"] = image
    terminal.setdefault("backend", "docker")


# ---------- terminal.docker_volumes (used by codex skill-enable) ----------


def add_docker_volume(doc: Any, mapping: str) -> None:
    """Append a host:container[:ro] entry to terminal.docker_volumes. Idempotent."""
    terminal = doc.setdefault("terminal", {})
    volumes = terminal.setdefault("docker_volumes", [])
    if mapping not in volumes:
        volumes.append(mapping)


def remove_docker_volume(doc: Any, mapping: str) -> None:
    """Remove a previously-added volume entry, if present."""
    terminal = doc.get("terminal") or {}
    volumes = terminal.get("docker_volumes") or []
    if mapping in volumes:
        volumes.remove(mapping)


# ---------- terminal.env_passthrough (used by secrets_cmd) ----------


def add_env_passthrough(doc: Any, key: str) -> None:
    """Append ``key`` to ``terminal.env_passthrough`` so it forwards into
    sandboxed shell / execute_code calls. Idempotent.

    Hermes' default ``terminal.env_passthrough: []`` is a security feature:
    env vars present in the Hermes Python process don't leak into the
    subprocesses it spawns for the ``terminal`` and ``execute_code``
    tools unless the user explicitly allowlists them. Per-agent secrets
    written by ``agx secret set`` need this entry too, otherwise the
    LLM's own shell-based "is this configured?" diagnostics will report
    "not set" even though Hermes' built-in tools see the var fine.
    """
    terminal = doc.setdefault("terminal", {})
    passthrough = terminal.setdefault("env_passthrough", [])
    if key not in passthrough:
        passthrough.append(key)


def remove_env_passthrough(doc: Any, key: str) -> None:
    """Drop ``key`` from ``terminal.env_passthrough`` if present.

    Used by ``agx secret unset``. Leaving a stale entry would be
    harmless (the var no longer exists in the container env, so nothing
    propagates), but removing it keeps the config tidy and avoids
    confusing future reads.
    """
    terminal = doc.get("terminal") or {}
    passthrough = terminal.get("env_passthrough") or []
    if key in passthrough:
        passthrough.remove(key)
