"""models_cmd: pull / list / rm / use."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents_cli import config as cfg_mod
from agents_cli import hermes_config, models_cmd
from agents_cli.agents_cmd import create
from agents_cli.compose import OLLAMA_SERVICE_NAME
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


def _fail(stderr: str = "boom") -> MpResult:
    return MpResult(stdout="", stderr=stderr, returncode=1)


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
def with_openclaw(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> Path:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    return saved_config


# ---------- pull ----------


def test_pull_ollama_passes_through_to_ollama_cli(with_coder: Path, mocker: Any) -> None:
    """`agents model pull ollama X` should exec `ollama pull X` inside
    the ollama container. Ollama accepts both registry refs and
    `hf.co/<repo>:<quant>` natively — we don't try to parse.
    """
    captured: list[tuple[str, ...]] = []

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "ps" in cmd:
            import json as _j

            return _ok(_j.dumps([{"Service": OLLAMA_SERVICE_NAME, "State": "running"}]))
        if "pull" in cmd and "ollama" in cmd:
            captured.append(cmd)
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=True)

    models_cmd.pull("ollama", "hf.co/bartowski/Llama-3.2-3B:Q4_K_M", config_path=with_coder)
    assert any("hf.co/bartowski/Llama-3.2-3B:Q4_K_M" in c for c in captured)


def test_pull_ollama_refuses_when_ollama_not_running(with_coder: Path, mocker: Any) -> None:
    """No useful error from `docker exec` against a stopped container —
    surface a clean UserError pointing at the fix.
    """
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=False)
    with pytest.raises(UserError, match="not running"):
        models_cmd.pull("ollama", "llama3.2:3b", config_path=with_coder)


def test_pull_llamacpp_requires_repo_filename_form(with_coder: Path, mocker: Any) -> None:
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=True)
    with pytest.raises(UserError, match="<hf-repo>:<filename>"):
        models_cmd.pull("llama-cpp", "just-a-name", config_path=with_coder)


def test_pull_rejects_unknown_engine(with_coder: Path) -> None:
    with pytest.raises(UserError, match="unknown engine"):
        models_cmd.pull("vllm", "anything", config_path=with_coder)


def _llamacpp_pull_runner(
    cfg: AgentsConfig, captured: list[tuple[str, ...]], *, mount_visible: bool
) -> Any:
    """Fake mp.exec_in_vm for llama-cpp pulls. When ``mount_visible``,
    the sentinel touch inside the container materializes host-side —
    simulating a container whose /llama-cpp bind actually reaches
    <data-dir>/models/llama-cpp/. Otherwise the write stays on the
    (simulated) overlay and the host never sees it.
    """
    sentinel = cfg.paths.host_data_dir / "models" / "llama-cpp" / ".toolkit-mount-probe"

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        captured.append(cmd)
        joined = " ".join(cmd)
        if "touch /llama-cpp/.toolkit-mount-probe" in joined and mount_visible:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        if "rm" in cmd and "/llama-cpp/.toolkit-mount-probe" in joined:
            sentinel.unlink(missing_ok=True)
        return _ok()

    return runner


def test_pull_llamacpp_downloads_into_bind_mounted_dir(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """The hf_hub_download MUST target /llama-cpp (the ollama fragment's
    bind to <data-dir>/models/llama-cpp/). The historical
    '/root/.ollama/../llama-cpp' resolved to /root/llama-cpp on the
    container OVERLAY — multi-GB download invisible to `model list` and
    wiped on recreate.
    """
    captured: list[tuple[str, ...]] = []
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_llamacpp_pull_runner(cfg, captured, mount_visible=True),
    )
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=True)

    payload = models_cmd.pull(
        "llama-cpp",
        "bartowski/Llama-3.2-3B-Instruct-GGUF:Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        config_path=saved_config,
    )
    assert payload["pulled"] is True

    download = next(c for c in captured if any("hf_hub_download" in a for a in c))
    snippet = next(a for a in download if "hf_hub_download" in a)
    assert "local_dir='/llama-cpp'" in snippet
    assert "/root/.ollama" not in snippet


def test_pull_llamacpp_refuses_when_bind_not_host_visible(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """An ollama container created from the pre-bind fragment only gains
    the /llama-cpp volume on recreate. The pull must fail loudly with
    the recreate hint BEFORE downloading, not let the multi-GB pull land
    on the overlay.
    """
    captured: list[tuple[str, ...]] = []
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_llamacpp_pull_runner(cfg, captured, mount_visible=False),
    )
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=True)

    with pytest.raises(UserError, match="agents inference down ollama && agents inference up"):
        models_cmd.pull("llama-cpp", "some/repo:file.gguf", config_path=saved_config)
    # And the download itself must never have started.
    assert not any(any("hf_hub_download" in a for a in c) for c in captured)


def test_pull_llamacpp_apt_bootstrap_is_guarded(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """`command -v python3 || apt-get update && apt-get install ...`
    parses as (A||B)&&C — apt-get install would run on EVERY pull and
    hard-fail when apt lists are stale/offline. The apt pair must be
    brace-grouped behind the python3 probe.
    """
    captured: list[tuple[str, ...]] = []
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_llamacpp_pull_runner(cfg, captured, mount_visible=True),
    )
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=True)

    models_cmd.pull("llama-cpp", "some/repo:file.gguf", config_path=saved_config)

    bootstrap = next(a for c in captured for a in c if "apt-get" in a)
    assert (
        "command -v python3 >/dev/null 2>&1 || { apt-get -qq update && "
        "apt-get -qq install -y python3 python3-pip; }" in bootstrap
    )


# ---------- use (wiring) ----------


def test_use_writes_structured_model_block(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """`agents model use coder --engine ollama --model llama3.2:3b`
    rewrites the Hermes config.yaml's `model:` block to point at the
    in-VM Ollama service. The structured (dict) form matches what the
    `hermes model` wizard produces, so doctor's model_set check stays
    green.
    """
    # Seed the agent config with a placeholder so write_atomically
    # has something to round-trip.
    p = hermes_config.config_path(cfg, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("model: ''\nproviders: {}\n", encoding="utf-8")
    # Stub the in-container validate that write_atomically runs.
    # Don't re-patch exec_in_vm — the simulator installed by `with_coder`
    # already handles compose-exec writes (write_atomically routes
    # through `docker compose exec cat > ...`) and lands them at the
    # host-side config_path, which read_doc then sees.

    payload = models_cmd.use(
        "coder", engine="ollama", model="llama3.2:3b", config_path=with_coder
    )

    doc = hermes_config.read_doc(cfg, "coder")
    assert doc["model"]["default"] == "llama3.2:3b"
    assert doc["model"]["provider"] == "custom"
    assert doc["model"]["base_url"] == f"http://{OLLAMA_SERVICE_NAME}:11434/v1"
    # api_key must be present and non-empty — OpenAI client libs refuse
    # to send requests without one even when the server ignores it.
    assert doc["model"]["api_key"]
    assert payload["base_url"] == f"http://{OLLAMA_SERVICE_NAME}:11434/v1"


def test_use_with_aux_all_routes_compression_and_vision(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """`--aux all` routes both compression AND vision to the same
    local endpoint, but doesn't touch any other auxiliary task.
    """
    p = hermes_config.config_path(cfg, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "model: ''\nproviders: {}\n"
        "auxiliary:\n"
        "  compression: {provider: openrouter, model: gemini}\n"
        "  vision: {provider: auto}\n"
        "  curator: {provider: openai}\n",
        encoding="utf-8",
    )
    # Don't re-patch exec_in_vm — the simulator installed by `with_coder`
    # already handles compose-exec writes (write_atomically routes
    # through `docker compose exec cat > ...`) and lands them at the
    # host-side config_path, which read_doc then sees.

    models_cmd.use(
        "coder",
        engine="ollama",
        model="llama3.2:3b",
        aux="all",
        config_path=with_coder,
    )

    doc = hermes_config.read_doc(cfg, "coder")
    aux = doc["auxiliary"]
    # Both compression and vision now point at the local endpoint
    assert aux["compression"]["provider"] == "custom"
    assert aux["compression"]["model"] == "llama3.2:3b"
    assert aux["vision"]["provider"] == "custom"
    assert aux["vision"]["model"] == "llama3.2:3b"
    # curator was untouched
    assert aux["curator"]["provider"] == "openai"


def test_use_with_aux_compression_only_leaves_vision_alone(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """`--aux compression` (singular) must NOT touch vision, even
    though both are common targets. Otherwise users with a separate
    vision-capable cloud model would silently lose it.
    """
    p = hermes_config.config_path(cfg, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "model: ''\nproviders: {}\n"
        "auxiliary:\n"
        "  compression: {provider: openrouter}\n"
        "  vision: {provider: openrouter, model: gemini-vision}\n",
        encoding="utf-8",
    )
    # Don't re-patch exec_in_vm — the simulator installed by `with_coder`
    # already handles compose-exec writes (write_atomically routes
    # through `docker compose exec cat > ...`) and lands them at the
    # host-side config_path, which read_doc then sees.

    models_cmd.use(
        "coder",
        engine="ollama",
        model="llama3.2:3b",
        aux="compression",
        config_path=with_coder,
    )

    doc = hermes_config.read_doc(cfg, "coder")
    assert doc["auxiliary"]["compression"]["provider"] == "custom"
    # vision untouched
    assert doc["auxiliary"]["vision"]["provider"] == "openrouter"
    assert doc["auxiliary"]["vision"]["model"] == "gemini-vision"


def _configure_external_ollama(config_path: Path) -> None:
    from agents_cli import config as cfg_mod

    c = cfg_mod.load(config_path)
    c.ollama.url = "http://192.168.122.1:11434"
    cfg_mod.save(c, config_path)


def _seed_agent_config(cfg: AgentsConfig) -> None:
    p = hermes_config.config_path(cfg, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("model: ''\n", encoding="utf-8")


def test_use_external_ollama_requires_configured_url(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    with pytest.raises(UserError, match="ollama configure"):
        models_cmd.use(
            "coder",
            engine="external-ollama",
            model="qwen3.8:27b",
            context_length=131072,
            config_path=with_coder,
        )


def test_use_external_ollama_requires_context_length(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """The server-side OLLAMA_CONTEXT_LENGTH cap can't be probed and the
    /api/show native window over-reports — refusing without an explicit
    value prevents the silent-truncation class."""
    _configure_external_ollama(with_coder)
    with pytest.raises(UserError, match="context-length"):
        models_cmd.use(
            "coder",
            engine="external-ollama",
            model="qwen3.8:27b",
            config_path=with_coder,
        )


def test_use_external_ollama_writes_model_block_and_provider_entry(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    _configure_external_ollama(with_coder)
    _seed_agent_config(cfg)

    models_cmd.use(
        "coder",
        engine="external-ollama",
        model="qwen3.8:27b",
        context_length=131072,
        config_path=with_coder,
    )

    doc = hermes_config.read_doc(cfg, "coder")
    assert doc["model"] == {
        "default": "qwen3.8:27b",
        "provider": "custom",
        "base_url": "http://192.168.122.1:11434/v1",
        "api_key": "ollama",
        "context_length": 131072,
    }
    [entry] = doc["custom_providers"]
    assert entry["base_url"] == "http://192.168.122.1:11434/v1"
    assert entry["models"]["qwen3.8:27b"]["context_length"] == 131072


def test_use_external_ollama_upserts_into_existing_provider_entry(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """A pre-existing entry for the same endpoint (whatever its name) is
    reused — other models' metadata survives, only this model's
    context_length is set."""
    _configure_external_ollama(with_coder)
    p = hermes_config.config_path(cfg, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "model: ''\n"
        "custom_providers:\n"
        "  - name: local-ollama\n"
        "    base_url: http://192.168.122.1:11434/v1\n"
        "    api_key: ollama\n"
        "    models:\n"
        '      "qwen3.8:27b": {context_length: 131072}\n',
        encoding="utf-8",
    )

    models_cmd.use(
        "coder",
        engine="external-ollama",
        model="gemma4:26b",
        context_length=65536,
        config_path=with_coder,
    )

    doc = hermes_config.read_doc(cfg, "coder")
    [entry] = doc["custom_providers"]
    assert entry["name"] == "local-ollama"  # reused, not duplicated
    assert entry["models"]["qwen3.8:27b"]["context_length"] == 131072  # untouched
    assert entry["models"]["gemma4:26b"]["context_length"] == 65536


def test_use_in_vm_engine_with_context_length_writes_provider_entry(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    _seed_agent_config(cfg)
    models_cmd.use(
        "coder",
        engine="ollama",
        model="llama3.2:3b",
        context_length=32768,
        config_path=with_coder,
    )
    doc = hermes_config.read_doc(cfg, "coder")
    assert doc["model"]["context_length"] == 32768
    [entry] = doc["custom_providers"]
    assert entry["base_url"] == f"http://{OLLAMA_SERVICE_NAME}:11434/v1"
    assert entry["models"]["llama3.2:3b"]["context_length"] == 32768


def test_use_without_context_length_writes_no_provider_entry(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    _seed_agent_config(cfg)
    models_cmd.use("coder", engine="ollama", model="llama3.2:3b", config_path=with_coder)
    doc = hermes_config.read_doc(cfg, "coder")
    assert "context_length" not in doc["model"]
    assert "custom_providers" not in doc


def test_use_refuses_openclaw_with_helpful_hint(
    with_openclaw: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """OpenClaw owns its config schema — toolkit refuses but spells
    out the URL the user needs to paste into `openclaw configure`,
    so the rejection is actionable, not just blocking.
    """
    exec_spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    with pytest.raises(UserError, match="Hermes-only") as exc:
        models_cmd.use(
            "personal",
            engine="ollama",
            model="llama3.2:3b",
            config_path=with_openclaw,
        )
    # The hint must include the exact base_url the user needs.
    assert f"http://{OLLAMA_SERVICE_NAME}:11434/v1" in str(exc.value)
    # Critically: must NOT have touched the container.
    exec_spy.assert_not_called()


def test_use_for_llamacpp_engine_uses_per_model_dns_name(
    with_coder: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """For llama-cpp the URL encodes the model slug (one service per
    model). `agents model use coder --engine llama-cpp --model
    Llama-3.2-3B-Q4_K_M` should resolve to
    `http://llama-cpp-llama-3-2-3b-q4-k-m:8080/v1`.
    """
    p = hermes_config.config_path(cfg, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("model: ''\nproviders: {}\n", encoding="utf-8")
    # Don't re-patch exec_in_vm — the simulator installed by `with_coder`
    # already handles compose-exec writes (write_atomically routes
    # through `docker compose exec cat > ...`) and lands them at the
    # host-side config_path, which read_doc then sees.

    payload = models_cmd.use(
        "coder",
        engine="llama-cpp",
        model="Llama-3.2-3B-Q4_K_M",
        config_path=with_coder,
    )

    assert payload["base_url"] == "http://llama-cpp-llama-3-2-3b-q4-k-m:8080/v1"


# ---------- slugify ----------


def test_slugify_lowercases_and_replaces_unsafe_chars() -> None:
    assert models_cmd._slugify("Llama-3.2-3B-Instruct-Q4_K_M") == "llama-3-2-3b-instruct-q4-k-m"


def test_slugify_clamps_to_docker_name_limit() -> None:
    """Docker container names cap around 63 chars; we leave room for
    the llama-cpp- prefix → 39-char slug max.
    """
    huge = "a" * 100
    assert len(models_cmd._slugify(huge)) <= 39


def test_slugify_returns_fallback_for_empty_input() -> None:
    """Edge case: all-unsafe input shouldn't produce a zero-length
    slug (which would yield `llama-cpp-` as a container name).
    """
    assert models_cmd._slugify("@@@!!!") == "model"


# ---------- progress narration ----------


def test_pull_ollama_narrates_without_verbose(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """The multi-GB duration hint used to go through --verbose-gated
    ui.info, defeating its purpose. It must be always-on ui.progress,
    with a completion line."""
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=True)
    progress_spy = mocker.spy(models_cmd.ui, "progress")
    info_spy = mocker.spy(models_cmd.ui, "info")

    models_cmd.pull("ollama", "llama3.2:3b", config_path=saved_config)

    lines = [c.args[0] for c in progress_spy.call_args_list]
    assert any("pulling llama3.2:3b" in ln and "multi-GB" in ln for ln in lines)
    assert any("pull complete" in ln for ln in lines)
    # The hint must not rely on the --verbose-gated channel.
    assert not any("multi-GB" in c.args[0] for c in info_spy.call_args_list)


def test_pull_llamacpp_narrates_download(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    captured: list[tuple[str, ...]] = []
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_llamacpp_pull_runner(cfg, captured, mount_visible=True),
    )
    mocker.patch("agents_cli.inference_cmd._service_running", return_value=True)
    spy = mocker.spy(models_cmd.ui, "progress")

    models_cmd.pull("llama-cpp", "some/repo:file.gguf", config_path=saved_config)

    lines = [c.args[0] for c in spy.call_args_list]
    assert any("downloading file.gguf" in ln and "multi-GB" in ln for ln in lines)
    assert any("download complete" in ln for ln in lines)
