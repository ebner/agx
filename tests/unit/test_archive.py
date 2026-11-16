"""Unit tests for the shared archive helpers (``archive.py``)."""

from __future__ import annotations

import re

from agx_cli import archive, templates


def test_cache_exclude_args_hermes_and_other_types() -> None:
    hermes = archive.cache_exclude_args("coder", "hermes")
    assert hermes.startswith("--anchored ")
    for path in archive.HERMES_CACHE_PATHS:
        assert f"--exclude=coder/{path}" in hermes or f"--exclude='coder/{path}'" in hermes
    # Never the generic flag: uv tags every venv it creates with
    # CACHEDIR.TAG, so --exclude-caches would empty Hermes' installs/.
    assert "--exclude-caches" not in hermes
    # OpenClaw (and unknown types): the toolkit doesn't guess at another
    # runtime's cache layout.
    assert archive.cache_exclude_args("personal", "openclaw") == ""
    assert archive.cache_exclude_args("personal", None) == ""


def test_cont_init_hook_tags_every_untagged_cache_path() -> None:
    """The export excludes and the boot-time CACHEDIR.TAG writer must name
    the same caches, or host backups of the live dir keep storing what
    exports drop. ``cache/uv`` is exempt: uv tags its own cache."""
    hook = templates.HERMES_CONT_INIT_FIXPERMS
    section = hook.split("# --- 4. Backup exclusion tags", 1)[1]
    tagged = set(re.findall(r"/opt/data/(\S+?)(?=\s|;|\\|$)", section.split("; do", 1)[0]))
    expected = {p.removeprefix(".data/") for p in archive.HERMES_CACHE_PATHS} - {"cache/uv"}
    assert tagged == expected
    assert "Signature: 8a477f597d28d172789f06886806bc55" in section
    assert '/command/s6-setuidgid "$HUID:$HGID"' in section  # written as hermes (9p)
