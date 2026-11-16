"""Unit tests for ``backup_tags`` — CACHEDIR.TAG markers that keep the
data dir's rebuildable directories out of host backups."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from agx_cli import backup_tags, compose, vm
from agx_cli import config as cfg_mod
from agx_cli.config import AgentsConfig, HostConfig, PathsConfig, VmRuntimeConfig


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock", ubuntu_uid=1000, ubuntu_gid=1000
        ),
    )


def test_tag_has_the_cachedir_signature_on_its_first_line(tmp_path: Path) -> None:
    """borg/restic/tar only honour a tag whose file STARTS with the exact
    signature (cachedir spec) — anything else is ignored silently."""
    assert backup_tags.tag_dir(tmp_path) is True
    first = (tmp_path / "CACHEDIR.TAG").read_bytes().split(b"\n", 1)[0]
    assert first == b"Signature: 8a477f597d28d172789f06886806bc55"


def test_tag_dir_is_idempotent_and_repairs_an_invalid_tag(tmp_path: Path) -> None:
    tag = tmp_path / "CACHEDIR.TAG"
    tag.write_text("Signature: valid-but-custom-text\n", encoding="utf-8")
    assert backup_tags.tag_dir(tmp_path) is True  # invalid signature → rewritten
    assert tag.read_text(encoding="utf-8") == backup_tags.TAG_CONTENT

    custom = backup_tags.TAG_SIGNATURE + "\n# hand-written comment\n"
    tag.write_text(custom, encoding="utf-8")
    assert backup_tags.tag_dir(tmp_path) is True  # valid tag → left alone
    assert tag.read_text(encoding="utf-8") == custom


def test_tag_dir_never_creates_a_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert backup_tags.tag_dir(missing) is False
    assert not missing.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory write permissions")
def test_tag_dir_swallows_write_errors(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        assert backup_tags.tag_dir(locked) is False
    finally:
        locked.chmod(0o700)


def test_tag_rebuildable_dirs_tags_existing_dirs_only(cfg: AgentsConfig) -> None:
    root = cfg.paths.host_data_dir
    for name in (".disks", ".images", "models", "backups", "coder"):
        (root / name).mkdir()
    tagged = backup_tags.tag_rebuildable_dirs(cfg)
    assert sorted(p.name for p in tagged) == [".disks", ".images", "models"]
    # Agent state and the export archives stay in the backup.
    assert not (root / "backups" / "CACHEDIR.TAG").exists()
    assert not (root / "coder" / "CACHEDIR.TAG").exists()
    assert not (root / ".upgrades").exists()  # never created


def test_host_staging_dir_is_tagged_when_created(cfg: AgentsConfig) -> None:
    d = compose.host_staging_dir(cfg)
    assert (d / "CACHEDIR.TAG").is_file()
    # The stale-temp sweep only removes *.yml — the tag survives it.
    compose.cleanup_stale_temps(cfg)
    assert (d / "CACHEDIR.TAG").is_file()


def test_vm_start_tags_rebuildable_dirs(cfg: AgentsConfig, tmp_path: Path, mocker: Any) -> None:
    cfg.vm.backend = "bare_metal"
    (cfg.paths.host_data_dir / ".upgrades").mkdir()
    config_path = tmp_path / "config.toml"
    cfg_mod.save(cfg, config_path)
    mocker.patch("agx_cli.compose.cleanup_stale_temps")
    mocker.patch("agx_cli.ui.emit")
    vm.start(config_path=config_path)
    assert (cfg.paths.host_data_dir / ".upgrades" / "CACHEDIR.TAG").is_file()
