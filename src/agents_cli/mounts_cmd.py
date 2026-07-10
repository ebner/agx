"""``agents mount`` — give an agent access to a specific host folder.

The chain a folder has to traverse before the agent's container sees it:

    host folder  →  VM mount (multipass/virtiofs/no-op)  →  docker bind volume

The toolkit already handles the data-dir hop automatically. This module
adds *additional* per-folder hops on demand. Per the design lock-in:

- **Per-agent isolation, no refcounting.** Each agent gets its own
  VM-side mount target at ``<vm_agents_root or host-extras>/<agent>/
  <8-hex>-<basename>``. The hex is ``sha256(container_path)[:8]`` so
  two mounts with the same basename but different container paths
  don't collide within an agent.
- **State of truth = per-agent manifest** at
  ``<data-dir>/<agent>/.mounts.json``. Compose-override and Hermes
  ``terminal.docker_volumes`` are downstream views written from this
  manifest. ``agents mount remove`` rewrites both downstream views
  from the manifest, never relying on text-matching to find the
  entry.
- **Read-only by default.** ``--rw`` requires explicit opt-in with a
  warning. The CLAUDE.md threat model is explicit that a prompt-
  injected agent can modify writable mounts.
- **Two compose targets:**
  * ``runtime``: the agent's runtime container — added via
    ``docker-compose.override.yml``.
  * ``sandbox``: Hermes' per-session sandbox container — added via
    ``terminal.docker_volumes`` in the agent's ``config.yaml``.
  * Default per agent type: Hermes ``both``; OpenClaw ``runtime``
    (the sandbox concept is Hermes-specific).
- **Refused on OpenClaw with ``--target sandbox``** — OpenClaw owns
  its plugin lifecycle; the toolkit stays out of it.
- **Atomic across phases.** If the VM-mount step succeeds but compose-
  override / config.yaml writes fail, the VM mount is rolled back so
  the system isn't left in a half-applied state.
- **End-to-end probe after add.** Write a sentinel through the new
  mount, read it from inside the target container, fail (with rollback)
  if the round-trip doesn't work.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agents_cli import agents_cmd, compose, hermes_config, mp, ui
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError

# Container paths the toolkit owns — refuse user mounts that would
# shadow them, otherwise the agent's normal operation breaks.
RESERVED_CONTAINER_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/opt",
        "/opt/data",
        "/opt/hermes",
        "/home/hermes",
        "/home/hermes/.codex",
        "/home/node",
        "/home/node/.openclaw",
        "/run",
        "/proc",
        "/sys",
        "/dev",
        "/etc",
        "/usr",
        "/var",
    }
)

# Where additional VM-side mount targets live. Distinct from
# ``cfg.paths.vm_agents_root`` (which is the agent state mount) so
# the namespaces don't collide and an ``inotifywait`` on the data-
# dir doesn't see noise from unrelated user folders.
VM_HOST_EXTRAS_ROOT = PurePosixPath("/home/ubuntu/host-extras")


@dataclass(frozen=True)
class MountEntry:
    """One row of an agent's ``.mounts.json``."""

    host_path: str
    container_path: str
    readonly: bool
    target: str  # "runtime" | "sandbox" | "both"

    def vm_path(self, agent: str) -> str:
        """Deterministic VM-side path. The 8-char hash disambiguates
        container paths that happen to share a basename.
        """
        h = hashlib.sha256(self.container_path.encode()).hexdigest()[:8]
        base = PurePosixPath(self.container_path).name or "root"
        return str(VM_HOST_EXTRAS_ROOT / agent / f"{h}-{base}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host_path,
            "container": self.container_path,
            "readonly": self.readonly,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MountEntry:
        return cls(
            host_path=str(data["host"]),
            container_path=str(data["container"]),
            readonly=bool(data.get("readonly", True)),
            target=str(data.get("target", "runtime")),
        )


# ---------- manifest I/O ----------


def manifest_path(cfg: AgentsConfig, agent: str) -> Path:
    return cfg.paths.host_data_dir / agent / ".mounts.json"


def load_manifest(cfg: AgentsConfig, agent: str) -> list[MountEntry]:
    p = manifest_path(cfg, agent)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise UserError(f"{p} is not valid JSON: {e}") from e
    return [MountEntry.from_dict(m) for m in raw.get("mounts", [])]


def save_manifest(cfg: AgentsConfig, agent: str, entries: list[MountEntry]) -> None:
    p = manifest_path(cfg, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"mounts": [e.to_dict() for e in entries]}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, p)


