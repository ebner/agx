"""GH #6 tier 2: the --profile dimension across the Hermes surface.

A named profile is a full HERMES_HOME under ``/opt/data/profiles/<p>/``.
These tests prove that every command that grew a ``--profile`` flag
targets THAT home (config.yaml, state.db, memory store) and leaves the
default profile untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agx_cli import config as cfg_mod
from agx_cli import conversations_cmd, hermes_config, models_cmd, profile_cmd, secrets_cmd
from agx_cli.agents_cmd import create
from agx_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    PathsConfig,
    VmRuntimeConfig,
)
from agx_cli.errors import UserError
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
def with_coder(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> Path:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


def _seed_profile(cfg: AgentsConfig, profile: str, content: str = "model: ''\n") -> Path:
    """Materialize a profile home host-side (what `hermes profile create`
    would have produced inside the container)."""
    home = cfg.paths.host_data_dir / "coder" / ".data" / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(content, encoding="utf-8")
    return home


def _seed_default(cfg: AgentsConfig, content: str = "model: ''\n") -> Path:
    p = hermes_config.config_path(cfg, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------- name normalization + paths ----------


@pytest.mark.parametrize("raw", [None, "", "default", "Default", " DEFAULT "])
def test_normalize_profile_default_forms(raw: str | None) -> None:
    assert hermes_config.normalize_profile(raw) is None


@pytest.mark.parametrize("raw", ["work", "x-eng", "chief_of.staff2"])
def test_normalize_profile_valid_names(raw: str) -> None:
    assert hermes_config.normalize_profile(raw) == raw


@pytest.mark.parametrize("raw", ["../etc", "a/b", "-lead", ".hidden", "spa ce"])
def test_normalize_profile_rejects_path_escapes(raw: str) -> None:
    with pytest.raises(UserError, match="invalid profile"):
        hermes_config.normalize_profile(raw)


def test_profile_paths(cfg: AgentsConfig) -> None:
    assert hermes_config.profile_home() == "/opt/data"
    assert hermes_config.profile_home("work") == "/opt/data/profiles/work"
    assert hermes_config.container_config_path("work") == "/opt/data/profiles/work/config.yaml"
    assert hermes_config.config_path(cfg, "coder", "work") == (
        cfg.paths.host_data_dir / "coder" / ".data" / "profiles" / "work" / "config.yaml"
    )
    # Uppercase input is refused, not silently lowered into a different path.
    with pytest.raises(UserError):
        hermes_config.config_path(cfg, "coder", "Work/../../x")


# ---------- config read/write targeting ----------


def test_write_atomically_targets_profile_home(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    _seed_default(cfg, "model: 'untouched'\n")
    _seed_profile(cfg, "work")

    doc = hermes_config.read_doc(cfg, "coder", "work")
    doc["model"] = {"default": "m", "provider": "custom", "base_url": "http://x/v1"}
    hermes_config.write_atomically(cfg, "coder", doc, "work")

    prof = hermes_config.load(hermes_config.config_path(cfg, "coder", "work"))
    assert prof["model"]["default"] == "m"
    default = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert default["model"] == "untouched"


def test_write_atomically_profile_applies_dashboard_refusal(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    _seed_profile(cfg, "work")
    doc = hermes_config.read_doc(cfg, "coder", "work")
    doc["dashboard"] = {"address": "0.0.0.0"}
    with pytest.raises(UserError, match=r"refusing to set dashboard\.address"):
        hermes_config.write_atomically(cfg, "coder", doc, "work")


# ---------- secrets: passthrough lands in the profile config ----------


def test_secret_set_profile_edits_profile_passthrough_only(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    _seed_default(cfg, "terminal:\n  env_passthrough: []\n")
    _seed_profile(cfg, "work", "terminal:\n  env_passthrough: []\n")

    secrets_cmd.set_secret("coder", "MY_KEY", "v", profile="work", config_path=with_coder)

    prof = hermes_config.load(hermes_config.config_path(cfg, "coder", "work"))
    assert "MY_KEY" in prof["terminal"]["env_passthrough"]
    default = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert "MY_KEY" not in default["terminal"]["env_passthrough"]
    # The .env half is container-wide regardless of profile.
    env_file = cfg.paths.host_data_dir / "coder" / ".env"
    assert "MY_KEY=v" in env_file.read_text(encoding="utf-8")

    secrets_cmd.unset_secret("coder", "MY_KEY", profile="work", config_path=with_coder)
    prof = hermes_config.load(hermes_config.config_path(cfg, "coder", "work"))
    assert "MY_KEY" not in prof["terminal"]["env_passthrough"]


def test_secret_profile_refused_for_openclaw(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="claw", agent_type="openclaw", config_path=saved_config)
    with pytest.raises(UserError, match="Hermes-only"):
        secrets_cmd.set_secret("claw", "K", "v", profile="work", config_path=saved_config)


# ---------- conversations: probe runs against the profile home ----------


def test_conversations_status_passes_profile_home_env(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    payload = {
        "freshness_window_s": 3600,
        "total": 0,
        "open": 0,
        "ended": 0,
        "active": 0,
        "by_platform": {},
        "active_sessions": [],
    }
    execs = mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok(json.dumps(payload)))
    out = conversations_cmd.status("coder", profile="work", config_path=with_coder)
    argv = execs.call_args.args
    assert "H_HOME=/opt/data/profiles/work" in argv
    assert out["profile"] == "work"


# ---------- model use: profile config targeted ----------


def test_model_use_profile_writes_profile_config(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    _seed_default(cfg, "model: 'untouched'\n")
    _seed_profile(cfg, "work")
    models_cmd.use(
        "coder", engine="ollama", model="llama3.2:3b", profile="work", config_path=with_coder
    )
    prof = hermes_config.load(hermes_config.config_path(cfg, "coder", "work"))
    assert prof["model"]["default"] == "llama3.2:3b"
    default = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert default["model"] == "untouched"


# ---------- memory: per-profile store + state record ----------


def test_memory_holographic_profile_targets_profile_home(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    from agx_cli import memory as memory_mod

    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())
    _seed_default(cfg, "model: 'untouched'\n")
    _seed_profile(cfg, "work")

    memory_mod.enable("holographic", "coder", profile="work", config_path=with_coder)

    prof = hermes_config.load(hermes_config.config_path(cfg, "coder", "work"))
    assert prof["memory"]["provider"] == "holographic"
    assert (
        prof["plugins"]["hermes-memory-store"]["db_path"]
        == "/opt/data/profiles/work/memory_store.db"
    )
    default = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert "memory" not in default
    state_file = cfg.paths.host_data_dir / ".memory-state" / "coder@work.json"
    assert state_file.exists()
    # Default-profile state record untouched/absent.
    assert not (cfg.paths.host_data_dir / ".memory-state" / "coder.json").exists()

    memory_mod.disable("coder", profile="work", config_path=with_coder)
    assert not state_file.exists()


def test_memory_openviking_refuses_profile(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    from agx_cli import memory as memory_mod

    _seed_profile(cfg, "work")
    with pytest.raises(UserError, match="per-profile"):
        memory_mod.enable("openviking", "coder", profile="work", config_path=with_coder)


# ---------- doctor: profile dashboard-binding sweep ----------


def test_doctor_profile_dashboards_warns_on_non_loopback(cfg: AgentsConfig, mocker: Any) -> None:
    from agx_cli.ops import DoctorReport, _check_profile_dashboard_bindings

    probe = {
        "rows": [
            {"profile": "work", "address": "0.0.0.0"},
            {"profile": "default", "address": "127.0.0.1"},
        ]
    }
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok(json.dumps(probe)))
    report = DoctorReport()
    _check_profile_dashboard_bindings(cfg, "hermes-coder", "coder", report)
    [check] = report.checks
    assert check.status == "warn"
    assert "work='0.0.0.0'" in check.detail
    assert "default" not in check.detail


def test_doctor_profile_dashboards_quiet_when_loopback(cfg: AgentsConfig, mocker: Any) -> None:
    from agx_cli.ops import DoctorReport, _check_profile_dashboard_bindings

    probe = {"rows": [{"profile": "default", "address": "127.0.0.1"}]}
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok(json.dumps(probe)))
    report = DoctorReport()
    _check_profile_dashboard_bindings(cfg, "hermes-coder", "coder", report)
    assert report.checks == []


# ---------- profile list ----------


def test_profile_list_parses_probe(with_coder: Path, cfg: AgentsConfig, mocker: Any) -> None:
    probe = {
        "profiles": [
            {
                "profile": "default",
                "config": True,
                "state_db": True,
                "gateway_desired": "running",
                "gateway_s6": "up",
            },
            {
                "profile": "work",
                "config": True,
                "state_db": False,
                "gateway_desired": None,
                "gateway_s6": None,
            },
        ]
    }
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok(json.dumps(probe)))
    payload = profile_cmd.list_profiles("coder", config_path=with_coder)
    assert [r["profile"] for r in payload["profiles"]] == ["default", "work"]


def test_profile_list_refuses_openclaw(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="claw", agent_type="openclaw", config_path=saved_config)
    with pytest.raises(UserError, match="Hermes-only"):
        profile_cmd.list_profiles("claw", config_path=saved_config)


# ---------- codex-review regression tests (2026-08-27) ----------


def test_cli_model_use_reaches_backend_with_profile(mocker: Any) -> None:
    """P1 regression: the --profile click option fell off model_use while
    the callback signature gained the parameter — EVERY `agx model
    use` invocation TypeError'd before reaching the backend. Invoking
    through the real command tree catches decorator/signature drift the
    direct-call tests can't see."""
    from tests.unit.test_cli import invoke

    use = mocker.patch("agx_cli.models_cmd.use")
    result = invoke(
        mocker,
        ["model", "use", "coder", "--engine", "ollama", "--model", "m", "--profile", "work"],
    )
    assert result.exit_code == 0, result.output
    assert use.call_args.kwargs["profile"] == "work"

    use.reset_mock()
    result = invoke(mocker, ["model", "use", "coder", "--engine", "ollama", "--model", "m"])
    assert result.exit_code == 0, result.output
    assert use.call_args.kwargs["profile"] is None


