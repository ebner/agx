"""codex.py: login wrapper, status parsing, skill-enable/skill-disable."""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import pytest

from agents_cli import codex, hermes_config
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import create
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail() -> MpResult:
    return MpResult(stdout="", stderr="boom", returncode=1)


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
def vm_fs_mocks(cfg: AgentsConfig, mocker) -> dict:
    from tests.conftest import install_vm_fs_simulation

    return install_vm_fs_simulation(mocker, cfg)


@pytest.fixture
def with_coder(cfg: AgentsConfig, saved_config: Path, mocker, vm_fs_mocks: dict) -> Path:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


# ---------- _sandbox_image_tag (slug correctness) ----------


def test_sandbox_image_tag_is_a_valid_docker_reference() -> None:
    """Even when the base contains / and :, the derived tag must be valid."""
    base = "nikolaik/python-nodejs:python3.11-nodejs20"
    tag = codex._sandbox_image_tag(base)
    assert tag.startswith("agents/hermes-sandbox-codex:")
    # The slug after the colon must not contain forbidden characters.
    slug = tag.split(":", 1)[1]
    forbidden = set("/:@ ")
    assert not (set(slug) & forbidden), f"slug {slug!r} has forbidden chars"


def test_sandbox_image_tag_is_deterministic() -> None:
    base = "nikolaik/python-nodejs:python3.11-nodejs20"
    assert codex._sandbox_image_tag(base) == codex._sandbox_image_tag(base)


def test_sandbox_image_tag_differs_per_base() -> None:
    a = codex._sandbox_image_tag("debian:bookworm-slim")
    b = codex._sandbox_image_tag("nikolaik/python-nodejs:python3.11-nodejs20")
    assert a != b


def test_sandbox_image_tag_matches_sha256_prefix() -> None:
    base = "weird:tag-name/with:colons"
    expected_prefix = hashlib.sha256(base.encode()).hexdigest()[:12]
    assert codex._sandbox_image_tag(base).endswith(f":{expected_prefix}")


# ---------- _sandbox_volume_mapping uses VM-side paths ----------


def test_sandbox_volume_mapping_uses_vm_host_path(cfg: AgentsConfig) -> None:
    mapping = codex._sandbox_volume_mapping(cfg)
    # MUST be the VM-side path, NOT a Hermes-container path like /home/hermes/.codex
    assert mapping.startswith("/home/ubuntu/agents/.codex:")
    assert mapping.endswith(":ro")
    assert "/home/hermes/" not in mapping


# ---------- skill-enable ----------


def test_skill_enable_refuses_openclaw(
    cfg: AgentsConfig, saved_config: Path, mocker, vm_fs_mocks: dict
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    with pytest.raises(UserError, match="only applies to Hermes"):
        codex.skill_enable("personal", config_path=saved_config)


def test_skill_enable_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="only applies to Hermes"):
        codex.skill_enable("ghost", config_path=saved_config)


def test_skill_enable_builds_image_and_updates_config(
    with_coder: Path, mocker, cfg: AgentsConfig, vm_fs_mocks: dict
) -> None:
    parent = mocker.Mock()
    parent.attach_mock(mocker.patch("agents_cli.compose_runner.up", return_value=_ok()), "up")
    parent.attach_mock(
        mocker.patch("agents_cli.compose_runner.restart", return_value=_ok()), "restart"
    )
    result = codex.skill_enable("coder", config_path=with_coder)

    # Derived image tag returned
    assert result["derived_image"].startswith("agents/hermes-sandbox-codex:")

    # Agent's config.yaml now points at the derived image
    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert hermes_config.get_terminal_docker_image(doc) == result["derived_image"]
    # Sandbox volume uses VM-side host path
    volumes = doc["terminal"]["docker_volumes"]
    # Sandbox mount target is /codex (user-independent; sandbox image sets
    # CODEX_HOME=/codex so Codex CLI reads auth from there regardless of
    # which user the base image runs as).
    assert any("/home/ubuntu/agents/.codex:/codex:ro" in v for v in volumes)

    # Container truly restarted, and created first if absent: `up` alone
    # would no-op for a running container (the edit is config.yaml-only,
    # compose config unchanged) leaving the old sandbox image live, while
    # `restart` alone cannot create an absent container (VM rebuild,
    # `docker compose rm`). Must be up THEN restart — mem0's pattern.
    assert [name for name, _args, _kw in parent.mock_calls] == ["up", "restart"]
    assert parent.up.call_args.args[1] == "hermes-coder"
    assert parent.restart.call_args.args[1] == "hermes-coder"
    # docker build was invoked through the simulator's exec_in_vm
    build_calls = vm_fs_mocks["exec_in_vm"].call_args_list
    assert any("docker" in c.args and "build" in c.args for c in build_calls)