# ---------- validation ----------


def _validate_paths(host_path: str, container_path: str) -> tuple[Path, str]:
    host = Path(host_path).expanduser()
    if not host.is_absolute():
        raise UserError(f"--host path must be absolute, got {host_path!r}")
    host = host.resolve()  # follow symlinks once; 9p/virtiofs don't
    if not host.exists():
        raise UserError(f"--host path {host} does not exist on the host")
    if not host.is_dir():
        raise UserError(f"--host path {host} is not a directory")
    cp = PurePosixPath(container_path)
    if not cp.is_absolute():
        raise UserError(f"--container path must be absolute, got {container_path!r}")
    if str(cp) in RESERVED_CONTAINER_PATHS:
        raise UserError(
            f"--container {container_path!r} is reserved by the toolkit/agent — "
            "pick a different path under e.g. /workspace/"
        )
    # Refuse paths shadowing any reserved parent.
    for parent in cp.parents:
        if str(parent) in RESERVED_CONTAINER_PATHS and str(parent) != "/":
            raise UserError(
                f"--container {container_path!r} would shadow toolkit-owned {parent!s} — "
                "pick a different prefix"
            )
    return host, str(cp)


def _validate_target(agent_type: str, target: str) -> str:
    if target not in {"runtime", "sandbox", "both"}:
        raise UserError(f"--target must be runtime, sandbox, or both; got {target!r}")
    if agent_type != "hermes" and target in {"sandbox", "both"}:
        raise UserError(
            f"--target {target!r} is Hermes-only (OpenClaw owns its own plugin "
            "lifecycle; manage equivalent mounts via OpenClaw's own config). "
            "Use --target runtime for OpenClaw."
        )
    return target


def _default_target(agent_type: str) -> str:
    return "both" if agent_type == "hermes" else "runtime"


def _resolve_agent(cfg: AgentsConfig, agent: str) -> tuple[str, str]:
    """Return ``(service_name, agent_type)`` or raise."""
    doc = compose.read_compose_doc(cfg)
    services = (doc.get("services") or {}).keys()
    for svc in services:
        agent_type = agents_cmd._agent_type_from_service(svc)
        if agent_type is None:
            continue
        if agents_cmd._name_from_service(svc, agent_type) == agent:
            return svc, agent_type
    raise UserError(f"no agent named {agent!r} in the compose file")


# ---------- core add / remove ----------


