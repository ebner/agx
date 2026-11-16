"""Mark the data dir's rebuildable directories for host backups.

Backups are the host's job (duplicity, restic, borg, …), and the natural
thing to back up is the whole ``<data-dir>``. Several directories under
it are not agent state but large, rebuildable artifacts — above all the
libvirt VM disk (``.disks/``, tens of GB, rewritten continuously) — that
would otherwise dominate every backup run.

Each such directory gets a ``CACHEDIR.TAG`` (https://bford.info/cachedir/):
``borg create --exclude-caches``, ``restic backup --exclude-caches`` and
``tar --exclude-caches`` skip a directory that holds one, and duplicity
does with ``--exclude-if-present CACHEDIR.TAG``. The toolkit only writes
the marker; whether it is honoured is the backup tool's configuration.

Tagging is best-effort and idempotent: a missing directory is skipped
(never created here), an existing tag is left alone, and a write error
never fails the command that triggered it.
"""

from __future__ import annotations

from pathlib import Path

from agx_cli.config import AgentsConfig

TAG_NAME = "CACHEDIR.TAG"
TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
TAG_CONTENT = (
    f"{TAG_SIGNATURE}\n"
    "# Rebuildable artifact of the agx toolkit, not agent state.\n"
    "# Backup tools that honour CACHEDIR.TAG (borg/restic/tar --exclude-caches,\n"
    "# duplicity --exclude-if-present CACHEDIR.TAG) skip this directory.\n"
)

# Top-level data-dir entries that never hold agent state:
#   .disks/ .images/ .seeds/  libvirt VM disk, cloud-image cache, cloud-init
#                             seed ISOs — recreated by `agx vm init/rebuild`
#   .staging/ .rebuild-staging/  transient transfer / rebuild staging
#   .upgrades/                host-local rollback tarballs (`agx upgrade`)
#   models/                   inference models — `agx model pull` again
# Agent dirs, compose files, .env, .codex/ and backups/ (the `agx export`
# archives) are deliberately NOT here.
REBUILDABLE_DIRS = (
    ".disks",
    ".images",
    ".seeds",
    ".staging",
    ".rebuild-staging",
    ".upgrades",
    "models",
)


def tag_dir(path: Path) -> bool:
    """Write ``CACHEDIR.TAG`` into an existing directory; ``True`` when
    the directory carries a valid tag afterwards."""
    tag = path / TAG_NAME
    try:
        if not path.is_dir():
            return False
        if tag.is_file() and tag.read_text(encoding="utf-8").startswith(TAG_SIGNATURE):
            return True
        tag.write_text(TAG_CONTENT, encoding="utf-8")
        return True
    except OSError:
        return False


def tag_rebuildable_dirs(cfg: AgentsConfig) -> list[Path]:
    """Tag every existing :data:`REBUILDABLE_DIRS` entry of the host data
    dir; returns the directories that carry a tag afterwards."""
    root = cfg.paths.host_data_dir
    return [root / name for name in REBUILDABLE_DIRS if tag_dir(root / name)]
