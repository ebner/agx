"""inference_cmd: Ollama + llama.cpp lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agx_cli import compose, inference_cmd
from agx_cli import config as cfg_mod
from agx_cli.compose import OLLAMA_IMAGE_BASE, OLLAMA_SERVICE_NAME
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
def vm_fs(cfg: AgentsConfig, mocker: Any) -> dict[str, Any]:
    """Layer the standard VM-FS simulator so write_compose_atomically's
    multipass-transfer + validate dance succeeds without real network.
    """
    from tests.conftest import install_vm_fs_simulation

    return install_vm_fs_simulation(mocker, cfg)


def _layer_runner(vm_fs: dict[str, Any], extra: dict[tuple[str, ...], MpResult]) -> Any:
    """Wrap the FS simulator with extra exec_in_vm responses for the
    specific commands a test cares about (docker pull, image inspect,
    etc.). Falls through to the simulator for everything else.
    """
    sim_runner = vm_fs["exec_in_vm"].side_effect

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        for prefix, response in extra.items():
            if cmd[: len(prefix)] == prefix:
                return response
        return sim_runner(vm_name, *cmd, check=check)

    return runner


# ---------- up ollama ----------


def test_up_ollama_resolves_digest_and_pins(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """First `up ollama`: pull → image inspect → write digest to compose
    AND to config.inference.ollama_image. Compose entry must reference
    the digest, never `:latest`.
    """
    digest = f"{OLLAMA_IMAGE_BASE}@sha256:" + "f" * 64
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_layer_runner(
            vm_fs,
            {
                ("docker", "pull"): _ok(),
                ("docker", "image", "inspect"): _ok(stdout=digest + "\n"),
            },
        ),
    )
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    payload = inference_cmd.up("ollama", config_path=saved_config)

    assert payload["image"] == digest
    doc = compose.read_compose_doc(cfg)
    assert doc["services"][OLLAMA_SERVICE_NAME]["image"] == digest
    # config.toml persisted
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.inference.ollama_image == digest
    assert reloaded.inference.ollama_enabled is True


def test_up_ollama_reuses_existing_digest(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """Re-running `up` without --upgrade reuses the pinned digest.
    No `docker pull` should fire — guards against accidental re-pulls
    of multi-GB images.
    """
    existing = f"{OLLAMA_IMAGE_BASE}@sha256:" + "a" * 64
    cfg.inference.ollama_image = existing
    cfg_mod.save(cfg, saved_config)

    pull_calls: list[tuple[str, ...]] = []
    sim_runner = vm_fs["exec_in_vm"].side_effect

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd[:2] == ("docker", "pull"):
            pull_calls.append(cmd)
            return _ok()
        return sim_runner(vm_name, *cmd, check=check)

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    inference_cmd.up("ollama", config_path=saved_config)
    assert pull_calls == []


def test_up_ollama_upgrade_flag_re_resolves(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """`--upgrade` forces a fresh digest lookup even when an existing
    pin is on file. The new digest must overwrite both compose and
    the inference.ollama_image config field.
    """
    cfg.inference.ollama_image = f"{OLLAMA_IMAGE_BASE}@sha256:" + "a" * 64
    cfg_mod.save(cfg, saved_config)
    new_digest = f"{OLLAMA_IMAGE_BASE}@sha256:" + "b" * 64

    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_layer_runner(
            vm_fs,
            {
                ("docker", "pull"): _ok(),
                ("docker", "image", "inspect"): _ok(stdout=new_digest + "\n"),
            },
        ),
    )
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    inference_cmd.up("ollama", upgrade=True, config_path=saved_config)
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.inference.ollama_image == new_digest


def test_up_rejects_unknown_engine(saved_config: Path) -> None:
    with pytest.raises(UserError, match="unknown inference engine"):
        inference_cmd.up("vllm", config_path=saved_config)


def test_up_refuses_when_vm_runtime_not_initialised(tmp_path: Path) -> None:
    """Pre-flight: bare config without populated vm_runtime → fail
    loud, not silently proceed and corrupt compose.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    cfg = AgentsConfig(paths=PathsConfig(host_data_dir=str(data_dir)))
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)

    with pytest.raises(UserError, match="vm_runtime not initialised"):
        inference_cmd.up("ollama", config_path=p)


# ---------- down ollama ----------


