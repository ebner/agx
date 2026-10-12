"""mem0 plugin: restart-after-config-edit + mode-aware project-env reads.

Separate from test_memory.py (which covers the plugin registry and the
per-plugin enable/disable round-trips) — this file owns the mem0-specific
audit fixes: the gateway must actually be restarted after the config.yaml
edit (a bare `compose up -d` is a NO-OP when the compose config didn't
change), and OPENAI_API_KEY detection must read the .env the container
actually gets fed under --data-dir-in-vm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agx_cli import config as cfg_mod
from agx_cli.agents_cmd import create
from agx_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    OllamaConfig,
    PathsConfig,
    VmRuntimeConfig,
)
from agx_cli.memory import mem0
from agx_cli.mp import MpResult


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


@pytest.fixture
def with_coder(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


def _track_order(mocker: Any) -> list[str]:
    """Instrument config-write / up / restart so the test can assert
    their relative order. write_atomically keeps its real behavior
    (the config edit must actually land); the compose verbs are stubs.
    """
    import agx_cli.hermes_config as hermes_config_mod

    order: list[str] = []
    real_write = hermes_config_mod.write_atomically

    def tracked_write(*a: Any, **k: Any) -> None:
        order.append("config")
        real_write(*a, **k)

    mocker.patch("agx_cli.hermes_config.write_atomically", side_effect=tracked_write)
    mocker.patch(
        "agx_cli.compose_runner.up",
        side_effect=lambda *a, **k: (order.append("up"), _ok())[1],
    )
    mocker.patch(
        "agx_cli.compose_runner.restart",
        side_effect=lambda *a, **k: (order.append("restart"), _ok())[1],
    )
    return order


def test_enable_restarts_gateway_after_config_edit(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """`compose up -d` is a NO-OP when the compose config didn't change
    (the memory provider lives in config.yaml, not compose) — without a
    restart the running gateway keeps the old provider while the CLI
    claims mem0 is enabled.
    """
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    order = _track_order(mocker)

    mem0.enable(cfg, "coder")

    assert order == ["config", "up", "restart"]


def test_enable_restart_targets_the_agent_service(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    restart = mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())

    mem0.enable(cfg, "coder")

    args, _ = restart.call_args
    assert args[1] == "hermes-coder"


def test_disable_restarts_gateway_after_config_edit(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())
    mem0.enable(cfg, "coder")

    order = _track_order(mocker)
    mem0.disable(cfg, "coder")

    assert order == ["config", "up", "restart"]


# ---------- mode-aware OPENAI_API_KEY detection ----------


def test_openai_key_present_reads_host_env_in_host_mode(cfg: AgentsConfig) -> None:
    assert mem0._openai_key_present(cfg) is False
    env_path = cfg.paths.host_data_dir / ".env"
    env_path.write_text(env_path.read_text() + "OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    assert mem0._openai_key_present(cfg) is True


def test_openai_key_present_reads_vm_env_in_data_dir_in_vm_mode(
    cfg: AgentsConfig, mocker: Any
) -> None:
    """Under --data-dir-in-vm the .env compose feeds the container lives
    at <vm_agents_root>/.env inside the VM — the key check must read it
    from there (via multipass transfer), not assume a host copy.
    """
    from tests.conftest import install_vm_fs_simulation

    cfg.paths.data_dir_in_vm = True
    mocks = install_vm_fs_simulation(mocker, cfg)
    # Simulator maps <vm_agents_root>/.env to the host file.
    env_path = cfg.paths.host_data_dir / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")

    assert mem0._openai_key_present(cfg) is True
    assert mocks["transfer_from_vm"].called
