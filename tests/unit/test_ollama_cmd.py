"""ollama_cmd: configure, status, clear, .env secret round-trip."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents_cli import config as cfg_mod
from agents_cli import ollama_cmd
from agents_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    OllamaConfig,
    PathsConfig,
    VmRuntimeConfig,
)
from agents_cli.errors import UserError
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail(stderr: str = "could not resolve host") -> MpResult:
    return MpResult(stdout="", stderr=stderr, returncode=6)


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


# ---------- URL validation (config schema) ----------


def test_localhost_url_refused_by_schema() -> None:
    with pytest.raises(ValueError, match="not reachable from inside the VM"):
        OllamaConfig(url="http://localhost:11434")


def test_127_0_0_1_url_refused_by_schema() -> None:
    with pytest.raises(ValueError, match="not reachable from inside the VM"):
        OllamaConfig(url="http://127.0.0.1:11434")


def test_0_0_0_0_url_refused_by_schema() -> None:
    with pytest.raises(ValueError, match="not reachable"):
        OllamaConfig(url="http://0.0.0.0:11434")


def test_url_must_be_http_or_https() -> None:
    with pytest.raises(ValueError, match="http"):
        OllamaConfig(url="ftp://example/")


def test_valid_url_accepted() -> None:
    OllamaConfig(url="http://10.20.30.1:11434")
    OllamaConfig(url="https://api.example.com/v1")


# ---------- configure (persistence + probe) ----------


def test_configure_writes_block_to_config(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])
    ollama_cmd.configure(
        url="http://10.20.30.1:11434",
        embed_model="nomic-embed-text",
        llm_model="qwen2.5:7b-instruct",
        config_path=saved_config,
    )
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.ollama.url == "http://10.20.30.1:11434"
    assert reloaded.ollama.embed_model == "nomic-embed-text"
    assert reloaded.ollama.llm_model == "qwen2.5:7b-instruct"


def test_configure_with_api_key_writes_to_env_only(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])
    ollama_cmd.configure(
        url="https://groq.example/v1",
        api_key="gsk_secret",
        config_path=saved_config,
    )

    # TOML records the env name, not the secret.
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.ollama.api_key_env == "OLLAMA_API_KEY"
    toml_text = saved_config.read_text(encoding="utf-8")
    assert "gsk_secret" not in toml_text

    # .env has the secret.
    env_text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY=gsk_secret" in env_text


def test_configure_rejects_bad_env_name(saved_config: Path) -> None:
    with pytest.raises(UserError, match="invalid --api-key-env"):
        ollama_cmd.configure(
            url="http://10.20.30.1:11434",
            api_key="x",
            api_key_env="not valid name",
            config_path=saved_config,
        )


def test_configure_clears_api_key_on_empty_string(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])
    ollama_cmd.configure(url="http://10.20.30.1:11434", api_key="initial", config_path=saved_config)
    env_text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY=initial" in env_text

    ollama_cmd.configure(url="http://10.20.30.1:11434", api_key="", config_path=saved_config)
    env_text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY" not in env_text


def test_clear_removes_config_and_env_secret(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])
    ollama_cmd.configure(url="http://10.20.30.1:11434", api_key="x", config_path=saved_config)
    ollama_cmd.clear(yes=True, config_path=saved_config)

    reloaded = cfg_mod.load(saved_config)
    assert reloaded.ollama.url == ""
    assert reloaded.ollama.api_key_env == ""
    env_text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY" not in env_text


def test_clear_requires_confirmation(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    """clear wipes the stored API key + [ollama] block — destructive, so
    it must confirm (default False) like the sibling wipe commands."""
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])
    ollama_cmd.configure(url="http://10.20.30.1:11434", api_key="x", config_path=saved_config)
    confirm = mocker.patch("agents_cli.ui.confirm", return_value=False)

    with pytest.raises(UserError, match="aborted"):
        ollama_cmd.clear(config_path=saved_config)

    confirm.assert_called_once()
    assert confirm.call_args.kwargs.get("default") is False
    # Nothing was wiped.
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.ollama.url == "http://10.20.30.1:11434"
    env_text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY=x" in env_text


def test_clear_proceeds_when_confirmed(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])
    ollama_cmd.configure(url="http://10.20.30.1:11434", api_key="x", config_path=saved_config)
    mocker.patch("agents_cli.ui.confirm", return_value=True)

    ollama_cmd.clear(config_path=saved_config)

    reloaded = cfg_mod.load(saved_config)
    assert reloaded.ollama.url == ""


def test_status_says_not_configured(saved_config: Path, mocker: Any) -> None:
    info = ollama_cmd.status(config_path=saved_config)
    assert info == {"configured": False}


def test_status_includes_probes_when_configured(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, saved_config)

    def fake_probe(cfg_: Any, *, agent_filter: Any) -> list[dict[str, str]]:
        return [{"from": "vm", "status": "ok", "detail": "/api/tags reachable"}]

    mocker.patch("agents_cli.ollama_cmd._probe_reachability", side_effect=fake_probe)

    info = ollama_cmd.status(config_path=saved_config)
    assert info["configured"] is True
    assert info["url"] == "http://10.20.30.1:11434"
    assert info["probes"][0]["status"] == "ok"


# ---------- probe behavior ----------


def test_probe_skipped_when_vm_runtime_unpopulated(saved_config: Path, mocker: Any) -> None:
    """vm_runtime fields blank → probe reports 'skipped', not 'fail'."""
    cfg = cfg_mod.load(saved_config)
    cfg.vm_runtime = VmRuntimeConfig()  # empty
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, saved_config)

    probes = ollama_cmd._probe_reachability(cfg, agent_filter=None)
    assert len(probes) == 1
    assert probes[0]["status"] == "skipped"


def test_probe_ok_when_curl_succeeds(saved_config: Path, mocker: Any) -> None:
    cfg = cfg_mod.load(saved_config)
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, saved_config)
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    mocker.patch("agents_cli.compose.read_compose_doc", return_value={"services": {}})

    probes = ollama_cmd._probe_reachability(cfg, agent_filter=None)
    assert any(p["status"] == "ok" for p in probes)


def test_probe_fail_on_curl_error(saved_config: Path, mocker: Any) -> None:
    cfg = cfg_mod.load(saved_config)
    cfg.ollama = OllamaConfig(url="http://10.20.30.1:11434")
    cfg_mod.save(cfg, saved_config)
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_fail())
    mocker.patch("agents_cli.compose.read_compose_doc", return_value={"services": {}})

    probes = ollama_cmd._probe_reachability(cfg, agent_filter=None)
    assert any(p["status"] == "fail" for p in probes)


def test_probe_uses_bearer_when_api_key_env_present(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    cfg.ollama = OllamaConfig(url="https://example/v1", api_key_env="OLLAMA_API_KEY")
    cfg_mod.save(cfg, saved_config)
    env_path = cfg.paths.host_data_dir / ".env"
    env_path.write_text(env_path.read_text() + "OLLAMA_API_KEY=gsk_test\n", encoding="utf-8")

    spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    ollama_cmd._probe_from_vm(cfg_mod.load(saved_config))

    args = spy.call_args.args
    assert "Authorization: Bearer gsk_test" in " ".join(args)


# ---------- .env round-trip ----------


def test_write_env_secret_preserves_unrelated_keys(cfg: AgentsConfig) -> None:
    """Unrelated entries survive the read-modify-write. (Comments are
    not preserved — the project .env goes through the same dict-based
    sidecar writer `firecrawl up` uses, which has always re-rendered
    the whole file.)
    """
    env_path = cfg.paths.host_data_dir / ".env"
    env_path.write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    ollama_cmd._write_env_secret(cfg, "OLLAMA_API_KEY", "x")
    txt = env_path.read_text(encoding="utf-8")
    assert "HERMES_UID=1000" in txt
    assert "HERMES_GID=1000" in txt
    assert "OLLAMA_API_KEY=x" in txt


def test_write_env_secret_replaces_existing_value(cfg: AgentsConfig) -> None:
    env_path = cfg.paths.host_data_dir / ".env"
    env_path.write_text("OLLAMA_API_KEY=old\n", encoding="utf-8")
    ollama_cmd._write_env_secret(cfg, "OLLAMA_API_KEY", "new")
    txt = env_path.read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY=new" in txt
    assert "OLLAMA_API_KEY=old" not in txt


def test_write_env_secret_empty_value_removes_line(cfg: AgentsConfig) -> None:
    env_path = cfg.paths.host_data_dir / ".env"
    env_path.write_text("OLLAMA_API_KEY=keep\nOTHER=x\n", encoding="utf-8")
    ollama_cmd._write_env_secret(cfg, "OLLAMA_API_KEY", "")
    txt = env_path.read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY" not in txt
    assert "OTHER=x" in txt


# ---------- locking + mode-awareness of the .env mutation ----------


def test_configure_holds_mutation_lock_around_env_write(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """The project-.env read-modify-write races `firecrawl up`'s locked
    writes (lost keys) unless configure serializes on the same
    <data-dir>/.agents.lock (rule #7). Assert the flock is taken and
    the write happens while it's held.
    """
    import contextlib

    events: list[str] = []

    @contextlib.contextmanager
    def fake_lock(path: Path) -> Any:
        assert path == cfg.paths.host_data_dir
        events.append("lock:enter")
        yield
        events.append("lock:exit")

    mocker.patch("agents_cli.compose.acquire_lock", side_effect=fake_lock)
    mocker.patch(
        "agents_cli.sidecar.write_project_env",
        side_effect=lambda *a, **k: events.append("env:write"),
    )
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])

    ollama_cmd.configure(
        url="http://10.20.30.1:11434", api_key="gsk_secret", config_path=saved_config
    )

    assert events == ["lock:enter", "env:write", "lock:exit"]


def test_clear_holds_mutation_lock_around_env_write(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    import contextlib

    events: list[str] = []

    @contextlib.contextmanager
    def fake_lock(path: Path) -> Any:
        events.append("lock:enter")
        yield
        events.append("lock:exit")

    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])
    ollama_cmd.configure(
        url="http://10.20.30.1:11434", api_key="gsk_secret", config_path=saved_config
    )

    mocker.patch("agents_cli.compose.acquire_lock", side_effect=fake_lock)
    mocker.patch(
        "agents_cli.sidecar.write_project_env",
        side_effect=lambda *a, **k: events.append("env:write"),
    )
    ollama_cmd.clear(yes=True, config_path=saved_config)

    assert events == ["lock:enter", "env:write", "lock:exit"]


def test_configure_writes_api_key_to_vm_env_in_data_dir_in_vm_mode(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """Regression: in --data-dir-in-vm mode the containers read
    <vm_agents_root>/.env inside the VM. A host-only write would leave
    the probes passing with auth the containers never see.
    """
    from tests.conftest import install_vm_fs_simulation

    cfg.paths.data_dir_in_vm = True
    cfg_mod.save(cfg, saved_config)
    mocks = install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.ollama_cmd._probe_reachability", return_value=[])

    ollama_cmd.configure(
        url="http://10.20.30.1:11434", api_key="gsk_secret", config_path=saved_config
    )

    # The write went through multipass transfer to the VM-side path
    # (mapped back to the host file by the simulator).
    assert mocks["transfer_to_vm"].called
    env_text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY=gsk_secret" in env_text