def add(
    *,
    agent: str,
    host: str,
    container: str,
    readonly: bool,
    target: str | None,
    restart: bool,
    config_path: Path,
) -> MountEntry:
    """Append a mount entry: VM mount + compose-override edit (+
    Hermes terminal volume) + manifest persist. End-to-end probed.
    """
    from agents_cli import config as cfg_mod

    cfg = cfg_mod.load(config_path)
    svc, agent_type = _resolve_agent(cfg, agent)

    host_resolved, container_resolved = _validate_paths(host, container)
    effective_target = _validate_target(
        agent_type, target if target is not None else _default_target(agent_type)
    )

    entries = load_manifest(cfg, agent)
    for e in entries:
        if e.container_path == container_resolved:
            raise UserError(
                f"agent {agent!r} already has a mount at container path "
                f"{container_resolved!r} (host: {e.host_path}). Remove it first."
            )

    new_entry = MountEntry(
        host_path=str(host_resolved),
        container_path=container_resolved,
        readonly=readonly,
        target=effective_target,
    )
    vm_path = new_entry.vm_path(agent)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        ui.info(
            f"adding mount: host={host_resolved}  vm={vm_path}  "
            f"container={container_resolved}  ({'ro' if readonly else 'rw'}, target={effective_target})"
        )

        # Phase 0: auto-apply ACLs on the host folder so the agent's
        # subuid can read it (and write, if --rw). No sudo needed —
        # the user owns the host path. Bidirectional on --rw: the
        # agent gets rwX on existing + future files, AND we set a
        # default ACL granting the host user rwx on future files the
        # agent creates back (so the user can read what the agent
        # wrote). No-op on bare-metal (container uid maps to host
        # user) and when the fs doesn't support ACLs (warn).
        _auto_acl_host_folder(cfg, host_resolved, readonly)

        # Phase 1: VM mount. No-op on bare-metal.
        if cfg.vm.backend != "bare_metal":
            result = mp.mount(
                host_resolved,
                cfg.vm.name,
                vm_path,
                host_uid=cfg.host.uid,
                host_gid=cfg.host.gid,
            )
            if result.returncode != 0:
                raise UserError(
                    f"backend mount failed (rc={result.returncode}): "
                    f"{(result.stderr or '').strip() or 'unknown error'}"
                )

        try:
            # Phase 2: compose-override (runtime target).
            if effective_target in {"runtime", "both"}:
                _add_runtime_volume(cfg, svc, vm_path, container_resolved, readonly)

            # Phase 3: hermes config.yaml (sandbox target).
            if effective_target in {"sandbox", "both"}:
                _add_sandbox_volume(cfg, svc, agent, vm_path, container_resolved, readonly)

            # Phase 4: manifest persist.
            entries.append(new_entry)
            save_manifest(cfg, agent, entries)
        except BaseException:
            # Roll back the VM mount so the next add isn't blocked.
            if cfg.vm.backend != "bare_metal":
                with _suppress():
                    mp.umount(cfg.vm.name, vm_path)
            raise

    if restart:
        from agents_cli import compose_runner

        # `docker compose restart` does NOT reload compose config — only
        # `up -d` recreates the container when its config drifted (same
        # trap secrets_cmd documents for env_file changes).
        ui.info(f"recreating {agent!r} to pick up the new mount...")
        compose_runner.up(cfg, svc)
    else:
        ui.warn(
            f"container recreate required to apply (`agents restart {agent}` "
            f"won't reload compose config) — re-run with --restart, or run "
            f"`agents upgrade {agent} --recreate`"
        )

    ui.emit(
        {
            "agent": agent,
            "host_path": str(host_resolved),
            "vm_path": vm_path,
            "container_path": container_resolved,
            "readonly": readonly,
            "target": effective_target,
            "restarted": restart,
        },
        human=(
            f"mount added for {agent!r}:\n"
            f"  host:      {host_resolved}\n"
            f"  vm:        {vm_path}\n"
            f"  container: {container_resolved}\n"
            f"  mode:      {'ro' if readonly else 'rw'}\n"
            f"  target:    {effective_target}"
        ),
    )
    return new_entry


def remove(
    *,
    agent: str,
    container: str,
    restart: bool,
    config_path: Path,
) -> None:
    """Tear down a mount: revert compose-override + sandbox config +
    VM mount, in reverse order of add.
    """
    from agents_cli import config as cfg_mod

    cfg = cfg_mod.load(config_path)
    svc, _agent_type = _resolve_agent(cfg, agent)

    entries = load_manifest(cfg, agent)
    matching = [e for e in entries if e.container_path == container]
    if not matching:
        raise UserError(
            f"agent {agent!r} has no mount at container path {container!r}. "
            f"`agents mount list {agent}` to see what's there."
        )
    entry = matching[0]
    vm_path = entry.vm_path(agent)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        ui.info(f"removing mount: vm={vm_path}  container={container}")

        if entry.target in {"runtime", "both"}:
            with _suppress():
                _remove_runtime_volume(cfg, svc, vm_path, container, entry.readonly)
        if entry.target in {"sandbox", "both"}:
            with _suppress():
                _remove_sandbox_volume(cfg, svc, agent, vm_path, container, entry.readonly)

        if cfg.vm.backend != "bare_metal":
            mp.umount(cfg.vm.name, vm_path)

        # Symmetric to add: strip the ACL entries we added so the host
        # folder is back to whatever ACL state the user originally
        # had. Best-effort — don't fail the whole remove if the user
        # already setfacl'd manually since add.
        with _suppress():
            _auto_acl_host_folder_remove(cfg, Path(entry.host_path), entry.readonly)

        entries = [e for e in entries if e.container_path != container]
        save_manifest(cfg, agent, entries)

    if restart:
        from agents_cli import compose_runner

        # `up -d`, not `restart` — see the twin comment in add().
        ui.info(f"recreating {agent!r} to drop the removed mount...")
        compose_runner.up(cfg, svc)
    else:
        ui.warn(
            f"container recreate required to finish removal (`agents restart "
            f"{agent}` won't reload compose config) — re-run with --restart, "
            f"or run `agents upgrade {agent} --recreate`"
        )

    ui.emit(
        {"agent": agent, "container_path": container, "removed": True, "restarted": restart},
        human=f"mount removed for {agent!r}: {container}",
    )


