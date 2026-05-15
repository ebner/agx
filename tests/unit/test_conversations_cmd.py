"""conversations_cmd: active-session stats for a Hermes agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents_cli import config as cfg_mod
from agents_cli import conversations_cmd
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
def with_coder(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> Path:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


@pytest.fixture
def with_openclaw_personal(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> Path:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    return saved_config


# ---------- happy path ----------


def test_conversations_returns_empty_active_when_all_sessions_cold(
    with_coder: Path, mocker: Any
) -> None:
    """20 sessions in DB, none touched within freshness window → 0 active.
    Per-platform map is still populated from the cold-but-open sessions.
    """
    canned = {
        "freshness_window_s": 3600,
        "total": 20,
        "open": 11,
        "ended": 9,
        "active": 0,
        "by_platform": {
            "whatsapp": {"total": 11, "open": 6, "active": 0},
            "cli": {"total": 4, "open": 2, "active": 0},
            "email": {"total": 5, "open": 3, "active": 0},
        },
        "active_sessions": [],
    }
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(json.dumps(canned)))

    payload = conversations_cmd.status("coder", config_path=with_coder)
    assert payload["agent"] == "coder"
    assert payload["active"] == 0
    assert payload["total"] == 20
    assert payload["by_platform"]["whatsapp"]["total"] == 11


def test_conversations_renders_active_sessions_when_present(with_coder: Path, mocker: Any) -> None:
    """One whatsapp session inside the 3600s window. Verify the human
    output mentions the title + age-formatted column.
    """
    canned = {
        "freshness_window_s": 3600,
        "total": 5,
        "open": 3,
        "ended": 2,
        "active": 1,
        "by_platform": {"whatsapp": {"total": 3, "open": 2, "active": 1}},
        "active_sessions": [
            {
                "id": "20260515_120000_abc",
                "source": "whatsapp",
                "title": "Birthday party planning",
                "message_count": 17,
                "input_tokens": 12500,
                "output_tokens": 4200,
                "cache_read_tokens": 0,
                "estimated_cost_usd": 0.04,
                "ended_at": None,
                "last_ts": 1234567890.0,
                "age_s": 900,
            },
        ],
    }
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(json.dumps(canned)))
    emit_spy = mocker.spy(conversations_cmd.ui, "emit")

    payload = conversations_cmd.status("coder", config_path=with_coder)
    assert payload["active"] == 1
    human_arg = emit_spy.call_args.kwargs["human"]
    assert "Birthday party planning" in human_arg
    assert "1 active" in human_arg
    assert "15m" in human_arg  # 900 seconds → 15m


def test_conversations_respects_limit(with_coder: Path, mocker: Any) -> None:
    """A high-volume agent with 50 active sessions should render only
    `limit` rows in the table, with a '... (N more)' suffix line.
    """
    active = [
        {
            "id": f"s{i:03d}",
            "source": "whatsapp",
            "title": f"chat {i}",
            "message_count": 5,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "estimated_cost_usd": 0.0,
            "ended_at": None,
            "last_ts": 1.0,
            "age_s": 60 * i,
        }
        for i in range(50)
    ]
    canned = {
        "freshness_window_s": 3600,
        "total": 50,
        "open": 50,
        "ended": 0,
        "active": 50,
        "by_platform": {"whatsapp": {"total": 50, "open": 50, "active": 50}},
        "active_sessions": active,
    }
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(json.dumps(canned)))
    emit_spy = mocker.spy(conversations_cmd.ui, "emit")

    conversations_cmd.status("coder", limit=5, config_path=with_coder)
    human_arg = emit_spy.call_args.kwargs["human"]
    # First 5 rows present
    for i in range(5):
        assert f"chat {i}" in human_arg
    # 5th not present
    assert "chat 6" not in human_arg
    # Trailing "more" hint
    assert "45 more" in human_arg


# ---------- rejections ----------


def test_conversations_rejects_openclaw(with_openclaw_personal: Path, mocker: Any) -> None:
    """OpenClaw has its own session storage — must refuse cleanly.
    Critically: must NOT call into the container, since the schema
    differs and the query would either error or return garbage.
    """
    exec_spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    with pytest.raises(UserError, match="Hermes-only"):
        conversations_cmd.status("personal", config_path=with_openclaw_personal)
    exec_spy.assert_not_called()


def test_conversations_rejects_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        conversations_cmd.status("ghost", config_path=saved_config)


# ---------- failure modes ----------


def test_conversations_surfaces_container_down_as_user_error(
    with_coder: Path, mocker: Any
) -> None:
    """When the container isn't running, the SQLite probe returns
    'service is not running'. Translate to UserError so the CLI exits
    with code 1 (user-fixable) instead of 2 (runtime crash).
    """
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        return_value=MpResult(stdout="", stderr="service is not running", returncode=1),
    )
    with pytest.raises(UserError, match="not running"):
        conversations_cmd.status("coder", config_path=with_coder)


def test_conversations_surfaces_unparseable_output_as_runtime_failure(
    with_coder: Path, mocker: Any
) -> None:
    """Malformed query output (e.g. python3 stderr leaked into stdout)
    should bubble up as RuntimeFailure with a hint, not silently emit
    a corrupt payload.
    """
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok("not json at all\n"))
    with pytest.raises(RuntimeFailure, match="could not parse"):
        conversations_cmd.status("coder", config_path=with_coder)
