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
    # ``_host_data_snapshot`` does a sudo-tar inside the VM and then
    # transfers the archive out (see the long comment in upgrades.py).
    # The simulator doesn't model that file motion — the upgrade-
    # state-machine tests below only care that the phase ran, so stub
    # the helper to write an empty placeholder archive at the expected
    # host path. Dedicated tests below exercise the real tar+transfer
    # argv shape against the multipass + libvirt backends.

    def _fake_snap(_cfg, _name):
        archives = _cfg.paths.host_data_dir / ".upgrades"
        archives.mkdir(parents=True, exist_ok=True)
        out = archives / f"{_name}-stub.tar.zst"
        out.write_bytes(b"")
        return out

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
        "host_snap": mocker.patch(
            "agents_cli.upgrades._host_data_snapshot", side_effect=_fake_snap
        ),
        # Bare ``agents upgrade`` now implicitly fetches the latest
        # upstream tag. Default to returning the current pin (v1.0.0
        # in the cfg fixture) so tests that exercise the bare form
        # without specifying their own intent get a clean no-op rather
        # than hitting the real GitHub API.
        "fetch_latest_tag": mocker.patch(
            "agents_cli.outdated_cmd.fetch_latest_tag", return_value="v1.0.0"
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


def test_upgrade_resume_after_tag_pinned_completes_original_upgrade(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Regression for the resume-false-no-op bug: a SIGKILL after
    tag_pinned leaves the compose pin already mutated to the NEW tag.
    A retry that reads old_tag from the pin concludes "already at vNEW
    — nothing to do" while the container still runs vOLD. The journal
    records {old_tag, new_tag} at begin; the retry must find it BEFORE
    the no-op short-circuit and complete the ORIGINAL upgrade: recreate
    runs, no noop emit, and the outcome reports the true old -> new.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    # Even if upstream released something newer mid-crash, resume must
    # not retarget — and must not even ask.
    mocks["fetch_latest_tag"].return_value = "v9.9.9"

    j = Journal.begin(
        cfg.paths.host_data_dir,
        "upgrade",
        "coder",
        upgrades.UPGRADE_PHASES,
        meta={"old_tag": "v1.0.0", "new_tag": "v2.0.0"},
    )
    j.advance("host_data_snapshot")
    j.advance("vm_snapshot")
    j.advance("tag_pinned")
    # Simulate the dead run's tag_pinned phase: pin already rewritten.
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    doc["services"]["hermes-coder"]["image"] = f"{compose.HERMES_IMAGE_BASE}:v2.0.0"
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )
    emit_spy = mocker.spy(upgrades.ui, "emit")

    outcome = upgrades.upgrade("coder", config_path=with_coder)  # bare retry

    assert outcome.old_tag == "v1.0.0"
    assert outcome.new_tag == "v2.0.0"
    # The remaining phases actually ran — no false no-op.
    mocks["pull"].assert_called_once()
    mocks["compose_up"].assert_called_once()
    noop_emits = [
        c for c in emit_spy.call_args_list if isinstance(c.args[0], dict) and c.args[0].get("noop")
    ]
    assert noop_emits == [], f"resume emitted a false no-op: {noop_emits}"
    # Journal is gone after the resumed upgrade completes.
    assert Journal.find_resumable(cfg.paths.host_data_dir, "upgrade", "coder") is None


def test_upgrade_resume_does_not_refetch_latest_or_bump_defaults(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Resume completes the recorded upgrade — it must not re-resolve
    upstream (a vNEWER released mid-crash would silently retarget the
    resume) and must not rewrite defaults.<type>_image_tag (the resumed
    target was never resolved as "latest" by THIS invocation)."""
    mocks = _stub_happy_upgrade(mocker, cfg)
    mocks["fetch_latest_tag"].return_value = "v9.9.9"
    j = Journal.begin(
        cfg.paths.host_data_dir,
        "upgrade",
        "coder",
        upgrades.UPGRADE_PHASES,
        meta={"old_tag": "v1.0.0", "new_tag": "v2.0.0"},
    )
    j.advance("host_data_snapshot")
    j.advance("vm_snapshot")

    outcome = upgrades.upgrade("coder", config_path=with_coder)

    assert outcome.new_tag == "v2.0.0"
    mocks["fetch_latest_tag"].assert_not_called()
    reloaded = cfg_mod.load(with_coder)
    assert reloaded.defaults.hermes_image_tag == "v1.0.0"


def test_upgrade_adopts_journal_tags_found_under_the_lock(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Regression for the unlocked-peek/locked-find disagreement: while
    this invocation resolved v3 upstream and waited for the lock, a
    parallel invocation began a v1->v2 upgrade and died after
    tag_pinned. Keeping the locally resolved v3 tags would skip the pin
    (tag_pinned already done), pull v3, recreate from the compose file
    still pinned to v2, and report v3 as deployed. The journal found
    under the lock is authoritative: the run must complete v2, emit
    v1->v2, leave the defaults alone, and never pin or verify v3.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    mocks["fetch_latest_tag"].return_value = "v3.0.0"

    # The parallel invocation's journal: begun with tag meta, dead
    # right after tag_pinned — the compose pin already reads v2.0.0.
    j = Journal.begin(
        cfg.paths.host_data_dir,
        "upgrade",
        "coder",
        upgrades.UPGRADE_PHASES,
        meta={"old_tag": "v1.0.0", "new_tag": "v2.0.0"},
    )
    j.advance("host_data_snapshot")
    j.advance("vm_snapshot")
    j.advance("tag_pinned")
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    doc["services"]["hermes-coder"]["image"] = f"{compose.HERMES_IMAGE_BASE}:v2.0.0"
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )

    # Simulate the race: the journal "does not exist yet" at the
    # unlocked peek but IS found by the re-find under the lock.
    real_find = Journal.find_resumable
    finds = {"n": 0}

    def raced_find(data_dir: Path, op: str, name: str) -> Journal | None:
        finds["n"] += 1
        if finds["n"] == 1:
            return None
        return real_find(data_dir, op, name)

    mocker.patch.object(Journal, "find_resumable", side_effect=raced_find)
    set_tag_spy = mocker.spy(upgrades, "_set_image_tag")
    emit_spy = mocker.spy(upgrades.ui, "emit")

    outcome = upgrades.upgrade("coder", config_path=with_coder)  # bare form resolves v3

    # The run completed the journal's v1->v2 upgrade, not the local v3.
    assert outcome.old_tag == "v1.0.0"
    assert outcome.new_tag == "v2.0.0"
    # tag_pinned was already done — no re-pin, and certainly not to v3.
    assert set_tag_spy.call_count == 0
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["image"].endswith(":v2.0.0")
    # The pulled-image verification targets the adopted tag, not v3.
    mocks["inspect"].assert_called_once()
    assert mocks["inspect"].call_args.args[1] == f"{compose.HERMES_IMAGE_BASE}:v2.0.0"
    # Upstream was consulted exactly once (the pre-lock resolution);
    # adoption must not re-fetch.
    mocks["fetch_latest_tag"].assert_called_once()
    # The locally resolved v3 was never proven out — defaults stay put.
    reloaded = cfg_mod.load(with_coder)
    assert reloaded.defaults.hermes_image_tag == "v1.0.0"
    # The summary reports the adopted upgrade; v3 appears nowhere.
    final = emit_spy.call_args_list[-1].args[0]
    assert (final["old_tag"], final["new_tag"]) == ("v1.0.0", "v2.0.0")
    assert not any("v3.0.0" in json.dumps(c.args[0]) for c in emit_spy.call_args_list), (
        "v3 leaked into an emit"
    )
    # The adopted journal completed.
    assert list((cfg.paths.host_data_dir / ".journal").glob("upgrade-coder-*.json")) == []


# ---------- progress narration ----------


def _progress_lines(spy) -> list[str]:
    return [c.args[0] for c in spy.call_args_list]


def test_upgrade_narrates_every_phase_with_stable_numbering(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """The VM snapshot and image pull run for minutes; a silent terminal
    reads as a hang. A full Hermes upgrade with snapshots plans all 7
    phases and numbers each progress line [i/7].
    """
    _stub_happy_upgrade(mocker, cfg)
    spy = mocker.spy(upgrades.ui, "progress")

    upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)

    lines = _progress_lines(spy)
    assert any("upgrading 'coder': v1.0.0 -> v1.0.1 (7 phases)" in ln for ln in lines)
    for i in range(1, 8):
        assert any(ln.startswith(f"[{i}/7]") for ln in lines), f"no [{i}/7] line in {lines}"
    # The slow phases carry a duration hint / artifact.
    assert any("large images take minutes" in ln for ln in lines)
    assert any("all agents pause" in ln for ln in lines)


def test_upgrade_no_snapshot_renumbers_progress_over_planned_phases(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """--no-snapshot drops two phases from the plan; numbering must
    follow ([i/5]) so the narration never promises skipped work."""
    _stub_happy_upgrade(mocker, cfg)
    spy = mocker.spy(upgrades.ui, "progress")

    upgrades.upgrade("coder", tag="v1.0.1", no_snapshot=True, config_path=with_coder)

    lines = _progress_lines(spy)
    assert any("(5 phases)" in ln for ln in lines)
    assert any(ln.startswith("[5/5]") for ln in lines)
    assert not any("/7]" in ln for ln in lines)
    assert not any("snapshot" in ln for ln in lines)


def test_upgrade_resume_announces_already_done_phases(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    _stub_happy_upgrade(mocker, cfg)
    j = Journal.begin(cfg.paths.host_data_dir, "upgrade", "coder", upgrades.UPGRADE_PHASES)
    j.advance("host_data_snapshot")
    j.advance("vm_snapshot")
    spy = mocker.spy(upgrades.ui, "progress")

    upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)

    lines = _progress_lines(spy)
    assert any(
        "resuming interrupted upgrade" in ln and "host_data_snapshot" in ln and "vm_snapshot" in ln
        for ln in lines
    )
    # The done phases are skipped, so their [1/7]/[2/7] start lines
    # must not reappear — first executed phase is tag_pinned at [3/7].
    assert not any(ln.startswith("[1/7]") or ln.startswith("[2/7]") for ln in lines)
    assert any(ln.startswith("[3/7]") for ln in lines)


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


def _write_rollback_archive(cfg: AgentsConfig, name: str, stamp: str = "20260512T120000Z") -> Path:
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(exist_ok=True)
    marker = cfg.paths.host_data_dir / f"{name}-marker.txt"
    marker.write_text("from-snapshot", encoding="utf-8")
    archive = upgrades_dir / f"{name}-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(marker, arcname=f"{name}/marker.txt")
    return archive


def test_rollback_locks_stops_restores_and_recreates_in_order(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Rollback is a compose/data mutation: it must hold the flock,
    stop the live container BEFORE extracting over its SQLite/WAL
    files, delegate the VM restore to vm_mod.restore_snapshot (which
    owns stop/wait/rc-check/restart — the raw mp.stop_vm/restore/
    start_vm calls it replaces never checked mp.restore's result), and
    recreate the container from the restored compose at the end."""
    _write_rollback_archive(cfg, "coder")
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(
            json.dumps(
                {"info": {"agents": {"snapshots": {"pre-upgrade-coder-20260512T120000Z": {}}}}}
            )
        ),
    )
    order: list[str] = []
    mocker.patch(
        "agents_cli.compose_runner.stop",
        side_effect=lambda *a, **k: order.append("stop") or _ok(),
    )
    mocker.patch(
        "agents_cli.upgrades._restore_host_snapshot",
        side_effect=lambda *a, **k: order.append("restore_host"),
    )
    vm_restore = mocker.patch(
        "agents_cli.vm.restore_snapshot",
        side_effect=lambda *a, **k: order.append("restore_vm"),
    )
    mocker.patch(
        "agents_cli.compose_runner.up",
        side_effect=lambda *a, **k: order.append("up") or _ok(),
    )
    raw_stop = mocker.patch("agents_cli.mp.stop_vm", return_value=_ok())
    raw_restore = mocker.patch("agents_cli.mp.restore", return_value=_ok())
    raw_start = mocker.patch("agents_cli.mp.start_vm", return_value=_ok())
    lock_spy = mocker.spy(upgrades.compose, "acquire_lock")

    upgrades.rollback("coder", config_path=with_coder)

    assert order == ["stop", "restore_host", "restore_vm", "up"]
    lock_spy.assert_called_once_with(cfg.paths.host_data_dir)
    vm_restore.assert_called_once_with(
        "pre-upgrade-coder-20260512T120000Z",
        destructive=True,
        yes=True,
        config_path=with_coder,
        # emit=False: rollback owns the command's single stdout object;
        # the restore helper must not land a second one mid-rollback.
        emit=False,
    )
    # The raw un-orchestrated VM calls must be gone.
    raw_stop.assert_not_called()
    raw_restore.assert_not_called()
    raw_start.assert_not_called()


def test_rollback_restarts_service_when_host_restore_fails(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Regression: once the live container is stopped, a failure in the
    restore steps (corrupt archive, extraction error) used to raise
    before the final ``compose_runner.up`` — turning a failed rollback
    ATTEMPT into an outage. The service must be brought back up
    best-effort and the ORIGINAL error must propagate."""
    _write_rollback_archive(cfg, "coder")
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": {}}}})),
    )
    order: list[str] = []
    mocker.patch(
        "agents_cli.compose_runner.stop",
        side_effect=lambda *a, **k: order.append("stop") or _ok(),
    )
    mocker.patch(
        "agents_cli.upgrades._restore_host_snapshot",
        side_effect=RuntimeFailure("archive is corrupt"),
    )
    up = mocker.patch(
        "agents_cli.compose_runner.up",
        side_effect=lambda *a, **k: order.append("up") or _ok(),
    )
    warn_spy = mocker.spy(upgrades.ui, "warn")

    with pytest.raises(RuntimeFailure, match="archive is corrupt"):
        upgrades.rollback("coder", config_path=with_coder)

    # The recovery up ran for the stopped service, after the stop.
    assert order == ["stop", "up"]
    up.assert_called_once()
    assert up.call_args.args[1] == "hermes-coder"
    assert any("restarting" in c.args[0] for c in warn_spy.call_args_list)


def test_rollback_restarts_service_when_vm_restore_fails(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Same guarantee for the later restore step: the host data already
    extracted fine, but the VM snapshot restore blows up before the
    final up. The service was stopped for the extraction — bring it
    back up and re-raise the VM error."""
    _write_rollback_archive(cfg, "coder")
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(
            json.dumps(
                {"info": {"agents": {"snapshots": {"pre-upgrade-coder-20260512T120000Z": {}}}}}
            )
        ),
    )
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agents_cli.upgrades._restore_host_snapshot")
    mocker.patch(
        "agents_cli.vm.restore_snapshot",
        side_effect=RuntimeFailure("snapshot restore blew up"),
    )
    up = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    with pytest.raises(RuntimeFailure, match="snapshot restore blew up"):
        upgrades.rollback("coder", config_path=with_coder)

    up.assert_called_once()
    assert up.call_args.args[1] == "hermes-coder"


def test_rollback_secondary_up_failure_only_warns(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """If the best-effort recovery up ALSO fails (e.g. the VM is the
    problem), that failure must only warn — the original restore error
    is the one the user needs to see."""
    _write_rollback_archive(cfg, "coder")
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": {}}}})),
    )
    mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    mocker.patch(
        "agents_cli.upgrades._restore_host_snapshot",
        side_effect=RuntimeFailure("archive is corrupt"),
    )
    mocker.patch("agents_cli.compose_runner.up", side_effect=RuntimeError("VM unreachable"))
    warn_spy = mocker.spy(upgrades.ui, "warn")

    with pytest.raises(RuntimeFailure, match="archive is corrupt"):
        upgrades.rollback("coder", config_path=with_coder)

    assert any("could not restart hermes-coder" in c.args[0] for c in warn_spy.call_args_list)


def test_rollback_of_removed_agent_skips_stop_and_up_with_warning(
    cfg: AgentsConfig, saved_config: Path, mocker
) -> None:
    """Rolling back an archive of an agent that is no longer in compose
    is legitimate (restore-after-remove). There is no service to stop
    or recreate — warn and restore the data anyway, don't fail."""
    _write_rollback_archive(cfg, "ghost")
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": {}}}})),
    )
    restore_host = mocker.patch("agents_cli.upgrades._restore_host_snapshot")
    stop = mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    up = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    warn_spy = mocker.spy(upgrades.ui, "warn")

    upgrades.rollback("ghost", config_path=saved_config)

    restore_host.assert_called_once()
    stop.assert_not_called()
    up.assert_not_called()
    assert any("not in compose" in c.args[0] for c in warn_spy.call_args_list)


