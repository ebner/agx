"""agents_cmd.reseat: regenerate compose service from current fragment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents_cli import agents_cmd, compose
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import create
from agents_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    PathsConfig,
    VmRuntimeConfig,
)
from agents_cli.errors import UserError
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


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


def _stub_in_vm(vm_fs_mocks: dict[str, Any], uid_value: str = "1000") -> Any:
    """Chain a `id -u hermes` stub onto the FS simulator's exec_in_vm so
    compose mutations (mv, transfer) keep working in tests.
    """
    sim_runner = vm_fs_mocks["exec_in_vm"].side_effect

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout=f"{uid_value}\n")
        return sim_runner(vm_name, *cmd, check=check)

    return runner


@pytest.fixture
def with_coder_stale_fragment(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    """Create coder, then manually strip HERMES_UID/HERMES_GID from its
    compose env block to simulate "agent created before those vars were
    added to the toolkit fragment".
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)

    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["hermes-coder"]
    env = svc.get("environment") or {}
    env.pop("HERMES_UID", None)
    env.pop("HERMES_GID", None)
    compose.write_compose_atomically(cfg, doc)
    return saved_config


# ---------- happy path ----------


def test_reseat_restores_hermes_uid_in_env(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Core scenario: a stale agent's compose entry is missing HERMES_UID,
    reseat regenerates the block from the current fragment which DOES
    include HERMES_UID, and the env var reappears.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    # Pre-condition: stale entry has no HERMES_UID
    doc = compose.read_compose_doc(cfg)
    pre_env = doc["services"]["hermes-coder"]["environment"]
    assert "HERMES_UID" not in pre_env

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    post_env = doc["services"]["hermes-coder"]["environment"]
    assert post_env["HERMES_UID"] == "${HERMES_UID}"
    assert post_env["HERMES_GID"] == "${HERMES_GID}"


def test_reseat_preserves_port(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Reseat must NOT silently change the port allocation. The host port
    from the existing service block is carried through to the new one.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    pre_doc = compose.read_compose_doc(cfg)
    pre_ports = pre_doc["services"]["hermes-coder"]["ports"]

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    post_doc = compose.read_compose_doc(cfg)
    post_ports = post_doc["services"]["hermes-coder"]["ports"]
    assert pre_ports == post_ports


def test_reseat_preserves_image_tag(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """User may have pinned a specific tag on this agent; reseat must
    keep it.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    doc = compose.read_compose_doc(cfg)
    doc["services"]["hermes-coder"]["image"] = "nousresearch/hermes-agent:v9.9.9"
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    assert doc["services"]["hermes-coder"]["image"] == "nousresearch/hermes-agent:v9.9.9"


def test_reseat_preserves_env_file(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """The secrets command writes ``env_file: ./<name>/.env`` into the
    service entry. Reseat must not drop that — would break every secret.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    # The new fragment already includes env_file. Set it to a non-default
    # value to be sure reseat preserves user-modified env_file (e.g. multi-file).
    doc = compose.read_compose_doc(cfg)
    doc["services"]["hermes-coder"]["env_file"] = ["./coder/.env", "./shared.env"]
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    assert doc["services"]["hermes-coder"]["env_file"] == [
        "./coder/.env",
        "./shared.env",
    ]


def test_reseat_preserves_depends_on(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """OpenViking sibling adds a depends_on entry; reseat must preserve it."""
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    doc = compose.read_compose_doc(cfg)
    doc["services"]["hermes-coder"]["depends_on"] = ["openviking-coder"]
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    assert "openviking-coder" in doc["services"]["hermes-coder"]["depends_on"]


def test_reseat_force_recreates(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """The whole point of reseat is to make the new compose entry actually
    take effect — which requires a force-recreate (stop + docker rm + up),
    not just `compose up -d`.
    """
    stop_spy = mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    up_spy = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch(
        "agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000")
    )

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    assert stop_spy.called
    assert up_spy.called
    rm_calls = [
        c
        for c in in_vm_spy.call_args_list
        if "docker" in c.args and "rm" in c.args and "-f" in c.args
    ]
    assert rm_calls, "expected `docker rm -f` force-remove"


def test_reseat_runs_chown_chmod_for_hermes(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Hermes reseat must also re-align /opt/data perms via the fixperms
    helpers — adding HERMES_UID to env will only chown newly created
    files, but stale files from before the fragment change are still at
    the old uid.
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch(
        "agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "10000")
    )

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    chown_calls = [c for c in in_vm_spy.call_args_list if "chown" in c.args]
    chmod_calls = [c for c in in_vm_spy.call_args_list if "chmod" in c.args]
    assert chown_calls, "expected chown invocation"
    assert chmod_calls, "expected chmod invocation"
    # chown uses the numeric uid we queried, not the toolkit's hardcoded one
    assert "10000:10000" in " ".join(chown_calls[0].args)


# ---------- non-Hermes path ----------


@pytest.fixture
def with_personal_openclaw(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    return saved_config


def test_reseat_openclaw_skips_chown_chmod(
    with_personal_openclaw: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """OpenClaw reseat regenerates compose + force-recreates, but doesn't
    chown /opt/data (that's a Hermes-specific path).
    """
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    agents_cmd.reseat("personal", yes=True, config_path=with_personal_openclaw)

    assert not any("chown" in c.args and "/opt/data" in c.args for c in in_vm_spy.call_args_list)
    assert not any("chmod" in c.args and "/opt/data" in c.args for c in in_vm_spy.call_args_list)


# ---------- refusal cases ----------


def test_reseat_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        agents_cmd.reseat("ghost", yes=True, config_path=saved_config)


def test_reseat_refuses_without_yes(
    with_coder_stale_fragment: Path,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))
    mocker.patch("agents_cli.ui.confirm", return_value=False)

    with pytest.raises(UserError, match="aborted by user"):
        agents_cmd.reseat("coder", yes=False, config_path=with_coder_stale_fragment)


# ---------- _extract_host_port / _extract_image_tag ----------


def test_extract_host_port_handles_127_format() -> None:
    p = agents_cmd._extract_host_port({"ports": ["127.0.0.1:8642:8642"]})
    assert p == 8642


def test_extract_host_port_handles_plain_format() -> None:
    p = agents_cmd._extract_host_port({"ports": ["8642:8642"]})
    assert p == 8642


def test_extract_host_port_raises_on_malformed() -> None:
    with pytest.raises(UserError, match="could not extract host port"):
        agents_cmd._extract_host_port({"ports": []})


def test_extract_image_tag_works() -> None:
    assert (
        agents_cmd._extract_image_tag({"image": "nousresearch/hermes-agent:v2026.5.7"})
        == "v2026.5.7"
    )


def test_extract_image_tag_refuses_no_colon() -> None:
    with pytest.raises(UserError, match="no parseable image tag"):
        agents_cmd._extract_image_tag({"image": "nousresearch/hermes-agent"})
