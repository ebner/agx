"""Archive naming shared by upgrade snapshots and exports.

Both are tar+zstd archives written by ``tar --zstd`` inside the VM (or
on the host under bare_metal) — the host never compresses or extracts
them itself. The suffix lives here so callers don't hardcode it.
"""

from __future__ import annotations

import re
import shlex
from typing import Final

ARCHIVE_SUFFIX: Final = ".tar.zst"

# ``YYYYMMDDTHHMMSSZ`` — sorts lexicographically in time order.
UTC_STAMP_RE: Final = r"\d{8}T\d{6}Z"


# Package-manager / XDG caches in a Hermes agent's data dir, relative to
# ``<data-dir>/<name>/`` (container ``/opt/data`` = ``.data/`` is also the
# runtime HOME, so ``~/.npm`` is ``.data/.npm``). Re-downloaded on demand,
# never agent state — about 60% of a long-lived agent's archive (npm alone:
# 3 GB raw / 600 MB compressed on a live agent, 2026-11). Kept out of
# exports and upgrade snapshots; the cont-init hook also tags them with
# CACHEDIR.TAG (``templates.HERMES_CONT_INIT_FIXPERMS``) so host backups of
# the live dir skip them too — keep both lists in sync (a unit test checks).
# NOT here, on purpose: ``.data/cache/`` itself (Hermes keeps scratch work
# files there), ``installs/`` / ``lazy-packages/`` (reinstallable, but a
# restore without them needs network before the first tool use).
HERMES_CACHE_PATHS: Final = (
    ".data/.npm",
    ".data/.cache",
    ".data/home/.cache",
    ".data/cache/uv",
    ".data/profiles/*/home/.cache",
)


def cache_exclude_args(name: str, agent_type: str | None) -> str:
    """Shell-ready GNU tar options that leave an agent's caches out of an
    archive whose members start with ``<name>/`` (tar run from the
    project dir); ``""`` for agent types without a known cache layout.

    Explicit paths only — deliberately NOT ``--exclude-caches``: uv puts a
    CACHEDIR.TAG into every virtualenv it creates, so the generic flag
    emptied Hermes' ``installs/*/environments/*/venv`` dirs (found on a
    live export, 2026-11) and a restore would get broken venvs.
    ``--anchored`` matches from the member-name start only, so an agent's
    own file that merely ends in e.g. ``home/.cache`` stays in.
    """
    if agent_type != "hermes":
        return ""
    args = ["--anchored"]
    args += [f"--exclude={shlex.quote(f'{name}/{path}')}" for path in HERMES_CACHE_PATHS]
    return " ".join(args)


def archive_name_re(name: str) -> re.Pattern[str]:
    """Match ``<name>-<UTC stamp>`` + the archive suffix, exactly.

    A plain ``<name>-*`` glob would also match agent ``<name>-other``'s
    archives, so retention for ``foo`` could delete ``foo-bar``'s.
    """
    return re.compile(re.escape(name) + "-" + UTC_STAMP_RE + re.escape(ARCHIVE_SUFFIX))