def test_rollback_explicit_snapshot_id_ignores_prefix_colliding_agent(
    cfg: AgentsConfig, saved_config: Path, mocker
) -> None:
    """Agent ``foo``'s candidate listing must not include ``foo-bar``'s
    archives: with the old prefix glob, a shared timestamp suffix made
    the id "ambiguous" (or worse, matched the wrong agent's archive)."""
    foo_archive = _write_rollback_archive(cfg, "foo", "20260512T120000Z")
    _write_rollback_archive(cfg, "foo-bar", "20260512T120000Z")
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": {}}}})),
    )
    restore_host = mocker.patch("agents_cli.upgrades._restore_host_snapshot")

    upgrades.rollback("foo", snapshot_id="20260512T120000Z", config_path=saved_config)

    restore_host.assert_called_once()
    assert restore_host.call_args.args[1] == foo_archive


# ---------- snapshot lookup: strict name matching ----------


def test_latest_host_snapshot_ignores_prefix_colliding_agent(cfg: AgentsConfig) -> None:
    """Agent ``foo``'s lookup must not pick up agent ``foo-bar``'s
    archives. With the old ``foo-*`` glob, ``foo-bar``'s newest beat
    ``foo``'s own in the sorted()[-1] pick (letters sort after digits)."""
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(exist_ok=True)
    (upgrades_dir / "foo-20260101T000000Z.tar.zst").write_bytes(b"")
    (upgrades_dir / "foo-bar-20260201T000000Z.tar.zst").write_bytes(b"")

    got = upgrades._latest_host_snapshot(cfg, "foo")
    assert got is not None
    assert got.name == "foo-20260101T000000Z.tar.zst"

    other = upgrades._latest_host_snapshot(cfg, "foo-bar")
    assert other is not None
    assert other.name == "foo-bar-20260201T000000Z.tar.zst"


