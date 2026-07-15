"""sidecar: shared leaf helpers (arch, digest pinning, liveness, project-env I/O)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents_cli import sidecar
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


# ---------- docker_arch ----------


@pytest.mark.parametrize(
    "machine,expected",
    [
        ("aarch64", "arm64"),  # Linux arm64 (RPi5 distros)
        ("arm64", "arm64"),  # macOS Apple Silicon
        ("ARM64", "arm64"),  # case-insensitive
        ("x86_64", "amd64"),
        # Exotic archs fall through to amd64 — no upstream sidecar image
        # targets them, so emulation kicks in either way and the amd64
        # tag is the safer bet than a guessed mapping.
        ("riscv64", "amd64"),
        ("ppc64le", "amd64"),
    ],
)
def test_docker_arch_mapping(mocker: Any, machine: str, expected: str) -> None:
    mocker.patch("platform.machine", return_value=machine)
    assert sidecar.docker_arch() == expected


def test_firecrawl_alias_delegates_to_sidecar(mocker: Any) -> None:
    """firecrawl_cmd._docker_arch is kept as an alias (ops.py + its tests
    reference it) — must stay in lock-step with the sidecar helper.
    """
    from agents_cli import firecrawl_cmd

    mocker.patch("platform.machine", return_value="aarch64")
    assert firecrawl_cmd._docker_arch() == sidecar.docker_arch() == "arm64"


# ---------- resolve_digest ----------


def test_resolve_digest_returns_pinned_ref(cfg: AgentsConfig, mocker: Any) -> None:
    digest = "redis@sha256:" + "a" * 64

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd[:2] == ("docker", "pull"):
            return _ok()
        if cmd[:3] == ("docker", "image", "inspect"):
            return _ok(digest + "\n")
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    assert sidecar.resolve_digest(cfg, "redis:alpine") == digest


def test_resolve_digest_rejects_off_allowlist_ref(cfg: AgentsConfig, mocker: Any) -> None:
    """Rule #5: even if the daemon reports a digest for some other repo
    (registry rewrite, mirror misconfig, tampered daemon), the ref must
    be allowlist-validated BEFORE it can reach any compose write.
    """
    evil = "evil.example/attacker/redis@sha256:" + "b" * 64

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd[:2] == ("docker", "pull"):
            return _ok()
        if cmd[:3] == ("docker", "image", "inspect"):
            return _ok(evil + "\n")
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    with pytest.raises(UserError, match="not on the allowlist"):
        sidecar.resolve_digest(cfg, "redis:alpine")


def test_resolve_digest_pull_failure_raises(cfg: AgentsConfig, mocker: Any) -> None:
    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd[:2] == ("docker", "pull"):
            return _fail("no route to registry")
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    with pytest.raises(RuntimeFailure, match="docker pull failed"):
        sidecar.resolve_digest(cfg, "redis:alpine")


def test_resolve_digest_empty_inspect_raises(cfg: AgentsConfig, mocker: Any) -> None:
    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd[:2] == ("docker", "pull"):
            return _ok()
        if cmd[:3] == ("docker", "image", "inspect"):
            return _ok("")
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    with pytest.raises(RuntimeFailure, match="could not resolve digest"):
        sidecar.resolve_digest(cfg, "redis:alpine")


def test_resolve_digest_unparseable_ref_raises(cfg: AgentsConfig, mocker: Any) -> None:
    """A RepoDigest without ``@sha256:`` must never be returned — it
    would land un-pinned in compose.
    """

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd[:2] == ("docker", "pull"):
            return _ok()
        if cmd[:3] == ("docker", "image", "inspect"):
            return _ok("redis:alpine\n")
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    with pytest.raises(RuntimeFailure, match="unparseable repo-digest"):
        sidecar.resolve_digest(cfg, "redis:alpine")


# ---------- service_running ----------


@pytest.mark.parametrize(
    "state,expected",
    [
        ("running", True),
        # docker compose reports "healthy" for services with a passing
        # healthcheck — firecrawl's old hand-rolled parse treated that
        # as down. One reconciled definition of "up".
        ("healthy", True),
        ("Healthy", True),
        ("exited", False),
        ("restarting", False),
    ],
)
def test_service_running_accepts_running_or_healthy(
    cfg: AgentsConfig, mocker: Any, state: str, expected: bool
) -> None:
    mocker.patch(
        "agents_cli.compose_runner.ps",
        return_value=_ok(json.dumps([{"Service": "ollama", "State": state}])),
    )
    assert sidecar.service_running(cfg, "ollama") is expected


def test_service_running_false_when_absent(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.ps", return_value=_ok("[]"))
    assert sidecar.service_running(cfg, "ollama") is False


def test_service_running_false_when_ps_fails(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.compose_runner.ps", return_value=_fail("daemon down"))
    assert sidecar.service_running(cfg, "ollama") is False


# ---------- project env I/O ----------


def test_project_env_round_trip_host_mode(cfg: AgentsConfig) -> None:
    env = sidecar.read_project_env(cfg)
    env["FIRECRAWL_API_KEY"] = "fc-x"
    sidecar.write_project_env(cfg, env)

    again = sidecar.read_project_env(cfg)
    assert again["FIRECRAWL_API_KEY"] == "fc-x"
    assert again["HERMES_UID"] == "1000"  # pre-existing keys survive
    # In host-mount mode the host file IS what compose reads.
    text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "FIRECRAWL_API_KEY=fc-x" in text


def test_project_env_round_trip_data_dir_in_vm_mode(cfg: AgentsConfig, mocker: Any) -> None:
    """In --data-dir-in-vm mode the authoritative .env lives at
    <vm_agents_root>/.env inside the VM (compose --env-file reads it
    there). The simulator maps that path back to the host file, so a
    round-trip through the transfer machinery must land there.
    """
    from tests.conftest import install_vm_fs_simulation

    cfg.paths.data_dir_in_vm = True
    mocks = install_vm_fs_simulation(mocker, cfg)

    sidecar.write_project_env(cfg, {"OLLAMA_API_KEY": "tok"})
    assert sidecar.read_project_env(cfg) == {"OLLAMA_API_KEY": "tok"}

    # Both directions actually went through multipass transfer.
    assert mocks["transfer_to_vm"].called
    assert mocks["transfer_from_vm"].called
    text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "OLLAMA_API_KEY=tok" in text


def test_read_project_env_missing_file_returns_empty(cfg: AgentsConfig) -> None:
    (cfg.paths.host_data_dir / ".env").unlink()
    assert sidecar.read_project_env(cfg) == {}


def test_parse_env_skips_comments_blanks_and_junk(cfg: AgentsConfig) -> None:
    (cfg.paths.host_data_dir / ".env").write_text(
        "# comment\n\nKEY=value with spaces\nnot-an-assignment\n", encoding="utf-8"
    )
    assert sidecar.read_project_env(cfg) == {"KEY": "value with spaces"}
