"""``agents access`` — let the host user read/write the agent's data.

The agent's runtime container writes files at rootless-Docker subuid
(e.g. host uid 100999 for container uid 1000), which the host user
can't read or edit without sudo. This module wraps ``setfacl`` to
grant the host user an ACL entry on the agent's data subtree,
surviving container recreates because the default ACL on the
directory makes new files inherit the grant.

Three commands:

- ``agents access grant <agent> [--rw]`` — add the ACL. ``setfacl``
  on a subuid-owned path needs sudo; the toolkit prints the exact
  command and confirms before running unless ``--yes``.
- ``agents access revoke <agent>`` — drop the toolkit's ACL entry.
- ``agents access list [<agent>]`` — show current ACL state per
  agent (``ok`` / ``missing``).

On bare-metal where the container uid maps back to the host user
1:1, this is a no-op + informational message. Granting requires no
sudo because the dir is already host-owned. Revoking is similarly a
no-op.

See also: :mod:`agents_cli.mounts_cmd` auto-applies the symmetric
ACL (host folder → agent's subuid) so `agents mount add` works
bidirectionally without a separate command.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agents_cli import agents_cmd, compose, permissions, ui
from agents_cli import config as cfg_mod
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError

# ---------- target-path resolution ----------


def _agent_dirs(cfg: AgentsConfig, agent: str, agent_type: str) -> list[Path]:
    """The host-side directories ``agents access`` operates on.

    For Hermes that's just ``<agent>/.data/`` (the bind-mount target
    for ``/opt/data``). For OpenClaw it's ``<agent>/.openclaw/`` AND
    ``<agent>/workspace/`` — the workspace is the one a user usually
    wants to peek at, but granting both is the consistent default
    (carving sub-scopes adds CLI surface for no clear win).

    Returns only directories that actually exist on disk — silently
    skips any the agent never produced. Refuses on
    ``data_dir_in_vm`` mode (the dirs live inside the VM disk; ACLs
    don't propagate across the boundary in any useful way; users on
    that mode should `agents export` and read the tarball instead).
    """
    if cfg.paths.data_dir_in_vm:
        raise UserError(
            "agents access is not supported in --data-dir-in-vm mode "
            "(the data lives inside the VM disk; host ACLs don't apply). "
            "Use `agents export` to pull a host-readable archive."
        )
    root = cfg.paths.host_data_dir / agent
    if agent_type == "hermes":
        candidates = [root / ".data"]
    elif agent_type == "openclaw":
        candidates = [root / ".openclaw", root / "workspace"]
    else:
        raise UserError(f"unknown agent type {agent_type!r}")
    return [p for p in candidates if p.exists()]


def _resolve_agent(cfg: AgentsConfig, agent: str) -> tuple[str, str, list[Path]]:
    """Look up service + type for ``agent``; return target dirs."""
    doc = compose.read_compose_doc(cfg)
    services = (doc.get("services") or {}).keys()
    for svc in services:
        agent_type = agents_cmd._agent_type_from_service(svc)
        if agent_type is None:
            continue
        if agents_cmd._name_from_service(svc, agent_type) == agent:
            return svc, agent_type, _agent_dirs(cfg, agent, agent_type)
    raise UserError(f"no agent named {agent!r} in the compose file")


def _bare_metal_noop_warning(cfg: AgentsConfig) -> None:
    """Bare-metal: the container uid maps back to the host user (1:1),
    so files are already host-owned. This is informational, not an
    error — calling grant/revoke is benign.
    """
    if cfg.vm.backend == "bare_metal":
        ui.warn(
            "bare_metal backend: the container runs as your host user; "
            "files in the agent's data dir are already accessible without "
            "an ACL. This command is a no-op here."
        )


# ---------- grant ----------


def _readable_access_label(status: str) -> str:
    """Translate an :func:`permissions.assess_host_access` status into
    a plain-English answer to "can I read this from the host right now?".

    The internal vocabulary distinguishes ``masked`` (ACL exists but
    is zeroed by the POSIX mask) from ``missing`` (no ACL at all) so
    the remediation hint can differ. The user-facing column collapses
    both into a single ``no`` — what they want to know is whether
    reads work, not the POSIX-ACL theory.
    """
    return {
        "ok": "yes",
        "missing": "no",
        "masked": "no",
        "path-missing": "?",
    }.get(status, status)


def _print_remediation_hints(rows: list[dict[str, Any]]) -> None:
    """For any non-ok rows, print a one-line remediation per agent
    BELOW the table. Keeps the table compact while still telling the
    user how to fix what's broken.
    """
    by_agent: dict[str, list[str]] = {}
    for r in rows:
        if r["status"] == "ok":
            continue
        hint: str | None
        if r["status"] == "missing":
            hint = f"no grant — run `agents access grant {r['agent']}` to enable host reads"
        elif r["status"] == "masked":
            hint = (
                f"grant exists but masked → reads blocked. Re-run "
                f"`agents access grant {r['agent']}` (or with --rw) to apply the "
                f"mask reset and unblock reads."
            )
        elif r["status"] == "path-missing":
            hint = f"data dir missing — start the agent first: `agents start {r['agent']}`"
        else:
            hint = None
        if hint:
            by_agent.setdefault(r["agent"], []).append(hint)
    if not by_agent:
        return
    lines = []
    for agent, hints in by_agent.items():
        # De-dup hints per agent (e.g. OpenClaw has two dirs that
        # usually fail the same way).
        for h in dict.fromkeys(hints):
            lines.append(f"  {agent}: {h}")
    ui.warn("hint:\n" + "\n".join(lines))


def _refuse_data_dir_in_vm(cfg: AgentsConfig) -> None:
    if cfg.paths.data_dir_in_vm:
        raise UserError(
            "agents access is not supported in --data-dir-in-vm mode "
            "(the data lives inside the VM disk; host ACLs don't apply). "
            "Use `agents export` to pull a host-readable archive."
        )


def grant(
    *,
    agent: str,
    writable: bool,
    yes: bool,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Add a recursive ACL on the agent's data subtree granting the
    host user access. Also sets the default ACL so future files the
    agent creates inherit the grant.

    Targets per agent type:

    - Hermes: ``<agent>/.data/``
    - OpenClaw: ``<agent>/.openclaw/`` and ``<agent>/workspace/``

    Requires sudo (path is subuid-owned). With ``yes=True`` skips the
    confirmation; otherwise prints the exact ``setfacl`` argv and
    confirms.
    """
    cfg = cfg_mod.load(config_path)
    _refuse_data_dir_in_vm(cfg)
    _, _, dirs = _resolve_agent(cfg, agent)
    if not dirs:
        raise UserError(
            f"agent {agent!r} has no on-disk data dir yet — bring it up first "
            f"(`agents start {agent}`) so the entrypoint can create it."
        )

    _bare_metal_noop_warning(cfg)
    if cfg.vm.backend == "bare_metal":
        # Files are already host-owned; nothing for setfacl to do.
        ui.emit({"agent": agent, "action": "grant", "noop": True}, human=f"{agent}: nothing to do")
        return

    recipient = permissions.host_user_recipient(cfg.host.uid, writable=writable)
    all_cmds: list[list[str]] = []
    for d in dirs:
        ok, err = permissions.probe_acl_support(d)
        if not ok:
            raise UserError(
                f"filesystem under {d} doesn't support ACLs: {err}. "
                "Move the data dir to an ext4/btrfs/zfs filesystem, "
                "or use `agents export` for host-side reads."
            )
        all_cmds.extend(
            permissions.apply_acl_argv(d, recipients=[recipient], recurse=True, defaults=True)
        )

    permissions.run_with_sudo(all_cmds, yes=yes)

    ui.emit(
        {
            "agent": agent,
            "action": "grant",
            "writable": writable,
            "host_uid": cfg.host.uid,
            "paths": [str(d) for d in dirs],
        },
        human=(
            f"granted host user (uid {cfg.host.uid}) {'rw' if writable else 'r'} access to:\n"
            + "\n".join(f"  {d}" for d in dirs)
        ),
    )