def test_latest_host_snapshot_still_accepts_legacy_tar_gz(cfg: AgentsConfig) -> None:
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(exist_ok=True)
    (upgrades_dir / "foo-20260101T000000Z.tar.gz").write_bytes(b"")
    (upgrades_dir / "foo-20260201T000000Z.tar.zst").write_bytes(b"")
    # Stray non-snapshot files with a matching prefix are ignored.
    (upgrades_dir / "foo-notes.txt").write_bytes(b"")

    got = upgrades._latest_host_snapshot(cfg, "foo")
    assert got is not None
    assert got.name == "foo-20260201T000000Z.tar.zst"


def test_latest_vm_snapshot_ignores_prefix_colliding_agent(mocker) -> None:
    snapshots = {
        "pre-upgrade-foo-20260101T000000Z": {},
        "pre-upgrade-foo-bar-20260201T000000Z": {},
    }
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": snapshots}}})),
    )
    assert upgrades._latest_vm_snapshot("agents", "foo") == "pre-upgrade-foo-20260101T000000Z"
    assert (
        upgrades._latest_vm_snapshot("agents", "foo-bar") == "pre-upgrade-foo-bar-20260201T000000Z"
    )


def test_latest_vm_snapshot_accepts_collision_counter_suffix(mocker) -> None:
    """``vm._unique_snapshot_name`` appends ``-2``/``-3`` on name
    collision; the strict matcher must still count those as foo's."""
    snapshots = {
        "pre-upgrade-foo-20260101T000000Z": {},
        "pre-upgrade-foo-20260101T000000Z-2": {},
    }
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"snapshots": snapshots}}})),
    )
    assert upgrades._latest_vm_snapshot("agents", "foo") == "pre-upgrade-foo-20260101T000000Z-2"


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


