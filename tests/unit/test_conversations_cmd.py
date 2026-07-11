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


# ---------- export ----------


def test_export_writes_one_markdown_per_session(
    with_coder: Path, tmp_path: Path, mocker: Any
) -> None:
    """The canonical case: a couple of sessions with a few messages each.
    Verify file naming (``<session_id>.md``), metadata header, and that
    each message lands in the output with its role + timestamp.
    """
    canned = {
        "sessions": [
            {
                "id": "20260515_120000_abc",
                "source": "whatsapp",
                "title": "Birthday plan",
                "started_at": 1747310400.0,
                "ended_at": None,
                "message_count": 2,
                "input_tokens": 200,
                "output_tokens": 100,
                "model": "gpt-5.5",
                "messages": [
                    {
                        "role": "user",
                        "content": "Where should we host?",
                        "timestamp": 1747310401.0,
                        "tool_name": None,
                        "tool_call_id": None,
                    },
                    {
                        "role": "assistant",
                        "content": "Let me think about indoor vs outdoor venues.",
                        "timestamp": 1747310410.0,
                        "tool_name": None,
                        "tool_call_id": None,
                    },
                ],
            },
            {
                "id": "20260515_130000_def",
                "source": "cli",
                "title": "(no title)",
                "started_at": 1747314000.0,
                "ended_at": 1747314600.0,
                "message_count": 1,
                "input_tokens": 50,
                "output_tokens": 20,
                "model": "gpt-5.5",
                "messages": [
                    {
                        "role": "user",
                        "content": "hello",
                        "timestamp": 1747314001.0,
                        "tool_name": None,
                        "tool_call_id": None,
                    },
                ],
            },
        ]
    }
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(json.dumps(canned)))

    out_dir = tmp_path / "transcripts"
    payload = conversations_cmd.export("coder", to_dir=out_dir, config_path=with_coder)

    assert payload["sessions_written"] == 2
    a = (out_dir / "20260515_120000_abc.md").read_text(encoding="utf-8")
    b = (out_dir / "20260515_130000_def.md").read_text(encoding="utf-8")
    assert "# Birthday plan" in a
    assert "Where should we host?" in a
    assert "Let me think about indoor vs outdoor" in a
    assert "Session ID" in a
    assert "**Model**: gpt-5.5" in a
    # Second session — ended, single message
    assert "Ended" in b and "open" not in b.split("Ended")[1].split("\n")[0]


def test_export_creates_to_dir_if_missing(with_coder: Path, tmp_path: Path, mocker: Any) -> None:
    """Don't make users mkdir first — just create the directory."""
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        return_value=_ok(json.dumps({"sessions": []})),
    )
    target = tmp_path / "deep" / "nested" / "transcripts"
    assert not target.exists()
    conversations_cmd.export("coder", to_dir=target, config_path=with_coder)
    assert target.is_dir()


def test_export_is_idempotent(with_coder: Path, tmp_path: Path, mocker: Any) -> None:
    """Running export twice with the same data overwrites the same files
    rather than erroring or appending — important for cron-driven
    archival.
    """
    canned = {
        "sessions": [
            {
                "id": "s1",
                "source": "cli",
                "title": "first",
                "started_at": 1.0,
                "ended_at": None,
                "message_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "model": None,
                "messages": [],
            },
        ]
    }
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(json.dumps(canned)))
    target = tmp_path / "out"
    conversations_cmd.export("coder", to_dir=target, config_path=with_coder)
    first = (target / "s1.md").read_text(encoding="utf-8")
    conversations_cmd.export("coder", to_dir=target, config_path=with_coder)
    second = (target / "s1.md").read_text(encoding="utf-8")
    assert first == second


def _minimal_session(sid: str) -> dict[str, Any]:
    return {
        "id": sid,
        "source": "cli",
        "title": "t",
        "started_at": 1.0,
        "ended_at": None,
        "message_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "model": None,
        "messages": [],
    }


def test_export_skips_hostile_session_ids_but_continues(
    with_coder: Path, tmp_path: Path, mocker: Any
) -> None:
    """Session ids come from the agent-writable state.db. An absolute id
    REPLACES to_dir under pathlib join semantics; `..` escapes it; a
    leading dot hides the file. All three must be skipped with a warning
    — NOT written, and NOT fatal to the rest of the export (it's
    advertised cron-safe)."""
    abs_escape = str(tmp_path / "abs-escape")
    canned = {
        "sessions": [
            _minimal_session(abs_escape),
            _minimal_session("../escape"),
            _minimal_session(".hidden"),
            _minimal_session("good_id-1.2"),
        ]
    }
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(json.dumps(canned)))
    warn_spy = mocker.spy(conversations_cmd.ui, "warn")

    out_dir = tmp_path / "transcripts"
    payload = conversations_cmd.export("coder", to_dir=out_dir, config_path=with_coder)

    assert payload["sessions_written"] == 1
    assert payload["files"] == ["good_id-1.2.md"]
    assert sorted(payload["skipped_ids"]) == sorted([abs_escape, "../escape", ".hidden"])
    # Only the sane file landed, and only inside the target dir.
    assert {p.name for p in out_dir.iterdir()} == {"good_id-1.2.md"}
    assert not (tmp_path / "escape.md").exists()
    assert not (tmp_path / "abs-escape.md").exists()
    assert not (out_dir / ".hidden.md").exists()
    assert warn_spy.call_count == 3


def test_export_without_hostile_ids_omits_skipped_key(
    with_coder: Path, tmp_path: Path, mocker: Any
) -> None:
    canned = {"sessions": [_minimal_session("plain")]}
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(json.dumps(canned)))
    payload = conversations_cmd.export("coder", to_dir=tmp_path / "out", config_path=with_coder)
    assert "skipped_ids" not in payload
    assert payload["sessions_written"] == 1


def test_export_rejects_openclaw(with_openclaw_personal: Path, tmp_path: Path, mocker: Any) -> None:
    exec_spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    with pytest.raises(UserError, match="Hermes-only"):
        conversations_cmd.export(
            "personal", to_dir=tmp_path / "out", config_path=with_openclaw_personal
        )
    exec_spy.assert_not_called()
