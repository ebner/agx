"""fixperms_cmd: stop + force-remove + recreate + chown + chmod."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents_cli import config as cfg_mod
from agents_cli import fixperms_cmd
from agents_cli.agents_cmd import create
from agents_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    PathsConfig,
    VmRuntimeConfig,
)
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail(stderr: str = "boom") -> MpResult:
    return MpResult(stdout="", stderr=stderr, returncode=1)


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (data_dir / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
        defaults=DefaultsConfig(hermes_image_tag="v1.0.0", openclaw_image_tag="0.1.0"),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


@pytest.fixture
def vm_fs_mocks(cfg: AgentsConfig, mocker: Any) -> dict[str, Any]:
    from tests.conftest import install_vm_fs_simulation

    return install_vm_fs_simulation(mocker, cfg)


@pytest.fixture
def with_coder(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


@pytest.fixture
def with_openclaw(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    return saved_config


# ---------- refusal cases ----------


def test_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        fixperms_cmd.fix_perms("ghost", yes=True, config_path=saved_config)


def test_refuses_openclaw(with_openclaw: Path) -> None:
    with pytest.raises(UserError, match="Hermes-only"):
        fixperms_cmd.fix_perms("personal", yes=True, config_path=with_openclaw)


# ---------- happy path ----------


def _stub_in_vm(uid_value: str = "1000") -> Any:
    """Return an `mp.exec_in_vm` side-effect that:
    - reports hermes uid via `id -u hermes` as `uid_value`
    - reports success for every other invocation
    """

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        # `id -u hermes` query — return the configured uid.
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout=f"{uid_value}\n")
        return _ok()

    return runner


def test_happy_path_when_entrypoint_aligned(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """hermes inside container = uid 1000 = toolkit RUNTIME_UID. fix-perms
    reports `aligned=True` and proceeds without warnings.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm("1000"))

    result = fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)
    assert result["aligned"] is True
    assert result["hermes_uid"] == 1000
    assert result["runtime_uid"] == 1000


def test_happy_path_when_entrypoint_did_not_usermod(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """Entrypoint failed to honour HERMES_UID — hermes still at 10000.
    fix-perms still runs to completion but reports `aligned=False` so
    the user knows the underlying image issue persists.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm("10000"))

    result = fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)
    assert result["aligned"] is False
    assert result["hermes_uid"] == 10000
    assert result["runtime_uid"] == 1000


# ---------- ordering ----------


def test_force_recreate_sequence(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    """Stop → docker rm -f → compose up — in that order."""
    stop_spy = mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    up_spy = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm("1000"))

    fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)

    # stop happens before any docker rm
    assert stop_spy.called
    # docker rm -f hermes-coder happens
    rm_calls = [
        c
        for c in in_vm_spy.call_args_list
        if "docker" in c.args and "rm" in c.args and "-f" in c.args
    ]
    assert rm_calls, "expected `docker rm -f` invocation"
    # compose up happens after the rm
    assert up_spy.called


def test_chown_uses_numeric_uid(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    """The chown must use the queried numeric uid, NOT a hardcoded value
    or the username 'hermes' (which would resolve to whatever uid is in
    /etc/passwd, defeating the purpose).
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm("10000"))

    fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)
    chown_calls = [c for c in in_vm_spy.call_args_list if "chown" in c.args]
    assert chown_calls, "expected chown invocation"
    # the chown's uid:gid arg matches the queried hermes uid (10000),
    # NOT the hardcoded RUNTIME_UID.
    args_joined = " ".join(chown_calls[0].args)
    assert "10000:10000" in args_joined
    assert "1000:1000" not in args_joined.split("chown")[1].split("/opt/data")[0]


