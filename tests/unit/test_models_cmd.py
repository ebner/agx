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