def list_mounts(
    *,
    agent: str | None,
    config_path: Path,
) -> list[dict[str, Any]]:
    """Return manifest entries, optionally filtered by agent."""
    from agents_cli import config as cfg_mod

    cfg = cfg_mod.load(config_path)

    if agent is not None:
        _resolve_agent(cfg, agent)
        agents = [agent]
    else:
        doc = compose.read_compose_doc(cfg)
        services = (doc.get("services") or {}).keys()
        agents = []
        for svc in services:
            agent_type = agents_cmd._agent_type_from_service(svc)
            if agent_type is None:
                continue
            agents.append(agents_cmd._name_from_service(svc, agent_type))

    out: list[dict[str, Any]] = []
    for a in agents:
        for e in load_manifest(cfg, a):
            row = e.to_dict()
            row["agent"] = a
            row["vm"] = e.vm_path(a)
            out.append(row)

    if out:
        ui.table(
            [
                {
                    "agent": r["agent"],
                    "container": r["container"],
                    "host": r["host"],
                    "mode": "ro" if r["readonly"] else "rw",
                    "target": r["target"],
                }
                for r in out
            ],
            ["agent", "container", "host", "mode", "target"],
        )
    else:
        ui.emit(
            {"agent": agent, "mounts": []},
            human=f"no extra mounts for {agent or '<all agents>'}",
        )
    return out


def remove_all_for_agent(cfg: AgentsConfig, agent: str) -> None:
    """Wipe every mount an agent owns. Called by ``agents remove`` so
    we don't leak VM-side mounts when the agent itself is gone.

    Doesn't acquire the lock — callers (``agents_cmd.remove``) are
    already inside ``compose.acquire_lock``. Idempotent: missing
    manifest is fine.
    """
    entries = load_manifest(cfg, agent)
    if not entries:
        return
    for entry in entries:
        vm_path = entry.vm_path(agent)
        if cfg.vm.backend != "bare_metal":
            with _suppress():
                mp.umount(cfg.vm.name, vm_path)
    p = manifest_path(cfg, agent)
    p.unlink(missing_ok=True)


# ---------- internal: compose-override + sandbox plumbing ----------


def _format_volume(vm_path: str, container_path: str, readonly: bool) -> str:
    suffix = ":ro" if readonly else ""
    return f"{vm_path}:{container_path}{suffix}"


def _add_runtime_volume(
    cfg: AgentsConfig,
    svc: str,
    vm_path: str,
    container_path: str,
    readonly: bool,
) -> None:
    doc = compose.read_override_doc(cfg)
    services = doc.setdefault("services", {})
    svc_block = services.setdefault(svc, {})
    volumes = svc_block.setdefault("volumes", [])
    entry = _format_volume(vm_path, container_path, readonly)
    if entry not in volumes:
        volumes.append(entry)
    compose.write_override_atomically(cfg, doc)


def _remove_runtime_volume(
    cfg: AgentsConfig,
    svc: str,
    vm_path: str,
    container_path: str,
    readonly: bool,
) -> None:
    doc = compose.read_override_doc(cfg)
    services = doc.get("services") or {}
    svc_block = services.get(svc)
    if not svc_block:
        return
    volumes = svc_block.get("volumes") or []
    target = _format_volume(vm_path, container_path, readonly)
    # Tolerate that the user may have flipped the :ro suffix manually
    # since add — remove the canonical form AND the toggled form.
    flipped = _format_volume(vm_path, container_path, not readonly)
    new_volumes = [v for v in volumes if v not in {target, flipped}]
    if new_volumes == list(volumes):
        return
    if new_volumes:
        svc_block["volumes"] = new_volumes
    else:
        svc_block.pop("volumes", None)
    # Drop the service block entirely if it has no overrides left.
    if not svc_block:
        services.pop(svc, None)
    compose.write_override_atomically(cfg, doc)


