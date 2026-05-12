"""agents_cmd.py: create/list/remove/rename with journal-driven idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents_cli import agents_cmd, compose
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import (
    create,
    list_agents,
    remove,
    rename,
    service_name,
    validate_name,
)
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import UserError
from agents_cli.journal import Journal
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
        defaults=DefaultsConfig(
            hermes_image_tag="v1.0.0",
            openclaw_image_tag="0.1.0",
        ),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


@pytest.fixture
def stub_runners(mocker):
    """Stub every multipass / docker-compose interaction with success."""
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.down", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())


# ---------- validate_name ----------


@pytest.mark.parametrize("good", ["coder", "personal", "x", "a1", "my-bot", "my_bot"])
def test_validate_name_accepts(good: str) -> None:
    validate_name(good)


@pytest.mark.parametrize(
    "bad", ["", "Coder", "coder!", "-leading-dash", "with space", "a" * 51, "../slashy"]
)
def test_validate_name_rejects(bad: str) -> None:
    with pytest.raises(UserError):
        validate_name(bad)


def test_service_name_hermes() -> None:
    assert service_name("coder", "hermes") == "hermes-coder"


def test_service_name_openclaw() -> None:
    assert service_name("personal", "openclaw") == "openclaw-personal"


def test_service_name_unknown_type() -> None:
    with pytest.raises(UserError):
        service_name("x", "bogus")


# ---------- create ----------


def test_create_hermes_writes_compose_and_dirs(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    # Data dir + .data created
    assert (cfg.paths.host_data_dir / "coder").is_dir()
    assert (cfg.paths.host_data_dir / "coder" / ".data").is_dir()
    # Compose has the service with the right shape
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" in doc["services"]
    svc = doc["services"]["hermes-coder"]
    assert svc["image"] == "nousresearch/hermes-agent:v1.0.0"
    assert any("/.codex:/home/hermes/.codex" in v for v in svc["volumes"])


def test_create_openclaw_uses_per_agent_paths(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    assert (cfg.paths.host_data_dir / "personal" / ".openclaw").is_dir()
    assert (cfg.paths.host_data_dir / "personal" / "workspace").is_dir()
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    svc = doc["services"]["openclaw-personal"]
    assert any("./personal/.openclaw" in v for v in svc["volumes"])


def test_create_refuses_unknown_type(saved_config: Path) -> None:
    with pytest.raises(UserError, match="unknown agent type"):
        create(name="coder", agent_type="bogus", config_path=saved_config)


def test_create_refuses_existing_data_dir_without_reuse(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    with pytest.raises(UserError, match="already exists"):
        create(name="coder", agent_type="hermes", config_path=saved_config)


def test_create_with_reuse_data_attaches_to_existing_dir(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    create(name="coder", agent_type="hermes", reuse_data=True, config_path=saved_config)
    assert (
        "hermes-coder"
        in compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")["services"]
    )


def test_create_refuses_when_service_already_in_compose(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    with pytest.raises(UserError, match="already exists"):
        create(name="coder", agent_type="hermes", config_path=saved_config)


def test_create_picks_next_free_port_when_none_given(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    mocker.patch("agents_cli.compose.used_ports", return_value={8642, 8643})
    create(name="coder", agent_type="hermes", config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["ports"] == ["127.0.0.1:8644:8642"]


def test_create_respects_explicit_port(cfg: AgentsConfig, saved_config: Path, stub_runners) -> None:
    create(name="coder", agent_type="hermes", port=9999, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["ports"] == ["127.0.0.1:9999:8642"]


def test_create_respects_explicit_image_tag(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", image_tag="v2.0.0-rc1", config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["image"].endswith(":v2.0.0-rc1")


def test_create_completes_journal(cfg: AgentsConfig, saved_config: Path, stub_runners) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    assert Journal.find_resumable(cfg.paths.host_data_dir, "create", "coder") is None


def test_create_resumes_from_partial_journal(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """Pre-seed a journal as if a previous run completed two of four phases.
    Re-running should pick up at service_up and skip the already-done phases.
    """
    j = Journal.begin(cfg.paths.host_data_dir, "create", "coder", agents_cmd.CREATE_PHASES)
    # Simulate data dir + compose already done
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.add_service(
        doc,
        "hermes-coder",
        compose.hermes_service_fragment(name="coder", cfg=cfg, port=8642, image_tag="v1.0.0"),
    )
    compose_path = cfg.paths.host_data_dir / "docker-compose.yml"
    compose_path.write_text(compose.dump_compose(doc), encoding="utf-8")
    j.advance("data_dir_created")
    j.advance("compose_updated")

    create_data_spy = mocker.spy(agents_cmd, "_create_data_dirs")
    add_service_spy = mocker.spy(agents_cmd, "_add_service_to_compose")
    up_spy = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    create(name="coder", agent_type="hermes", reuse_data=True, config_path=saved_config)

    # Skipped finished phases
    create_data_spy.assert_not_called()
    add_service_spy.assert_not_called()
    # Ran remaining phase
    up_spy.assert_called_once()


def test_create_with_setup_runs_wizard(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    setup_spy = mocker.spy(agents_cmd, "_run_agent_setup")
    create(name="coder", agent_type="hermes", run_setup=True, config_path=saved_config)
    setup_spy.assert_called_once()


# ---------- list ----------


def test_list_agents_returns_rows_for_each_service(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    rows = list_agents(config_path=saved_config)
    names = {r["name"] for r in rows}
    assert names == {"coder", "personal"}
    types = {r["type"] for r in rows}
    assert types == {"hermes", "openclaw"}


def test_list_agents_ignores_non_agent_services(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.add_service(doc, "random-helper", {"image": "x:y"})
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )
    rows = list_agents(config_path=saved_config)
    assert rows == []


# ---------- remove ----------


def test_remove_stops_and_drops_service(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    remove("coder", yes=True, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" not in (doc.get("services") or {})
    assert not (cfg.paths.host_data_dir / "coder").exists()


def test_remove_preserves_data_when_delete_data_false(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    remove("coder", yes=True, delete_data=False, config_path=saved_config)
    assert (cfg.paths.host_data_dir / "coder").exists()


def test_remove_unknown_agent_raises(saved_config: Path, stub_runners) -> None:
    with pytest.raises(UserError, match="not found"):
        remove("ghost", yes=True, config_path=saved_config)


def test_remove_aborts_without_confirmation(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    with pytest.raises(UserError, match="aborted"):
        remove("coder", config_path=saved_config)


# ---------- rename ----------


def test_rename_updates_compose_and_dir(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    rename("coder", "writer", config_path=saved_config)

    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-writer" in doc["services"]
    assert "hermes-coder" not in doc["services"]
    # Volume paths rewritten
    vols = doc["services"]["hermes-writer"]["volumes"]
    assert any("./writer/.data:/opt/data" in v for v in vols)
    assert not any("./coder/" in v for v in vols)
    # Container name rewritten
    assert doc["services"]["hermes-writer"]["container_name"] == "hermes-writer"
    # Dir renamed
    assert (cfg.paths.host_data_dir / "writer").is_dir()
    assert not (cfg.paths.host_data_dir / "coder").exists()


def test_rename_refuses_when_target_exists(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    create(name="writer", agent_type="hermes", config_path=saved_config)
    with pytest.raises(UserError, match="already exists"):
        rename("coder", "writer", config_path=saved_config)


def test_rename_refuses_unknown_source(saved_config: Path, stub_runners) -> None:
    with pytest.raises(UserError, match="not in compose"):
        rename("ghost", "x", config_path=saved_config)
