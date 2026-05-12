"""Tar+zstd archive helpers.

The plan specifies ``.tar.zst`` for upgrade snapshots and exports. We
use the ``zstandard`` library for host-side I/O (streaming compress and
decompress) and external ``tar --zstd`` inside the VM (Ubuntu 26.04
ships tar 1.34+ which supports it).

The suffix is exposed as :data:`ARCHIVE_SUFFIX` so callers don't hardcode
it.
"""

from __future__ import annotations

import tarfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import zstandard

ARCHIVE_SUFFIX: Final = ".tar.zst"


def write_tar_zst(target: Path, members: Iterable[tuple[Path, str]]) -> None:
    """Stream-compress a tar archive to ``target``.

    Each ``(src, arcname)`` tuple is added; non-existent ``src`` paths
    are skipped silently to match the prior tar.gz behavior under
    upgrades.
    """
    cctx = zstandard.ZstdCompressor(level=3)
    with (
        target.open("wb") as fh,
        cctx.stream_writer(fh, closefd=False) as zw,
        tarfile.open(fileobj=zw, mode="w|") as tf,
    ):
        for src, arcname in members:
            if src.exists():
                tf.add(src, arcname=arcname)


def extract_tar_zst(archive: Path, dest: Path) -> None:
    """Stream-decompress and extract ``archive`` into ``dest`` with the
    ``data`` filter to refuse absolute paths and dangerous link entries.
    """
    dctx = zstandard.ZstdDecompressor()
    with (
        archive.open("rb") as fh,
        dctx.stream_reader(fh, closefd=False) as zr,
        tarfile.open(fileobj=zr, mode="r|") as tf,
    ):
        tf.extractall(dest, filter="data")