def test_skill_enable_persists_original_for_restore(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    mocker.patch("agents_cli.compose_runner.restart", return_value=_ok())
    codex.skill_enable("coder", config_path=with_coder)
    state_path = codex._skill_state_path(cfg, "coder")
    state = json.loads(state_path.read_text())
    # Original was the default Hermes sandbox image (config.yaml didn't set one)
    assert state["original_docker_image"]
    assert state["derived_image"].startswith("agents/hermes-sandbox-codex:")


def test_skill_enable_is_idempotent(with_coder: Path, mocker) -> None:
    mocker.patch("agents_cli.compose_runner.restart", return_value=_ok())
    first = codex.skill_enable("coder", config_path=with_coder)
    second = codex.skill_enable("coder", config_path=with_coder)
    assert first["derived_image"] == second["derived_image"]


def test_skill_enable_refuses_derived_image_without_state_file(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """terminal.docker_image already points at our derived sandbox image
    but the .codex-skill/<name>.json state file is gone (lost, migrated
    partially, hand-deleted). Recording the DERIVED image as the
    original would make the true base unrecoverable and wedge every
    enable/disable cycle on the derived image — refuse instead, naming
    the state file and the manual fix.
    """
    mocker.patch("agents_cli.compose_runner.restart", return_value=_ok())
    # Simulate the trap: config.yaml points at a derived image, no state.
    p = hermes_config.config_path(cfg, "coder")
    doc = hermes_config.load(p)
    hermes_config.set_terminal_docker_image(doc, f"{codex.SANDBOX_IMAGE_PREFIX}:cafebabe0000")
    p.write_text(hermes_config.dump(doc), encoding="utf-8")
    assert not codex._skill_state_path(cfg, "coder").exists()

    with pytest.raises(UserError) as exc:
        codex.skill_enable("coder", config_path=with_coder)
    msg = str(exc.value)
    # Must name the state file and the recovery step, not silently
    # fall back to recording the derived image as the original.
    assert str(codex._skill_state_path(cfg, "coder")) in msg
    assert "terminal.docker_image" in msg
    # And it must NOT have created a poisoned state file.
    assert not codex._skill_state_path(cfg, "coder").exists()


def test_skill_enable_propagates_docker_build_failure(
    with_coder: Path, mocker, vm_fs_mocks: dict
) -> None:
    # Override the simulator so any exec_in_vm with `docker build` fails;
    # everything else still runs the simulator's host-side side effects.
    real_runner = vm_fs_mocks["exec_in_vm"].side_effect

    def failing_build(vm_name: str, *cmd: str, check: bool = True):
        if "docker" in cmd and "build" in cmd:
            return _fail()
        return real_runner(vm_name, *cmd, check=check)

    vm_fs_mocks["exec_in_vm"].side_effect = failing_build

    with pytest.raises(RuntimeFailure, match="docker build"):
        codex.skill_enable("coder", config_path=with_coder)


# ---------- skill-disable ----------


def test_skill_disable_restores_original(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    mocker.patch("agents_cli.compose_runner.restart", return_value=_ok())
    codex.skill_enable("coder", config_path=with_coder)
    state = json.loads(codex._skill_state_path(cfg, "coder").read_text())
    original = state["original_docker_image"]

    parent = mocker.Mock()
    parent.attach_mock(mocker.patch("agents_cli.compose_runner.up", return_value=_ok()), "up")
    parent.attach_mock(
        mocker.patch("agents_cli.compose_runner.restart", return_value=_ok()), "restart"
    )
    codex.skill_disable("coder", config_path=with_coder)
    # Disable is also a config.yaml-only edit on a possibly-absent
    # container → same up-then-restart order as enable.
    assert [name for name, _args, _kw in parent.mock_calls] == ["up", "restart"]
    assert parent.up.call_args.args[1] == "hermes-coder"
    assert parent.restart.call_args.args[1] == "hermes-coder"
    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    assert hermes_config.get_terminal_docker_image(doc) == original
    # Sandbox volume mapping removed
    volumes = doc["terminal"].get("docker_volumes") or []
    assert not any("/home/ubuntu/agents/.codex" in v for v in volumes)
    # State file gone
    assert not codex._skill_state_path(cfg, "coder").exists()


def test_skill_disable_refuses_when_not_enabled(with_coder: Path) -> None:
    with pytest.raises(UserError, match="not enabled"):
        codex.skill_disable("coder", config_path=with_coder)


# ---------- expiry parsing ----------


def test_parse_expiry_iso_with_z() -> None:
    auth = {"expires_at": "2026-12-01T00:00:00Z"}
    parsed = codex._parse_expiry(auth)
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_expiry_iso_offset() -> None:
    parsed = codex._parse_expiry({"expires_at": "2026-12-01T00:00:00+00:00"})
    assert parsed is not None


def test_parse_expiry_epoch_seconds() -> None:
    epoch = 1900000000  # ~2030
    parsed = codex._parse_expiry({"expires_at": epoch})
    assert parsed is not None
    assert parsed.year >= 2030


def test_parse_expiry_nested_tokens() -> None:
    parsed = codex._parse_expiry({"tokens": {"expires_at": "2026-12-01T00:00:00Z"}})
    assert parsed is not None


def test_parse_expiry_missing_returns_none() -> None:
    assert codex._parse_expiry({}) is None


def test_parse_expiry_garbage_returns_none() -> None:
    assert codex._parse_expiry({"expires_at": "not-a-date"}) is None


# ---------- status ----------


def test_status_reports_missing_auth(cfg: AgentsConfig, saved_config: Path) -> None:
    info = codex.status(config_path=saved_config)
    assert info["auth_present"] is False
    assert info["expiry_warning"] is False


def test_status_reports_expiry_warning_under_7_days(cfg: AgentsConfig, saved_config: Path) -> None:
    codex_home = cfg.paths.host_data_dir / ".codex"
    codex_home.mkdir()
    near_expiry = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=2)
    (codex_home / "auth.json").write_text(
        json.dumps({"expires_at": near_expiry.isoformat()}), encoding="utf-8"
    )
    info = codex.status(config_path=saved_config)
    assert info["auth_present"] is True
    assert info["expiry_warning"] is True
    assert info["expires_in_days"] is not None and info["expires_in_days"] < 7


def test_status_no_warning_when_far_from_expiry(cfg: AgentsConfig, saved_config: Path) -> None:
    codex_home = cfg.paths.host_data_dir / ".codex"
    codex_home.mkdir()
    far = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30)
    (codex_home / "auth.json").write_text(
        json.dumps({"expires_at": far.isoformat()}), encoding="utf-8"
    )
    info = codex.status(config_path=saved_config)
    assert info["expiry_warning"] is False


def test_status_includes_per_agent_mount_status(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    mocker.patch("agents_cli.compose_runner.restart", return_value=_ok())
    info = codex.status(config_path=with_coder)
    names = {entry["name"] for entry in info["per_agent_mounts"]}
    assert "coder" in names
    coder = next(e for e in info["per_agent_mounts"] if e["name"] == "coder")
    assert coder["skill_enabled"] is False

    codex.skill_enable("coder", config_path=with_coder)
    info = codex.status(config_path=with_coder)
    coder = next(e for e in info["per_agent_mounts"] if e["name"] == "coder")
    assert coder["skill_enabled"] is True


# ---------- mount probe argv (override merge consistency) ----------


def test_mount_probe_argv_merges_override_iff_present(cfg: AgentsConfig, mocker) -> None:
    """The probe's compose exec must be built on compose_runner._base_argv
    so the override file (architecture rule #10) is merged exactly like
    every other compose invocation: no override -f when the file is
    absent, the extra -f pair when it exists.
    """
    (cfg.paths.host_data_dir / ".codex").mkdir()
    (cfg.paths.host_data_dir / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    exec_spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    assert codex._container_codex_mount_probe(cfg, "hermes-coder") == "ok"
    argv = exec_spy.call_args.args
    assert "-f" in argv
    assert "/home/ubuntu/agents/docker-compose.yml" in argv
    assert not any("docker-compose.override.yml" in str(a) for a in argv)

    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    codex._container_codex_mount_probe(cfg, "hermes-coder")
    argv = exec_spy.call_args.args
    assert "/home/ubuntu/agents/docker-compose.override.yml" in argv


# ---------- login / refresh (subprocess wrappers) ----------


def test_login_invokes_codex_with_codex_home(mocker, saved_config: Path, cfg: AgentsConfig) -> None:
    mocker.patch("shutil.which", return_value="/usr/local/bin/codex")
    run_spy = mocker.patch("subprocess.run", return_value=type("R", (), {"returncode": 0})())
    codex.login(config_path=saved_config)
    args, kwargs = run_spy.call_args
    assert args[0] == ["/usr/local/bin/codex", "login"]
    assert kwargs["env"]["CODEX_HOME"] == str(cfg.paths.host_data_dir / ".codex")


def test_login_device_auth_appends_flag(mocker, saved_config: Path, cfg: AgentsConfig) -> None:
    """--device-auth switches Codex to the device-code flow, required on
    headless hosts where the localhost callback URL isn't reachable.
    """
    mocker.patch("shutil.which", return_value="/usr/local/bin/codex")
    run_spy = mocker.patch("subprocess.run", return_value=type("R", (), {"returncode": 0})())
    codex.login(device_auth=True, config_path=saved_config)
    args, _ = run_spy.call_args
    assert args[0] == ["/usr/local/bin/codex", "login", "--device-auth"]


def test_login_raises_when_codex_missing(mocker, saved_config: Path) -> None:
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(UserError, match="codex CLI not found"):
        codex.login(config_path=saved_config)


def test_refresh_raises_when_auth_absent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="codex login"):
        codex.refresh(config_path=saved_config)


def test_refresh_calls_status_for_silent_check(
    mocker, saved_config: Path, cfg: AgentsConfig
) -> None:
    (cfg.paths.host_data_dir / ".codex").mkdir()
    (cfg.paths.host_data_dir / ".codex" / "auth.json").write_text("{}")
    mocker.patch("shutil.which", return_value="/usr/local/bin/codex")
    run_spy = mocker.patch(
        "subprocess.run",
        return_value=type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})(),
    )
    codex.refresh(config_path=saved_config)
    args, _ = run_spy.call_args
    assert args[0] == ["/usr/local/bin/codex", "auth", "status"]
