"""compose_runner.py: argv construction for docker compose wrappers."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents_cli import compose_runner
from agents_cli.config import AgentsConfig, PathsConfig, VmRuntimeConfig
from agents_cli.mp import MpResult


def _ok() -> MpResult:
    return MpResult(stdout="", stderr="", returncode=0)


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(tmp_path / "data")),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
    )


def _base_argv() -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        "/home/ubuntu/agents",
        "-f",
        "/home/ubuntu/agents/docker-compose.yml",
        "--env-file",
        "/home/ubuntu/agents/.env",
    ]


def test_up_passes_detach_and_service(cfg: AgentsConfig, mocker) -> None:
    runner = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    compose_runner.up(cfg, "hermes-coder")
    args, _ = runner.call_args
    assert list(args[1:]) == [*_base_argv(), "up", "-d", "hermes-coder"]


def test_down_with_remove_orphans(cfg: AgentsConfig, mocker) -> None:
    runner = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    compose_runner.down(cfg, remove_orphans=True)
    args, _ = runner.call_args
    assert list(args[1:]) == [*_base_argv(), "down", "--remove-orphans"]


def test_pull_only_specific_service(cfg: AgentsConfig, mocker) -> None:
    runner = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    compose_runner.pull(cfg, "hermes-coder")
    args, _ = runner.call_args
    assert list(args[1:]) == [*_base_argv(), "pull", "hermes-coder"]


def test_logs_with_follow_and_since(cfg: AgentsConfig, mocker) -> None:
    runner = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    compose_runner.logs(cfg, "hermes-coder", follow=True, since="10m")
    args, _ = runner.call_args
    assert list(args[1:]) == [*_base_argv(), "logs", "-f", "--since", "10m", "hermes-coder"]


def test_ps_default_format_json(cfg: AgentsConfig, mocker) -> None:
    runner = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    compose_runner.ps(cfg)
    args, kwargs = runner.call_args
    assert list(args[1:]) == [*_base_argv(), "ps", "--format", "json"]
    assert kwargs.get("check") is False


def test_docker_image_inspect_uses_ref(cfg: AgentsConfig, mocker) -> None:
    runner = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    compose_runner.docker_image_inspect(cfg, "nousresearch/hermes-agent:v1.0.0")
    args, kwargs = runner.call_args
    assert list(args[1:]) == [
        "docker",
        "image",
        "inspect",
        "nousresearch/hermes-agent:v1.0.0",
    ]
    assert kwargs.get("check") is False
