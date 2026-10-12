"""``agx access`` — read (or edit) an agent's files from the host.

The agent's runtime container writes its state as a rootless-Docker
subuid (e.g. host uid 100999 for container uid 1000) under mode 700, so
the host user can't open it. The userns map is bijective and the host
uid is already taken by container root, so ownership can't be fixed at
the source; and POSIX ACLs — what this command used to manage — don't
hold: a ``mkdir(0700)`` under a default ACL is born with ``mask::---``.

Instead this mounts an **idmapped bind-mount view** of the state dir at
``<view-root>/<agent>`` (default ``~/agents-view``) with the owner ids
swapped: through the view the host user simply is the owner, whatever
the mode bits or a later ``chmod`` say. The data itself is untouched.

- ``agx access mount <agent> [--rw]`` — one printed + confirmed
  ``sudo mount``. Read-only unless ``--rw``. Always ``nosymfollow``
  (agent-planted symlinks must not resolve in the host namespace),
  ``nosuid,nodev,noexec``. Prints a ``nofail`` fstab line for
  persistence — the toolkit never edits ``/etc``.
- ``agx access unmount <agent>`` — ``sudo umount`` + plain ``rmdir``.
  NEVER a recursive delete of a view path: it is the real data.
- ``agx access list [<agent>]`` — registered views and their state.

Registered views live in ``<data-dir>/.access-views.json`` (opt-in gate
for the doctor check, like the tailscale/firecrawl state files). Mount
state is read from ``/proc/self/mountinfo`` — ``os.path.ismount`` can't
see a bind mount on the same filesystem.

Not available under ``--data-dir-in-vm`` (nothing on the host to view).
For quick edits without any mount, see ``agx edit``.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agx_cli import agents_cmd, compose, idmap_view, permissions, ui
from agx_cli import config as cfg_mod
from agx_cli.config import AgentsConfig
from agx_cli.errors import RuntimeFailure, UserError

STATE_FILE = ".access-views.json"
DEFAULT_VIEW_ROOT = "~/agents-view"

# ---------- target-path resolution ----------


def _refuse_data_dir_in_vm(cfg: AgentsConfig) -> None:
    if cfg.paths.data_dir_in_vm:
        raise UserError(
            "agx access is not supported in --data-dir-in-vm mode (the data lives "
            "on the VM disk; there is nothing on the host to view). Use `agx edit`, "
            "`agx exec`, or `agx export`."
        )


def _agent_dirs(cfg: AgentsConfig, agent: str, agent_type: str) -> list[Path]:
    """The runtime-owned directories of an agent on the host: Hermes
    ``<agent>/.data``; OpenClaw ``<agent>/.openclaw`` + ``<agent>/workspace``.
    Only those that exist."""
    _refuse_data_dir_in_vm(cfg)
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


def view_root(cfg: AgentsConfig) -> Path:
    """The views' parent dir. Must not nest with the data dir in either
    direction: a view inside it would be re-exported into the VM over
    the share (and archived twice); the data dir inside the view root
    would mount views over themselves."""
    root = (cfg.paths.host_view_dir or Path(DEFAULT_VIEW_ROOT).expanduser()).resolve()
    data = cfg.paths.host_data_dir.resolve()
    if root == data or data in root.parents or root in data.parents:
        raise UserError(
            f"[paths].host_view_dir ({root}) must not contain, or be inside, the data dir ({data})"
        )
    return root


def _view_path(cfg: AgentsConfig, agent: str, agent_type: str, source: Path) -> Path:
    base = view_root(cfg) / agent
    return base if agent_type == "hermes" else base / source.name.lstrip(".")


# ---------- state file ----------


@dataclass(frozen=True)
class View:
    agent: str
    source: Path
    view: Path
    writable: bool

    def to_json(self) -> dict[str, Any]:
        return {"source": str(self.source), "view": str(self.view), "rw": self.writable}


def _state_path(cfg: AgentsConfig) -> Path:
    return cfg.paths.host_data_dir / STATE_FILE


def registered_views(cfg: AgentsConfig, agent: str | None = None) -> list[View]:
    path = _state_path(cfg)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    views = [
        View(name, Path(item["source"]), Path(item["view"]), bool(item.get("rw", False)))
        for name, items in (raw.get("views") or {}).items()
        for item in items
        if isinstance(item, dict) and "source" in item and "view" in item
    ]
    return [v for v in views if agent is None or v.agent == agent]


def _save_views(cfg: AgentsConfig, views: list[View]) -> None:
    path = _state_path(cfg)
    if not views:
        path.unlink(missing_ok=True)
        return
    grouped: dict[str, list[dict[str, Any]]] = {}
    for v in views:
        grouped.setdefault(v.agent, []).append(v.to_json())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"views": grouped}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def refuse_if_view_registered(cfg: AgentsConfig, agent: str | None, action: str) -> None:
    """Lifecycle guard: a view pins the directory it was made from, so
    removing/renaming/migrating underneath it leaves a stale mount (and a
    stale fstab line) pointing at deleted data."""
    if cfg.paths.data_dir_in_vm:
        return
    views = registered_views(cfg, agent)
    if views:
        names = sorted({v.agent for v in views})
        raise UserError(
            f"cannot {action}: host view(s) registered for {', '.join(names)}. Run "
            + " / ".join(f"`agx access unmount {n}`" for n in names)
            + " first (and drop the matching /etc/fstab line)."
        )


# ---------- leftovers of the old ACL mechanism ----------


def legacy_acl_dirs(dirs: list[Path]) -> list[Path]:
    """Dirs still carrying a *default* ACL from the retired
    ``access grant`` / create-time auto-grant. Harmless for the view,
    but they keep propagating to every new file (overriding the agent's
    umask), so tell the user how to drop them. The toolkit doesn't do it
    itself: it needs sudo on a subuid-owned tree and changes nothing the
    toolkit depends on."""
    found = []
    for d in dirs:
        try:
            if "system.posix_acl_default" in os.listxattr(d):
                found.append(d)
        except OSError:
            continue
    return found


def _legacy_acl_hint(dirs: list[Path]) -> str | None:
    legacy = legacy_acl_dirs(dirs)
    if not legacy:
        return None
    return (
        "note: leftover ACLs from the retired `agx access grant` are still set on "
        + ", ".join(str(d) for d in legacy)
        + ". They are unused now; to drop them: "
        + " ; ".join(f"sudo setfacl -R -b -k {d}" for d in legacy)
    )


# ---------- mount ----------


def _primary_gid(cfg: AgentsConfig) -> int:
    """The host user's PRIMARY group — what files seen through the view
    should be grouped as. Not ``cfg.host.gid``: that is whatever gid the
    process had when the config was first written, which on the libvirt
    backend is often a supplementary group (``sg libvirt …``)."""
    try:
        return pwd.getpwuid(cfg.host.uid).pw_gid
    except KeyError:
        return cfg.host.gid


def _owner_ids(source: Path) -> tuple[int, int]:
    st = source.stat()  # stat needs no permission on the dir itself
    return st.st_uid, st.st_gid


def _spec_for(cfg: AgentsConfig, source: Path) -> str | None:
    """idmap spec from the ids actually on disk; None when the host user
    already owns ``source`` (nothing to map — e.g. a host-created
    OpenClaw ``workspace``)."""
    disk_uid, disk_gid = _owner_ids(source)
    if disk_uid == cfg.host.uid:
        return None
    return idmap_view.idmap_spec(
        host_uid=cfg.host.uid,
        host_gid=_primary_gid(cfg),
        disk_uid=disk_uid,
        disk_gid=disk_gid,
    )


def _prepare_view_dir(view: Path, root: Path) -> None:
    """Create the mountpoint as the invoking user. Every component from
    the view root down must be a real directory — the mount itself runs
    as root, and a symlink here would aim it somewhere else."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    rel = view.relative_to(root)
    current = root
    for part in ("", *rel.parts):
        current = current / part if part else current
        if current.is_symlink():
            raise UserError(f"refusing: {current} is a symlink")
        current.mkdir(exist_ok=True)
    if any(view.iterdir()):
        raise UserError(f"refusing to mount over non-empty directory {view}")


