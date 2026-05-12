"""flock-based mutation lock at <data-dir>/.agents.lock.

Plan rule #10: every compose / .env / hermes-config mutation acquires
the lock; read-only commands don't. The first half is verified here
(two concurrent locked workers serialize). The second half — that
read-only paths don't lock — is exercised throughout the rest of the
suite by tests that never patch / acquire the lock and still pass
inside larger flows that do.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agents_cli.compose import acquire_lock
from agents_cli.config import AgentsConfig, PathsConfig


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def test_acquire_lock_serializes_two_threads(data_dir: Path) -> None:
    """Two workers both grab the lock; without serialization their
    start/end events would interleave."""
    timeline: list[str] = []

    def worker(label: str, hold: float) -> None:
        with acquire_lock(data_dir):
            timeline.append(f"{label}:start")
            time.sleep(hold)
            timeline.append(f"{label}:end")

    t1 = threading.Thread(target=worker, args=("A", 0.05))
    t2 = threading.Thread(target=worker, args=("B", 0.05))
    t1.start()
    time.sleep(0.01)  # ensure A acquires first
    t2.start()
    t1.join()
    t2.join()

    assert timeline == ["A:start", "A:end", "B:start", "B:end"]


def test_lock_file_is_at_canonical_location(data_dir: Path) -> None:
    """The lock file lives at <data-dir>/.agents.lock — referenced by
    threat-model + runbook documentation."""
    with acquire_lock(data_dir):
        assert (data_dir / ".agents.lock").exists()


def test_lock_releases_on_exception(data_dir: Path) -> None:
    """An exception inside the with-block must not leave the lock held."""

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom), acquire_lock(data_dir):
        raise _Boom

    # A subsequent acquisition must succeed immediately (no deadlock).
    acquired = False
    with acquire_lock(data_dir):
        acquired = True
    assert acquired


def test_lock_takes_cfg_compatible_dir(
    data_dir: Path,
) -> None:
    cfg = AgentsConfig(paths=PathsConfig(host_data_dir=str(data_dir)))
    with acquire_lock(cfg.paths.host_data_dir):
        pass
