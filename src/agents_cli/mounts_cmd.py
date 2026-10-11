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
- **Atomic across phases.** If any later phase fails after the VM
  mount succeeded, everything *this call* applied is rolled back — the
  override volume entry, the sandbox volume, the VM mount, and the
  host-folder ACLs (unless another manifest still references the same
  host folder) — so the system isn't left in a half-applied state the
  manifest-driven ``remove`` can't see. Pre-existing state (a volume
  entry the user hand-added to the override, an ACL grant that was
  already present) is never removed by the rollback: each apply helper
  reports whether it actually changed anything.
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

    def volume_source(self, agent: str, backend: str) -> str:
        """Bind source as the Docker daemon sees it. On the VM backends
        that is the VM-side mount point; on bare_metal the daemon runs
        on the host, so it is the host folder itself — there is no
        ``host-extras`` mount behind :meth:`vm_path` there.
        """
        if backend == "bare_metal":
            return self.host_path
        return self.vm_path(agent)

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


def _agents_referencing_host_path(cfg: AgentsConfig, host_path: str) -> list[str]:
    """Agents whose ``.mounts.json`` references ``host_path``.

    Every agent's container uid maps to the SAME rootless-Docker subuid,
    so the ACL entries on a host folder are shared state: stripping them
    while any other mount (this agent's or another's) still uses the
    folder silently breaks that mount's access. Corrupt manifests are
    skipped — they can't prove a reference.
    """
    referencing: list[str] = []
    for manifest in sorted(cfg.paths.host_data_dir.glob("*/.mounts.json")):
        agent = manifest.parent.name
        try:
            entries = load_manifest(cfg, agent)
        except UserError:
            continue
        if any(e.host_path == host_path for e in entries):
            referencing.append(agent)
    return referencing


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
    Hermes terminal volume) + manifest persist. Rolled back as a unit
    on any phase failure.
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
    source = new_entry.volume_source(agent, cfg.vm.backend)
    bare_metal = cfg.vm.backend == "bare_metal"

    with compose.acquire_lock(cfg.paths.host_data_dir):
        where = "" if bare_metal else f"  vm={vm_path}"
        ui.info(
            f"adding mount: host={host_resolved}{where}  "
            f"container={container_resolved}  ({'ro' if readonly else 'rw'}, target={effective_target})"
        )

        # Phase 0: auto-apply ACLs on the host folder so the agent's
        # subuid can read it (and write, if --rw). No sudo needed —
        # the user owns the host path. Bidirectional on --rw: the
        # agent gets rwX on existing + future files, AND we set a
        # default ACL granting the host user rwx on future files the
        # agent creates back (so the user can read what the agent
        # wrote). Applies on bare-metal too: rootless Docker runs there
        # as well, so the agent is a subuid on the host. No-op (warn)
        # when the fs doesn't support ACLs.
        applied_acl = _auto_acl_host_folder(cfg, host_resolved, readonly)

        # Phase 1: VM mount. No-op on bare-metal.
        if not bare_metal:
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

        applied_runtime = False
        applied_sandbox = False
        try:
            # Phase 2: compose-override (runtime target). The helper
            # reports whether it actually inserted anything — an entry
            # the user already hand-added to the override is not ours,
            # and the rollback below must not delete it (rule #10).
            if effective_target in {"runtime", "both"}:
                applied_runtime = _add_runtime_volume(
                    cfg, svc, source, container_resolved, readonly
                )

            # Phase 3: hermes config.yaml (sandbox target). Same
            # actually-inserted contract as phase 2.
            if effective_target in {"sandbox", "both"}:
                applied_sandbox = _add_sandbox_volume(
                    cfg, svc, agent, source, container_resolved, readonly
                )

            # Phase 4: manifest persist.
            entries.append(new_entry)
            save_manifest(cfg, agent, entries)
        except BaseException:
            # Unwind everything THIS CALL applied, in reverse order. The
            # manifest was never written (it's the last phase), so
            # ``remove`` cannot see — let alone clean up — any of these
            # orphans; the rollback has to do it here. Pre-existing
            # state (user-added volume entries, pre-set ACLs) is left
            # untouched — the applied_* flags are False for those.
            if applied_sandbox:
                with _suppress():
                    _remove_sandbox_volume(
                        cfg,
                        svc,
                        agent,
                        source,
                        container_resolved,
                        readonly,
                        include_flipped=False,
                    )
            if applied_runtime:
                with _suppress():
                    _remove_runtime_volume(
                        cfg,
                        svc,
                        source,
                        container_resolved,
                        readonly,
                        include_flipped=False,
                    )
            if not bare_metal:
                with _suppress():
                    mp.umount(cfg.vm.name, vm_path)
            # Phase-0 ACLs: strip only when this call introduced the
            # grant AND no other manifest still references this host
            # folder (shared-subuid rule — see
            # _agents_referencing_host_path).
            with _suppress():
                if applied_acl and not _agents_referencing_host_path(cfg, str(host_resolved)):
                    _auto_acl_host_folder_remove(cfg, host_resolved, readonly)
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

    vm_line = "" if bare_metal else f"  vm:        {vm_path}\n"
    ui.emit(
        {
            "agent": agent,
            "host_path": str(host_resolved),
            "vm_path": None if bare_metal else vm_path,
            "volume_source": source,
            "container_path": container_resolved,
            "readonly": readonly,
            "target": effective_target,
            "restarted": restart,
        },
        human=(
            f"mount added for {agent!r}:\n"
            f"  host:      {host_resolved}\n"
            f"{vm_line}"
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
    bare_metal = cfg.vm.backend == "bare_metal"
    # Toolkit versions before 2026-10 wrote the VM-side path as the bind
    # source on bare-metal too (a dead mount: nothing backs it on the
    # host). Strip that legacy form as well so such entries still go.
    sources = [entry.volume_source(agent, cfg.vm.backend)]
    if bare_metal:
        sources.append(vm_path)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        ui.info(f"removing mount: source={sources[0]}  container={container}")

        for source in sources:
            if entry.target in {"runtime", "both"}:
                with _suppress():
                    _remove_runtime_volume(cfg, svc, source, container, entry.readonly)
            if entry.target in {"sandbox", "both"}:
                with _suppress():
                    _remove_sandbox_volume(cfg, svc, agent, source, container, entry.readonly)

        if not bare_metal:
            mp.umount(cfg.vm.name, vm_path)

        entries = [e for e in entries if e.container_path != container]
        save_manifest(cfg, agent, entries)

        # Symmetric to add: strip the ACL entries we added so the host
        # folder is back to whatever ACL state the user originally had.
        # BUT: all agents share one container subuid, so the ACLs are
        # shared state — if any remaining mount (this agent's or any
        # other's) still references the same host folder, leave them in
        # place or that mount silently loses access. Best-effort — don't
        # fail the whole remove if the user already setfacl'd manually
        # since add. Manifest is saved first so the scan doesn't count
        # the entry being removed.
        still_used_by = _agents_referencing_host_path(cfg, entry.host_path)
        if still_used_by:
            ui.info(
                f"leaving ACLs on {entry.host_path} in place — still referenced "
                f"by mount(s) of: {', '.join(still_used_by)}"
            )
        else:
            with _suppress():
                _auto_acl_host_folder_remove(cfg, Path(entry.host_path), entry.readonly)

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
            row["vm"] = None if cfg.vm.backend == "bare_metal" else e.vm_path(a)
            row["source"] = e.volume_source(a, cfg.vm.backend)
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
    source: str,
    container_path: str,
    readonly: bool,
) -> bool:
    """Add the volume entry to the compose override.

    Returns True only when this call actually inserted the entry.
    A pre-existing identical entry (the user hand-added it to the
    override — which is theirs to edit) returns False WITHOUT
    rewriting the file, so ``add``'s rollback knows the entry is not
    ours to remove (rule #10: the override is sacred).
    """
    doc = compose.read_override_doc(cfg)
    services = doc.setdefault("services", {})
    svc_block = services.setdefault(svc, {})
    volumes = svc_block.setdefault("volumes", [])
    entry = _format_volume(source, container_path, readonly)
    if entry in volumes:
        return False
    volumes.append(entry)
    compose.write_override_atomically(cfg, doc)
    return True


def _remove_runtime_volume(
    cfg: AgentsConfig,
    svc: str,
    source: str,
    container_path: str,
    readonly: bool,
    *,
    include_flipped: bool = True,
) -> None:
    doc = compose.read_override_doc(cfg)
    services = doc.get("services") or {}
    svc_block = services.get(svc)
    if not svc_block:
        return
    volumes = svc_block.get("volumes") or []
    target = _format_volume(source, container_path, readonly)
    # Tolerate that the user may have flipped the :ro suffix manually
    # since add — remove the canonical form AND the toggled form.
    # ``add``'s rollback passes ``include_flipped=False``: it must only
    # remove the exact entry it inserted moments earlier, never a
    # flipped-suffix twin the user may have hand-added.
    forms = {target}
    if include_flipped:
        forms.add(_format_volume(source, container_path, not readonly))
    new_volumes = [v for v in volumes if v not in forms]
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
    source: str,
    container_path: str,
    readonly: bool,
) -> bool:
    """Append to ``terminal.docker_volumes`` in the agent's
    config.yaml. Uses the existing ``hermes_config`` round-trip
    helpers — same path the codex skill-enable command uses.

    Returns True only when this call actually inserted the entry —
    same actually-changed contract as :func:`_add_runtime_volume`, so
    ``add``'s rollback never strips a user-added pre-existing entry.
    """
    entry = _format_volume(source, container_path, readonly)
    doc = hermes_config.read_doc(cfg, agent)
    volumes = (doc.get("terminal") or {}).get("docker_volumes") or []
    if entry in volumes:
        return False
    hermes_config.add_docker_volume(doc, entry)
    hermes_config.write_atomically(cfg, agent, doc)
    return True