def test_down_ollama_removes_service_and_clears_enabled_flag(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """Down: stop + rm + drop compose entry + clear ollama_enabled.
    The pinned digest stays in config (so re-running `up` reuses it
    without re-resolving).
    """
    cfg.inference.ollama_enabled = True
    cfg.inference.ollama_image = f"{OLLAMA_IMAGE_BASE}@sha256:" + "c" * 64
    cfg_mod.save(cfg, saved_config)
    # Seed compose with the service we're going to remove.
    doc = compose.read_compose_doc(cfg)
    doc.setdefault("services", {})[OLLAMA_SERVICE_NAME] = {"image": cfg.inference.ollama_image}
    compose.write_compose_atomically(cfg, doc)

    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.rm", return_value=_ok())

    inference_cmd.down("ollama", config_path=saved_config)

    doc = compose.read_compose_doc(cfg)
    assert OLLAMA_SERVICE_NAME not in (doc.get("services") or {})
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.inference.ollama_enabled is False
    # Digest preserved — re-up should reuse it
    assert reloaded.inference.ollama_image.startswith(OLLAMA_IMAGE_BASE)


def test_down_ollama_refuses_when_not_installed(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not installed"):
        inference_cmd.down("ollama", config_path=saved_config)


# ---------- llama-cpp ----------


def test_up_llamacpp_pins_image_without_starting_service(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """`up llama-cpp` only pins the image — no compose service added,
    since each model is its own service spawned by `inference load`.
    """
    digest = "ghcr.io/ggml-org/llama.cpp@sha256:" + "d" * 64

    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_layer_runner(
            vm_fs,
            {
                ("docker", "pull"): _ok(),
                ("docker", "image", "inspect"): _ok(stdout=digest + "\n"),
            },
        ),
    )

    payload = inference_cmd.up("llama-cpp", config_path=saved_config)
    assert payload["image"] == digest
    # No service block added — that comes from `inference load`.
    doc = compose.read_compose_doc(cfg)
    assert not any(s.startswith("llama-cpp-") for s in (doc.get("services") or {}))
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.inference.llamacpp_image == digest


def test_load_llamacpp_model_refuses_without_pinned_image(saved_config: Path) -> None:
    """`inference load` requires `inference up llama-cpp` first —
    otherwise we don't know what image to use.
    """
    with pytest.raises(UserError, match="image not pinned"):
        inference_cmd.up_llamacpp_model(
            "test-model",
            gguf_relative_path="model.gguf",
            port=8080,
            config_path=saved_config,
        )


def test_load_llamacpp_model_refuses_when_gguf_missing(
    cfg: AgentsConfig, saved_config: Path
) -> None:
    """Pre-flight: GGUF file must exist on disk. Otherwise the service
    starts and the container crash-loops trying to load a missing file.
    """
    cfg.inference.llamacpp_image = "ghcr.io/ggml-org/llama.cpp@sha256:" + "e" * 64
    cfg_mod.save(cfg, saved_config)

    with pytest.raises(UserError, match="GGUF not found"):
        inference_cmd.up_llamacpp_model(
            "test-model",
            gguf_relative_path="nope.gguf",
            port=8080,
            config_path=saved_config,
        )


def test_load_llamacpp_model_validates_slug(cfg: AgentsConfig, saved_config: Path) -> None:
    """Slug becomes a docker container name — enforce safe charset.
    Caps that look fine to a human (e.g. ``Llama-3.2-3B``) are
    rejected to keep container naming predictable + URL-safe.
    """
    with pytest.raises(UserError, match="invalid llama-cpp model slug"):
        inference_cmd._validate_slug("Llama-3.2-3B")  # uppercase rejected
    with pytest.raises(UserError, match="invalid llama-cpp model slug"):
        inference_cmd._validate_slug("model with spaces")
    # Boundary case: empty string should also fail.
    with pytest.raises(UserError, match="invalid llama-cpp model slug"):
        inference_cmd._validate_slug("")


# ---------- models-dir pre-create ----------


def test_up_ollama_ensures_llamacpp_models_dir_too(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """The ollama fragment bind-mounts ./models/llama-cpp as the HF
    download bridge — pre-create it at `up ollama` so the toolkit owns
    it with correct ownership instead of Docker auto-creating it
    root-owned on first start.
    """
    cfg.inference.ollama_image = f"{OLLAMA_IMAGE_BASE}@sha256:" + "a" * 64
    cfg_mod.save(cfg, saved_config)
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    inference_cmd.up("ollama", config_path=saved_config)

    assert (cfg.paths.host_data_dir / "models" / "ollama").is_dir()
    assert (cfg.paths.host_data_dir / "models" / "llama-cpp").is_dir()


# ---------- emit suppression for composite callers ----------


def test_up_ollama_emit_false_suppresses_stdout_object(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """vm rebuild embeds `inference up ollama` and owns its command's
    single stdout object — the final emit must be suppressible while
    the payload is still returned.
    """
    cfg.inference.ollama_image = f"{OLLAMA_IMAGE_BASE}@sha256:" + "a" * 64
    cfg_mod.save(cfg, saved_config)
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    emit_spy = mocker.patch("agx_cli.ui.emit")

    payload = inference_cmd.up("ollama", config_path=saved_config, emit=False)

    emit_spy.assert_not_called()
    assert payload["running"] is True
    assert payload["engine"] == "ollama"


def test_up_llamacpp_emit_false_suppresses_stdout_object(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    cfg.inference.llamacpp_image = "ghcr.io/ggml-org/llama.cpp@sha256:" + "d" * 64
    cfg_mod.save(cfg, saved_config)
    emit_spy = mocker.patch("agx_cli.ui.emit")

    payload = inference_cmd.up("llama-cpp", config_path=saved_config, emit=False)

    emit_spy.assert_not_called()
    assert payload["engine"] == "llama-cpp"


# ---------- reup_llamacpp_services (vm rebuild helper) ----------


def _seed_llamacpp_services(cfg: AgentsConfig, *slugs: str) -> None:
    """Write one digest-pinned llama-cpp service block per slug (plus a
    hermes bystander) into compose, the way `inference load` leaves it.
    """
    doc = compose.read_compose_doc(cfg)
    services = doc.setdefault("services", {})
    services["hermes-coder"] = {"image": "nousresearch/hermes-agent:v1.0.0"}
    for i, slug in enumerate(slugs):
        services[f"llama-cpp-{slug}"] = compose.llamacpp_service_fragment(
            model_slug=slug,
            image="ghcr.io/ggml-org/llama.cpp@sha256:" + "d" * 64,
            gguf_relative_path=f"{slug}.gguf",
            port=8081 + i,
        )
    compose.write_compose_atomically(cfg, doc)


def test_reup_llamacpp_services_ups_each_defined_service(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """One `compose up` per llama-cpp service (a broken model must not
    block the rest); non-llama-cpp services untouched.
    """
    _seed_llamacpp_services(cfg, "tiny", "big")
    up_spy = mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.inference_cmd._wait_service_running", return_value=True)

    result = inference_cmd.reup_llamacpp_services(cfg)

    assert result == {"started": ["llama-cpp-tiny", "llama-cpp-big"], "failed": []}
    upped = [c.args[1] for c in up_spy.call_args_list]
    assert upped == ["llama-cpp-tiny", "llama-cpp-big"]  # hermes-coder NOT upped here


def test_reup_llamacpp_services_warns_and_continues_on_failure(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """A model service that fails to start is warned about — with the
    exact `agx inference load` recovery command including the GGUF
    from the fragment — and the remaining services still come up.
    """
    from agx_cli.errors import RuntimeFailure

    _seed_llamacpp_services(cfg, "bad", "good")

    def failing_up(cfg_arg: Any, svc: str, **kw: Any) -> MpResult:
        if svc == "llama-cpp-bad":
            raise RuntimeFailure("compose up failed (exit 1)")
        return _ok()

    mocker.patch("agx_cli.compose_runner.up", side_effect=failing_up)
    mocker.patch("agx_cli.inference_cmd._wait_service_running", return_value=True)
    warn_spy = mocker.spy(inference_cmd.ui, "warn")

    result = inference_cmd.reup_llamacpp_services(cfg)

    assert result == {"started": ["llama-cpp-good"], "failed": ["llama-cpp-bad"]}
    warnings = [c.args[0] for c in warn_spy.call_args_list]
    assert any(
        "llama-cpp-bad" in w and "agx inference load llama-cpp bad --gguf bad.gguf" in w
        for w in warnings
    )


def test_reup_llamacpp_services_counts_only_actually_running(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """Detached `compose up` returns 0 once the container is CREATED —
    llama-server exiting immediately (missing/corrupt GGUF after a
    rebuild wiped the models dir) still looks like success. Only a
    service that probes as running counts as started; a crash-looper
    gets the recovery warning.
    """
    _seed_llamacpp_services(cfg, "crashy", "good")
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch(
        "agx_cli.inference_cmd._wait_service_running",
        side_effect=lambda cfg_arg, svc, **kw: svc != "llama-cpp-crashy",
    )
    warn_spy = mocker.spy(inference_cmd.ui, "warn")

    result = inference_cmd.reup_llamacpp_services(cfg)

    assert result == {"started": ["llama-cpp-good"], "failed": ["llama-cpp-crashy"]}
    warnings = [c.args[0] for c in warn_spy.call_args_list]
    assert any(
        "llama-cpp-crashy" in w
        and "not running" in w
        and "agx inference load llama-cpp crashy --gguf crashy.gguf" in w
        for w in warnings
    )


def test_wait_service_running_polls_until_running(cfg: AgentsConfig, mocker: Any) -> None:
    probe = mocker.patch(
        "agx_cli.inference_cmd._service_running",
        side_effect=[False, False, True],
    )
    assert inference_cmd._wait_service_running(cfg, "llama-cpp-x", timeout_s=5.0, interval_s=0.0)
    assert probe.call_count == 3


def test_wait_service_running_gives_up_after_window(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agx_cli.inference_cmd._service_running", return_value=False)
    assert not inference_cmd._wait_service_running(
        cfg, "llama-cpp-x", timeout_s=0.05, interval_s=0.01
    )


def test_reup_llamacpp_services_noop_when_none_defined(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    up_spy = mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    result = inference_cmd.reup_llamacpp_services(cfg)
    assert result == {"started": [], "failed": []}
    up_spy.assert_not_called()


# ---------- status ----------


def test_status_reports_ollama_not_installed_when_compose_clean(
    saved_config: Path, mocker: Any
) -> None:
    """No compose entries, no config flag → status reports
    `not installed`. Doesn't probe the daemon — that would be noisy
    in tight loops.
    """
    mocker.patch("agx_cli.compose_runner.ps", return_value=_ok("[]"))
    payload = inference_cmd.status(config_path=saved_config)
    assert payload["ollama"]["installed"] is False
    assert payload["ollama"]["running"] is False


def test_status_lists_loaded_models_when_running(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """When Ollama is up, `status` parses `ollama list` output into
    a structured model array.
    """
    # Seed compose with ollama service so the "installed" check returns True.
    doc = compose.read_compose_doc(cfg)
    doc.setdefault("services", {})[OLLAMA_SERVICE_NAME] = {"image": "test"}
    compose.write_compose_atomically(cfg, doc)

    ollama_list_output = (
        "NAME                         ID              SIZE      MODIFIED\n"
        "llama3.2:3b                  abc123          2.0 GB    2 days ago\n"
        "qwen2.5:1.5b                 def456          1.1 GB    1 hour ago\n"
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "exec" in cmd and OLLAMA_SERVICE_NAME in cmd and "list" in cmd:
            return _ok(ollama_list_output)
        return _ok()

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=runner)
    mocker.patch(
        "agx_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": OLLAMA_SERVICE_NAME, "State": "running"}])),
    )

    payload = inference_cmd.status(config_path=saved_config)
    assert payload["ollama"]["running"] is True
    names = [m["name"] for m in payload["ollama"]["models"]]
    assert names == ["llama3.2:3b", "qwen2.5:1.5b"]


# ---------- allowlist enforcement (rule #5) ----------


def test_up_ollama_rejects_off_allowlist_digest_from_daemon(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """Resolve path: even when the daemon reports a digest for some
    other repo (mirror misconfig, tampered daemon), nothing off the
    allowlist may land in compose or config.toml.
    """
    evil = "evil.example/notollama@sha256:" + "f" * 64
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_layer_runner(
            vm_fs,
            {
                ("docker", "pull"): _ok(),
                ("docker", "image", "inspect"): _ok(stdout=evil + "\n"),
            },
        ),
    )
    up_spy = mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    with pytest.raises(UserError, match="not on the allowlist"):
        inference_cmd.up("ollama", config_path=saved_config)

    up_spy.assert_not_called()
    doc = compose.read_compose_doc(cfg)
    assert OLLAMA_SERVICE_NAME not in (doc.get("services") or {})
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.inference.ollama_enabled is False
    assert reloaded.inference.ollama_image == ""


def test_up_ollama_reuse_path_rejects_doctored_config_image(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """Reuse path: a doctored config.toml pin must not be written into
    compose verbatim — validate before any compose write.
    """
    cfg.inference.ollama_image = "evil.example/miner@sha256:" + "a" * 64
    cfg_mod.save(cfg, saved_config)
    up_spy = mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    with pytest.raises(UserError, match="not on the allowlist"):
        inference_cmd.up("ollama", config_path=saved_config)

    up_spy.assert_not_called()
    doc = compose.read_compose_doc(cfg)
    assert OLLAMA_SERVICE_NAME not in (doc.get("services") or {})


def test_up_llamacpp_reuse_path_rejects_doctored_config_image(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    cfg.inference.llamacpp_image = "evil.example/miner@sha256:" + "b" * 64
    cfg_mod.save(cfg, saved_config)

    with pytest.raises(UserError, match="not on the allowlist"):
        inference_cmd.up("llama-cpp", config_path=saved_config)


def test_load_llamacpp_model_rejects_doctored_pinned_image(
    cfg: AgentsConfig, saved_config: Path
) -> None:
    """`model load` writes the pinned llama-cpp image into a fresh
    compose service block — the cfg-supplied ref must be validated
    there too, not just at pin time.
    """
    cfg.inference.llamacpp_image = "evil.example/miner@sha256:" + "c" * 64
    cfg_mod.save(cfg, saved_config)
    gguf = cfg.paths.host_data_dir / "models" / "llama-cpp" / "model.gguf"
    gguf.parent.mkdir(parents=True, exist_ok=True)
    gguf.write_bytes(b"gguf")

    with pytest.raises(UserError, match="not on the allowlist"):
        inference_cmd.up_llamacpp_model(
            "test-model",
            gguf_relative_path="model.gguf",
            port=8080,
            config_path=saved_config,
        )

    doc = compose.read_compose_doc(cfg)
    assert not any(s.startswith("llama-cpp-") for s in (doc.get("services") or {}))


# ---------- enabled flag persisted only after successful up ----------


def test_up_ollama_failed_up_does_not_persist_enabled_flag(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """A failed first `compose up` must not leave enabled-but-never-ran
    state in config.toml (doctor would then probe a service that never
    existed).
    """
    from agx_cli.errors import RuntimeFailure

    digest = f"{OLLAMA_IMAGE_BASE}@sha256:" + "f" * 64
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_layer_runner(
            vm_fs,
            {
                ("docker", "pull"): _ok(),
                ("docker", "image", "inspect"): _ok(stdout=digest + "\n"),
            },
        ),
    )
    mocker.patch(
        "agx_cli.compose_runner.up",
        side_effect=RuntimeFailure("compose up failed (exit 1)"),
    )

    with pytest.raises(RuntimeFailure):
        inference_cmd.up("ollama", config_path=saved_config)

    reloaded = cfg_mod.load(saved_config)
    assert reloaded.inference.ollama_enabled is False
    # The digest pin is also only persisted on success — the retry
    # re-resolves (cheap; the pull is already in the daemon cache).
    assert reloaded.inference.ollama_image == ""


# ---------- progress narration ----------


def test_up_ollama_narrates_pull(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """First `inference up ollama` pulls a multi-GB image — narrate via
    always-on ui.progress with a duration hint."""
    digest = f"{OLLAMA_IMAGE_BASE}@sha256:" + "f" * 64
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_layer_runner(
            vm_fs,
            {
                ("docker", "pull"): _ok(),
                ("docker", "image", "inspect"): _ok(stdout=digest + "\n"),
            },
        ),
    )
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    spy = mocker.spy(inference_cmd.ui, "progress")

    inference_cmd.up("ollama", config_path=saved_config)

    lines = [c.args[0] for c in spy.call_args_list]
    assert any("pulling" in ln and OLLAMA_IMAGE_BASE in ln and "minutes" in ln for ln in lines)


def test_up_ollama_reuse_does_not_narrate_pull(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    """Reuse path pulls nothing — the narration must not promise a pull."""
    cfg.inference.ollama_image = f"{OLLAMA_IMAGE_BASE}@sha256:" + "a" * 64
    cfg_mod.save(cfg, saved_config)
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    spy = mocker.spy(inference_cmd.ui, "progress")

    inference_cmd.up("ollama", config_path=saved_config)

    assert not any("pulling" in c.args[0] for c in spy.call_args_list)


def test_up_llamacpp_narrates_pull(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs: dict[str, Any]
) -> None:
    from agx_cli.compose import LLAMACPP_IMAGE_BASE

    digest = f"{LLAMACPP_IMAGE_BASE}@sha256:" + "e" * 64
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        side_effect=_layer_runner(
            vm_fs,
            {
                ("docker", "pull"): _ok(),
                ("docker", "image", "inspect"): _ok(stdout=digest + "\n"),
            },
        ),
    )
    spy = mocker.spy(inference_cmd.ui, "progress")

    inference_cmd.up("llama-cpp", config_path=saved_config)

    lines = [c.args[0] for c in spy.call_args_list]
    assert any("pulling" in ln and LLAMACPP_IMAGE_BASE in ln and "minutes" in ln for ln in lines)
