"""upgrades.py: state machine, journal resume, rollback paths."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from agents_cli import compose, upgrades
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import create
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.journal import Journal
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
        defaults=DefaultsConfig(
            hermes_image_tag="v1.0.0",
            openclaw_image_tag="0.1.0",
        ),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


@pytest.fixture
def with_coder(cfg: AgentsConfig, saved_config: Path, mocker) -> Path:
    """One Hermes agent named 'coder' in compose."""
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    return saved_config


def _stub_happy_upgrade(mocker, cfg: AgentsConfig | None = None) -> dict:
    """Every multipass/docker call returns success; doctor passes.

    Re-installs the VM filesystem simulation so that the new compose
    mutation path (host-temp -> multipass transfer -> VM-side mv)
    actually moves the file in the test environment.
    """
    from tests.conftest import install_vm_fs_simulation

    if cfg is not None:
        install_vm_fs_simulation(mocker, cfg)
    return {
        "stop": mocker.patch("agents_cli.mp.stop_vm", return_value=_ok()),
        "start": mocker.patch("agents_cli.mp.start_vm", return_value=_ok()),
        "snapshot": mocker.patch("agents_cli.mp.snapshot", return_value=_ok()),
        "info_vm": mocker.patch(
            "agents_cli.mp.info_vm",
            return_value=_ok(json.dumps({"info": {"agents": {"snapshots": {}}}})),
        ),
        "pull": mocker.patch("agents_cli.compose_runner.pull", return_value=_ok()),
        "inspect": mocker.patch(
            "agents_cli.compose_runner.docker_image_inspect", return_value=_ok()
        ),
        "compose_up": mocker.patch("agents_cli.compose_runner.up", return_value=_ok()),
        "doctor": mocker.patch(
            "agents_cli.ops.doctor", return_value=type("R", (), {"failed": False})()
        ),
    }


# ---------- happy path ----------


def test_upgrade_happy_path_runs_every_phase(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    mocks = _stub_happy_upgrade(mocker, cfg)
    outcome = upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)
    assert outcome.old_tag == "v1.0.0"
    assert outcome.new_tag == "v1.0.1"
    assert outcome.host_snapshot is not None
    assert outcome.vm_snapshot is not None
    # All phases executed: stop -> snapshot -> start -> tag write -> pull -> inspect -> up -> doctor
    mocks["snapshot"].assert_called_once()
    mocks["pull"].assert_called_once()
    mocks["inspect"].assert_called_once()
    mocks["compose_up"].assert_called_once()


def test_upgrade_writes_new_tag_to_compose(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    _stub_happy_upgrade(mocker, cfg)
    upgrades.upgrade("coder", tag="v9.9.9", config_path=with_coder)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["image"].endswith(":v9.9.9")


def test_upgrade_completes_journal_on_success(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    _stub_happy_upgrade(mocker, cfg)
    upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)
    assert Journal.find_resumable(cfg.paths.host_data_dir, "upgrade", "coder") is None


def test_upgrade_with_no_snapshot_skips_both_snapshots(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    mocks = _stub_happy_upgrade(mocker, cfg)
    outcome = upgrades.upgrade("coder", tag="v1.0.1", no_snapshot=True, config_path=with_coder)
    assert outcome.host_snapshot is None
    assert outcome.vm_snapshot is None
    mocks["snapshot"].assert_not_called()


def test_upgrade_to_latest_resolves_upstream_and_writes_default(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """--latest pulls the upstream tag, pins it on the agent, AND
    refreshes the toolkit-wide default so future `agents create`
    starts on the latest baseline. Without the default refresh, new
    agents would silently re-anchor to the (now-stale) `vm init`-time
    tag.
    """
    _stub_happy_upgrade(mocker, cfg)
    mocker.patch("agents_cli.outdated_cmd.fetch_latest_tag", return_value="v9.9.9")

    upgrades.upgrade("coder", to_latest=True, config_path=with_coder)

    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["image"].endswith(":v9.9.9")
    reloaded = cfg_mod.load(with_coder)
    assert reloaded.defaults.hermes_image_tag == "v9.9.9"


def test_upgrade_to_latest_refuses_when_github_unreachable(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Network fetch failure must NOT silently fall back to the current
    pinned tag — that would be a no-op disguised as a successful
    upgrade. Caller asked for "latest", give them an error if we can't
    determine what that is.
    """
    _stub_happy_upgrade(mocker, cfg)
    mocker.patch("agents_cli.outdated_cmd.fetch_latest_tag", return_value=None)

    with pytest.raises(UserError, match="could not resolve latest"):
        upgrades.upgrade("coder", to_latest=True, config_path=with_coder)


# ---------- failure paths ----------