def _remove_sandbox_volume(
    cfg: AgentsConfig,
    svc: str,
    agent: str,
    source: str,
    container_path: str,
    readonly: bool,
    *,
    include_flipped: bool = True,
) -> None:
    doc = hermes_config.read_doc(cfg, agent)
    hermes_config.remove_docker_volume(doc, _format_volume(source, container_path, readonly))
    if include_flipped:
        # Same rationale as _remove_runtime_volume: only the manifest-
        # driven remove tolerates a user-flipped :ro suffix; add's
        # rollback removes exactly what it inserted.
        hermes_config.remove_docker_volume(
            doc, _format_volume(source, container_path, not readonly)
        )
    hermes_config.write_atomically(cfg, agent, doc)


# ---------- helpers ----------


def _auto_acl_host_folder(cfg: AgentsConfig, host_path: Path, readonly: bool) -> bool:
    """Apply ACL on the user-owned host folder so the agent's subuid
    can reach it (and the user can read what the agent writes back
    when ``--rw``). Idempotent — setfacl on an already-present entry
    is a no-op. No sudo needed; the user owns the folder.

    Applies on bare-metal too: rootless Docker runs on the host there,
    so the container uid is a subuid of the host user exactly as in the
    VM (the host's ``/etc/subuid`` IS the daemon's mapping on bare-metal,
    so ``compute_container_subuid`` is exact). Warn-only if the
    filesystem can't carry ACLs;
    the mount still works in the obvious case (mode-permissive files)
    but file-level permission errors will surface at runtime.

    Returns True only when the subuid grant did NOT pre-exist — i.e.
    this call introduced it. When the folder already carried an ACL
    entry for the container subuid (the user set one manually, or an
    earlier grant drifted out of the manifests), setfacl still runs to
    converge recursion/mask, but the return is False so ``add``'s
    rollback leaves the pre-existing grant in place rather than
    stripping state this call didn't create.
    """
    from agents_cli import permissions

    ok, err = permissions.probe_acl_support(host_path)
    if not ok:
        ui.warn(
            f"{host_path} is on a filesystem without ACL support ({err}). "
            "The agent (uid 100xxx via rootless-Docker subuid mapping) "
            "may hit permission errors on files with restrictive mode bits. "
            "Move the folder to ext4/btrfs/zfs to fix."
        )
        return False
    subuid_recipient = permissions.container_recipient(cfg.host.uid, writable=not readonly)
    preexisting = permissions.has_user_acl(host_path, subuid_recipient.uid)
    recipients = [subuid_recipient]
    if not readonly:
        # Future agent writes carry default ACL granting the user rwx
        # so `--rw` mounts are symmetric: user → agent works via the
        # container_recipient entry; agent → user works via this one.
        recipients.append(permissions.host_user_recipient(cfg.host.uid, writable=True))
    cmds = permissions.apply_acl_argv(host_path, recipients=recipients, recurse=True, defaults=True)
    permissions.run_owned(cmds)
    return not preexisting


def _auto_acl_host_folder_remove(cfg: AgentsConfig, host_path: Path, readonly: bool) -> None:
    """Reverse :func:`_auto_acl_host_folder`. Only removes the entries
    the toolkit added — leaves any user-set ACLs intact.

    Tolerates a missing host folder (the user may have removed it after add).
    """
    from agents_cli import permissions

    if not host_path.exists():
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
