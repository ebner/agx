"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Iterator[Path]:
    """A throwaway data-dir layout that mimics `<host_data_dir>` on disk."""
    (tmp_path / ".journal").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / "backups").mkdir()
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    yield tmp_path