def test_upgrade_reverts_tag_when_pull_fails(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    _stub_happy_upgrade(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.pull", return_value=_ok())
    mocker.patch(
        "agents_cli.compose_runner.docker_image_inspect",
        return_value=_fail("manifest unknown"),
    )
    with pytest.raises(RuntimeFailure, match="docker image inspect"):
        upgrades.upgrade("coder", tag="v9.9.9", config_path=with_coder)
    # Tag reverted
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["image"].endswith(":v1.0.0")


def test_upgrade_reverts_when_doctor_fails(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    _stub_happy_upgrade(mocker, cfg)
    from agents_cli.errors import HealthCheckFailure

    mocker.patch("agents_cli.ops.doctor", side_effect=HealthCheckFailure("bad"))
    with pytest.raises(HealthCheckFailure):
        upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["image"].endswith(":v1.0.0")


def test_upgrade_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        upgrades.upgrade("ghost", tag="v1.0.1", config_path=saved_config)


def test_upgrade_refuses_when_current_image_has_no_tag(with_coder: Path, cfg: AgentsConfig) -> None:
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    doc["services"]["hermes-coder"]["image"] = "nousresearch/hermes-agent"
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )
    with pytest.raises(UserError, match="no tag"):
        upgrades.upgrade("coder", tag="v2.0.0", config_path=with_coder)


# ---------- journal resume ----------


def test_upgrade_resume_from_post_snapshot_phase(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """If the journal already has host_data_snapshot + vm_snapshot done,
    re-running upgrade must skip the snapshot calls and pick up at tag_pinned.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    j = Journal.begin(cfg.paths.host_data_dir, "upgrade", "coder", upgrades.UPGRADE_PHASES)
    j.advance("host_data_snapshot")
    j.advance("vm_snapshot")

    upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)

    mocks["snapshot"].assert_not_called()
    mocks["stop"].assert_not_called()


# ---------- rollback ----------


def test_rollback_restores_from_latest_archive(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    # Hand-craft a snapshot archive
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(exist_ok=True)
    snapshot_data = cfg.paths.host_data_dir / "snapshot-data"
    snapshot_data.mkdir()
    (snapshot_data / "marker.txt").write_text("from-snapshot", encoding="utf-8")
    archive = upgrades_dir / "coder-20260512T120000Z.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(snapshot_data / "marker.txt", arcname="coder/marker.txt")
    # Stub VM ops + skip confirm
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": {}}}})),
    )
    mocker.patch("agents_cli.mp.stop_vm", return_value=_ok())
    mocker.patch("agents_cli.mp.start_vm", return_value=_ok())

    upgrades.rollback("coder", config_path=with_coder)
    # Marker file from the archive landed at <data-dir>/coder/marker.txt
    assert (cfg.paths.host_data_dir / "coder" / "marker.txt").read_text() == "from-snapshot"


def test_rollback_aborts_without_confirmation(with_coder: Path, mocker, cfg: AgentsConfig) -> None:
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(exist_ok=True)
    (upgrades_dir / "coder-20260512T120000Z.tar.gz").write_bytes(b"")
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    with pytest.raises(UserError, match="aborted"):
        upgrades.rollback("coder", config_path=with_coder)


def test_rollback_refuses_when_no_archive(with_coder: Path) -> None:
    with pytest.raises(UserError, match="no host snapshot"):
        upgrades.rollback("coder", config_path=with_coder)


# ---------- snapshot name collision ----------


def test_vm_snapshot_uses_monotonic_suffix_on_collision(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    # The unique_snapshot_name path is invoked via vm_mod._unique_snapshot_name
    # and we already have a test for that in test_vm. Here we verify that
    # _vm_snapshot uses it.
    base_existing = "pre-upgrade-coder-20260512T120000Z"
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": {base_existing: {}}}}})),
    )
    mocker.patch("agents_cli.mp.stop_vm", return_value=_ok())
    mocker.patch("agents_cli.mp.start_vm", return_value=_ok())
    snap_spy = mocker.patch("agents_cli.mp.snapshot", return_value=_ok())
    # Force the timestamp portion so we hit the collision deterministically.
    mocker.patch("agents_cli.upgrades._utc_stamp", return_value="20260512T120000Z")
    name = upgrades._vm_snapshot(cfg, "coder")
    assert name == f"{base_existing}-2"
    snap_spy.assert_called_once()


def test_vm_snapshot_waits_for_ssh_on_libvirt_after_start(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Regression for the 2026-05-22 upgrade-on-libvirt failure:
    ``_vm_snapshot`` must NOT return until the VM is back online
    after the snapshot stop/start cycle. The next phase (``tag_pinned``)
    does compose-write-via-scp which races DHCP otherwise — error
    surfaces as "could not get IP for libvirt VM".

    Routing through ``vm_mod.take_snapshot`` gets us:
    wait_for_stopped after stop (so the snapshot doesn't race a
    still-shutting-down VM with active virtiofs), rc check on the
    snapshot itself (libvirt's snapshot-create-as is invoked with
    check=False so silent failures would otherwise let the journal
    advance past a snapshot that doesn't actually exist), and
    wait_for_ssh after start.
    """
    cfg.vm.backend = "libvirt"
    cfg_mod.save(cfg, with_coder)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"state": "Running", "snapshots": {}}}})),
    )
    mocker.patch("agents_cli.mp.stop_vm", return_value=_ok())
    mocker.patch("agents_cli.mp.start_vm", return_value=_ok())
    mocker.patch("agents_cli.mp.snapshot", return_value=_ok())
    wait_ssh = mocker.patch("agents_cli.backends.libvirt.wait_for_ssh")
    # `_vm_running` polling inside take_snapshot's wait_for_stopped
    # would loop forever on a non-mocked info_vm — short-circuit it.
    mocker.patch("agents_cli.vm._wait_for_vm_stopped")

    upgrades._vm_snapshot(cfg, "coder")

    wait_ssh.assert_called()
    assert wait_ssh.call_args.args[0] == cfg.vm.name


# ---------- pull ----------


def test_pull_without_name_pulls_all(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agents_cli.compose_runner.pull", return_value=_ok())
    upgrades.pull(config_path=with_coder)
    spy.assert_called_once()
    # No service name -> pull all
    args, _ = spy.call_args
    assert len(args) == 1


def test_pull_with_name_pulls_one(with_coder: Path, mocker) -> None:
    spy = mocker.patch("agents_cli.compose_runner.pull", return_value=_ok())
    upgrades.pull("coder", config_path=with_coder)
    args, _ = spy.call_args
    assert "hermes-coder" in args
