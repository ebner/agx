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

    def _strip_docker_exec_prefix(
        cmd: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str] | None:
        """Recognise ``docker compose ... exec [flags] <svc> <inner...>`` and
        return ``(<inner...>, <svc>)``. Lets us reuse the bare-mv /
        bare-rm handlers when the production code routes them through
        ``docker compose exec`` (necessary in real --data-dir-in-vm mode
        where ubuntu can't touch subuid-owned dirs directly).
        """
        if not cmd or cmd[0] != "docker" or "exec" not in cmd:
            return None
        i = cmd.index("exec") + 1
        while i < len(cmd) and cmd[i].startswith("-"):
            if cmd[i] in ("--user", "-e"):
                i += 2
            else:
                i += 1
        if i + 1 >= len(cmd):
            return None
        return tuple(cmd[i + 1 :]), cmd[i]

    def _container_path(svc: str, path: str) -> Path:
        """Translate an in-container path (``/opt/data/...``) to its
        host-side location for a given service name. Hermes mounts
        ``./<name>/.data:/opt/data`` so ``/opt/data/X`` → ``<data-dir>/<name>/.data/X``.
        """
        for prefix in ("hermes-", "openclaw-"):
            if svc.startswith(prefix):
                agent = svc[len(prefix) :]
                if path.startswith("/opt/data"):
                    rel = path[len("/opt/data") :].lstrip("/")
                    return host_root / agent / ".data" / rel
                if path.startswith("/home/node/.openclaw"):
                    rel = path[len("/home/node/.openclaw") :].lstrip("/")
                    return host_root / agent / ".openclaw" / rel
        return host_path(path)

    def in_vm_runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        svc: str | None = None
        stripped = _strip_docker_exec_prefix(cmd)
        if stripped is not None:
            cmd, svc = stripped

        def resolve(p: str) -> Path:
            return _container_path(svc, p) if svc else host_path(p)

        if cmd and cmd[0] == "mv" and len(cmd) >= 3:
            src = resolve(cmd[1])
            dst = resolve(cmd[2])
            os.replace(src, dst)
            return _ok()
        if cmd and cmd[0] == "rm":
            for arg in cmd[1:]:
                if arg.startswith("-"):
                    continue
                p = resolve(arg)
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            return _ok()
        if cmd and cmd[0] == "cat" and len(cmd) >= 2:
            # `docker compose exec <svc> cat /opt/data/config.yaml` — read
            # the file the runtime-user-inside-container would see.
            p = resolve(cmd[1])
            if not p.exists():
                return MpResult(
                    stdout="", stderr=f"cat: {cmd[1]}: No such file or directory\n", returncode=1
                )
            try:
                return _ok(p.read_text(encoding="utf-8"))
            except OSError as e:
                return MpResult(stdout="", stderr=f"cat: {cmd[1]}: {e}\n", returncode=1)
        # `docker image inspect <hermes-image>`: simulate the image already
        # present so `_ensure_hermes_image_pulled` skips the pull.
        if (
            cmd
            and cmd[:3] == ("docker", "image", "inspect")
            and any("hermes-agent" in c for c in cmd)
        ):
            return _ok()
        # docker pull is a no-op success in the simulator.
        if cmd and cmd[:2] == ("docker", "pull"):
            return _ok()
        return _ok()

    def in_vm_with_stdin_runner(
        vm_name: str, *cmd: str, input_bytes: bytes, check: bool = True
    ) -> MpResult:
        """Simulate `docker compose exec -T <svc> sh -c 'cat > FILE'` —
        the production write path for hermes_config writes. Pulls FILE
        out of the shell snippet, maps it to its host path, writes the
        piped bytes.
        """
        svc: str | None = None
        stripped = _strip_docker_exec_prefix(cmd)
        inner = cmd
        if stripped is not None:
            inner, svc = stripped
        if (
            len(inner) >= 3
            and inner[0] == "sh"
            and inner[1] == "-c"
            and inner[2].startswith("cat > ")
        ):
            path = inner[2][len("cat > ") :].strip()
            dst = _container_path(svc, path) if svc else host_path(path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(input_bytes)
            return _ok()
        return _ok()

    def bash_runner(
        vm_name: str, script: str, *, check: bool = True, timeout: float | None = None
    ) -> MpResult:
        if ".compose.*.new" in script and "rm -f" in script:
            for p in host_root.glob(".compose.*.new"):
                p.unlink(missing_ok=True)
        # Mirror the production behaviour of the in-VM tar paths
        # (``upgrades._host_data_snapshot`` and ``export_cmd.export``
        # in host-mount mode): the bash script does
        # ``cd <vm_root> && [sudo] tar --zstd -cf <vm_archive>
        # <files...>`` and the toolkit then calls
        # ``mp.transfer_from_vm`` on ``<vm_archive>``. To keep the
        # end-to-end export/upgrade tests honest (they assert on
        # archive contents after round-trip), translate the cd target
        # to the host data-dir and run a real tar locally. Strips
        # ``sudo`` since we're already in the test runner's user
        # namespace and the seeded files are user-owned.
        import re as _re
        import subprocess as _subprocess

        # Tar EXTRACT (used by `agents import` + `agents rollback`)
        # — matches ``cd <vm_root> && [sudo] tar -xf <archive>``. Run
        # locally with the VM-path translation; ``sudo`` is stripped
        # because the test runner is already the file owner.
        mx = _re.search(
            r"cd\s+(\S+)\s*\n[^\n]*?tar\s+-xf\s+(\S+)",
            script,
        )
        if mx:
            vm_cwd_x, vm_archive_x = mx.groups()
            try:
                relx = Path(vm_cwd_x).relative_to(vm_root)
                host_cwd_x = host_root / relx
            except ValueError:
                host_cwd_x = host_root
            archive_host = Path(vm_archive_x)
            if archive_host.exists():
                _subprocess.run(
                    ["tar", "-xf", str(archive_host)],
                    cwd=str(host_cwd_x),
                    check=False,
                )

        # Match ``cd <root>`` then ``tar [--zstd] [extra flags] -cf
        # <archive> <files...>``. Production currently passes
        # ``--warning=no-file-changed`` and ``--ignore-failed-read``
        # between ``--zstd`` and ``-cf`` to tolerate live-write
        # rewrites of state.db/kanban.db; the regex permits arbitrary
        # tar flags before ``-cf`` so future flag additions don't
        # silently break the test simulator.
        m = _re.search(
            r"cd\s+(\S+)\s*\n[^\n]*?tar\s+(?:[^\n]*?\s)?-cf\s+(\S+)\s+([^\n]+)",
            script,
        )
        if m:
            vm_cwd, vm_archive, files_blob = m.groups()
            try:
                rel = Path(vm_cwd).relative_to(vm_root)
                host_cwd = host_root / rel
            except ValueError:
                host_cwd = host_root
            # The export-cmd path emits literal ``<name>`` tokens; the
            # upgrade-snapshot path emits a shell ``$([ -d X ] && ...)``
            # form. Both reduce to space-separated tokens after the
            # shell evaluates them in production. For the simulator
            # just include every token that's an existing file/dir
            # under host_cwd.
            tokens = [t.strip() for t in files_blob.split() if t.strip()]
            existing = [t for t in tokens if (host_cwd / t).exists()]
            if existing:
                _subprocess.run(
                    ["tar", "--zstd", "-cf", vm_archive, *existing],
                    cwd=str(host_cwd),
                    check=False,
                )
            else:
                # No matching files — still produce a valid empty archive
                # so transfer_from_vm doesn't FileNotFoundError.
                Path(vm_archive).write_bytes(b"")
        return _ok()

    mocks = {
        "transfer_to_vm": mocker.patch("agents_cli.mp.transfer_to_vm", side_effect=transfer_to),
        "transfer_from_vm": mocker.patch(
            "agents_cli.mp.transfer_from_vm", side_effect=transfer_from
        ),
        "exec_in_vm": mocker.patch("agents_cli.mp.exec_in_vm", side_effect=in_vm_runner),
        "exec_in_vm_with_stdin": mocker.patch(
            "agents_cli.mp.exec_in_vm_with_stdin", side_effect=in_vm_with_stdin_runner
        ),
        "exec_bash": mocker.patch("agents_cli.mp.exec_bash", side_effect=bash_runner),
        # `_run_agent_setup` uses the interactive variant so the wizard
        # actually sees stdin. The unit-test stub just reports rc=0.
        "exec_in_vm_interactive": mocker.patch(
            "agents_cli.mp.exec_in_vm_interactive", return_value=0
        ),
    }
    return mocks