# ---------- revoke ----------


def revoke(
    *,
    agent: str,
    yes: bool,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Remove the host user's ACL entries on the agent's data subtree.

    Does NOT touch unrelated ACL entries — only the toolkit-added
    ``u:<host_uid>:`` entries (both access and default). User-set
    ACLs and any third-party-added ACLs stay put.
    """
    cfg = cfg_mod.load(config_path)
    _refuse_data_dir_in_vm(cfg)
    _, _, dirs = _resolve_agent(cfg, agent)
    if not dirs:
        raise UserError(f"agent {agent!r} has no on-disk data dir to revoke ACL on")

    _bare_metal_noop_warning(cfg)
    if cfg.vm.backend == "bare_metal":
        ui.emit({"agent": agent, "action": "revoke", "noop": True}, human=f"{agent}: nothing to do")
        return

    # Build a no-perms recipient; remove_acl_argv only uses the uid.
    recipient = permissions.AclRecipient(uid=cfg.host.uid, perms="")
    all_cmds: list[list[str]] = []
    for d in dirs:
        all_cmds.extend(
            permissions.remove_acl_argv(d, recipients=[recipient], recurse=True, defaults=True)
        )

    permissions.run_with_sudo(all_cmds, yes=yes)

    ui.emit(
        {
            "agent": agent,
            "action": "revoke",
            "host_uid": cfg.host.uid,
            "paths": [str(d) for d in dirs],
        },
        human=(
            f"revoked host user (uid {cfg.host.uid}) ACL on:\n" + "\n".join(f"  {d}" for d in dirs)
        ),
    )


# ---------- list ----------


def list_access(
    *,
    agent: str | None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    """Per-agent ACL status. ``ok`` if the host user has access (either
    by ownership or ACL); ``missing`` if they'd hit EACCES.
    """
    cfg = cfg_mod.load(config_path)
    _refuse_data_dir_in_vm(cfg)

    if agent is not None:
        _, _, dirs_per_agent = _resolve_agent(cfg, agent)
        agents_to_probe = [(agent, dirs_per_agent)]
    else:
        doc = compose.read_compose_doc(cfg)
        services = (doc.get("services") or {}).keys()
        agents_to_probe = []
        for svc in services:
            agent_type = agents_cmd._agent_type_from_service(svc)
            if agent_type is None:
                continue
            name = agents_cmd._name_from_service(svc, agent_type)
            try:
                dirs = _agent_dirs(cfg, name, agent_type)
            except UserError:
                continue
            agents_to_probe.append((name, dirs))

    rows: list[dict[str, Any]] = []
    for name, dirs in agents_to_probe:
        for d in dirs:
            status = permissions.assess_host_access(d, cfg.host.uid)
            owner_uid: int | None
            try:
                owner_uid = d.stat().st_uid
            except OSError:
                owner_uid = None
            # If the host user is the dir's owner, no ACL entry is
            # involved — show "owner" as the mode for clarity.
            if owner_uid == cfg.host.uid:
                mode = "owner"
                perms: str | None = None
            else:
                perms = permissions.get_user_acl_perms(d, cfg.host.uid)
                mode = permissions.summarize_acl_mode(perms)
            rows.append(
                {
                    "agent": name,
                    "path": str(d),
                    "owner_uid": owner_uid,
                    "host_uid": cfg.host.uid,
                    "mode": mode,
                    "perms": perms,
                    "status": status,
                    "access": _readable_access_label(status),
                }
            )

    if rows:
        ui.table(
            [
                {
                    "agent": r["agent"],
                    "path": r["path"],
                    "owner": r["owner_uid"] if r["owner_uid"] is not None else "?",
                    "mode": r["mode"],
                    "host can read?": r["access"],
                }
                for r in rows
            ],
            ["agent", "path", "owner", "mode", "host can read?"],
        )
        _print_remediation_hints(rows)
    else:
        ui.emit(
            {"agent": agent, "entries": []},
            human=f"no data dirs to probe for {agent or '<all agents>'}",
        )
    return rows


# ---------- create-time auto-grant hook ----------


def auto_grant_on_create(cfg: AgentsConfig, agent: str, agent_type: str) -> None:
    """Called by ``agents_cmd.create`` after the agent's data dir is
    laid down but BEFORE the container starts.

    At that moment the host user owns the empty data dir, so we can
    set the *default ACL* without sudo. When the container then
    starts and writes files at subuid 100999, those files inherit
    the default ACL — the host user can read everything the agent
    creates from day one.

    No-op on:

    - ``data_dir_in_vm`` mode (the host dir is empty; the actual
      data lives in the VM — ACLs there don't propagate)
    - ``bare_metal`` (container uid maps to the host user; no
      subuid translation; nothing to grant)
    - filesystems without ACL support (warn; agent creation
      continues — the user can later `agents export` archives or
      move the data dir)
    """
    if cfg.paths.data_dir_in_vm or cfg.vm.backend == "bare_metal":
        return
    dirs = _agent_dirs(cfg, agent, agent_type)
    if not dirs:
        return
    for d in dirs:
        ok, err = permissions.probe_acl_support(d)
        if not ok:
            ui.warn(
                f"skipping default-ACL grant on {d}: {err}. "
                "Files the agent creates will be owned by a subuid and "
                "unreadable without `sudo`. Move the data dir to a "
                "filesystem with ACL support to fix."
            )
            return
    # Two recipients — the host user (so files the agent creates
    # inherit user-rwx) AND the container subuid (matches what the
    # container would set anyway, but makes the inheritance explicit
    # and survives chowns).
    recipients = [
        permissions.host_user_recipient(cfg.host.uid, writable=True),
        permissions.container_recipient(cfg.host.uid, writable=True),
    ]
    cmds: list[list[str]] = []
    for d in dirs:
        # Only the default ACL — the dir is empty at this point, so
        # the recurse-existing pass would be a no-op. We DON'T set
        # an access ACL on the dir itself: the user owns it, so they
        # already have rwx; the container will then own its own
        # subtree via the entrypoint's chown.
        cmds.extend(
            permissions.apply_acl_argv(d, recipients=recipients, recurse=False, defaults=True)
        )
    if cmds:
        try:
            permissions.run_owned(cmds)
        except Exception as e:
            ui.warn(
                f"default-ACL grant failed: {e}. Agent creation continues; "
                "you can grant access later via `agents access grant {agent}`."
            )


_ = os  # used indirectly by callers via cfg.host.uid
