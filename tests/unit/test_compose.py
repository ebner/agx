"""compose.py: round-trip, service fragments, port allocator, atomic write, flock."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agents_cli.compose import (
    ALLOWED_IMAGE_BASES,
    HERMES_IMAGE_BASE,
    OPENCLAW_IMAGE_BASE,
    acquire_lock,
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
    assert s["user"] == "${HERMES_UID}:${HERMES_GID}"

    # Three required mounts: data dir, socket, codex auth at the
    # *container user's* home (NOT /opt/data).
    assert "./coder/.data:/opt/data" in s["volumes"]
    assert "/run/user/1000/docker.sock:/run/user/1000/docker.sock" in s["volumes"]
    assert "./.codex:/home/hermes/.codex:ro" in s["volumes"]

    # DOCKER_HOST env must be set so the Hermes-bundled docker CLI finds
    # the rootless socket.
    assert s["environment"]["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"


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
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    write_compose_atomically(cfg, doc)
    final = (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert "hermes-coder" in final
    # No leftover temp files
    assert not any(cfg.paths.host_data_dir.glob(".compose.*.new"))


def test_write_compose_atomically_validation_failure_keeps_old_file(
    cfg: AgentsConfig, mocker
) -> None:
    # Seed an existing valid compose
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        "services:\n  existing: {image: i}\n", encoding="utf-8"
    )
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_fail("yaml error"))
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "broken", {"image": "x:y"})
    with pytest.raises(UserError, match="compose file invalid"):
        write_compose_atomically(cfg, doc)
    # Old file untouched, temp cleaned up
    assert "existing" in (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert "broken" not in (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert not any(cfg.paths.host_data_dir.glob(".compose.*.new"))


def test_write_refuses_under_data_dir_in_vm(cfg: AgentsConfig) -> None:
    cfg.paths.data_dir_in_vm = True
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    with pytest.raises(UserError, match="data-dir-in-vm"):
        write_compose_atomically(cfg, doc)


def test_write_refuses_when_vm_runtime_unpopulated(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = AgentsConfig(paths=PathsConfig(host_data_dir=str(data_dir)))
    doc = load_compose(data_dir / "docker-compose.yml")
    with pytest.raises(UserError, match="vm init"):
        write_compose_atomically(cfg, doc)


# ---------- temp cleanup ----------


def test_cleanup_stale_temps_removes_residue(cfg: AgentsConfig) -> None:
    d = cfg.paths.host_data_dir
    (d / ".compose.1234.abc.new").write_text("junk", encoding="utf-8")
    (d / ".compose.5678.def.new").write_text("junk", encoding="utf-8")
    (d / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    cleanup_stale_temps(d)
    assert not any(d.glob(".compose.*.new"))
    # Real compose untouched
    assert (d / "docker-compose.yml").exists()


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


# ---------- flock ----------


def test_acquire_lock_serializes_two_threads(cfg: AgentsConfig) -> None:
    timeline = []

    def worker(label: str, hold: float) -> None:
        with acquire_lock(cfg.paths.host_data_dir):
            timeline.append(f"{label}:start")
            time.sleep(hold)
            timeline.append(f"{label}:end")

    t1 = threading.Thread(target=worker, args=("A", 0.05))
    t2 = threading.Thread(target=worker, args=("B", 0.05))
    t1.start()
    time.sleep(0.01)  # ensure A acquires first
    t2.start()
    t1.join()
    t2.join()

    # No interleaving: A's end must come before B's start.
    assert timeline == ["A:start", "A:end", "B:start", "B:end"]
