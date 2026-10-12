"""memory/: foundation + holographic + mem0 + openviking plugins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agx_cli import compose, hermes_config, memory
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
from agx_cli.errors import UserError
from agx_cli.memory import holographic, mem0, openviking
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


@pytest.fixture
def with_personal_openclaw(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    return saved_config


# ---------- registry / dispatch ----------


def test_list_plugins_returns_known_three() -> None:
    rows = memory.list_plugins()
    names = {r["name"] for r in rows}
    assert names == {"holographic", "mem0", "openviking"}


def test_unknown_plugin_raises_user_error(with_coder: Path) -> None:
    with pytest.raises(UserError, match="unknown memory plugin"):
        memory.enable("ghost", "coder", config_path=with_coder)


def test_enable_refuses_on_openclaw(with_personal_openclaw: Path) -> None:
    with pytest.raises(UserError, match="only applies to Hermes"):
        memory.enable("holographic", "personal", config_path=with_personal_openclaw)


def test_enable_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        memory.enable("holographic", "ghost", config_path=saved_config)


# ---------- holographic ----------


def test_holographic_enable_sets_provider_and_block(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    result = memory.enable("holographic", "coder", config_path=with_coder)
    assert result["db_path"] == holographic.DEFAULT_DB_PATH
    assert result["hrr_dim"] == holographic.DEFAULT_HRR_DIM

    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert doc["memory"]["provider"] == "holographic"
    assert doc["plugins"]["hermes-memory-store"]["db_path"] == holographic.DEFAULT_DB_PATH
    assert doc["plugins"]["hermes-memory-store"]["hrr_dim"] == holographic.DEFAULT_HRR_DIM

    # state file written
    state = memory.read_state(cfg, "coder")
    assert state is not None
    assert state["plugin"] == "holographic"


def test_holographic_enable_warns_on_9p_when_not_data_dir_in_vm(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    result = memory.enable("holographic", "coder", config_path=with_coder)
    assert any("9p" in w or "SQLite WAL" in w for w in result["warnings"])


def test_holographic_disable_restores_config(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    memory.enable("holographic", "coder", config_path=with_coder)
    memory.disable("coder", config_path=with_coder)

    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert "memory" not in doc or doc["memory"].get("provider") != "holographic"
    assert memory.read_state(cfg, "coder") is None


def test_holographic_enable_is_idempotent(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    memory.enable("holographic", "coder", config_path=with_coder)
    # Second call should not duplicate state or change defaults.
    memory.enable("holographic", "coder", config_path=with_coder)
    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert doc["plugins"]["hermes-memory-store"]["db_path"] == holographic.DEFAULT_DB_PATH


def test_holographic_enable_ups_then_restarts_container(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """The enable is a config.yaml-only edit, so `compose up -d` alone is
    a NO-OP for a running container (the gateway would keep the old
    provider) — but `restart` alone cannot CREATE an absent container
    (VM rebuild, `docker compose rm`). Must be `up` (guarantee
    existence) then `restart` (force the config.yaml re-read), in that
    order — same pattern as mem0.
    """
    parent = mocker.Mock()
    parent.attach_mock(mocker.patch("agx_cli.compose_runner.up", return_value=_ok()), "up")
    parent.attach_mock(
        mocker.patch("agx_cli.compose_runner.restart", return_value=_ok()), "restart"
    )

    memory.enable("holographic", "coder", config_path=with_coder)

    assert [name for name, _args, _kw in parent.mock_calls] == ["up", "restart"]
    assert parent.up.call_args.args[1] == "hermes-coder"
    assert parent.restart.call_args.args[1] == "hermes-coder"


def test_holographic_disable_ups_then_restarts_container(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """Same up-then-restart requirement on the disable path — restoring
    built-in memory is also a config.yaml-only edit, and the container
    may equally be absent.
    """
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    memory.enable("holographic", "coder", config_path=with_coder)

    parent = mocker.Mock()
    parent.attach_mock(mocker.patch("agx_cli.compose_runner.up", return_value=_ok()), "up")
    parent.attach_mock(
        mocker.patch("agx_cli.compose_runner.restart", return_value=_ok()), "restart"
    )

    memory.disable("coder", config_path=with_coder)

    assert [name for name, _args, _kw in parent.mock_calls] == ["up", "restart"]
    assert parent.up.call_args.args[1] == "hermes-coder"
    assert parent.restart.call_args.args[1] == "hermes-coder"


# ---------- mem0 ----------


def test_mem0_refuses_without_any_provider(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    # No [ollama] block, no OPENAI_API_KEY in .env → must refuse loudly.
    with pytest.raises(UserError, match=r"neither \[ollama\] nor OPENAI_API_KEY"):
        memory.enable("mem0", "coder", config_path=with_coder)


def test_mem0_picks_ollama_when_configured(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, with_coder)

    result = memory.enable("mem0", "coder", config_path=with_coder)
    assert result["embedder"] == "ollama"
    assert result["llm"] == "ollama"

    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert doc["memory"]["provider"] == "mem0"
    embedder = doc["plugins"]["mem0"]["embedder"]
    assert embedder["config"]["openai_base_url"].endswith("/v1")


def test_mem0_picks_openai_when_only_key_present(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    env_path = cfg.paths.host_data_dir / ".env"
    env_path.write_text(env_path.read_text() + "OPENAI_API_KEY=sk-xxx\n", encoding="utf-8")

    result = memory.enable("mem0", "coder", config_path=with_coder)
    assert result["embedder"] == "openai"
    assert result["llm"] == "openai"


def test_mem0_explicit_ollama_refused_when_unconfigured(with_coder: Path, mocker: Any) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    with pytest.raises(UserError, match=r"--embedder ollama requested but no \[ollama\]"):
        memory.enable("mem0", "coder", embedder="ollama", config_path=with_coder)


def test_mem0_disable_strips_config(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, with_coder)

    memory.enable("mem0", "coder", config_path=with_coder)
    memory.disable("coder", config_path=with_coder)

    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert "mem0" not in (doc.get("plugins") or {})


def test_mem0_appends_v1_when_missing(cfg: AgentsConfig) -> None:
    assert mem0._vfix("http://example/").endswith("/v1")
    assert mem0._vfix("http://example/v1") == "http://example/v1"
    assert mem0._vfix("http://example/v1/") == "http://example/v1"


# ---------- openviking ----------


def test_openviking_refuses_without_ollama(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    with pytest.raises(UserError, match="agx ollama configure"):
        memory.enable("openviking", "coder", config_path=with_coder)


def test_openviking_adds_sibling_service_and_wires_hermes(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, with_coder)

    result = memory.enable("openviking", "coder", config_path=with_coder)
    assert result["sidecar_service"] == "openviking-coder"
    assert result["endpoint"] == "http://openviking-coder:1933"

    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    assert "openviking-coder" in services
    sidecar = services["openviking-coder"]
    assert sidecar["image"].startswith("volcengine/openviking:")
    # No host port mapping — only the docker network reaches it.
    assert "ports" not in sidecar
    # Hermes service got a depends_on entry.
    hermes_svc = services["hermes-coder"]
    assert "openviking-coder" in hermes_svc.get("depends_on", [])


def test_openviking_hermes_config_points_at_sidecar(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, with_coder)

    memory.enable("openviking", "coder", config_path=with_coder)

    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert doc["memory"]["provider"] == "openviking"
    assert doc["plugins"]["openviking"]["endpoint"] == "http://openviking-coder:1933"


def test_openviking_disable_removes_sidecar(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.rm", return_value=_ok())
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, with_coder)

    memory.enable("openviking", "coder", config_path=with_coder)
    memory.disable("coder", config_path=with_coder)

    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    assert "openviking-coder" not in services
    hermes_svc = services["hermes-coder"]
    # depends_on entry pruned (empty list dropped).
    assert "openviking-coder" not in (hermes_svc.get("depends_on") or [])

    hdoc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert "openviking" not in (hdoc.get("plugins") or {})


def test_openviking_image_on_allowlist() -> None:
    """The toolkit MUST validate the sidecar image against the plan's
    parsed (registry, repo) tuple check, not a substring match.
    """
    from agx_cli.compose import validate_image_ref

    validate_image_ref(f"{openviking.OPENVIKING_IMAGE_BASE}:{openviking.DEFAULT_IMAGE_TAG}")


# ---------- status ----------


def test_status_reports_no_backend_initially(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    rows = memory.status("coder", config_path=with_coder)
    assert rows[0]["plugin"] == "(none)"
    assert rows[0]["managed_by_toolkit"] is False


def test_status_after_enable(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    memory.enable("holographic", "coder", config_path=with_coder)

    rows = memory.status("coder", config_path=with_coder)
    assert rows[0]["plugin"] == "holographic"
    assert rows[0]["managed_by_toolkit"] is True


def test_disable_refuses_when_nothing_enabled(with_coder: Path) -> None:
    with pytest.raises(UserError, match="no memory backend enabled"):
        memory.disable("coder", config_path=with_coder)
