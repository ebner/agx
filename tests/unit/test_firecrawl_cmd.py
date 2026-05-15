"""firecrawl_cmd: up/down/destroy/status + agent auto-wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents_cli import compose, firecrawl_cmd, hermes_config
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import create
from agents_cli.compose import FIRECRAWL_SERVICE_PREFIX
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


# Stable digest stub returned by `docker image inspect`. Each pull
# resolves to a per-role digest so tests can assert that distinct images
# land in compose.
DIGEST_FOR: dict[str, str] = {
    "ghcr.io/firecrawl/firecrawl:linux-amd64": ("ghcr.io/firecrawl/firecrawl@sha256:" + "a" * 64),
    "ghcr.io/firecrawl/playwright-service:latest": (
        "ghcr.io/firecrawl/playwright-service@sha256:" + "b" * 64
    ),
    "ghcr.io/firecrawl/nuq-postgres:latest": ("ghcr.io/firecrawl/nuq-postgres@sha256:" + "c" * 64),
    "redis:alpine": "redis@sha256:" + "d" * 64,
    "rabbitmq:3-management": "rabbitmq@sha256:" + "e" * 64,
}


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
def vm_fs_mocks(cfg: AgentsConfig, mocker: Any) -> dict[str, Any]:
    from tests.conftest import install_vm_fs_simulation

    return install_vm_fs_simulation(mocker, cfg)


def _firecrawl_runner(vm_fs_mocks: dict[str, Any]) -> Any:
    """Layer firecrawl-aware responses (docker pull + image inspect) onto
    the FS simulator so digest resolution succeeds in tests.
    """
    sim_runner = vm_fs_mocks["exec_in_vm"].side_effect

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if cmd[:2] == ("docker", "pull"):
            return _ok()
        if cmd[:3] == ("docker", "image", "inspect"):
            for ref, digest in DIGEST_FOR.items():
                if ref in cmd:
                    return _ok(stdout=digest + "\n")
            return _ok(stdout="unknown\n")
        return sim_runner(vm_name, *cmd, check=check)

    return runner


@pytest.fixture
def with_coder(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


# ---------- up ----------


def test_docker_arch_maps_aarch64_to_arm64(mocker: Any) -> None:
    """Linux arm64 hosts report ``aarch64`` via ``platform.machine``;
    that's what RPi5's distro returns. Docker tag convention is
    ``arm64`` though — verify the mapping.
    """
    mocker.patch("platform.machine", return_value="aarch64")
    assert firecrawl_cmd._docker_arch() == "arm64"


def test_docker_arch_maps_arm64_macos_to_arm64(mocker: Any) -> None:
    """macOS Apple Silicon returns ``arm64`` directly — round-trip clean."""
    mocker.patch("platform.machine", return_value="arm64")
    assert firecrawl_cmd._docker_arch() == "arm64"


def test_docker_arch_defaults_to_amd64_for_x86_64(mocker: Any) -> None:
    mocker.patch("platform.machine", return_value="x86_64")
    assert firecrawl_cmd._docker_arch() == "amd64"


def test_docker_arch_falls_back_to_amd64_for_exotic_archs(mocker: Any) -> None:
    """riscv64, ppc64le, etc. — Firecrawl publishes no tags for these,
    so picking amd64 is the safer choice than guessing a mapping.
    Emulation will kick in regardless.
    """
    mocker.patch("platform.machine", return_value="riscv64")
    assert firecrawl_cmd._docker_arch() == "amd64"


def test_floating_refs_use_host_arch_for_api_image() -> None:
    """The api image is single-arch per tag (``:linux-amd64`` and
    ``:linux-arm64`` are separate). The OTHER firecrawl images are
    multi-arch manifests, so they keep a single tag.
    """
    # Whatever the test host actually is, the FLOATING_REFS api tag
    # must end in -amd64 or -arm64. (Module-import-time resolution.)
    assert firecrawl_cmd.FLOATING_REFS["api"].endswith(("-amd64", "-arm64")), (
        "api tag must encode an arch"
    )
    # The other three remain multi-arch — no per-arch suffix.
    assert "amd64" not in firecrawl_cmd.FLOATING_REFS["playwright"]
    assert "amd64" not in firecrawl_cmd.FLOATING_REFS["redis"]
    assert "amd64" not in firecrawl_cmd.FLOATING_REFS["rabbitmq"]


def test_up_resolves_digests_and_writes_state(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    payload = firecrawl_cmd.up(config_path=with_coder)

    assert payload["running"] is True
    assert payload["api_url"] == "http://firecrawl-api:3002"
    state = firecrawl_cmd.read_state(cfg)
    assert state["images"]["api"].startswith("ghcr.io/firecrawl/firecrawl@sha256:")
    assert state["images"]["playwright"].startswith("ghcr.io/firecrawl/playwright-service@sha256:")
    assert state["images"]["redis"].startswith("redis@sha256:")


def test_up_writes_five_compose_services(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)

    doc = compose.read_compose_doc(cfg)
    services = doc["services"]
    expected = {
        "firecrawl-api",
        "firecrawl-playwright",
        "firecrawl-redis",
        "firecrawl-rabbitmq",
        "firecrawl-postgres",
    }
    assert expected.issubset(set(services))


def test_up_populates_project_env_with_secrets(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    env = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "FIRECRAWL_API_KEY=fc-self-hosted-" in env
    assert "FIRECRAWL_API_URL=http://firecrawl-api:3002" in env
    # BULL auth and postgres password are also generated.
    assert "FIRECRAWL_BULL_AUTH_KEY=" in env
    assert "FIRECRAWL_POSTGRES_PASSWORD=" in env


def test_up_writes_project_env_to_vm_in_data_dir_in_vm_mode(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """Regression: in --data-dir-in-vm mode the project .env lives at
    `<vm_agents_root>/.env` (see vm._seed_vm_data_dir). Compose reads
    from there via `--env-file`. Writing to the host's `<data-dir>/.env`
    in this mode is a no-op silent bug — postgres init fails because
    POSTGRES_PASSWORD never reaches compose.
    """
    cfg.paths.data_dir_in_vm = True
    cfg_mod.save(cfg, saved_config)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=saved_config)

    # The simulator maps <vm_agents_root>/.env back to <host_data_dir>/.env
    # (via host_path), so we can read it from the host side to assert.
    env = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "FIRECRAWL_API_KEY=fc-self-hosted-" in env
    assert "FIRECRAWL_BULL_AUTH_KEY=" in env
    assert "FIRECRAWL_POSTGRES_PASSWORD=" in env


def test_up_wires_existing_hermes_agents_passthrough(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """The whole point: every Hermes agent's terminal.env_passthrough
    gets the FIRECRAWL pair so the LLM's shell-based diagnostic doesn't
    falsely report "not configured".
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)

    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    passthrough = doc["terminal"]["env_passthrough"]
    assert "FIRECRAWL_API_KEY" in passthrough
    assert "FIRECRAWL_API_URL" in passthrough