def test_cli_prune_honors_root_yes(mocker: Any) -> None:
    """P2 regression: `agx --yes prune` prompted anyway (callback
    ignored the root context), hanging automation."""
    from tests.unit.test_cli import invoke

    prune = mocker.patch("agx_cli.prune_cmd.prune")
    result = invoke(mocker, ["--yes", "prune"])
    assert result.exit_code == 0, result.output
    assert prune.call_args.kwargs["yes"] is True


def test_conversation_scripts_open_sqlite_read_only() -> None:
    """P1 regression: sqlite3.connect on a bare path CREATES a missing
    state.db — as container root, breaking Hermes' own init for fresh
    profiles and violating the command's read-only contract."""
    assert "mode=ro" in conversations_cmd._QUERY_SCRIPT
    assert "mode=ro" in conversations_cmd._EXPORT_SCRIPT


def test_holographic_profile_overrides_cloned_default_db_path(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """P1 regression: a profile created with `--clone default` inherits
    the DEFAULT profile's db_path; setdefault kept it, silently sharing
    one memory DB across profiles."""
    from agx_cli import memory as memory_mod

    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())
    _seed_profile(
        cfg,
        "work",
        "memory:\n  provider: holographic\n"
        "plugins:\n  hermes-memory-store:\n    db_path: /opt/data/memory_store.db\n",
    )

    memory_mod.enable("holographic", "coder", profile="work", config_path=with_coder)

    prof = hermes_config.load(hermes_config.config_path(cfg, "coder", "work"))
    assert (
        prof["plugins"]["hermes-memory-store"]["db_path"]
        == "/opt/data/profiles/work/memory_store.db"
    )