def test_upgrade_runs_hermes_config_migrate_after_recreate(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """After the container is recreated on the new image, the upgrade
    must call ``hermes config migrate`` so any schema bumps in the
    new version take effect. Without this, gateway features that
    depend on new config fields stay dormant and `hermes config check`
    reports drift the user has to chase manually.
    """
    _stub_happy_upgrade(mocker, cfg)
    # Capture the exec_in_vm calls — config migrate runs through
    # docker compose exec, which goes through mp.exec_in_vm.
    exec_spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)

    migrate_calls = [
        call
        for call in exec_spy.call_args_list
        if "hermes" in call.args and "config" in call.args and "migrate" in call.args
    ]
    assert migrate_calls, (
        "expected `hermes config migrate` to be exec'd after recreate; "
        f"observed exec_in_vm calls: {[c.args for c in exec_spy.call_args_list]}"
    )


def test_upgrade_skips_config_migrate_for_openclaw(
    cfg: AgentsConfig, saved_config: Path, mocker
) -> None:
    """OpenClaw owns its own plugin/config lifecycle (CLAUDE.md
    Hermes-vs-OpenClaw matrix); the toolkit must not call
    ``hermes config migrate`` on an OpenClaw agent — that would be
    invoking the wrong binary entirely.
    """
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="bob", agent_type="openclaw", config_path=saved_config)
    _stub_happy_upgrade(mocker, cfg)
    exec_spy = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    upgrades.upgrade("bob", tag="0.1.1", config_path=saved_config)

    migrate_calls = [
        call
        for call in exec_spy.call_args_list
        if "hermes" in call.args and "config" in call.args and "migrate" in call.args
    ]
    assert not migrate_calls