def test_up_reports_openclaw_agents_as_wired_via_fragment_env(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """OpenClaw agents have no terminal.env_passthrough analogue —
    their wiring is purely compose-fragment env interpolation. The
    fragment template carries ``FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY:-}``
    and ``FIRECRAWL_BASE_URL=${FIRECRAWL_API_URL:-}`` (bridging the
    Hermes-vs-OpenClaw env-var-name asymmetry), so fresh OpenClaw
    agents pick up firecrawl automatically once the project .env
    has the values.
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    payload = firecrawl_cmd.up(config_path=saved_config)

    assert "personal" in payload["wired_agents"]
    doc = compose.read_compose_doc(cfg)
    env = doc["services"]["openclaw-personal"]["environment"]
    assert env["FIRECRAWL_API_KEY"] == "${FIRECRAWL_API_KEY:-}"
    assert env["FIRECRAWL_BASE_URL"] == "${FIRECRAWL_API_URL:-}"


def test_up_flags_stale_openclaw_fragments(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """An OpenClaw agent created before this auto-wire change has an
    environment block missing the firecrawl entries. ``firecrawl up``
    must surface it as stale (alongside any Hermes stale agents) so
    the user knows to recreate it — there's no ``agents reseat`` for
    OpenClaw yet.
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)

    # Simulate a pre-auto-wire fragment: strip the firecrawl entries.
    doc = compose.read_compose_doc(cfg)
    env = doc["services"]["openclaw-personal"]["environment"]
    del env["FIRECRAWL_API_KEY"]
    del env["FIRECRAWL_BASE_URL"]
    compose.write_compose_atomically(cfg, doc)

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))
    payload = firecrawl_cmd.up(config_path=saved_config)

    assert "personal" in payload["stale_agents"]
    assert "personal" not in payload["wired_agents"]


