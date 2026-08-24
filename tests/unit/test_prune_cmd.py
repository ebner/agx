"""prune_cmd.py: generation-counted retention of upgrade artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents_cli import prune_cmd
from agents_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    PathsConfig,
    UpgradesConfig,
    VmRuntimeConfig,
)
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
    (data_dir / "docker-compose.yml").write_text(
        "services:\n  hermes-coder:\n    image: nousresearch/hermes-agent:v1.0.0\n",
        encoding="utf-8",
    )
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
        upgrades=UpgradesConfig(keep_generations=2),
    )


def _write_archives(cfg: AgentsConfig, names: list[str]) -> None:
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(exist_ok=True)
    for n in names:
        (upgrades_dir / n).write_bytes(b"x" * 10)


# ---------- archive planning ----------


def test_plan_archives_keeps_newest_and_matches_strictly(cfg: AgentsConfig) -> None:
    _write_archives(
        cfg,
        [
            "coder-20260101T000000Z.tar.zst",
            "coder-20260201T000000Z.tar.gz",  # legacy suffix still counted
            "coder-20260301T000000Z.tar.zst",
            "coder-two-20260401T000000Z.tar.zst",  # other agent — never touched
            "not-an-archive.txt",
        ],
    )
    doomed = prune_cmd._plan_archives(cfg, "coder", keep=2)
    assert [p.name for p in doomed] == ["coder-20260101T000000Z.tar.zst"]


def test_plan_archives_keep_zero_selects_all(cfg: AgentsConfig) -> None:
    _write_archives(cfg, ["coder-20260101T000000Z.tar.zst", "coder-20260201T000000Z.tar.zst"])
    doomed = prune_cmd._plan_archives(cfg, "coder", keep=0)
    assert len(doomed) == 2


# ---------- snapshot planning ----------


def _stub_info_vm(mocker: Any, cfg: AgentsConfig, snapshot_names: list[str]) -> None:
    payload = {"info": {cfg.vm.name: {"snapshots": {n: {} for n in snapshot_names}}}}
    mocker.patch("agents_cli.mp.info_vm", return_value=_ok(json.dumps(payload)))


def test_plan_snapshots_filters_to_agents_upgrade_snaps(cfg: AgentsConfig, mocker: Any) -> None:
    _stub_info_vm(
        mocker,
        cfg,
        [
            "pre-upgrade-coder-20260101T000000Z",
            "pre-upgrade-coder-20260201T000000Z",
            "pre-upgrade-coder-20260301T000000Z",
            "pre-upgrade-coder-two-20260401T000000Z",  # other agent
            "manual-checkpoint",  # user's own snapshot — never touched
        ],
    )
    doomed = prune_cmd._plan_snapshots(cfg, "coder", keep=2)
    assert doomed == ["pre-upgrade-coder-20260101T000000Z"]


def test_plan_snapshots_empty_on_info_failure(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.mp.info_vm", return_value=_fail("VM unreachable"))
    assert prune_cmd._plan_snapshots(cfg, "coder", keep=2) == []


# ---------- image planning ----------


def _docker_images_stdout(rows: list[tuple[str, str]]) -> str:
    return "".join(f"{tag}\t{created}\n" for tag, created in rows)


def test_plan_images_protects_referenced_baseline_and_newest(
    cfg: AgentsConfig, mocker: Any
) -> None:
    """v1.0.0 is compose-referenced AND the defaults baseline; v0.9.0
    survives inside the newest-2 window; v0.8.0/v0.7.0 go."""
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        return_value=_ok(
            _docker_images_stdout(
                [
                    ("v1.0.0", "2026-08-13 05:00:00 +0000 UTC"),
                    ("v0.9.0", "2026-07-01 05:00:00 +0000 UTC"),
                    ("v0.8.0", "2026-06-01 05:00:00 +0000 UTC"),
                    ("v0.7.0", "2026-05-01 05:00:00 +0000 UTC"),
                    ("<none>", "2026-04-01 05:00:00 +0000 UTC"),
                ]
            )
        ),
    )
    doomed = prune_cmd._plan_images(cfg, {"nousresearch/hermes-agent"}, keep=2)
    assert doomed == [
        "nousresearch/hermes-agent:v0.7.0",
        "nousresearch/hermes-agent:v0.8.0",
    ]


def test_plan_images_keeps_exactly_keep_when_defaults_tag_is_stale(
    cfg: AgentsConfig, mocker: Any
) -> None:
    """The upgrade-hook race: auto-prune runs before _persist_default_tag,
    so during a --latest upgrade compose already references the new tag
    while defaults still holds the outgoing one. Newest-N counting must
    yield exactly ``keep`` survivors, not keep + 1 (codex review
    2026-08-24)."""
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        "services:\n  hermes-coder:\n    image: nousresearch/hermes-agent:v1.0.1\n",
        encoding="utf-8",
    )
    assert cfg.defaults.hermes_image_tag == "v1.0.0"  # stale baseline
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        return_value=_ok(
            _docker_images_stdout(
                [
                    ("v1.0.1", "2026-08-20 05:00:00 +0000 UTC"),
                    ("v1.0.0", "2026-08-13 05:00:00 +0000 UTC"),
                    ("v0.9.0", "2026-07-01 05:00:00 +0000 UTC"),
                    ("v0.8.0", "2026-06-01 05:00:00 +0000 UTC"),
                ]
            )
        ),
    )
    doomed = prune_cmd._plan_images(cfg, {"nousresearch/hermes-agent"}, keep=2)
    assert doomed == [
        "nousresearch/hermes-agent:v0.8.0",
        "nousresearch/hermes-agent:v0.9.0",
    ]


def test_plan_images_referenced_tag_survives_even_when_old(cfg: AgentsConfig, mocker: Any) -> None:
    """A rolled-back agent pinned to an old tag: the referenced tag is
    outside the newest-N window but must never be deleted."""
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        return_value=_ok(
            _docker_images_stdout(
                [
                    ("v1.2.0", "2026-08-20 05:00:00 +0000 UTC"),
                    ("v1.1.0", "2026-08-13 05:00:00 +0000 UTC"),
                    ("v1.0.0", "2026-07-01 05:00:00 +0000 UTC"),  # referenced + baseline
                    ("v0.9.0", "2026-06-01 05:00:00 +0000 UTC"),
                ]
            )
        ),
    )
    doomed = prune_cmd._plan_images(cfg, {"nousresearch/hermes-agent"}, keep=2)
    assert doomed == ["nousresearch/hermes-agent:v0.9.0"]


def test_plan_images_skips_repo_when_docker_unreachable(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_fail("VM down"))
    assert prune_cmd._plan_images(cfg, {"nousresearch/hermes-agent"}, keep=2) == []


# ---------- plan assembly ----------


def test_build_plan_unknown_agent_raises(cfg: AgentsConfig, mocker: Any) -> None:
    with pytest.raises(UserError, match="not in compose"):
        prune_cmd.build_plan(cfg, "ghost", keep=2)


def test_build_plan_scopes_to_named_agent(cfg: AgentsConfig, mocker: Any) -> None:
    _write_archives(cfg, [f"coder-2026010{i}T000000Z.tar.zst" for i in range(1, 5)])
    _stub_info_vm(mocker, cfg, [])
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(""))
    plan = prune_cmd.build_plan(cfg, "coder", keep=2)
    assert len(plan.archives) == 2
    assert plan.snapshots == []
    assert plan.images == []


# ---------- execution ----------


def test_execute_plan_deletes_and_collects_per_item_failures(
    cfg: AgentsConfig, mocker: Any
) -> None:
    _write_archives(cfg, ["coder-20260101T000000Z.tar.zst"])
    archive = cfg.paths.host_data_dir / ".upgrades" / "coder-20260101T000000Z.tar.zst"
    missing = cfg.paths.host_data_dir / ".upgrades" / "coder-20250101T000000Z.tar.zst"
    mocker.patch(
        "agents_cli.mp.delete_snapshot",
        side_effect=[_ok(), _fail("snapshot busy")],
    )
    rmi = mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())

    plan = prune_cmd.PrunePlan(
        keep=2,
        archives=[archive, missing],
        snapshots=["pre-upgrade-coder-a", "pre-upgrade-coder-b"],
        images=["nousresearch/hermes-agent:v0.7.0"],
    )
    result = prune_cmd.execute_plan(cfg, plan)

    assert not archive.exists()
    assert result.archives == [archive.name]
    assert result.archive_bytes_freed == 10
    assert result.snapshots == ["pre-upgrade-coder-a"]
    assert result.images == ["nousresearch/hermes-agent:v0.7.0"]
    assert {f["kind"] for f in result.failed} == {"archive", "snapshot"}
    rmi.assert_called_once_with(
        cfg.vm.name, "docker", "rmi", "nousresearch/hermes-agent:v0.7.0", check=False
    )


# ---------- command surface ----------


def test_prune_dry_run_deletes_nothing(cfg: AgentsConfig, mocker: Any, tmp_path: Path) -> None:
    from agents_cli import config as cfg_mod

    _write_archives(cfg, [f"coder-2026010{i}T000000Z.tar.zst" for i in range(1, 5)])
    _stub_info_vm(mocker, cfg, [])
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(""))
    delete_snap = mocker.patch("agents_cli.mp.delete_snapshot")
    config_path = tmp_path / "config.toml"
    cfg_mod.save(cfg, config_path)

    prune_cmd.prune("coder", dry_run=True, config_path=config_path)

    remaining = list((cfg.paths.host_data_dir / ".upgrades").iterdir())
    assert len(remaining) == 4
    delete_snap.assert_not_called()


def test_prune_executes_with_yes(cfg: AgentsConfig, mocker: Any, tmp_path: Path) -> None:
    from agents_cli import config as cfg_mod

    _write_archives(cfg, [f"coder-2026010{i}T000000Z.tar.zst" for i in range(1, 5)])
    _stub_info_vm(mocker, cfg, [])
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(""))
    config_path = tmp_path / "config.toml"
    cfg_mod.save(cfg, config_path)

    prune_cmd.prune("coder", yes=True, config_path=config_path)

    remaining = sorted(p.name for p in (cfg.paths.host_data_dir / ".upgrades").iterdir())
    assert remaining == ["coder-20260103T000000Z.tar.zst", "coder-20260104T000000Z.tar.zst"]


def test_prune_refuses_without_confirmation(cfg: AgentsConfig, mocker: Any, tmp_path: Path) -> None:
    from agents_cli import config as cfg_mod

    _write_archives(cfg, [f"coder-2026010{i}T000000Z.tar.zst" for i in range(1, 5)])
    _stub_info_vm(mocker, cfg, [])
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(""))
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    config_path = tmp_path / "config.toml"
    cfg_mod.save(cfg, config_path)

    with pytest.raises(UserError, match="aborted"):
        prune_cmd.prune("coder", config_path=config_path)
    assert len(list((cfg.paths.host_data_dir / ".upgrades").iterdir())) == 4


# ---------- upgrade hook ----------


def test_auto_prune_disabled_returns_none(cfg: AgentsConfig) -> None:
    cfg.upgrades.keep_generations = 0
    assert prune_cmd.auto_prune_after_upgrade(cfg, "coder") is None


def test_auto_prune_no_op_on_clean_state(cfg: AgentsConfig, mocker: Any) -> None:
    _stub_info_vm(mocker, cfg, [])
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok(""))
    result = prune_cmd.auto_prune_after_upgrade(cfg, "coder")
    assert result is not None
    assert result.archives == [] and result.snapshots == [] and result.images == []
