"""Pure helpers for ``agents access``'s idmapped bind-mount views.

An agent's state subtree is owned on disk by a rootless-Docker subuid
(container uid 1000 → e.g. host uid 100999) under mode 700. The userns
map is bijective and host uid 1000 is already taken by container root,
so that ownership can't be changed at the source. An *idmapped bind
mount* instead presents the same directory at a second path with the
owner ids swapped — through the view the host user simply IS the owner,
so mode bits, ``chmod`` and ACL masks stop mattering.

Everything here is side-effect free (spec/option/fstab builders and a
``mountinfo`` parser); :mod:`agents_cli.access_cmd` does the mounting.

Why a parser instead of :func:`os.path.ismount`: that compares
``st_dev`` of a path and its parent, and a view sits on the same
filesystem as the data it mirrors — it is invisible to ``ismount``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Highest id a mapping may cover: (uid_t)-1 is the kernel's "invalid id".
_ID_LIMIT = 2**32 - 1

# The view carries agent-written — i.e. untrusted — content into the host
# namespace. ``nosymfollow`` matters most: without it a planted
# ``SOUL.md -> ~/.bashrc`` resolves on the HOST when the user opens it.
_HARDENING = ("nosuid", "nodev", "noexec", "nosymfollow")


def _map_entries(kind: str, host_id: int, disk_id: int) -> list[str]:
    """``id-type:id-mount:id-host:id-range`` entries that swap
    ``host_id`` ↔ ``disk_id`` and map every other id to itself (anything
    left unmapped would show up as ``nobody`` and be inaccessible)."""
    if host_id == disk_id:
        return [f"{kind}:0:0:{_ID_LIMIT}"]
    low, high = sorted((host_id, disk_id))
    entries = [f"{kind}:{host_id}:{disk_id}:1", f"{kind}:{disk_id}:{host_id}:1"]
    for start, end in ((0, low), (low + 1, high), (high + 1, _ID_LIMIT)):
        if end > start:
            entries.append(f"{kind}:{start}:{start}:{end - start}")
    return entries


def idmap_spec(*, host_uid: int, host_gid: int, disk_uid: int, disk_gid: int) -> str:
    """The ``X-mount.idmap=`` value for a view of a tree owned on disk by
    ``disk_uid:disk_gid``. Callers take those from ``os.stat`` of the
    source dir — NOT from ``/etc/subuid`` math: the mapping that put the
    files there belongs to the Docker daemon's user (in the VM), and the
    host's subuid file only matches it by coincidence.
    """
    if host_uid == disk_uid:
        raise ValueError("source is already owned by the host user; no view needed")
    for value in (host_uid, host_gid, disk_uid, disk_gid):
        if not 0 <= value < _ID_LIMIT:
            raise ValueError(f"id out of range: {value}")
    return " ".join(
        [*_map_entries("u", host_uid, disk_uid), *_map_entries("g", host_gid, disk_gid)]
    )


def mount_options(spec: str, *, writable: bool) -> str:
    return ",".join(["rw" if writable else "ro", *_HARDENING, f"X-mount.idmap={spec}"])


# ---------- fstab ----------

_FSTAB_ESCAPES = {"\\": "\\134", " ": "\\040", "\t": "\\011", "\n": "\\012"}


def fstab_escape(field: str) -> str:
    """fstab fields are whitespace-separated with octal escapes — it is
    not shell syntax, quotes don't help."""
    return "".join(_FSTAB_ESCAPES.get(ch, ch) for ch in field)


def fstab_line(source: Path, view: Path, options: str) -> str:
    # ``nofail``: a missing agent dir must never block boot.
    fields = [str(source), str(view), "none", f"bind,nofail,{options}", "0", "0"]
    return " ".join(fstab_escape(f) for f in fields)


# ---------- /proc/self/mountinfo ----------


@dataclass(frozen=True)
class MountEntry:
    mount_point: str
    root: str  # path of the mounted subtree within its filesystem
    options: tuple[str, ...]
    fstype: str
    source: str

    @property
    def deleted(self) -> bool:
        """The directory the view was made from no longer exists (agent
        removed/renamed/recreated underneath it) — the view is stale."""
        return self.root.endswith("//deleted")

    @property
    def idmapped(self) -> bool:
        return "idmapped" in self.options


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[MountEntry]:
    """Parse ``/proc/self/mountinfo`` (proc(5)): ``id parent maj:min root
    mount-point options [optional…] - fstype source super-options``."""
    entries: list[MountEntry] = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        head, tail = left.split(), right.split()
        if not sep or len(head) < 6 or len(tail) < 2:
            continue
        entries.append(
            MountEntry(
                mount_point=_unescape(head[4]),
                root=_unescape(head[3]),
                options=tuple(head[5].split(",")),
                fstype=tail[0],
                source=_unescape(tail[1]),
            )
        )
    return entries


def mount_at(entries: Iterable[MountEntry], path: Path) -> MountEntry | None:
    """The mount visible at exactly ``path`` — the LAST match, since
    stacked mounts on one mountpoint are listed oldest first."""
    found = None
    for entry in entries:
        if entry.mount_point == str(path):
            found = entry
    return found


def expected_root(entries: Iterable[MountEntry], source: Path) -> str | None:
    """What a bind mount of ``source`` reports as its ``root``: the path
    of ``source`` *within its filesystem*. That equals the absolute path
    only when the filesystem is mounted at ``/`` — with a separate
    ``/home`` it is ``/<user>/agents/…`` — so derive it from the mount
    that contains ``source``."""
    best: MountEntry | None = None
    for entry in entries:
        point = Path(entry.mount_point)
        if (source == point or point in source.parents) and (
            best is None or len(entry.mount_point) >= len(best.mount_point)
        ):
            best = entry
    if best is None:
        return None
    rel = source.relative_to(best.mount_point)
    return str(Path(best.root) / rel)


VIEW_STATUSES = ("mounted", "not-mounted", "stale", "foreign")


def view_status(entries: list[MountEntry], source: Path, view: Path) -> str:
    """``mounted`` | ``not-mounted`` | ``stale`` (source dir was deleted
    or replaced underneath it) | ``foreign`` (something else is mounted
    there, or it isn't an idmapped view of ``source``)."""
    entry = mount_at(entries, view)
    if entry is None:
        return "not-mounted"
    if entry.deleted:
        return "stale"
    others = [e for e in entries if e is not entry]
    if not entry.idmapped or entry.root != expected_root(others, source):
        return "foreign"
    return "mounted"


def read_mountinfo() -> list[MountEntry]:
    return parse_mountinfo(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