def test_up_is_idempotent_does_not_regenerate_digests(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """Calling `up` again must reuse the captured digests — even if the
    upstream tags' digests would resolve differently — so the toolkit
    never silently pulls a new image version.
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    first_state = json.loads(json.dumps(firecrawl_cmd.read_state(cfg)))

    # Drop the docker pull call count; we'll verify the second `up`
    # doesn't pull again.
    pull_count_before = sum(
        1 for c in in_vm_spy.call_args_list if c.args[1:3] == ("docker", "pull")
    )

    firecrawl_cmd.up(config_path=with_coder)
    second_state = firecrawl_cmd.read_state(cfg)
    assert first_state == second_state

    pull_count_after = sum(1 for c in in_vm_spy.call_args_list if c.args[1:3] == ("docker", "pull"))
    # No new pulls between the two ups.
    assert pull_count_after == pull_count_before


def test_up_upgrade_re_resolves_digests(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """`--upgrade` re-pulls the floating tags and refreshes digests."""
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    pull_count_before = sum(
        1 for c in in_vm_spy.call_args_list if c.args[1:3] == ("docker", "pull")
    )

    firecrawl_cmd.up(upgrade=True, config_path=with_coder)
    pull_count_after = sum(1 for c in in_vm_spy.call_args_list if c.args[1:3] == ("docker", "pull"))
    # Five new pulls (one per image) on upgrade.
    assert pull_count_after - pull_count_before == 5


# ---------- down ----------


def test_down_removes_agent_passthrough_wiring(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.rm", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    firecrawl_cmd.down(config_path=with_coder)

    doc = hermes_config.load(hermes_config.config_path(cfg, "coder"))
    passthrough = (doc.get("terminal") or {}).get("env_passthrough") or []
    assert "FIRECRAWL_API_KEY" not in passthrough
    assert "FIRECRAWL_API_URL" not in passthrough


def test_down_keeps_compose_entries(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """`down` is reversible — compose entries stay so a subsequent
    `up` can bring the same digests back without re-resolving.
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.rm", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    firecrawl_cmd.down(config_path=with_coder)

    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    assert any(s.startswith(FIRECRAWL_SERVICE_PREFIX) for s in services)
    # State file still present (digests preserved for next up).
    assert "images" in firecrawl_cmd.read_state(cfg)


def test_down_refuses_when_not_installed(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not installed"):
        firecrawl_cmd.down(config_path=saved_config)


# ---------- destroy ----------


def test_destroy_strips_everything(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.rm", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    firecrawl_cmd.destroy(yes=True, config_path=with_coder)

    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    assert not any(s.startswith(FIRECRAWL_SERVICE_PREFIX) for s in services)
    assert firecrawl_cmd.read_state(cfg) == {}

    env_text = (cfg.paths.host_data_dir / ".env").read_text(encoding="utf-8")
    assert "FIRECRAWL_API_KEY" not in env_text
    assert "FIRECRAWL_API_URL" not in env_text
    assert "FIRECRAWL_BULL_AUTH_KEY" not in env_text


def test_destroy_aborts_without_yes(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    firecrawl_cmd.up(config_path=with_coder)

    with pytest.raises(UserError, match="aborted by user"):
        firecrawl_cmd.destroy(yes=False, config_path=with_coder)


# ---------- agents create auto-wiring ----------


def test_agents_create_auto_wires_when_firecrawl_is_up(
    saved_config: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """A Hermes agent created AFTER `firecrawl up` should pick up the
    passthrough automatically — no second user step needed.
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    # Bring firecrawl up first, with no agents yet.
    firecrawl_cmd.up(config_path=saved_config)
    # Now create a fresh Hermes agent.
    create(name="newbie", agent_type="hermes", config_path=saved_config)

    doc = hermes_config.load(hermes_config.config_path(cfg, "newbie"))
    passthrough = doc["terminal"]["env_passthrough"]
    assert "FIRECRAWL_API_KEY" in passthrough
    assert "FIRECRAWL_API_URL" in passthrough


def test_agents_create_skips_wiring_when_firecrawl_is_down(
    saved_config: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """No firecrawl installed → no passthrough wired (clean baseline)."""
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="solo", agent_type="hermes", config_path=saved_config)

    config_path_file = hermes_config.config_path(cfg, "solo")
    if config_path_file.exists():
        doc = hermes_config.load(config_path_file)
        passthrough = (doc.get("terminal") or {}).get("env_passthrough") or []
        assert "FIRECRAWL_API_KEY" not in passthrough


# ---------- compose fragment shape ----------


def test_firecrawl_fragments_use_only_digest_pinned_images() -> None:
    """The compose fragments must never reference a floating tag —
    every image must be ``<repo>@sha256:…``. Reflects the toolkit's
    locked "no :latest outside canary" rule.
    """
    digest_images = {
        "api": DIGEST_FOR["ghcr.io/firecrawl/firecrawl:linux-amd64"],
        "playwright": DIGEST_FOR["ghcr.io/firecrawl/playwright-service:latest"],
        "postgres": DIGEST_FOR["ghcr.io/firecrawl/nuq-postgres:latest"],
        "redis": DIGEST_FOR["redis:alpine"],
        "rabbitmq": DIGEST_FOR["rabbitmq:3-management"],
    }
    fragments = compose.firecrawl_service_fragments(images=digest_images)
    for svc_name, fragment in fragments.items():
        assert "@sha256:" in fragment["image"], f"{svc_name} not digest-pinned"


def test_firecrawl_postgres_bind_mounts_data_dir() -> None:
    """Postgres data must live on the host-mount (covered by duplicity)
    via a bind mount, NOT a docker-managed volume.
    """
    digest_images = {
        "api": DIGEST_FOR["ghcr.io/firecrawl/firecrawl:linux-amd64"],
        "playwright": DIGEST_FOR["ghcr.io/firecrawl/playwright-service:latest"],
        "postgres": DIGEST_FOR["ghcr.io/firecrawl/nuq-postgres:latest"],
        "redis": DIGEST_FOR["redis:alpine"],
        "rabbitmq": DIGEST_FOR["rabbitmq:3-management"],
    }
    fragments = compose.firecrawl_service_fragments(images=digest_images)
    volumes = fragments["firecrawl-postgres"]["volumes"]
    assert any("./firecrawl/postgres" in v for v in volumes)


def test_firecrawl_api_not_exposed_on_host() -> None:
    """The api service must only `expose:` the internal port, never
    publish it via `ports:`. Reach it from sibling containers via the
    docker network.
    """
    digest_images = {
        "api": DIGEST_FOR["ghcr.io/firecrawl/firecrawl:linux-amd64"],
        "playwright": DIGEST_FOR["ghcr.io/firecrawl/playwright-service:latest"],
        "postgres": DIGEST_FOR["ghcr.io/firecrawl/nuq-postgres:latest"],
        "redis": DIGEST_FOR["redis:alpine"],
        "rabbitmq": DIGEST_FOR["rabbitmq:3-management"],
    }
    fragments = compose.firecrawl_service_fragments(images=digest_images)
    api = fragments["firecrawl-api"]
    assert "ports" not in api
    assert "3002" in api["expose"]


# ---------- status ----------


def test_status_when_not_installed(saved_config: Path) -> None:
    info = firecrawl_cmd.status(config_path=saved_config)
    assert info["installed"] is False
    assert info["running"] is False


def test_status_reports_wired_agents(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.ps", return_value=_ok(""))
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    info = firecrawl_cmd.status(config_path=with_coder)
    assert info["installed"] is True
    assert "coder" in info["wired_agents"]


def test_status_includes_containers_and_api_health(
    with_coder: Path, cfg: AgentsConfig, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> None:
    """The richer status surface should include per-container state +
    api health, even when the simulator returns generic ok for
    docker stats / nc probes.
    """
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agents_cli.compose_runner.ps", return_value=_ok(""))
    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=_firecrawl_runner(vm_fs_mocks))

    firecrawl_cmd.up(config_path=with_coder)
    info = firecrawl_cmd.status(config_path=with_coder)
    assert "containers" in info
    # All five firecrawl services accounted for, even if the simulator
    # returned no stats per-container (default `state: "absent"`).
    expected_services = {
        "firecrawl-api",
        "firecrawl-playwright",
        "firecrawl-redis",
        "firecrawl-rabbitmq",
        "firecrawl-postgres",
    }
    assert expected_services.issubset(info["containers"].keys())
    assert info["api_health"] in {"healthy", "unhealthy", "unknown"}
    assert "rate_limits" in info
    assert "queues" in info


def test_queue_stats_parses_rabbitmqctl_and_redis(cfg: AgentsConfig, mocker: Any) -> None:
    """Direct unit test of `_queue_stats` parsing logic — feed it
    realistic rabbitmqctl + redis-cli outputs and verify the dict
    shape.
    """

    # Live `rabbitmqctl list_queues --quiet` is TSV.
    rmq_output = (
        "extract.jobs\t0\t0\t0\t1\n"
        "nuq.queue_scrape.prefetch\t0\t0\t0\t0\n"
        "extract.dlq\t2\t1\t1\t1\n"
        "nuq.queue_crawl_finished.prefetch\t0\t0\t0\t0\n"
    )

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        # `rabbitmqctl list_queues ...`
        if "rabbitmqctl" in cmd:
            return _ok(rmq_output)
        # `redis-cli LLEN bull:{<queue>}:<state>`
        if "redis-cli" in cmd and "LLEN" in cmd:
            key = cmd[-1]
            # Fixture: precrawlQueue has 5 completed jobs, others empty.
            if key == "bull:{precrawlQueue}:completed":
                return _ok("5\n")
            return _ok("0\n")
        # `redis-cli ZCARD ...` fallback path
        if "redis-cli" in cmd and "ZCARD" in cmd:
            return _ok("0\n")
        return _ok("")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)

    stats = firecrawl_cmd._queue_stats(cfg)

    # RabbitMQ: prefetch queues are filtered out of the human-relevant list.
    rmq_names = [q["name"] for q in stats["rabbitmq"]]
    assert "extract.jobs" in rmq_names
    assert "extract.dlq" in rmq_names
    assert all("prefetch" not in n for n in rmq_names)

    dlq = next(q for q in stats["rabbitmq"] if q["name"] == "extract.dlq")
    assert dlq == {
        "name": "extract.dlq",
        "total": 2,
        "pending": 1,
        "in_flight": 1,
        "consumers": 1,
    }

    # Bull: all four queues listed; precrawlQueue has completed=5.
    bull_names = [q["name"] for q in stats["bull"]]
    assert bull_names == [
        "precrawlQueue",
        "billingQueue",
        "generateLlmsTxtQueue",
        "deepResearchQueue",
    ]
    precrawl = next(q for q in stats["bull"] if q["name"] == "precrawlQueue")
    assert precrawl["completed"] == 5
    assert precrawl["active"] == 0


@pytest.mark.parametrize(
    "worst_mem_pct,expected_level",
    [
        (45.0, "ok"),
        (79.9, "ok"),
        (80.0, "warn"),
        (90.0, "warn"),
        (95.0, "critical"),
        (99.9, "critical"),
    ],
)
def test_saturation_classifies_worst_container(worst_mem_pct: float, expected_level: str) -> None:
    """The classifier picks the WORST container's mem_pct as the signal.
    Fixture: api gets the test value, others are intentionally lower.
    """
    containers = {
        "firecrawl-api": {"state": "running", "mem_pct": f"{worst_mem_pct}%"},
        "firecrawl-playwright": {"state": "running", "mem_pct": "10%"},
        "firecrawl-redis": {"state": "running", "mem_pct": "5%"},
    }
    sat = firecrawl_cmd._saturation(containers)
    assert sat["level"] == expected_level
    assert sat["worst_service"] == "firecrawl-api"
    assert sat["worst_mem_pct"] == worst_mem_pct


def test_saturation_handles_missing_mem_pct() -> None:
    """Containers with no mem_pct field (e.g. simulator returning bare
    state) shouldn't crash the classifier — they get treated as 0.
    """
    containers = {"firecrawl-api": {"state": "absent"}}
    sat = firecrawl_cmd._saturation(containers)
    assert sat["level"] == "unknown"


def test_queue_stats_handles_rabbitmq_unavailable(cfg: AgentsConfig, mocker: Any) -> None:
    """If rabbitmqctl can't be exec'd (container down, image lacks the
    tool, etc.), we still return a well-formed dict with empty
    rabbitmq list and best-effort Bull stats.
    """

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "rabbitmqctl" in cmd:
            return MpResult(stdout="", stderr="not running", returncode=1)
        if "redis-cli" in cmd:
            return _ok("0\n")
        return _ok("")

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    stats = firecrawl_cmd._queue_stats(cfg)
    assert stats["rabbitmq"] == []
    # Bull still gets a slot per queue even when redis returns 0.
    assert len(stats["bull"]) == 4
