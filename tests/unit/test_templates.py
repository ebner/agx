"""Templates: bootstrap script structure, sandbox Dockerfile rendering."""

from __future__ import annotations

from agents_cli import templates
from agents_cli.mp import parse_bootstrap_ok
from agents_cli.templates import (
    FALLBACK_HERMES_IMAGE_TAG,
    FALLBACK_OPENCLAW_IMAGE_TAG,
    HERMES_DEFAULT_SANDBOX_IMAGE,
    render_codex_sandbox_dockerfile,
)


def test_bootstrap_script_is_bash_with_safety_flags() -> None:
    s = templates.VM_BOOTSTRAP_SCRIPT
    assert s.startswith("#!/usr/bin/env bash")
    assert "set -uo pipefail" in s


def test_bootstrap_script_emits_handshake_for_every_required_field() -> None:
    """The trailing echo must mention every field the parser requires."""
    s = templates.VM_BOOTSTRAP_SCRIPT
    handshake_lines = [line for line in s.splitlines() if 'echo "BOOTSTRAP_OK' in line]
    assert len(handshake_lines) == 1
    handshake = handshake_lines[0]
    for field in [
        "uidmap=",
        "dbus=",
        "lingering=",
        "docker_host=",
        "daemon=",
        "user_socket_active=",
        "net=",
        "ubuntu_uid=",
        "ubuntu_gid=",
        "socket_path=",
    ]:
        assert field in handshake, f"handshake missing {field}: {handshake}"


def test_bootstrap_handshake_parses_to_failed_status_when_simulated() -> None:
    """If every shell variable were left at its `fail` default, the parser
    would correctly classify the run as failed. Exercises the contract
    between the script template and the parser."""
    simulated_output = (
        "BOOTSTRAP_OK uidmap=fail dbus=fail lingering=fail docker_host=fail "
        "daemon=fail user_socket_active=fail net=fail ubuntu_uid=1000 "
        "ubuntu_gid=1000 socket_path=/run/user/1000/docker.sock"
    )
    status = parse_bootstrap_ok(simulated_output)
    assert not status.is_ok()
    assert len(status.fail_reasons()) == 7  # every required ok-field reports fail


def test_render_codex_sandbox_dockerfile_uses_base() -> None:
    dockerfile = render_codex_sandbox_dockerfile(HERMES_DEFAULT_SANDBOX_IMAGE)
    assert dockerfile.startswith(f"FROM {HERMES_DEFAULT_SANDBOX_IMAGE}\n")
    assert "npm install -g @openai/codex" in dockerfile


def test_render_codex_sandbox_dockerfile_preserves_custom_base() -> None:
    dockerfile = render_codex_sandbox_dockerfile("custom/sandbox:abc123")
    assert "FROM custom/sandbox:abc123" in dockerfile


def test_fallback_tags_are_non_empty() -> None:
    """Sanity — these get written to config.toml if GitHub Releases is unreachable."""
    assert FALLBACK_HERMES_IMAGE_TAG
    assert FALLBACK_OPENCLAW_IMAGE_TAG


def test_initial_env_content_renders_uid_gid() -> None:
    out = templates.initial_env_content(1001, 1002)
    assert "HERMES_UID=1001" in out
    assert "HERMES_GID=1002" in out


def test_initial_compose_content_is_empty_services_dict() -> None:
    assert templates.initial_compose_content() == "services: {}\n"