def _fstab_hint(lines: list[str], root: Path) -> str:
    return (
        "to keep the view across reboots, add to /etc/fstab:\n"
        + "\n".join(f"  {line}" for line in lines)
        + f"\nexclude {root} from host backups (duplicity etc.) — it is a second path "
        "to the same data, secrets included. Never open *.db files through the view "
        "while the agent runs."
    )


def mount(
    *,
    agent: str,
    writable: bool,
    yes: bool,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    agents_cmd.validate_name(agent)
    cfg = cfg_mod.load(config_path)
    _refuse_data_dir_in_vm(cfg)
    _, agent_type, dirs = _resolve_agent(cfg, agent)
    if not dirs:
        raise UserError(
            f"agent {agent!r} has no on-disk data dir yet — bring it up first "
            f"(`agx start {agent}`)."
        )
    root = view_root(cfg)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        entries = idmap_view.read_mountinfo()
        planned: list[tuple[View, str]] = []
        already: list[View] = []
        for unresolved in dirs:
            if unresolved.is_symlink():
                raise UserError(f"refusing: {unresolved} is a symlink")
            # The kernel reports resolved paths in mountinfo; a symlinked
            # component in the data-dir path would otherwise make our own
            # fresh mount look foreign.
            source = unresolved.resolve()
            spec = _spec_for(cfg, source)
            if spec is None:
                ui.info(f"{source} is already owned by you; no view needed")
                continue
            view = View(agent, source, _view_path(cfg, agent, agent_type, source), writable)
            status = idmap_view.view_status(entries, source, view.view)
            if status == "mounted":
                current = idmap_view.mount_at(entries, view.view)
                if current is not None and ("rw" in current.options) != writable:
                    raise UserError(
                        f"{view.view} is already mounted "
                        f"{'read-write' if 'rw' in current.options else 'read-only'}; run "
                        f"`agx access unmount {agent}` first to change the mode."
                    )
                already.append(view)
                continue
            if status != "not-mounted":
                raise UserError(
                    f"{view.view} holds a {status} mount; run `agx access unmount {agent}` first."
                )
            _prepare_view_dir(view.view, root)
            planned.append((view, idmap_view.mount_options(spec, writable=writable)))

        done: list[View] = []
        try:
            for view, options in planned:
                permissions.run_with_sudo(
                    [["mount", "--bind", "-o", options, str(view.source), str(view.view)]],
                    yes=yes,
                )
                done.append(view)
            entries = idmap_view.read_mountinfo()
            for view in done:
                if idmap_view.view_status(entries, view.source, view.view) != "mounted":
                    raise RuntimeFailure(
                        f"mount of {view.view} reported success but no idmapped view of "
                        f"{view.source} is visible there"
                    )
        except BaseException:
            for view in done:  # roll back this call's mounts; never leave half an agent
                subprocess.run(["sudo", "umount", str(view.view)], check=False)
            for view, _ in planned:
                _rmdir_quietly(view.view, root)
            raise

        active = [*already, *done]
        others = [v for v in registered_views(cfg) if v.agent != agent]
        _save_views(cfg, [*others, *active])

    lines = [line for v in active if (line := _fstab_line_for(cfg, v)) is not None]
    if (hint := _legacy_acl_hint(dirs)) is not None:
        ui.progress(hint)
    ui.emit(
        {
            "agent": agent,
            "action": "mount",
            "writable": writable,
            "views": [v.to_json() for v in active],
            "fstab_lines": lines,
            "view_root": str(root),
        },
        human=(
            (f"mounted {'read-write' if writable else 'read-only'} view(s):\n" if active else "")
            + "\n".join(f"  {v.view}  ->  {v.source}" for v in active)
            + ("\n" + _fstab_hint(lines, root) if active else "nothing to mount")
        ),
    )


# ---------- unmount ----------


def _rmdir_quietly(view: Path, root: Path) -> None:
    """Plain ``rmdir`` of the (now empty) mountpoint and its empty
    parents up to the view root. Never recursive: a view path shares its
    ``st_dev`` with the data, so a recursive delete of a still-mounted
    view would be a delete of the agent's state."""
    for p in (view, *view.parents):
        if p == root or root not in p.parents:
            break
        try:
            p.rmdir()
        except OSError:
            break


def unmount(
    *,
    agent: str,
    yes: bool,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Works from the state file and the live mount table — the agent
    may already be gone from compose."""
    agents_cmd.validate_name(agent)
    cfg = cfg_mod.load(config_path)
    root = view_root(cfg)

    with compose.acquire_lock(cfg.paths.host_data_dir):
        base = root / agent
        recorded = registered_views(cfg, agent)
        # The recorded view paths as well as the current root: if
        # [paths].host_view_dir changed since the mount, the live view is
        # still where it was made.
        anchors = {base, *(v.view for v in recorded)}
        mounted = sorted(
            {
                Path(e.mount_point)
                for e in idmap_view.read_mountinfo()
                for a in anchors
                if Path(e.mount_point) == a or a in Path(e.mount_point).parents
            },
            key=lambda p: len(p.parts),
            reverse=True,  # deepest first
        )
        if mounted:
            # Once per stacked layer would need a loop; one pass + the
            # re-read below keeps whatever is left registered.
            permissions.run_with_sudo([["umount", str(p)] for p in mounted], yes=yes)
        still = {e.mount_point for e in idmap_view.read_mountinfo()}
        for view in {*mounted, *(v.view for v in recorded)}:
            if str(view) in still:
                continue
            if root in view.parents:
                _rmdir_quietly(view, root)
            else:  # made under a previous view root: just the mountpoint
                with contextlib.suppress(OSError):
                    view.rmdir()
        kept = [v for v in recorded if str(v.view) in still]
        _save_views(cfg, [*(v for v in registered_views(cfg) if v.agent != agent), *kept])
        if kept:
            raise RuntimeFailure(
                "still mounted after umount: "
                + ", ".join(str(v.view) for v in kept)
                + " — close whatever is using it and run the command again."
            )

    ui.emit(
        {"agent": agent, "action": "unmount", "unmounted": [str(p) for p in mounted]},
        human=(
            "unmounted:\n" + "\n".join(f"  {p}" for p in mounted)
            if mounted
            else f"no view mounted for {agent!r}"
        )
        + f"\nremember to drop any /etc/fstab line for {base}",
    )


# ---------- list ----------


def _fstab_line_for(cfg: AgentsConfig, v: View) -> str | None:
    try:
        spec = _spec_for(cfg, v.source)
    except (OSError, ValueError):
        return None
    if spec is None:
        return None
    return idmap_view.fstab_line(
        v.source, v.view, idmap_view.mount_options(spec, writable=v.writable)
    )


def view_rows(cfg: AgentsConfig, agent: str | None = None) -> list[dict[str, Any]]:
    entries = idmap_view.read_mountinfo()
    rows = []
    for v in registered_views(cfg, agent):
        rows.append(
            {
                "agent": v.agent,
                "view": str(v.view),
                "source": str(v.source),
                "mode": "rw" if v.writable else "ro",
                "status": idmap_view.view_status(entries, v.source.resolve(), v.view),
                "fstab_line": _fstab_line_for(cfg, v),
            }
        )
    return rows


def list_views(
    *,
    agent: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    cfg = cfg_mod.load(config_path)
    _refuse_data_dir_in_vm(cfg)
    rows = view_rows(cfg, agent)
    if ui.is_json():
        ui.emit({"views": rows})
    elif rows:
        ui.table(rows, ["agent", "view", "mode", "status"])
        for r in rows:
            if r["status"] != "mounted":
                ui.echo_human(
                    f"{r['agent']}: {r['status']} — `agx access unmount {r['agent']}` then "
                    f"`agx access mount {r['agent']}`"
                    + (" (or add the fstab line it prints)" if r["status"] == "not-mounted" else "")
                )
        if (hint := _legacy_acl_hint([Path(r["source"]) for r in rows])) is not None:
            ui.echo_human(hint)
    else:
        ui.echo_human(
            "no host views registered — `agx access mount <agent>` creates one "
            "(`agx edit <agent> <path>` needs none)."
        )
    return rows