def test_secret_set_profile_restarts_gateway(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """P2 regression: a profile-only passthrough edit changes config.yaml
    but not compose inputs — `up` no-ops and the running gateway kept
    filtering the var until an unrelated restart."""
    restart = mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())
    _seed_default(cfg, "terminal:\n  env_passthrough: []\n")
    _seed_profile(cfg, "work", "terminal:\n  env_passthrough: []\n")

    secrets_cmd.set_secret("coder", "K1", "v", profile="work", config_path=with_coder)
    restart.assert_called_once()

    restart.reset_mock()
    secrets_cmd.set_secret("coder", "K2", "v", config_path=with_coder)
    restart.assert_not_called()  # default-profile sets flow through env_file recreate


def test_clear_sweeps_every_profile_passthrough(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """P2 regression: clear wiped the .env key inventory but swept only
    one profile's passthrough list — remaining profiles could never be
    cleaned and would re-expose a future value under the same key."""
    _seed_default(cfg, "terminal:\n  env_passthrough: [MY_KEY]\n")
    _seed_profile(cfg, "work", "terminal:\n  env_passthrough: [MY_KEY]\n")
    (cfg.paths.host_data_dir / "coder" / ".env").write_text("MY_KEY=v\n", encoding="utf-8")
    mocker.patch("agx_cli.secrets_cmd._list_profile_names", return_value=["work"])

    secrets_cmd.clear_secrets("coder", yes=True, config_path=with_coder)

    default = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    prof = hermes_config.load(hermes_config.config_path(cfg, "coder", "work"))
    assert "MY_KEY" not in default["terminal"]["env_passthrough"]
    assert "MY_KEY" not in prof["terminal"]["env_passthrough"]


def test_external_ollama_api_key_env_resolution(tmp_path: Path) -> None:
    """P2 regression: authenticated external endpoints ([ollama].api_key_env)
    got the literal dummy key — every Hermes request would 401."""
    from agx_cli.config import OllamaConfig
    from agx_cli.models_cmd import _external_ollama_api_key

    data_dir = tmp_path / "d"
    data_dir.mkdir()
    c = AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock", ubuntu_uid=1000, ubuntu_gid=1000
        ),
        ollama=OllamaConfig(url="http://10.0.0.1:11434", api_key_env="OLLAMA_API_KEY"),
    )
    (data_dir / ".env").write_text("OLLAMA_API_KEY=sk-real\n", encoding="utf-8")
    assert _external_ollama_api_key(c) == "sk-real"

    (data_dir / ".env").write_text("", encoding="utf-8")
    with pytest.raises(UserError, match="api_key_env"):
        _external_ollama_api_key(c)

    c.ollama.api_key_env = ""
    assert _external_ollama_api_key(c) == "ollama"