def test_chown_hermes_install_paths_chowns_both_targets(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """The helper must chown both /opt/hermes/scripts and /home/hermes
    so `hermes setup`'s install branches don't hit EACCES. Verifies
    `test -e <path>` is probed first (skip-on-missing semantics).
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    seen_chowns: list[tuple[str, ...]] = []

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout="1000\n")
        if "test" in cmd and "-e" in cmd:
            return _ok()
        if "chown" in cmd:
            seen_chowns.append(cmd)
            return _ok()
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)

    fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)

    # The install-paths helper uses `find <path> -xdev -exec chown ...`
    # (so it skips bind-mounts like /home/hermes/.codex); the data-dir
    # helper still uses `chown -R /opt/data`. Both forms put the target
    # path in argv as a literal element, so a set-membership check
    # works uniformly.
    chowned = {arg for c in seen_chowns for arg in c}
    assert "/opt/data" in chowned
    assert "/opt/hermes/scripts" in chowned
    assert "/home/hermes" in chowned


def test_chown_hermes_install_paths_skips_missing(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """If `test -e /opt/hermes/scripts` returns non-zero (older Hermes
    image without that dir), the helper must skip rather than warn or
    fail. /home/hermes is always present so it still gets chowned.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    seen_chowns: list[tuple[str, ...]] = []

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout="1000\n")
        # /opt/hermes/scripts missing; /home/hermes present
        if "test" in cmd and "-e" in cmd and "/opt/hermes/scripts" in cmd:
            return MpResult(stdout="", stderr="", returncode=1)
        if "test" in cmd and "-e" in cmd:
            return _ok()
        if "chown" in cmd:
            seen_chowns.append(cmd)
            return _ok()
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)

    fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)

    chowned = {arg for c in seen_chowns for arg in c}
    assert "/opt/hermes/scripts" not in chowned
    assert "/home/hermes" in chowned


def test_chown_hermes_install_paths_prunes_codex_mount(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """The /home/hermes chown must `-prune` the .codex bind-mount, which
    is mounted :ro for the Codex import bridge. Without pruning, the
    recursive chown would emit a noisy "Read-only file system" warning
    on every entry under .codex (regression seen 2026-05-13 on coder).
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    find_invocations: list[tuple[str, ...]] = []

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout="1000\n")
        if "test" in cmd and "-e" in cmd:
            return _ok()
        if "find" in cmd:
            find_invocations.append(cmd)
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)

    fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)

    home_find = next(c for c in find_invocations if "/home/hermes" in c and "/opt" not in c[-3:])
    # Required argv order around the prune: `-path /home/hermes/.codex -prune -o`
    idx = home_find.index("/home/hermes/.codex")
    assert home_find[idx - 1] == "-path"
    assert home_find[idx + 1] == "-prune"
    assert home_find[idx + 2] == "-o"
    # /opt/hermes/scripts has no prune list — must NOT include -prune at all.
    scripts_find = next(c for c in find_invocations if "/opt/hermes/scripts" in c)
    assert "-prune" not in scripts_find


def test_chmod_opens_data_dir_traversal(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    """The chmod 755 must always run, regardless of alignment, so the
    toolkit's RUNTIME_UID exec can traverse /opt/data even when the
    image entrypoint didn't usermod.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm("10000"))

    fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)
    chmod_calls = [c for c in in_vm_spy.call_args_list if "chmod" in c.args]
    assert any("755" in c.args and "/opt/data" in c.args for c in chmod_calls)


# ---------- failure propagation ----------


def test_compose_up_failure_raises(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_fail("up exploded"))
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm("1000"))

    with pytest.raises(RuntimeFailure, match="compose up failed"):
        fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)


def test_unparseable_hermes_uid_raises(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout="not-a-uid\n")
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    with pytest.raises(RuntimeFailure, match="unparseable hermes uid"):
        fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)


def test_chown_failure_propagates(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout="1000\n")
        if "chown" in cmd:
            return _fail("chown denied")
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    with pytest.raises(RuntimeFailure, match="chown"):
        fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)


# ---------- confirmation ----------


def test_refuses_without_yes_and_no_confirm(with_coder: Path, mocker: Any) -> None:
    """In non-interactive mode (no TTY, no --yes), the confirm prompt
    is what blocks. Simulate by patching `ui.confirm` to return False.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.ui.confirm", return_value=False)

    with pytest.raises(UserError, match="aborted by user"):
        fixperms_cmd.fix_perms("coder", yes=False, config_path=with_coder)


# ---------- progress narration ----------


def test_fix_perms_narrates_recreate_and_chown(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """fix-perms force-recreates the container and chown -R's /opt/data
    (minutes on big data dirs) — each stage gets an always-on progress
    line so the terminal never reads as hung."""
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm("1000"))
    spy = mocker.spy(fixperms_cmd.ui, "progress")

    fixperms_cmd.fix_perms("coder", yes=True, config_path=with_coder)

    lines = [c.args[0] for c in spy.call_args_list]
    assert any("force-recreating hermes-coder" in ln for ln in lines)
    assert any("chown -R" in ln and "/opt/data" in ln and "minutes" in ln for ln in lines)
    assert any("install paths" in ln for ln in lines)
