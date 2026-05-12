"""hermes_config.py: round-trip preservation, dashboard refusal, volume ops."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents_cli import hermes_config
from agents_cli.config import AgentsConfig, PathsConfig
from agents_cli.errors import UserError


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    return AgentsConfig(paths=PathsConfig(host_data_dir=str(tmp_path / "data")))


# ---------- load / dump round-trip ----------


def test_load_missing_returns_empty_doc(tmp_path: Path) -> None:
    doc = hermes_config.load(tmp_path / "nothing.yaml")
    # Empty mapping — nothing crashes on .get
    assert doc.get("model") is None


def test_round_trip_preserves_comments_and_key_order(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "# top-level\n"
        "model:\n"
        "  provider: openai-codex  # picked at setup\n"
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: sandbox/base:v1\n",
        encoding="utf-8",
    )
    doc = hermes_config.load(p)
    dumped = hermes_config.dump(doc)
    assert "top-level" in dumped
    assert "picked at setup" in dumped
    # Order preserved: model before terminal
    assert dumped.index("model:") < dumped.index("terminal:")


# ---------- dashboard binding refusal ----------


def test_validate_dashboard_refuses_0_0_0_0(tmp_path: Path) -> None:
    doc = hermes_config.load(tmp_path / "x.yaml")
    doc["dashboard"] = {"address": "0.0.0.0"}
    with pytest.raises(UserError, match=r"dashboard\.address"):
        hermes_config.validate_dashboard_binding(doc)


def test_validate_dashboard_refuses_public_ip(tmp_path: Path) -> None:
    doc = hermes_config.load(tmp_path / "x.yaml")
    doc["dashboard"] = {"address": "10.0.0.5"}
    with pytest.raises(UserError, match=r"dashboard\.address"):
        hermes_config.validate_dashboard_binding(doc)


def test_validate_dashboard_accepts_localhost_variants(tmp_path: Path) -> None:
    for addr in ["127.0.0.1", "localhost", "::1"]:
        doc = hermes_config.load(tmp_path / "x.yaml")
        doc["dashboard"] = {"address": addr}
        hermes_config.validate_dashboard_binding(doc)  # no raise


def test_validate_dashboard_accepts_missing_address(tmp_path: Path) -> None:
    doc = hermes_config.load(tmp_path / "x.yaml")
    hermes_config.validate_dashboard_binding(doc)  # no raise


def test_write_atomically_refuses_zero_address(cfg: AgentsConfig) -> None:
    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    doc["dashboard"] = {"address": "0.0.0.0"}
    with pytest.raises(UserError, match=r"dashboard\.address"):
        hermes_config.write_atomically(cfg, "coder", doc)


# ---------- atomic write ----------


def test_write_atomically_creates_path_and_file(cfg: AgentsConfig) -> None:
    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    hermes_config.set_terminal_docker_image(doc, "sandbox/base:v1")
    hermes_config.write_atomically(cfg, "coder", doc)
    p = hermes_config.config_path(cfg, "coder")
    assert p.exists()
    assert "sandbox/base:v1" in p.read_text(encoding="utf-8")


def test_write_atomically_leaves_no_temp_files(cfg: AgentsConfig) -> None:
    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    hermes_config.set_terminal_docker_image(doc, "sandbox/base:v1")
    hermes_config.write_atomically(cfg, "coder", doc)
    parent = hermes_config.config_path(cfg, "coder").parent
    assert not any(parent.glob("config.yaml.*.new"))


# ---------- terminal.docker_image helpers ----------


def test_set_terminal_docker_image_initializes_terminal_block() -> None:
    doc = hermes_config._yaml().load("{}\n")
    hermes_config.set_terminal_docker_image(doc, "sandbox/base:v1")
    assert doc["terminal"]["docker_image"] == "sandbox/base:v1"
    assert doc["terminal"]["backend"] == "docker"


def test_set_terminal_docker_image_preserves_existing_backend() -> None:
    doc = hermes_config._yaml().load("terminal:\n  backend: docker\n  docker_image: old:tag\n")
    hermes_config.set_terminal_docker_image(doc, "new:tag")
    assert doc["terminal"]["docker_image"] == "new:tag"
    assert doc["terminal"]["backend"] == "docker"


def test_get_terminal_docker_image_returns_none_when_missing() -> None:
    doc = hermes_config._yaml().load("{}\n")
    assert hermes_config.get_terminal_docker_image(doc) is None


# ---------- terminal.docker_volumes append/remove ----------


def test_add_docker_volume_creates_list() -> None:
    doc = hermes_config._yaml().load("{}\n")
    hermes_config.add_docker_volume(doc, "/host/path:/container/path:ro")
    assert doc["terminal"]["docker_volumes"] == ["/host/path:/container/path:ro"]


def test_add_docker_volume_is_idempotent() -> None:
    doc = hermes_config._yaml().load("{}\n")
    hermes_config.add_docker_volume(doc, "/a:/b:ro")
    hermes_config.add_docker_volume(doc, "/a:/b:ro")
    assert doc["terminal"]["docker_volumes"] == ["/a:/b:ro"]


def test_remove_docker_volume_removes_entry() -> None:
    doc = hermes_config._yaml().load(
        "terminal:\n  docker_volumes:\n    - /a:/b:ro\n    - /c:/d:ro\n"
    )
    hermes_config.remove_docker_volume(doc, "/a:/b:ro")
    assert list(doc["terminal"]["docker_volumes"]) == ["/c:/d:ro"]


def test_remove_docker_volume_no_op_when_missing() -> None:
    doc = hermes_config._yaml().load("terminal:\n  docker_volumes: []\n")
    hermes_config.remove_docker_volume(doc, "/never:/added:ro")
    assert list(doc["terminal"]["docker_volumes"]) == []


# ---------- model.provider getter ----------


def test_get_model_provider() -> None:
    doc = hermes_config._yaml().load("model:\n  provider: openai-codex\n")
    assert hermes_config.get_model_provider(doc) == "openai-codex"


def test_get_model_provider_returns_none_when_missing() -> None:
    doc = hermes_config._yaml().load("{}\n")
    assert hermes_config.get_model_provider(doc) is None


# ---------- config_path helper ----------


def test_config_path_layout(cfg: AgentsConfig) -> None:
    p = hermes_config.config_path(cfg, "coder")
    assert p == cfg.paths.host_data_dir / "coder" / ".data" / "config.yaml"