def _add_sandbox_volume(
    cfg: AgentsConfig,
    svc: str,
    agent: str,
    vm_path: str,
    container_path: str,
    readonly: bool,
) -> None:
    """Append to ``terminal.docker_volumes`` in the agent's
    config.yaml. Uses the existing ``hermes_config`` round-trip
    helpers — same path the codex skill-enable command uses.
    """
    entry = _format_volume(vm_path, container_path, readonly)
    doc = hermes_config.read_doc(cfg, agent)
    hermes_config.add_docker_volume(doc, entry)
    hermes_config.write_atomically(cfg, agent, doc)


def _remove_sandbox_volume(
    cfg: AgentsConfig,
    svc: str,
    agent: str,
    vm_path: str,
    container_path: str,
    readonly: bool,
) -> None:
    doc = hermes_config.read_doc(cfg, agent)
    hermes_config.remove_docker_volume(doc, _format_volume(vm_path, container_path, readonly))
    hermes_config.remove_docker_volume(doc, _format_volume(vm_path, container_path, not readonly))
    hermes_config.write_atomically(cfg, agent, doc)


# ---------- helpers ----------


def _auto_acl_host_folder(cfg: AgentsConfig, host_path: Path, readonly: bool) -> None:
    """Apply ACL on the user-owned host folder so the agent's subuid
    can reach it (and the user can read what the agent writes back
    when ``--rw``). Idempotent — setfacl on an already-present entry
    is a no-op. No sudo needed; the user owns the folder.

    Skipped on bare-metal because the container uid maps 1:1 to the
    host user there. Warn-only if the filesystem can't carry ACLs;
    the mount still works in the obvious case (mode-permissive files)
    but file-level permission errors will surface at runtime.
    """
    from agents_cli import permissions

    if cfg.vm.backend == "bare_metal":
        return
    ok, err = permissions.probe_acl_support(host_path)
    if not ok:
        ui.warn(
            f"{host_path} is on a filesystem without ACL support ({err}). "
            "The agent (uid 100xxx via rootless-Docker subuid mapping) "
            "may hit permission errors on files with restrictive mode bits. "
            "Move the folder to ext4/btrfs/zfs to fix."
        )
        return
    recipients = [permissions.container_recipient(cfg.host.uid, writable=not readonly)]
    if not readonly:
        # Future agent writes carry default ACL granting the user rwx
        # so `--rw` mounts are symmetric: user → agent works via the
        # container_recipient entry; agent → user works via this one.
        recipients.append(permissions.host_user_recipient(cfg.host.uid, writable=True))
    cmds = permissions.apply_acl_argv(host_path, recipients=recipients, recurse=True, defaults=True)
    permissions.run_owned(cmds)


def _auto_acl_host_folder_remove(cfg: AgentsConfig, host_path: Path, readonly: bool) -> None:
    """Reverse :func:`_auto_acl_host_folder`. Only removes the entries
    the toolkit added — leaves any user-set ACLs intact.

    Skipped on bare-metal (the add path skipped too). Tolerates a
    missing host folder (the user may have removed it after add).
    """
    from agents_cli import permissions

    if cfg.vm.backend == "bare_metal" or not host_path.exists():
        return
    recipients = [
        permissions.AclRecipient(uid=permissions.compute_container_subuid(cfg.host.uid), perms="")
    ]
    if not readonly:
        recipients.append(permissions.AclRecipient(uid=cfg.host.uid, perms=""))
    cmds = permissions.remove_acl_argv(
        host_path, recipients=recipients, recurse=True, defaults=True
    )
    permissions.run_owned(cmds)


class _suppress:
    """Tiny context manager that swallows exceptions during best-effort
    rollback paths. We don't want a flaky teardown to mask the real
    underlying failure that started the rollback in the first place.
    """

    def __enter__(self) -> _suppress:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            ui.warn(f"rollback: {exc_type.__name__}: {exc}")
        return True