def test_upgrade_config_migrate_failure_warns_but_continues(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Older Hermes images may not have the ``migrate`` subcommand,
    or the container may be slow to accept exec right after restart.
    Non-zero exit must NOT abort the upgrade — emit a warning and let
    doctor catch any real misconfiguration.
    """
    _stub_happy_upgrade(mocker, cfg)

    def runner(vm_name: str, *cmd: str, check: bool = True):
        if "hermes" in cmd and "migrate" in cmd:
            return MpResult(stdout="", stderr="unknown subcommand: migrate", returncode=2)
        return _ok()

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)

    # No exception expected.
    outcome = upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)
    assert outcome.new_tag == "v1.0.1"


def test_upgrade_to_latest_is_noop_when_already_at_latest(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """``agents upgrade --latest`` while already at the latest tag must
    short-circuit BEFORE touching the VM. ``vm.take_snapshot`` on
    libvirt+virtiofs stops the VM for ~30-60s; doing that for a no-op
    is pure penalty and would make periodic ``upgrade --latest`` calls
    (e.g. from cron) unusable.
    """
    mocker.patch("agents_cli.outdated_cmd.fetch_latest_tag", return_value="v1.0.0")
    mocks = _stub_happy_upgrade(mocker, cfg)
    outcome = upgrades.upgrade("coder", to_latest=True, config_path=with_coder)

    assert outcome.old_tag == outcome.new_tag == "v1.0.0"
    assert outcome.host_snapshot is None
    assert outcome.vm_snapshot is None
    # The expensive phases must not have run.
    mocks["stop"].assert_not_called()
    mocks["snapshot"].assert_not_called()
    mocks["pull"].assert_not_called()
    mocks["compose_up"].assert_not_called()


def test_upgrade_explicit_tag_matching_current_is_noop(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """``--tag X`` when the agent is already at X is also a no-op —
    the user named the target and we're there. Differs from bare
    ``agents upgrade <name>`` (no flags) which IS a force-refresh.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    outcome = upgrades.upgrade("coder", tag="v1.0.0", config_path=with_coder)
    assert outcome.old_tag == outcome.new_tag == "v1.0.0"
    mocks["stop"].assert_not_called()
    mocks["pull"].assert_not_called()


def test_upgrade_bare_call_implicitly_fetches_latest(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Bare ``agents upgrade <name>`` MUST hit the upstream API to
    discover newer versions — matches ``apt upgrade`` / ``brew upgrade``
    convention. Without this, a user typing the bare form would see
    "nothing to do" forever even when newer versions exist upstream,
    silently freezing them on whatever was current at ``vm init``
    time.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    upgrades.upgrade("coder", config_path=with_coder)
    mocks["fetch_latest_tag"].assert_called_once()
    args = mocks["fetch_latest_tag"].call_args
    assert args.args[0] == "hermes"


def test_upgrade_bare_call_noops_when_at_latest(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Bare ``upgrade`` fetches latest, finds it matches current pin,
    no-ops cleanly — no snapshot dance, no misleading ``vX -> vX``
    output. Caught a user-facing regression today where a user typed
    ``agents upgrade coder`` expecting either a real upgrade or a
    quick "you're current" message, got ~30-60s of avoidable downtime
    instead.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)  # fetch returns "v1.0.0" = current
    outcome = upgrades.upgrade("coder", config_path=with_coder)
    assert outcome.old_tag == outcome.new_tag == "v1.0.0"
    # Nothing expensive ran.
    mocks["stop"].assert_not_called()
    mocks["snapshot"].assert_not_called()
    mocks["pull"].assert_not_called()
    mocks["compose_up"].assert_not_called()


def test_upgrade_bare_call_runs_pipeline_when_newer_upstream(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Bare form discovers a newer upstream → runs the full upgrade
    pipeline. Differs from the noop-at-latest case only in the
    upstream resolution; same code path as ``--latest``.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    mocks["fetch_latest_tag"].return_value = "v9.9.9"  # newer than v1.0.0
    outcome = upgrades.upgrade("coder", config_path=with_coder)
    assert outcome.old_tag == "v1.0.0"
    assert outcome.new_tag == "v9.9.9"
    mocks["snapshot"].assert_called_once()
    mocks["pull"].assert_called_once()
    mocks["compose_up"].assert_called_once()


def test_upgrade_with_recreate_flag_runs_full_pipeline_even_with_same_tag(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """``--recreate`` is the explicit opt-in for the force-refresh
    use case: snapshot + recreate container + run doctor even though
    the image tag doesn't change. Useful for picking up env-file
    changes (per-agent .env modifications) or recovering from a
    wedged container without manually destroying + recreating it.
    Crucially, ``--recreate`` does NOT trigger the implicit-latest
    fetch — the user said "refresh in place", not "upgrade".
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    outcome = upgrades.upgrade("coder", recreate=True, config_path=with_coder)
    assert outcome.old_tag == outcome.new_tag == "v1.0.0"
    mocks["snapshot"].assert_called_once()
    mocks["pull"].assert_called_once()
    mocks["compose_up"].assert_called_once()
    # No upstream fetch with --recreate.
    mocks["fetch_latest_tag"].assert_not_called()


def test_upgrade_recreate_with_explicit_tag_match_still_runs_pipeline(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """``--recreate`` overrides the same-tag short-circuit regardless
    of whether the tag came in via ``--tag`` or was inferred. A user
    saying ``--tag v1.0.0 --recreate`` knows what they want.
    """
    mocks = _stub_happy_upgrade(mocker, cfg)
    upgrades.upgrade("coder", tag="v1.0.0", recreate=True, config_path=with_coder)
    mocks["snapshot"].assert_called_once()
    mocks["compose_up"].assert_called_once()


def test_upgrade_rollback_clears_journal_to_force_clean_retry(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Regression for the 2026-05-22 silent-no-op-on-retry bug:
    when the upgrade except handler reverts the compose tag, the
    journal must be cleared too. Without this, the next retry sees
    completed_phases = [host_data_snapshot, vm_snapshot, tag_pinned,
    pulled, recreated] and skips them — so the live container stays
    on the OLD tag while the doctor phase (which IS re-run) reports
    success, making the upgrade look like it landed when it didn't.
    """
    _stub_happy_upgrade(mocker, cfg)
    # Force doctor to fail so the except handler runs.
    mocker.patch(
        "agents_cli.ops.doctor",
        side_effect=RuntimeFailure("doctor probe blew up"),
    )

    with pytest.raises(RuntimeFailure, match="doctor probe"):
        upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)

    # The journal MUST be gone so the next retry doesn't think the
    # tag/pull/recreate phases already succeeded at the new tag.
    journals = list((cfg.paths.host_data_dir / ".journal").glob("upgrade-coder-*.json"))
    assert journals == [], (
        f"journal was not cleared after rollback: {journals} — "
        f"a retry would skip tag_pinned/pulled/recreated and report success "
        f"against the OLD tag."
    )


def test_upgrade_failed_run_does_not_bump_default_tag(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """defaults.<type>_image_tag must only move once the upgrade has
    demonstrably landed. Persisting at resolve time meant a failed-and-
    rolled-back upgrade still re-baselined every future `agents create`
    onto the bad tag."""
    _stub_happy_upgrade(mocker, cfg)
    mocker.patch("agents_cli.outdated_cmd.fetch_latest_tag", return_value="v9.9.9")
    mocker.patch("agents_cli.ops.doctor", side_effect=RuntimeFailure("doctor probe blew up"))

    with pytest.raises(RuntimeFailure, match="doctor probe"):
        upgrades.upgrade("coder", to_latest=True, config_path=with_coder)

    reloaded = cfg_mod.load(with_coder)
    assert reloaded.defaults.hermes_image_tag == "v1.0.0"


def test_upgrade_noop_at_latest_still_refreshes_default_tag(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """The no-op path is also a success path: the agent demonstrably
    runs the resolved-latest tag, so a stale default may be refreshed
    even though no upgrade machinery runs."""
    cfg.defaults.hermes_image_tag = "v0.9.0"  # stale default, agent pinned at v1.0.0
    cfg_mod.save(cfg, with_coder)
    _stub_happy_upgrade(mocker, cfg)
    mocker.patch("agents_cli.outdated_cmd.fetch_latest_tag", return_value="v1.0.0")

    outcome = upgrades.upgrade("coder", to_latest=True, config_path=with_coder)

    assert outcome.old_tag == outcome.new_tag == "v1.0.0"
    reloaded = cfg_mod.load(with_coder)
    assert reloaded.defaults.hermes_image_tag == "v1.0.0"


def test_upgrade_revert_failure_preserves_original_error_and_cleanup(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """The failure handler's compose-tag revert talks to the VM — and an
    unreachable VM is often the very reason the upgrade failed. A raising
    revert must NOT replace the original exception, and the journal clear
    plus the snapshots-preserved hint must still run."""
    _stub_happy_upgrade(mocker, cfg)
    from agents_cli.errors import HealthCheckFailure

    mocker.patch("agents_cli.ops.doctor", side_effect=HealthCheckFailure("doctor says no"))
    # First call = the tag_pinned phase; second = the revert in the
    # except handler, which blows up like an unreachable VM would.
    mocker.patch(
        "agents_cli.upgrades._set_image_tag",
        side_effect=[None, RuntimeError("VM unreachable")],
    )
    warn_spy = mocker.spy(upgrades.ui, "warn")

    with pytest.raises(HealthCheckFailure, match="doctor says no"):
        upgrades.upgrade("coder", tag="v1.0.1", config_path=with_coder)

    warnings = [c.args[0] for c in warn_spy.call_args_list]
    assert any("could not revert compose tag" in w for w in warnings)
    assert any("snapshots preserved" in w for w in warnings)
    # Journal still cleared so the retry starts from a clean slate.
    journals = list((cfg.paths.host_data_dir / ".journal").glob("upgrade-coder-*.json"))
    assert journals == []


def test_host_data_snapshot_runs_sudo_tar_inside_vm_in_host_mount_mode(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """Regression for the 2026-05-22 EACCES on coder's .data: the
    naïve "stream through zstandard on the host" path doesn't work
    when the agent's data is owned by a rootless-Docker subuid the
    host user can't read (mode 700 + foreign uid). The host-mount
    branch must run ``sudo tar`` inside the VM (where root reads
    everything via virtiofs passthrough) and transfer the archive out.
    """
    exec_bash = mocker.patch("agents_cli.mp.exec_bash", return_value=_ok())
    transfer = mocker.patch("agents_cli.mp.transfer_from_vm", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    upgrades._host_data_snapshot(cfg, "coder")

    exec_bash.assert_called_once()
    script = exec_bash.call_args.args[1]
    # The tar MUST run as root via sudo — without it, the host user
    # can't read subuid-owned files inside the VM either.
    assert "sudo tar" in script
    # And the archive MUST get chowned back to ubuntu before SFTP
    # tries to read it — otherwise the transfer hits EACCES.
    assert "sudo chown ubuntu:ubuntu" in script
    transfer.assert_called_once()


def test_host_data_snapshot_data_dir_in_vm_branch_unchanged(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """In ``--data-dir-in-vm`` mode the data already lives in the VM
    as the ubuntu user — no sudo needed. Make sure the fix to the
    host-mount branch didn't accidentally tighten this one too.
    """
    cfg.paths = cfg.paths.model_copy(update={"data_dir_in_vm": True})
    cfg_mod.save(cfg, with_coder)
    cfg = cfg_mod.load(with_coder)
    exec_bash = mocker.patch("agents_cli.mp.exec_bash", return_value=_ok())
    mocker.patch("agents_cli.mp.transfer_from_vm", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    upgrades._host_data_snapshot(cfg, "coder")

    script = exec_bash.call_args.args[1]
    assert "tar --zstd" in script
    assert "sudo tar" not in script  # not needed in this mode


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


def test_pull_all_narrates_start_and_end(with_coder: Path, mocker) -> None:
    """Image pulls run for minutes on a cold cache; a silent terminal
    reads as a hang. Both paths narrate start + completion via
    ui.progress (stderr) while stdout keeps its single result object."""
    mocker.patch("agents_cli.compose_runner.pull", return_value=_ok())
    progress = mocker.patch("agents_cli.ui.progress")
    emit = mocker.patch("agents_cli.ui.emit")

    upgrades.pull(config_path=with_coder)

    lines = [c.args[0] for c in progress.call_args_list]
    assert any("pulling images for all services" in ln for ln in lines)
    assert any("pull complete" in ln for ln in lines)
    emit.assert_called_once()


def test_pull_single_agent_narrates_start_and_end(with_coder: Path, mocker) -> None:
    mocker.patch("agents_cli.compose_runner.pull", return_value=_ok())
    progress = mocker.patch("agents_cli.ui.progress")
    emit = mocker.patch("agents_cli.ui.emit")

    upgrades.pull("coder", config_path=with_coder)

    lines = [c.args[0] for c in progress.call_args_list]
    assert any(ln.startswith("pulling hermes-coder") for ln in lines)
    assert any(ln.startswith("pulled hermes-coder") for ln in lines)
    emit.assert_called_once()


def test_vm_snapshot_suppresses_take_snapshot_emit(
    with_coder: Path, mocker, cfg: AgentsConfig
) -> None:
    """The upgrade state machine narrates the snapshot phase itself via
    ui.progress; take_snapshot's own emit would put a second JSON object
    on stdout mid-upgrade."""
    take = mocker.patch("agents_cli.vm.take_snapshot", return_value="pre-upgrade-coder-x")

    name = upgrades._vm_snapshot(cfg, "coder")

    assert name == "pre-upgrade-coder-x"
    assert take.call_args.kwargs["emit"] is False
    assert take.call_args.kwargs["yes"] is True
