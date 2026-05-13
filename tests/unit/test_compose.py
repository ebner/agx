"""compose.py: round-trip, service fragments, port allocator, atomic write, flock."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents_cli.compose import (
    ALLOWED_IMAGE_BASES,
    HERMES_IMAGE_BASE,
    OPENCLAW_IMAGE_BASE,
    add_service,
    cleanup_stale_temps,
    dump_compose,
    hermes_service_fragment,
    list_services,
    load_compose,
    next_free_port,
    openclaw_service_fragment,
    remove_service,
    rename_service,
    used_ports,
    validate_image_ref,
    write_compose_atomically,
)
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
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
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
        defaults=DefaultsConfig(
            hermes_image_tag="v1.0.0",
            openclaw_image_tag="0.1.0",
        ),
    )


# ---------- service fragments ----------


def test_hermes_fragment_has_required_mounts(cfg: AgentsConfig) -> None:
    s = hermes_service_fragment(name="coder", cfg=cfg, port=8642, image_tag="v1.0.0")

    assert s["image"] == "nousresearch/hermes-agent:v1.0.0"
    assert s["container_name"] == "hermes-coder"
    assert s["ports"] == ["127.0.0.1:8642:8642"]
    # No `user:` override — rootless docker maps container uid 0 to the
    # rootless host user, which owns the multipass-mounted data dir.
    # Forcing user: 1000:1000 would push the container into a subuid that
    # cannot write to that mount.
    assert "user" not in s

    # Three required mounts: data dir, socket, codex auth at the
    # *container user's* home (NOT /opt/data).
    assert "./coder/.data:/opt/data" in s["volumes"]
    assert "/run/user/1000/docker.sock:/run/user/1000/docker.sock" in s["volumes"]
    assert "./.codex:/home/hermes/.codex:ro" in s["volumes"]

    # DOCKER_HOST env must be set so the Hermes-bundled docker CLI finds
    # the rootless socket.
    assert s["environment"]["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"

    # HOME must be pinned in env so the Codex auth import bridge resolves
    # to the same path we mount, regardless of the image's USER/HOME drift.
    assert s["environment"]["HOME"] == "/home/hermes"

    # HERMES_UID/GID are non-negotiable — the image entrypoint reads them
    # to remap its internal hermes user (default 10000) to host uid 1000
    # before `gosu hermes`. Without these vars the entrypoint skips the
    # remap and the toolkit's --user 1000:1000 exec hits permission-denied
    # on /opt/data (which the entrypoint creates as the un-remapped uid).
    # Interpolated from .env (HERMES_UID/HERMES_GID written there by vm init).
    assert s["environment"]["HERMES_UID"] == "${HERMES_UID}"
    assert s["environment"]["HERMES_GID"] == "${HERMES_GID}"


def test_hermes_fragment_never_binds_zero_address(cfg: AgentsConfig) -> None:
    s = hermes_service_fragment(name="coder", cfg=cfg, port=8642, image_tag="v1.0.0")
    for p in s["ports"]:
        assert p.startswith("127.0.0.1:"), f"insecure binding: {p!r}"


def test_openclaw_fragment_uses_per_agent_paths(cfg: AgentsConfig) -> None:
    s = openclaw_service_fragment(name="personal", port=18789, image_tag="0.1.0")
    assert s["image"] == "openclaw/openclaw:0.1.0"
    assert s["container_name"] == "openclaw-personal"
    assert "./personal/.openclaw:/home/node/.openclaw" in s["volumes"]
    assert "./personal/workspace:/home/node/.openclaw/workspace" in s["volumes"]
    # No Codex mount: OpenClaw bundles its own.
    for v in s["volumes"]:
        assert ".codex" not in v
    assert s["environment"]["OPENCLAW_DISABLE_BONJOUR"] == "1"


def test_openclaw_named_agents_dont_collide(cfg: AgentsConfig) -> None:
    a = openclaw_service_fragment(name="personal", port=18789, image_tag="0.1.0")
    b = openclaw_service_fragment(name="home", port=18790, image_tag="0.1.0")
    assert set(a["volumes"]).isdisjoint(b["volumes"])


def test_hermes_socket_path_comes_from_config(tmp_path: Path) -> None:
    cfg = AgentsConfig(
        paths=PathsConfig(host_data_dir=str(tmp_path / "data")),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1234/docker.sock",
            ubuntu_uid=1234,
            ubuntu_gid=1234,
        ),
    )
    s = hermes_service_fragment(name="c", cfg=cfg, port=8642, image_tag="v1")
    assert "/run/user/1234/docker.sock:/run/user/1234/docker.sock" in s["volumes"]
    assert s["environment"]["DOCKER_HOST"] == "unix:///run/user/1234/docker.sock"


# ---------- image allowlist ----------


def test_allowlist_accepts_hermes_and_openclaw_with_any_tag() -> None:
    assert validate_image_ref("nousresearch/hermes-agent:v1.0.0") == HERMES_IMAGE_BASE
    assert validate_image_ref("openclaw/openclaw:0.2.1") == OPENCLAW_IMAGE_BASE


def test_allowlist_rejects_registry_prefixed_lookalikes() -> None:
    for evil in [
        "ghcr.io/evil/nousresearch/hermes-agent:v1",
        "evil-registry.io/nousresearch/hermes-agent",
        "hermes-agent.org/hermes-agent:latest",
    ]:
        with pytest.raises(UserError, match="allowlist"):
            validate_image_ref(evil)


def test_allowlist_rejects_unknown_repo() -> None:
    with pytest.raises(UserError, match="allowlist"):
        validate_image_ref("random/image:tag")


def test_allowlist_set_is_what_we_advertise() -> None:
    assert frozenset({HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE}) == ALLOWED_IMAGE_BASES


# ---------- service ops ----------


def test_add_service_inserts_into_services(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    assert "hermes-coder" in doc["services"]


def test_add_service_refuses_duplicate(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    with pytest.raises(UserError, match="already exists"):
        add_service(doc, "hermes-coder", {"image": "x:z"})


def test_remove_service_drops_entry(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "x", {"image": "i"})
    remove_service(doc, "x")
    assert "x" not in (doc.get("services") or {})


def test_remove_service_refuses_unknown(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    with pytest.raises(UserError, match="not in compose"):
        remove_service(doc, "nope")


def test_rename_service_atomic(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "old", {"image": "i"})
    rename_service(doc, "old", "new")
    assert "new" in doc["services"]
    assert "old" not in doc["services"]


def test_rename_service_refuses_existing_target(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "a", {"image": "i"})
    add_service(doc, "b", {"image": "i"})
    with pytest.raises(UserError, match="already exists"):
        rename_service(doc, "a", "b")


def test_list_services(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "a", {"image": "i"})
    add_service(doc, "b", {"image": "i"})
    assert set(list_services(doc)) == {"a", "b"}


# ---------- round-trip preserves comments ----------


def test_round_trip_preserves_comments(tmp_path: Path) -> None:
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(
        "# top-level comment\n"
        "services:\n"
        "  hermes-coder:\n"
        "    # service-level comment\n"
        "    image: nousresearch/hermes-agent:v1.0.0\n",
        encoding="utf-8",
    )
    doc = load_compose(compose_path)
    dumped = dump_compose(doc)
    assert "top-level comment" in dumped
    assert "service-level comment" in dumped


# ---------- atomic write ----------


def test_write_compose_atomically_succeeds(cfg: AgentsConfig, mocker) -> None:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    write_compose_atomically(cfg, doc)
    final = (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert "hermes-coder" in final
    # No leftover temp files (VM staging mapped to host via mount-sim)
    assert not any(cfg.paths.host_data_dir.glob(".compose.*.new"))
    # No host temp left in /tmp
    assert not any(Path("/tmp").glob(f"agents-compose-{__import__('os').getpid()}-*.yml"))


def test_write_compose_atomically_validation_failure_keeps_old_file(
    cfg: AgentsConfig, mocker
) -> None:
    from tests.conftest import install_vm_fs_simulation

    # Seed an existing valid compose
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        "services:\n  existing: {image: i}\n", encoding="utf-8"
    )
    install_vm_fs_simulation(mocker, cfg)
    # Validation step (docker compose ... config -q) gets a failing rc. We
    # override the `in_vm_runner` for that one call by routing every exec
    # through a stub that returns failure when "config" is in the cmd.
    original = mocker.patch("agents_cli.mp.exec_in_vm")

    def selective(vm_name: str, *cmd: str, check: bool = True):
        joined = " ".join(cmd)
        if "config" in joined and "-q" in joined:
            return _fail("yaml error")
        # mv operation still needs to execute the host-side rename
        if cmd and cmd[0] == "mv" and len(cmd) >= 3:
            import os

            src = Path(cmd[1])
            dst = Path(cmd[2])
            # Map VM paths to host
            vm_root = str(cfg.paths.vm_agents_root)
            if str(src).startswith(vm_root):
                src = cfg.paths.host_data_dir / src.relative_to(vm_root)
            if str(dst).startswith(vm_root):
                dst = cfg.paths.host_data_dir / dst.relative_to(vm_root)
            os.replace(src, dst)
            return _ok()
        if cmd and cmd[0] == "rm":
            for arg in cmd[1:]:
                if arg.startswith("-"):
                    continue
                p = Path(arg)
                vm_root = str(cfg.paths.vm_agents_root)
                if str(p).startswith(vm_root):
                    p = cfg.paths.host_data_dir / p.relative_to(vm_root)
                p.unlink(missing_ok=True)
            return _ok()
        return _ok()

    original.side_effect = selective

    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "broken", {"image": "x:y"})
    with pytest.raises(UserError, match="compose file invalid"):
        write_compose_atomically(cfg, doc)
    # Old file untouched, temp cleaned up
    assert "existing" in (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert "broken" not in (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert not any(cfg.paths.host_data_dir.glob(".compose.*.new"))


def test_write_supports_data_dir_in_vm_mode(cfg: AgentsConfig, mocker) -> None:
    """Under --data-dir-in-vm the mutation pattern is identical; only
    the read side differs. The simulator treats both modes the same."""
    from tests.conftest import install_vm_fs_simulation

    cfg.paths.data_dir_in_vm = True
    install_vm_fs_simulation(mocker, cfg)
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    write_compose_atomically(cfg, doc)  # no UserError


def test_write_refuses_when_vm_runtime_unpopulated(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = AgentsConfig(paths=PathsConfig(host_data_dir=str(data_dir)))
    doc = load_compose(data_dir / "docker-compose.yml")
    with pytest.raises(UserError, match="vm init"):
        write_compose_atomically(cfg, doc)


# ---------- temp cleanup ----------


def test_cleanup_stale_temps_removes_residue(cfg: AgentsConfig, mocker) -> None:
    from agents_cli.compose import host_staging_dir
    from tests.conftest import install_vm_fs_simulation

    d = cfg.paths.host_data_dir
    install_vm_fs_simulation(mocker, cfg)
    (d / ".compose.1234.abc.new").write_text("junk", encoding="utf-8")
    (d / ".compose.5678.def.new").write_text("junk", encoding="utf-8")
    (d / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    # Also leave a host-side staging-dir straggler.
    host_temp = host_staging_dir(cfg) / "stale.yml"
    host_temp.write_text("stale", encoding="utf-8")
    cleanup_stale_temps(cfg)
    assert not any(d.glob(".compose.*.new"))
    # Real compose untouched
    assert (d / "docker-compose.yml").exists()
    # Host staging-dir straggler was swept
    assert not host_temp.exists()


# ---------- port allocator ----------


def test_used_ports_parses_compose_config_json(cfg: AgentsConfig, mocker) -> None:
    payload = json.dumps(
        {
            "services": {
                "hermes-coder": {
                    "ports": [{"target": 8642, "published": "8642"}],
                },
                "openclaw": {
                    "ports": [{"target": 18789, "published": 18789}],
                },
            }
        }
    )
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(payload))
    assert used_ports(cfg) == {8642, 18789}


def test_used_ports_handles_compose_failure(cfg: AgentsConfig, mocker) -> None:
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_fail())
    assert used_ports(cfg) == set()


def test_used_ports_handles_bad_json(cfg: AgentsConfig, mocker) -> None:
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok("not json"))
    assert used_ports(cfg) == set()


def test_next_free_port_returns_base_when_unused(cfg: AgentsConfig) -> None:
    assert next_free_port(cfg, base=8642, used=set()) == 8642


def test_next_free_port_skips_used(cfg: AgentsConfig) -> None:
    assert next_free_port(cfg, base=8642, used={8642, 8643}) == 8644


def test_next_free_port_finds_gap(cfg: AgentsConfig) -> None:
    # Plan implies sequential allocation; gaps in the middle are not skipped
    # specially — we just pick the first free port at or above base.
    assert next_free_port(cfg, base=8642, used={8642, 8644}) == 8643


# flock tests moved to test_lock.py
