"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from agents_cli.config import AgentsConfig
from agents_cli.mp import MpResult


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Iterator[Path]:
    """A throwaway data-dir layout that mimics `<host_data_dir>` on disk."""
    (tmp_path / ".journal").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / "backups").mkdir()
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    yield tmp_path


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def install_vm_fs_simulation(mocker, cfg: AgentsConfig) -> dict:
    """Stub multipass primitives so VM-side filesystem ops act on the
    host data-dir mount.

    Tests that exercise the full create/upgrade/remove flow rely on this
    because the production code path goes:
      host-temp -> multipass transfer -> VM-side mv
    Without simulation, the mocked transfer/mv leaves the host file
    state unchanged and assertions about final compose content fail.

    Translates any VM path under ``cfg.paths.vm_agents_root`` to the
    corresponding ``cfg.paths.host_data_dir`` path (the host-mount
    mapping).
    """
    vm_root = str(cfg.paths.vm_agents_root)
    host_root = cfg.paths.host_data_dir

    def host_path(vm_path: str) -> Path:
        p = Path(vm_path)
        try:
            rel = p.relative_to(vm_root)
            return host_root / rel
        except ValueError:
            return p

    def transfer_to(host_src: Path, vm_name: str, vm_dst: str) -> MpResult:
        dst = host_path(vm_dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(host_src), str(dst))
        return _ok()

    def transfer_from(vm_name: str, vm_src: str, host_dst: Path) -> MpResult:
        src = host_path(vm_src)
        shutil.copy(str(src), str(host_dst))
        return _ok()

    def in_vm_runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd and cmd[0] == "mv" and len(cmd) >= 3:
            src = host_path(cmd[1])
            dst = host_path(cmd[2])
            os.replace(src, dst)
            return _ok()
        if cmd and cmd[0] == "rm":
            for arg in cmd[1:]:
                if arg.startswith("-"):
                    continue
                p = host_path(arg)
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            return _ok()
        # docker inspect for the Hermes image: simulate the expected
        # USER+HOME so _verify_hermes_image_user passes without a real
        # image pull.
        if cmd and cmd[:2] == ("docker", "inspect") and any("hermes-agent" in c for c in cmd):
            return _ok("hermes\tHOME=/home/hermes\n")
        # docker pull is a no-op success in the simulator.
        if cmd and cmd[:2] == ("docker", "pull"):
            return _ok()
        return _ok()

    def bash_runner(
        vm_name: str, script: str, *, check: bool = True, timeout: float | None = None
    ) -> MpResult:
        if ".compose.*.new" in script and "rm -f" in script:
            for p in host_root.glob(".compose.*.new"):
                p.unlink(missing_ok=True)
        return _ok()

    mocks = {
        "transfer_to_vm": mocker.patch("agents_cli.mp.transfer_to_vm", side_effect=transfer_to),
        "transfer_from_vm": mocker.patch(
            "agents_cli.mp.transfer_from_vm", side_effect=transfer_from
        ),
        "exec_in_vm": mocker.patch("agents_cli.mp.exec_in_vm", side_effect=in_vm_runner),
        "exec_bash": mocker.patch("agents_cli.mp.exec_bash", side_effect=bash_runner),
    }
    return mocks
