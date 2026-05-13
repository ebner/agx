"""secrets_cmd: per-agent .env round-trip + compose env_file backfill."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents_cli import compose, secrets_cmd
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


@pytest.fixture
def with_coder(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


# ---------- key validation ----------


def test_set_refuses_lowercase_key(with_coder: Path) -> None:
    with pytest.raises(UserError, match="invalid secret name"):
        secrets_cmd.set_secret("coder", "lower", "v", config_path=with_coder)


def test_set_refuses_leading_digit(with_coder: Path) -> None:
    with pytest.raises(UserError, match="invalid secret name"):
        secrets_cmd.set_secret("coder", "1FOO", "v", config_path=with_coder)


def test_set_refuses_dash(with_coder: Path) -> None:
    with pytest.raises(UserError, match="invalid secret name"):
        secrets_cmd.set_secret("coder", "FOO-BAR", "v", config_path=with_coder)


def test_set_refuses_newline_in_value(with_coder: Path) -> None:
    with pytest.raises(UserError, match="newlines"):
        secrets_cmd.set_secret("coder", "FIRECRAWL_API_KEY", "abc\ndef", config_path=with_coder)


def test_set_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        secrets_cmd.set_secret("ghost", "KEY", "v", config_path=saved_config)


# ---------- set / list / unset round-trip ----------


def test_set_writes_per_agent_env(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "FIRECRAWL_API_KEY", "fc-test", config_path=with_coder)

    env_path = cfg.paths.host_data_dir / "coder" / ".env"
    content = env_path.read_text(encoding="utf-8")
    assert "FIRECRAWL_API_KEY=fc-test" in content

    # Project-level .env MUST NOT be touched.
    project_env = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "FIRECRAWL_API_KEY" not in project_env


def test_set_replaces_existing_value(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "KEY", "old", config_path=with_coder)
    secrets_cmd.set_secret("coder", "KEY", "new", config_path=with_coder)
    env_path = cfg.paths.host_data_dir / "coder" / ".env"
    content = env_path.read_text(encoding="utf-8")
    assert "KEY=new" in content
    assert "KEY=old" not in content


def test_set_preserves_other_secrets(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "A", "1", config_path=with_coder)
    secrets_cmd.set_secret("coder", "B", "2", config_path=with_coder)
    env_path = cfg.paths.host_data_dir / "coder" / ".env"
    content = env_path.read_text(encoding="utf-8")
    assert "A=1" in content
    assert "B=2" in content


def test_unset_removes_key(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "KEEP", "yes", config_path=with_coder)
    secrets_cmd.set_secret("coder", "DROP", "yes", config_path=with_coder)
    secrets_cmd.unset_secret("coder", "DROP", config_path=with_coder)
    env_path = cfg.paths.host_data_dir / "coder" / ".env"
    content = env_path.read_text(encoding="utf-8")
    assert "KEEP=yes" in content
    assert "DROP" not in content


def test_unset_refuses_unknown_key(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    with pytest.raises(UserError, match="not set"):
        secrets_cmd.unset_secret("coder", "NEVER_SET", config_path=with_coder)


def test_list_returns_keys_only(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "ALPHA", "sensitive-a", config_path=with_coder)
    secrets_cmd.set_secret("coder", "BETA", "sensitive-b", config_path=with_coder)
    keys = secrets_cmd.list_secrets("coder", config_path=with_coder)
    assert keys == ["ALPHA", "BETA"]
    # The returned list does NOT contain the values anywhere.
    assert "sensitive-a" not in str(keys)


def test_clear_wipes_all_secrets(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "A", "1", config_path=with_coder)
    secrets_cmd.set_secret("coder", "B", "2", config_path=with_coder)
    secrets_cmd.clear_secrets("coder", yes=True, config_path=with_coder)
    keys = secrets_cmd.list_secrets("coder", config_path=with_coder)
    assert keys == []


# ---------- restart-on-mutation ----------


def test_set_calls_compose_up(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    up_spy = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "X", "y", config_path=with_coder)
    # compose up was called with the agent's service name so env_file
    # changes propagate to a recreated container.
    assert any("hermes-coder" in c.args for c in up_spy.call_args_list)


def test_unset_calls_compose_up(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "X", "y", config_path=with_coder)
    up_spy = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.unset_secret("coder", "X", config_path=with_coder)
    assert any("hermes-coder" in c.args for c in up_spy.call_args_list)


# ---------- compose env_file backfill ----------


def test_new_agent_has_env_file_in_compose(with_coder: Path, cfg: AgentsConfig) -> None:
    """New agents created with this build already have env_file wired
    by the service fragment — no backfill needed.
    """
    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["hermes-coder"]
    assert svc["env_file"] == ["./coder/.env"]


def test_backfill_adds_env_file_when_missing(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """Agents created before this command existed have no env_file
    clause. First `secret set` repairs the compose entry idempotently.
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    # Hand-build an "old-style" service entry without env_file.
    doc = compose.read_compose_doc(cfg)
    doc["services"] = {
        "hermes-old": {
            "image": "nousresearch/hermes-agent:v1",
            "container_name": "hermes-old",
            "volumes": ["./old/.data:/opt/data"],
        }
    }
    compose.write_compose_atomically(cfg, doc)
    # Touch the per-agent .env (would normally be created by `agents create`).
    (cfg.paths.host_data_dir / "old").mkdir(exist_ok=True)
    (cfg.paths.host_data_dir / "old" / ".env").write_text("", encoding="utf-8")

    secrets_cmd.set_secret("old", "FOO", "bar", config_path=saved_config)

    doc = compose.read_compose_doc(cfg)
    assert doc["services"]["hermes-old"]["env_file"] == ["./old/.env"]


def test_backfill_is_idempotent(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    """Already-wired env_file must not be duplicated."""
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "K1", "v1", config_path=with_coder)
    secrets_cmd.set_secret("coder", "K2", "v2", config_path=with_coder)
    doc = compose.read_compose_doc(cfg)
    assert doc["services"]["hermes-coder"]["env_file"] == ["./coder/.env"]


# ---------- env file parsing edge cases ----------


def test_parse_env_file_skips_comments_and_blanks() -> None:
    parsed = secrets_cmd._parse_env_file("# header\n\nKEY=value\n# inline\nOTHER=x=y\n")
    assert parsed == {"KEY": "value", "OTHER": "x=y"}


def test_render_env_file_sorts_for_determinism() -> None:
    rendered = secrets_cmd._render_env_file({"B": "2", "A": "1"})
    assert rendered == "A=1\nB=2\n"


def test_render_empty_env_file_is_empty_string() -> None:
    assert secrets_cmd._render_env_file({}) == ""


# ---------- all_secret_keys (toolkit-wide view) ----------


def test_all_secret_keys_lists_every_agent(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    secrets_cmd.set_secret("coder", "FIRECRAWL_API_KEY", "fc-x", config_path=with_coder)
    summary = secrets_cmd.all_secret_keys(cfg)
    assert summary["coder"] == ["FIRECRAWL_API_KEY"]


# ---------- list refuses unknown agent ----------


def test_list_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        secrets_cmd.list_secrets("ghost", config_path=saved_config)
