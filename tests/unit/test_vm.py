"""vm.py: orchestration tests with all multipass calls mocked.

The unit suite does not touch a real VM. Integration coverage lives behind
the `multipass` pytest marker.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agx_cli import config as cfg_mod
from agx_cli import vm
from agx_cli.config import AgentsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agx_cli.errors import HealthCheckFailure, RuntimeFailure, UserError
from agx_cli.mp import MpResult

# ---------- helpers ----------


def _ok(stdout: str = "", stderr: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr=stderr, returncode=0)


def _saved_config(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def _make_config(tmp_path: Path) -> AgentsConfig:
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(tmp_path / "data")),
        host=HostConfig(uid=1001, gid=1001),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
    )


# ---------- _select_ubuntu_image ----------


def test_select_ubuntu_image_returns_requested_when_listed(mocker) -> None:
    catalog = {"images": {"x": {"aliases": ["26.04", "noble"]}}}
    mocker.patch("agx_cli.mp.find", return_value=_ok(json.dumps(catalog)))
    assert vm._select_ubuntu_image("26.04") == "26.04"


def test_select_ubuntu_image_falls_back_to_24_04_when_missing(mocker) -> None:
    catalog = {"images": {"x": {"aliases": ["24.04", "noble"]}}}
    mocker.patch("agx_cli.mp.find", return_value=_ok(json.dumps(catalog)))
    assert vm._select_ubuntu_image("26.04") == "24.04"


def test_select_ubuntu_image_returns_requested_when_find_fails(mocker) -> None:
    mocker.patch("agx_cli.mp.find", side_effect=RuntimeFailure("network"))
    assert vm._select_ubuntu_image("26.04") == "26.04"


def test_select_ubuntu_image_handles_bad_json(mocker) -> None:
    mocker.patch("agx_cli.mp.find", return_value=_ok("not json"))
    assert vm._select_ubuntu_image("26.04") == "26.04"


# ---------- _vm_exists ----------


def test_vm_exists_true(mocker) -> None:
    listing = {"list": [{"name": "agents"}, {"name": "other"}]}
    mocker.patch("agx_cli.mp.list_vms", return_value=_ok(json.dumps(listing)))
    assert vm._vm_exists("agents") is True


def test_vm_exists_false(mocker) -> None:
    listing = {"list": [{"name": "other"}]}
    mocker.patch("agx_cli.mp.list_vms", return_value=_ok(json.dumps(listing)))
    assert vm._vm_exists("agents") is False


def test_vm_exists_handles_list_failure(mocker) -> None:
    mocker.patch("agx_cli.mp.list_vms", side_effect=RuntimeFailure("x"))
    assert vm._vm_exists("agents") is False


# ---------- _unique_snapshot_name ----------


def test_unique_snapshot_name_returns_base_when_no_collision(mocker) -> None:
    info = {"info": {"agents": {"snapshots": {"foo": {}}}}}
    mocker.patch("agx_cli.mp.info_vm", return_value=_ok(json.dumps(info)))
    assert vm._unique_snapshot_name("agents", "bar") == "bar"


def test_unique_snapshot_name_appends_counter_on_collision(mocker) -> None:
    info = {"info": {"agents": {"snapshots": {"snap": {}, "snap-2": {}}}}}
    mocker.patch("agx_cli.mp.info_vm", return_value=_ok(json.dumps(info)))
    assert vm._unique_snapshot_name("agents", "snap") == "snap-3"


def test_unique_snapshot_name_handles_info_failure(mocker) -> None:
    mocker.patch("agx_cli.mp.info_vm", side_effect=RuntimeFailure("x"))
    assert vm._unique_snapshot_name("agents", "snap") == "snap"


# ---------- _seed_data_dir ----------


def test_seed_data_dir_creates_files_and_dirs(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.paths.host_data_dir.mkdir()
    vm._seed_data_dir(cfg)
    d = cfg.paths.host_data_dir
    assert (d / "docker-compose.yml").read_text() == "services: {}\n"
    assert "HERMES_UID=1000" in (d / ".env").read_text()
    assert (d / ".codex").is_dir()
    assert (d / "backups").is_dir()
    assert (d / ".journal").is_dir()
    assert (d / ".upgrades").is_dir()


def test_seed_data_dir_is_idempotent_and_preserves_user_compose(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.paths.host_data_dir.mkdir()
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        "services:\n  custom: {image: x}\n", encoding="utf-8"
    )
    vm._seed_data_dir(cfg)
    content = (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert "custom" in content  # user content preserved


def test_seed_data_dir_in_vm_repushes_host_override(mocker, tmp_path: Path) -> None:
    """--data-dir-in-vm: the host override file survives `vm rebuild`
    (and keeps the extra -f armed) while its in-VM mirror dies with the
    VM disk. Seeding the fresh VM must push it back or every compose
    verb fails on the missing file (bit `agx mount` users)."""
    cfg = _make_config(tmp_path)
    cfg.paths.data_dir_in_vm = True
    cfg.paths.host_data_dir.mkdir()
    (cfg.paths.host_data_dir / ".journal").mkdir()
    override = cfg.paths.host_data_dir / "docker-compose.override.yml"
    override.write_text("services:\n  hermes-a:\n    volumes: [/x:/y:ro]\n", encoding="utf-8")
    mocker.patch("agx_cli.mp.exec_bash", return_value=_ok())
    transfer = mocker.patch("agx_cli.mp.transfer_to_vm", return_value=_ok())
    exec_in_vm = mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok())

    vm._seed_data_dir(cfg)

    transfer.assert_called_once()
    assert transfer.call_args.args[0] == override  # verbatim, not re-dumped
    assert transfer.call_args.args[2].startswith(f"{cfg.paths.vm_agents_root}/.compose.")
    mv = [c.args for c in exec_in_vm.call_args_list if c.args[1] == "mv"]
    assert mv and mv[-1][3] == f"{cfg.paths.vm_agents_root}/docker-compose.override.yml"


def test_seed_data_dir_in_vm_without_override_transfers_nothing(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.paths.data_dir_in_vm = True
    cfg.paths.host_data_dir.mkdir()
    mocker.patch("agx_cli.mp.exec_bash", return_value=_ok())
    transfer = mocker.patch("agx_cli.mp.transfer_to_vm", return_value=_ok())
    vm._seed_data_dir(cfg)
    transfer.assert_not_called()


def test_start_in_vm_mode_heals_missing_override_mirror(mocker, tmp_path: Path) -> None:
    """A VM rebuilt by an older toolkit has the host override but no
    mirror; `vm start` re-pushes it — and only then (a live mirror is
    never clobbered)."""
    cfg = _make_config(tmp_path)
    cfg.paths.data_dir_in_vm = True
    cfg.paths.host_data_dir.mkdir()
    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocker.patch("agx_cli.mp.start_vm", return_value=_ok())
    mocker.patch("agx_cli.compose.cleanup_stale_temps")
    mocker.patch("agx_cli.ui.emit")
    transfer = mocker.patch("agx_cli.mp.transfer_to_vm", return_value=_ok())

    def exec_in_vm(vm_name: str, *argv: str, **kw: object) -> MpResult:
        if argv[0] == "test":
            return MpResult(stdout="", stderr="", returncode=1)  # mirror missing
        return _ok()

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=exec_in_vm)
    vm.start(config_path=config_path)
    transfer.assert_called_once()

    # Mirror present → no push.
    transfer.reset_mock()
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok())
    vm.start(config_path=config_path)
    transfer.assert_not_called()


# ---------- _prepare_host_data_dir ----------


def test_prepare_host_data_dir_creates_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "new"
    vm._prepare_host_data_dir(target)
    assert target.is_dir()


def test_prepare_host_data_dir_accepts_existing_toolkit_dir(tmp_path: Path) -> None:
    target = tmp_path / "data"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    vm._prepare_host_data_dir(target)


def test_prepare_host_data_dir_refuses_non_empty_non_toolkit(tmp_path: Path) -> None:
    target = tmp_path / "data"
    target.mkdir()
    (target / "something.txt").write_text("user file", encoding="utf-8")
    with pytest.raises(UserError, match="non-empty"):
        vm._prepare_host_data_dir(target)


# ---------- _mount_probe ----------


def test_mount_probe_round_trips_through_mount(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.paths.host_data_dir.mkdir()

    def fake_runner(vm_name, *cmd, check=True):
        # Read whatever file the probe just wrote on the host and echo it back.
        filename = cmd[1].split("/")[-1]
        return _ok(stdout=(cfg.paths.host_data_dir / filename).read_text())

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=fake_runner)
    vm._mount_probe(cfg)


def test_mount_probe_raises_when_round_trip_fails(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.paths.host_data_dir.mkdir()
    mocker.patch(
        "agx_cli.mp.exec_in_vm",
        return_value=MpResult(stdout="", stderr="cat: no such file", returncode=1),
    )
    with pytest.raises(HealthCheckFailure, match="mount probe"):
        vm._mount_probe(cfg)


# ---------- init: end-to-end orchestration with all mp calls mocked ----------


_GOOD_BOOTSTRAP = (
    "BOOTSTRAP_OK uidmap=ok dbus=ok lingering=ok docker_host=ok daemon=ok "
    "user_socket_active=ok net=ok ubuntu_uid=1000 ubuntu_gid=1000 "
    "socket_path=/run/user/1000/docker.sock"
)


def _stub_all_mp(mocker, *, bootstrap_output: str = _GOOD_BOOTSTRAP) -> dict[str, MagicMock]:
    catalog = json.dumps({"images": {"x": {"aliases": ["26.04"]}}})
    return {
        "find": mocker.patch("agx_cli.mp.find", return_value=_ok(catalog)),
        "list_vms": mocker.patch("agx_cli.mp.list_vms", return_value=_ok(json.dumps({"list": []}))),
        "launch": mocker.patch("agx_cli.mp.launch", return_value=_ok()),
        "info_vm": mocker.patch(
            "agx_cli.mp.info_vm",
            return_value=_ok(json.dumps({"info": {"agents": {"mounts": {}}}})),
        ),
        "mount": mocker.patch("agx_cli.mp.mount", return_value=_ok()),
        "bootstrap": mocker.patch(
            "agx_cli.mp.exec_bash", return_value=_ok(stdout=bootstrap_output)
        ),
        "fetch": mocker.patch("agx_cli.vm._fetch_latest_tag", return_value=None),
    }


def test_init_full_flow(mocker, tmp_path: Path) -> None:
    mocks = _stub_all_mp(mocker)
    data_dir = tmp_path / "data"
    config_path = _saved_config(tmp_path)

    vm.init(backend="multipass", data_dir=data_dir, config_path=config_path)

    mocks["launch"].assert_called_once()
    mocks["mount"].assert_called_once()
    mocks["bootstrap"].assert_called_once()
    args, _kwargs = mocks["bootstrap"].call_args
    assert args[0] == "agents"
    assert "BOOTSTRAP_OK" in args[1]

    cfg = cfg_mod.load(config_path)
    assert cfg.paths.host_data_dir == data_dir
    assert cfg.vm_runtime.docker_socket_path == "/run/user/1000/docker.sock"
    assert cfg.vm_runtime.ubuntu_uid == 1000
    assert cfg.vm_runtime.is_populated()

    assert (data_dir / "docker-compose.yml").exists()
    assert (data_dir / ".env").exists()
    assert (data_dir / ".codex").is_dir()


def test_init_skips_launch_when_vm_already_exists(mocker, tmp_path: Path) -> None:
    mocks = _stub_all_mp(mocker)
    mocks["list_vms"].return_value = _ok(json.dumps({"list": [{"name": "agents"}]}))

    vm.init(backend="multipass", data_dir=tmp_path / "data", config_path=_saved_config(tmp_path))

    mocks["launch"].assert_not_called()
    mocks["mount"].assert_called_once()


def test_init_uses_baked_in_fallback_tags_when_github_unreachable(mocker, tmp_path: Path) -> None:
    _stub_all_mp(mocker)  # `_fetch_latest_tag` already stubbed to None
    config_path = _saved_config(tmp_path)
    vm.init(backend="multipass", data_dir=tmp_path / "data", config_path=config_path)
    cfg = cfg_mod.load(config_path)
    from agx_cli.templates import FALLBACK_HERMES_IMAGE_TAG, FALLBACK_OPENCLAW_IMAGE_TAG

    assert cfg.defaults.hermes_image_tag == FALLBACK_HERMES_IMAGE_TAG
    assert cfg.defaults.openclaw_image_tag == FALLBACK_OPENCLAW_IMAGE_TAG


def test_init_uses_live_tags_when_github_succeeds(mocker, tmp_path: Path) -> None:
    mocks = _stub_all_mp(mocker)
    mocks["fetch"].side_effect = lambda repo: f"v{repo}.live"
    config_path = _saved_config(tmp_path)
    vm.init(backend="multipass", data_dir=tmp_path / "data", config_path=config_path)
    cfg = cfg_mod.load(config_path)
    assert cfg.defaults.hermes_image_tag == "vhermes.live"
    assert cfg.defaults.openclaw_image_tag == "vopenclaw.live"


def test_init_refuses_when_bootstrap_fails(mocker, tmp_path: Path) -> None:
    bad = (
        "BOOTSTRAP_OK uidmap=ok dbus=ok lingering=ok docker_host=fail daemon=ok "
        "user_socket_active=ok net=ok ubuntu_uid=1000 ubuntu_gid=1000 "
        "socket_path=/run/user/1000/docker.sock"
    )
    _stub_all_mp(mocker, bootstrap_output=bad)
    with pytest.raises(RuntimeFailure, match="bootstrap failed"):
        vm.init(
            backend="multipass", data_dir=tmp_path / "data", config_path=_saved_config(tmp_path)
        )


def test_init_falls_back_when_release_not_in_catalog(mocker, tmp_path: Path) -> None:
    mocks = _stub_all_mp(mocker)
    mocks["find"].return_value = _ok(json.dumps({"images": {"x": {"aliases": ["24.04"]}}}))
    config_path = _saved_config(tmp_path)
    vm.init(backend="multipass", data_dir=tmp_path / "data", config_path=config_path)
    cfg = cfg_mod.load(config_path)
    assert cfg.vm.ubuntu_release == "24.04"


def test_init_respects_ubuntu_release_override(mocker, tmp_path: Path) -> None:
    """`--ubuntu-release 26.04` should be honored when the catalog lists it."""
    mocks = _stub_all_mp(mocker)
    catalog = json.dumps({"images": {"x": {"aliases": ["24.04", "26.04"]}}})
    mocks["find"].return_value = _ok(catalog)
    config_path = _saved_config(tmp_path)
    vm.init(
        backend="multipass",
        data_dir=tmp_path / "data",
        ubuntu_release="26.04",
        config_path=config_path,
    )
    cfg = cfg_mod.load(config_path)
    assert cfg.vm.ubuntu_release == "26.04"


def test_init_dumps_bootstrap_output_on_failure(mocker, tmp_path: Path) -> None:
    """When the handshake fails, the captured script output must surface."""
    bad = (
        "+ curl https://get.docker.com/rootless\n"
        "curl: (28) Connection timed out\n"
        "BOOTSTRAP_OK uidmap=ok dbus=ok lingering=ok docker_host=fail daemon=fail "
        "user_socket_active=fail net=fail ubuntu_uid=1000 ubuntu_gid=1000 "
        "socket_path=/run/user/1000/docker.sock"
    )
    _stub_all_mp(mocker, bootstrap_output=bad)
    with pytest.raises(RuntimeFailure) as exc:
        vm.init(
            backend="multipass", data_dir=tmp_path / "data", config_path=_saved_config(tmp_path)
        )
    msg = str(exc.value)
    assert "curl: (28) Connection timed out" in msg
    assert "bootstrap stdout" in msg


def test_init_emits_exactly_one_object_and_narrates_stages(mocker, tmp_path: Path) -> None:
    """Item: one JSON object on stdout per command. The launch line is
    narration (ui.progress, stderr) — only the final "ready" payload is
    emitted. The genuinely slow phases (bootstrap, mount, seed) narrate."""
    _stub_all_mp(mocker)
    emit = mocker.patch("agx_cli.ui.emit")
    progress = mocker.patch("agx_cli.ui.progress")

    vm.init(backend="multipass", data_dir=tmp_path / "data", config_path=_saved_config(tmp_path))

    emit.assert_called_once()
    payload = emit.call_args.args[0]
    assert payload["vm"] == "agents"
    lines = [c.args[0] for c in progress.call_args_list]
    assert any("launching VM" in ln for ln in lines)
    assert any("provisioning rootless Docker" in ln for ln in lines)
    assert any(ln.startswith("mounting ") for ln in lines)
    assert any("seeding data-dir" in ln for ln in lines)


def test_init_emit_false_suppresses_final_object(mocker, tmp_path: Path) -> None:
    """vm rebuild embeds init and owns the command's single stdout
    object — init's "ready" payload must be suppressible."""
    _stub_all_mp(mocker)
    emit = mocker.patch("agx_cli.ui.emit")
    vm.init(
        backend="multipass",
        data_dir=tmp_path / "data",
        config_path=_saved_config(tmp_path),
        emit=False,
    )
    emit.assert_not_called()


def test_init_hints_at_ubuntu_release_when_net_fails_on_non_24_04(mocker, tmp_path: Path) -> None:
    bad = (
        "BOOTSTRAP_OK uidmap=ok dbus=ok lingering=ok docker_host=fail daemon=fail "
        "user_socket_active=fail net=fail ubuntu_uid=1000 ubuntu_gid=1000 "
        "socket_path=/run/user/1000/docker.sock"
    )
    mocks = _stub_all_mp(mocker, bootstrap_output=bad)
    mocks["find"].return_value = _ok(json.dumps({"images": {"x": {"aliases": ["26.04"]}}}))
    with pytest.raises(RuntimeFailure, match="ubuntu_release"):
        vm.init(
            backend="multipass",
            data_dir=tmp_path / "data",
            ubuntu_release="26.04",
            config_path=_saved_config(tmp_path),
        )


# ---------- destroy ----------


def test_destroy_requires_confirmation(mocker, tmp_path: Path) -> None:
    config_path = _saved_config(tmp_path)
    cfg_mod.save(_make_config(tmp_path), config_path)
    mocker.patch("agx_cli.ui.confirm", return_value=False)
    delete = mocker.patch("agx_cli.mp.delete_vm")
    with pytest.raises(UserError, match="aborted"):
        vm.destroy(config_path=config_path)
    delete.assert_not_called()


def test_destroy_with_yes_skips_prompt_and_purges(mocker, tmp_path: Path) -> None:
    config_path = _saved_config(tmp_path)
    cfg_mod.save(_make_config(tmp_path), config_path)
    delete = mocker.patch("agx_cli.mp.delete_vm", return_value=_ok())
    vm.destroy(yes=True, config_path=config_path)
    delete.assert_called_once()
    _, kwargs = delete.call_args
    assert kwargs.get("purge") is True


# ---------- resize ----------


def _stub_resize_mp(mocker) -> dict:
    return {
        "stop": mocker.patch("agx_cli.mp.stop_vm", return_value=_ok()),
        "start": mocker.patch("agx_cli.mp.start_vm", return_value=_ok()),
        "set": mocker.patch("agx_cli.mp.set_config", return_value=_ok()),
        "info_vm": mocker.patch(
            "agx_cli.mp.info_vm",
            return_value=_ok(
                json.dumps(
                    {
                        "info": {
                            "agents": {
                                "mounts": {"/home/ubuntu/agents": {}},
                            }
                        }
                    }
                )
            ),
        ),
        "exec_in_vm": mocker.patch(
            "agx_cli.mp.exec_in_vm",
            return_value=MpResult(stdout="ok\n", stderr="", returncode=0),
        ),
    }


def test_resize_refuses_without_any_arg(tmp_path: Path) -> None:
    config_path = _saved_config(tmp_path)
    cfg_mod.save(_make_config(tmp_path), config_path)
    with pytest.raises(UserError, match="provide at least one"):
        vm.resize(config_path=config_path)


def test_resize_refuses_disk_shrink(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.vm.disk_gb = 50
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    cfg.paths.host_data_dir.mkdir(exist_ok=True)
    _stub_resize_mp(mocker)
    with pytest.raises(UserError, match="disk shrink"):
        vm.resize(disk_gb=25, config_path=config_path)


def test_resize_stops_sets_starts_and_updates_config(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    cfg.paths.host_data_dir.mkdir(exist_ok=True)

    mocks = _stub_resize_mp(mocker)
    vm.resize(cpus=8, memory_gb=16, disk_gb=50, config_path=config_path)

    mocks["stop"].assert_called_once()
    mocks["start"].assert_called_once()
    # Three set calls in order
    set_calls = [c.args for c in mocks["set"].call_args_list]
    assert set_calls == [
        ("local.agents.cpus", 8),
        ("local.agents.memory", "16G"),
        ("local.agents.disk", "50G"),
    ]

    reloaded = cfg_mod.load(config_path)
    assert reloaded.vm.cpus == 8
    assert reloaded.vm.memory_gb == 16
    assert reloaded.vm.disk_gb == 50


def test_resize_partial_only_writes_the_provided_fields(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    cfg.paths.host_data_dir.mkdir(exist_ok=True)

    mocks = _stub_resize_mp(mocker)
    vm.resize(cpus=12, config_path=config_path)

    set_calls = [c.args for c in mocks["set"].call_args_list]
    assert set_calls == [("local.agents.cpus", 12)]

    reloaded = cfg_mod.load(config_path)
    assert reloaded.vm.cpus == 12
    # memory_gb / disk_gb unchanged from the saved config
    assert reloaded.vm.memory_gb == cfg.vm.memory_gb
    assert reloaded.vm.disk_gb == cfg.vm.disk_gb


def test_resize_starts_vm_even_if_set_raises(mocker, tmp_path: Path) -> None:
    """A failed `multipass set` must not leave the VM stopped."""
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    cfg.paths.host_data_dir.mkdir(exist_ok=True)

    from agx_cli.errors import RuntimeFailure

    mocks = _stub_resize_mp(mocker)
    mocks["set"].side_effect = RuntimeFailure("multipass set failed")

    with pytest.raises(RuntimeFailure):
        vm.resize(cpus=8, config_path=config_path)

    mocks["stop"].assert_called_once()
    mocks["start"].assert_called_once()  # cleanup ran


# ---------- rebuild ----------


def _stub_rebuild(mocker, cfg: AgentsConfig, services: dict[str, str]):
    """Stub everything rebuild needs.

    ``services`` is ``{service_name: agent_type}``.
    Returns the mock dict so tests can assert on call sequence.
    """
    import json as _json

    # Pre-populate the compose doc so the rebuild planner sees the
    # services it needs to round-trip.
    cfg.paths.host_data_dir.mkdir(parents=True, exist_ok=True)
    compose_doc = {
        "services": {name: {"image": "x:y"} for name in services},
    }
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        _json.dumps(compose_doc), encoding="utf-8"
    )

    return {
        "export": mocker.patch(
            "agx_cli.export_cmd.export",
            side_effect=lambda name, **kw: kw["to"] / f"{name}-stamp.tar.zst",
        ),
        "import_archive": mocker.patch("agx_cli.export_cmd.import_archive"),
        "init": mocker.patch("agx_cli.vm.init"),
        "delete_vm": mocker.patch("agx_cli.mp.delete_vm", return_value=_ok()),
        "_vm_exists": mocker.patch("agx_cli.vm._vm_exists", return_value=True),
        "up": mocker.patch("agx_cli.compose_runner.up", return_value=_ok()),
        "confirm": mocker.patch("agx_cli.ui.confirm", return_value=True),
    }


def test_rebuild_runs_phases_in_order(mocker, tmp_path: Path) -> None:
    """Full phase walk: export every agent → destroy → init → import → up.
    The order matters — destroying before exporting would lose state.
    """
    import json as _json

    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes", "openclaw-personal": "openclaw"})

    # Pre-create the staging-dir archives that import_archive expects.
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    (staging / "personal-stamp.tar.zst").write_bytes(b"")

    vm.rebuild(yes=True, config_path=config_path)

    # Both agents exported, then VM destroyed, then init, then both
    # imported and brought up.
    assert mocks["export"].call_count == 2
    mocks["delete_vm"].assert_called_once()
    mocks["init"].assert_called_once()
    assert mocks["import_archive"].call_count == 2
    assert mocks["up"].call_count == 2
    del _json  # silence unused-import warning under strict ruff settings


def test_rebuild_overrides_sizing(mocker, tmp_path: Path) -> None:
    """User-provided cpus/memory/disk override the existing config when
    passed; otherwise current config is reused.
    """
    cfg = _make_config(tmp_path)
    cfg.vm.cpus = 4
    cfg.vm.memory_gb = 8
    cfg.vm.disk_gb = 25
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {})

    vm.rebuild(disk_gb=10, yes=True, config_path=config_path)

    init_kwargs = mocks["init"].call_args.kwargs
    assert init_kwargs["cpus"] == 4  # kept
    assert init_kwargs["memory_gb"] == 8  # kept
    assert init_kwargs["disk_gb"] == 10  # overridden (shrink)


def test_rebuild_preserves_backend_and_gpu(mocker, tmp_path: Path) -> None:
    """A libvirt user's rebuild must not silently migrate to multipass or
    drop GPU passthrough — init() falls back to the platform default and
    writes cfg.vm.backend unconditionally, so rebuild has to pass both through.
    """
    cfg = _make_config(tmp_path)
    cfg.vm.backend = "libvirt"
    cfg.vm.gpu_passthrough = ["0000:01:00.0", "0000:01:00.1"]
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {})

    vm.rebuild(yes=True, config_path=config_path)

    init_kwargs = mocks["init"].call_args.kwargs
    assert init_kwargs["backend"] == "libvirt"
    assert init_kwargs["gpu"] == ["0000:01:00.0", "0000:01:00.1"]


def test_rebuild_passes_multipass_backend_without_gpu(mocker, tmp_path: Path) -> None:
    """Default multipass config: backend passes through unchanged and gpu
    stays None — a truthy gpu would trip init()'s '--gpu requires
    --backend libvirt' refusal.
    """
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {})

    vm.rebuild(yes=True, config_path=config_path)

    init_kwargs = mocks["init"].call_args.kwargs
    assert init_kwargs["backend"] == "multipass"
    assert init_kwargs["gpu"] is None


def test_rebuild_requires_confirmation_unless_yes(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {})
    mocks["confirm"].return_value = False

    with pytest.raises(UserError, match="aborted"):
        vm.rebuild(config_path=config_path)
    mocks["delete_vm"].assert_not_called()


# ---------- take_snapshot / restore_snapshot ----------


def _stub_snapshot_mp(
    mocker,
    *,
    snapshot_rc: int = 0,
    restore_rc: int = 0,
    running: bool = True,
) -> dict[str, MagicMock]:
    """Stub every mp primitive + wait helper the snapshot paths touch.

    ``info_vm`` drives both ``_unique_snapshot_name`` (empty snapshot
    map → requested name wins) and ``_vm_running`` (libvirt downtime
    decision).
    """
    info = {"info": {"agents": {"state": "Running" if running else "Stopped", "snapshots": {}}}}
    return {
        "info_vm": mocker.patch("agx_cli.mp.info_vm", return_value=_ok(json.dumps(info))),
        "snapshot": mocker.patch(
            "agx_cli.mp.snapshot",
            return_value=MpResult(
                stdout="", stderr="snapshot boom" if snapshot_rc else "", returncode=snapshot_rc
            ),
        ),
        "restore": mocker.patch(
            "agx_cli.mp.restore",
            return_value=MpResult(
                stdout="", stderr="revert boom" if restore_rc else "", returncode=restore_rc
            ),
        ),
        "stop": mocker.patch("agx_cli.mp.stop_vm", return_value=_ok()),
        "start": mocker.patch("agx_cli.mp.start_vm", return_value=_ok()),
        "wait_stopped": mocker.patch("agx_cli.vm._wait_for_vm_stopped"),
        "wait_ssh": mocker.patch("agx_cli.backends.libvirt.wait_for_ssh"),
    }


def _saved_snapshot_config(tmp_path: Path, *, backend: str = "multipass") -> Path:
    cfg = _make_config(tmp_path)
    cfg.vm.backend = backend
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    return config_path


def test_take_snapshot_multipass_runs_on_live_vm(mocker, tmp_path: Path) -> None:
    """Multipass snapshots a running VM directly — no stop/start cycle."""
    config_path = _saved_snapshot_config(tmp_path)
    mocks = _stub_snapshot_mp(mocker)

    name = vm.take_snapshot("pre-upgrade", config_path=config_path)

    assert name == "pre-upgrade"
    mocks["snapshot"].assert_called_once_with("agents", "pre-upgrade", comment=None)
    mocks["stop"].assert_not_called()
    mocks["start"].assert_not_called()


def test_take_snapshot_emit_false_suppresses_stdout_object(mocker, tmp_path: Path) -> None:
    """upgrades._vm_snapshot narrates the phase itself; take_snapshot's
    own emit would land a second JSON object mid-upgrade."""
    config_path = _saved_snapshot_config(tmp_path)
    _stub_snapshot_mp(mocker)
    emit = mocker.patch("agx_cli.ui.emit")

    name = vm.take_snapshot("pre-upgrade", emit=False, config_path=config_path)

    assert name == "pre-upgrade"
    emit.assert_not_called()


def test_take_snapshot_emits_by_default(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path)
    _stub_snapshot_mp(mocker)
    emit = mocker.patch("agx_cli.ui.emit")
    vm.take_snapshot("pre-upgrade", config_path=config_path)
    emit.assert_called_once()


def test_take_snapshot_raises_on_nonzero_rc(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path)
    _stub_snapshot_mp(mocker, snapshot_rc=1)
    with pytest.raises(RuntimeFailure, match="snapshot failed"):
        vm.take_snapshot("pre-upgrade", config_path=config_path)


def test_take_snapshot_libvirt_stops_and_restarts_running_vm(mocker, tmp_path: Path) -> None:
    """libvirt + virtiofs can't snapshot a running VM: stop → wait →
    snapshot → start → wait-for-ssh. ``yes=True`` skips the prompt."""
    config_path = _saved_snapshot_config(tmp_path, backend="libvirt")
    mocks = _stub_snapshot_mp(mocker)

    vm.take_snapshot("pre-upgrade", yes=True, config_path=config_path)

    mocks["stop"].assert_called_once_with("agents")
    mocks["wait_stopped"].assert_called_once()
    mocks["snapshot"].assert_called_once()
    mocks["start"].assert_called_once_with("agents")
    mocks["wait_ssh"].assert_called_once()


def test_take_snapshot_libvirt_restarts_vm_even_on_failure(mocker, tmp_path: Path) -> None:
    """The finally-block restart must run even when snapshot rc != 0 —
    a failed snapshot must not leave the VM (and every agent) down."""
    config_path = _saved_snapshot_config(tmp_path, backend="libvirt")
    mocks = _stub_snapshot_mp(mocker, snapshot_rc=1)

    with pytest.raises(RuntimeFailure, match="snapshot failed"):
        vm.take_snapshot("pre-upgrade", yes=True, config_path=config_path)

    mocks["start"].assert_called_once()
    mocks["wait_ssh"].assert_called_once()


def test_take_snapshot_refused_on_bare_metal(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path, backend="bare_metal")
    mocks = _stub_snapshot_mp(mocker)
    with pytest.raises(UserError, match="bare_metal"):
        vm.take_snapshot("pre-upgrade", config_path=config_path)
    mocks["snapshot"].assert_not_called()


def test_restore_snapshot_multipass_succeeds_on_rc0(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path)
    mocks = _stub_snapshot_mp(mocker)

    vm.restore_snapshot("pre-upgrade", config_path=config_path)

    mocks["restore"].assert_called_once_with("agents.pre-upgrade", destructive=False)
    mocks["stop"].assert_not_called()
    mocks["start"].assert_not_called()


def test_restore_snapshot_emit_false_suppresses_stdout_object(mocker, tmp_path: Path) -> None:
    """upgrades.rollback delegates the VM restore here while owning the
    command's single stdout object — the "restored: ..." emit must be
    suppressible."""
    config_path = _saved_snapshot_config(tmp_path)
    _stub_snapshot_mp(mocker)
    emit = mocker.patch("agx_cli.ui.emit")

    vm.restore_snapshot("pre-upgrade", emit=False, config_path=config_path)

    emit.assert_not_called()


def test_restore_snapshot_emits_by_default(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path)
    _stub_snapshot_mp(mocker)
    emit = mocker.patch("agx_cli.ui.emit")
    vm.restore_snapshot("pre-upgrade", config_path=config_path)
    emit.assert_called_once()


def test_restore_snapshot_raises_on_nonzero_rc(mocker, tmp_path: Path) -> None:
    """libvirt's restore uses check=False and can hand back rc=1; the CLI
    must not report 'restored' on a failed revert."""
    config_path = _saved_snapshot_config(tmp_path)
    _stub_snapshot_mp(mocker, restore_rc=1)
    with pytest.raises(RuntimeFailure, match="restore failed"):
        vm.restore_snapshot("pre-upgrade", config_path=config_path)


def test_restore_snapshot_surfaces_backend_stderr(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path)
    _stub_snapshot_mp(mocker, restore_rc=1)
    with pytest.raises(RuntimeFailure, match="revert boom"):
        vm.restore_snapshot("pre-upgrade", config_path=config_path)


def test_restore_snapshot_libvirt_stops_and_restarts_running_vm(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path, backend="libvirt")
    mocks = _stub_snapshot_mp(mocker)

    vm.restore_snapshot("pre-upgrade", yes=True, config_path=config_path)

    mocks["stop"].assert_called_once_with("agents")
    mocks["wait_stopped"].assert_called_once()
    mocks["restore"].assert_called_once_with("agents.pre-upgrade", destructive=False)
    mocks["start"].assert_called_once_with("agents")
    mocks["wait_ssh"].assert_called_once()


def test_restore_snapshot_libvirt_failure_still_restarts_vm(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path, backend="libvirt")
    mocks = _stub_snapshot_mp(mocker, restore_rc=1)

    with pytest.raises(RuntimeFailure, match="restore failed"):
        vm.restore_snapshot("pre-upgrade", yes=True, config_path=config_path)

    mocks["start"].assert_called_once()
    mocks["wait_ssh"].assert_called_once()


def test_restore_snapshot_refused_on_bare_metal(mocker, tmp_path: Path) -> None:
    config_path = _saved_snapshot_config(tmp_path, backend="bare_metal")
    mocks = _stub_snapshot_mp(mocker)
    with pytest.raises(UserError, match="bare_metal"):
        vm.restore_snapshot("pre-upgrade", config_path=config_path)
    mocks["restore"].assert_not_called()


def test_rebuild_emits_one_object_and_narrates_stages(mocker, tmp_path: Path) -> None:
    """rebuild owns its command's single stdout object: every embedded
    sub-op (export, init, import) runs with emit=False and the stages
    are narrated via ui.progress on stderr instead."""
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes"})
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    emit = mocker.patch("agx_cli.ui.emit")
    progress = mocker.patch("agx_cli.ui.progress")

    vm.rebuild(yes=True, config_path=config_path)

    emit.assert_called_once()
    assert emit.call_args.args[0]["rebuilt"] is True
    lines = [c.args[0] for c in progress.call_args_list]
    assert any("exporting 'coder'" in ln for ln in lines)
    assert any("destroying VM" in ln for ln in lines)
    assert any("re-initializing the VM" in ln for ln in lines)
    assert any("importing 'coder'" in ln for ln in lines)
    assert any("starting hermes-coder" in ln for ln in lines)
    # Every embedded sub-op ran in non-emitting mode.
    assert mocks["export"].call_args.kwargs["emit"] is False
    assert mocks["init"].call_args.kwargs["emit"] is False
    assert mocks["import_archive"].call_args.kwargs["emit"] is False


def test_rebuild_resumes_from_journal(mocker, tmp_path: Path) -> None:
    """A resumable journal skips already-done phases — re-running after
    a crash mid-init doesn't re-export (which could overwrite the staged
    archives) nor re-destroy (no-op anyway, but the test pins behaviour).
    """
    from agx_cli.journal import Journal

    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes"})
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")

    # Pre-seed a journal as if a previous run completed exports + destroy.
    j = Journal.begin(cfg.paths.host_data_dir, "rebuild", cfg.vm.name, vm.REBUILD_PHASES)
    j.advance("staged_exports")
    j.advance("vm_destroyed")

    vm.rebuild(yes=True, config_path=config_path)

    mocks["export"].assert_not_called()  # already done
    mocks["delete_vm"].assert_not_called()  # already done
    mocks["init"].assert_called_once()
    mocks["import_archive"].assert_called_once()
    mocks["up"].assert_called_once()


# ---------- rebuild: sidecar stack re-up ----------


def _stub_sidecar_reups(mocker) -> dict[str, MagicMock]:
    """Stub the sidecar entry points rebuild's last phase calls."""
    return {
        "firecrawl_up": mocker.patch(
            "agx_cli.firecrawl_cmd.up",
            return_value={"running": True, "wired_agents": ["coder"]},
        ),
        "inference_up": mocker.patch(
            "agx_cli.inference_cmd.up",
            return_value={"engine": "ollama", "running": True},
        ),
        "reup_llamacpp": mocker.patch(
            "agx_cli.inference_cmd.reup_llamacpp_services",
            return_value={"started": [], "failed": []},
        ),
    }


def test_rebuild_reups_sidecars_when_installed(mocker, tmp_path: Path) -> None:
    """After a VM migration/rebuild, agent containers get restored but
    sidecar stacks (firecrawl, ollama, llama-cpp models) lived only in
    the old VM's Docker engine and stayed silently absent. rebuild must
    re-up each installed sidecar via the Python API with emit=False
    (rebuild owns the command's single stdout object) and fold a summary
    into its final payload.
    """
    cfg = _make_config(tmp_path)
    cfg.inference.ollama_enabled = True
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(
        mocker,
        cfg,
        {"hermes-coder": "hermes", "firecrawl-api": "x", "llama-cpp-tiny": "x"},
    )
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    sidecars = _stub_sidecar_reups(mocker)
    sidecars["reup_llamacpp"].return_value = {"started": ["llama-cpp-tiny"], "failed": []}
    emit = mocker.patch("agx_cli.ui.emit")

    vm.rebuild(yes=True, config_path=config_path)

    sidecars["firecrawl_up"].assert_called_once_with(config_path=config_path, emit=False)
    sidecars["inference_up"].assert_called_once_with("ollama", config_path=config_path, emit=False)
    sidecars["reup_llamacpp"].assert_called_once()
    # Exactly one stdout object, with the sidecar summary folded in.
    emit.assert_called_once()
    payload = emit.call_args.args[0]
    assert payload["rebuilt"] is True
    assert payload["sidecars"] == {
        "firecrawl": "running",
        "ollama": "running",
        "llamacpp": {"started": ["llama-cpp-tiny"], "failed": []},
    }
    del mocks


def test_rebuild_skips_sidecars_cleanly_when_absent(mocker, tmp_path: Path) -> None:
    """No firecrawl in compose, ollama disabled, no llama-cpp services →
    no sidecar entry point is touched and the summary says so.
    """
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes"})
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    sidecars = _stub_sidecar_reups(mocker)
    emit = mocker.patch("agx_cli.ui.emit")

    vm.rebuild(yes=True, config_path=config_path)

    sidecars["firecrawl_up"].assert_not_called()
    sidecars["inference_up"].assert_not_called()
    sidecars["reup_llamacpp"].assert_not_called()
    payload = emit.call_args.args[0]
    assert payload["sidecars"] == {
        "firecrawl": "absent",
        "ollama": "absent",
        "llamacpp": {"started": [], "failed": []},
    }


def test_rebuild_sidecar_failure_warns_but_rebuild_succeeds(mocker, tmp_path: Path) -> None:
    """A sidecar that won't come back must not fail the rebuild — the
    agents matter more. Each failure warns with the exact manual
    command; the journal still completes and exactly one object is
    emitted.
    """
    from agx_cli.journal import Journal

    cfg = _make_config(tmp_path)
    cfg.inference.ollama_enabled = True
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes", "firecrawl-api": "x"})
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    sidecars = _stub_sidecar_reups(mocker)
    sidecars["firecrawl_up"].side_effect = RuntimeFailure("firecrawl did not start: probe timeout")
    sidecars["inference_up"].side_effect = RuntimeFailure("compose up failed (exit 1)")
    emit = mocker.patch("agx_cli.ui.emit")
    warn = mocker.patch("agx_cli.ui.warn")

    vm.rebuild(yes=True, config_path=config_path)  # must NOT raise

    emit.assert_called_once()
    payload = emit.call_args.args[0]
    assert payload["rebuilt"] is True
    assert payload["sidecars"]["firecrawl"] == "failed"
    assert payload["sidecars"]["ollama"] == "failed"
    warnings = [c.args[0] for c in warn.call_args_list]
    assert any("agx firecrawl up" in w for w in warnings)
    assert any("agx inference up ollama" in w for w in warnings)
    # Journal completed despite the sidecar failures — nothing resumable left.
    assert Journal.find_resumable(cfg.paths.host_data_dir, "rebuild", cfg.vm.name) is None


def test_rebuild_resume_skips_already_done_sidecars(mocker, tmp_path: Path) -> None:
    """A rebuild that crashed AFTER the sidecar phase must not re-up the
    sidecars on resume (firecrawl up force-recreates nothing, but the
    probe window alone is a minute of pointless wait).
    """
    from agx_cli.journal import Journal

    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes", "firecrawl-api": "x"})
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    sidecars = _stub_sidecar_reups(mocker)
    emit = mocker.patch("agx_cli.ui.emit")

    j = Journal.begin(cfg.paths.host_data_dir, "rebuild", cfg.vm.name, vm.REBUILD_PHASES)
    for phase in vm.REBUILD_PHASES:
        j.advance(phase)

    vm.rebuild(yes=True, config_path=config_path)

    sidecars["firecrawl_up"].assert_not_called()
    payload = emit.call_args.args[0]
    assert payload["sidecars"] == {"skipped": True}


def test_rebuild_reups_ollama_on_compose_presence_with_flag_false(mocker, tmp_path: Path) -> None:
    """`inference up ollama` persists ollama_enabled only AFTER a
    successful compose up — a process that died between the service
    start and that final config save leaves a live compose block with
    the flag still false. Rebuild must treat compose presence as
    installed too, or it silently skips Ollama.
    """
    cfg = _make_config(tmp_path)
    assert cfg.inference.ollama_enabled is False  # the lagging-flag scenario
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes", "ollama": "x"})
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    sidecars = _stub_sidecar_reups(mocker)
    emit = mocker.patch("agx_cli.ui.emit")

    vm.rebuild(yes=True, config_path=config_path)

    sidecars["inference_up"].assert_called_once_with("ollama", config_path=config_path, emit=False)
    assert emit.call_args.args[0]["sidecars"]["ollama"] == "running"


# ---------- rebuild: --data-dir-in-vm sidecar state (consent + staging) ----------


def test_rebuild_data_dir_in_vm_refuses_sidecar_loss_without_consent(
    mocker, tmp_path: Path
) -> None:
    """In --data-dir-in-vm mode the sidecars' bulk state (firecrawl's
    postgres, model weights) lives on the VM disk the rebuild destroys.
    Without explicit consent the rebuild must refuse BEFORE anything is
    exported or destroyed — silent loss is not an option.
    """
    from tests.conftest import install_vm_fs_simulation

    cfg = _make_config(tmp_path)
    cfg.paths.data_dir_in_vm = True
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes", "firecrawl-api": "x"})
    install_vm_fs_simulation(mocker, cfg)
    _stub_sidecar_reups(mocker)
    # Plan confirm accepted; sidecar-loss consent declined.
    mocks["confirm"].side_effect = [True, False]

    with pytest.raises(UserError, match="sidecar state"):
        vm.rebuild(config_path=config_path)

    mocks["export"].assert_not_called()
    mocks["delete_vm"].assert_not_called()


def test_rebuild_data_dir_in_vm_stages_and_restores_sidecar_fragments(
    mocker, tmp_path: Path
) -> None:
    """--data-dir-in-vm: the compose file (and with it the sidecar
    fragments + digest pins) lives on the doomed VM disk. The rebuild
    must stage the fragments host-side before the destroy, restore them
    into the freshly seeded compose, re-up the stack against the SAME
    pins, and warn that the bulk state (postgres, model weights) did
    not survive.
    """
    from agx_cli import compose as compose_mod
    from tests.conftest import install_vm_fs_simulation

    cfg = _make_config(tmp_path)
    cfg.paths.data_dir_in_vm = True
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    mocks = _stub_rebuild(mocker, cfg, {"hermes-coder": "hermes"})
    install_vm_fs_simulation(mocker, cfg)
    api_image = "ghcr.io/firecrawl/firecrawl@sha256:" + "a" * 64
    compose_doc = {
        "services": {
            "hermes-coder": {"image": "x:y"},
            "firecrawl-api": {"image": api_image},
        }
    }
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        json.dumps(compose_doc), encoding="utf-8"
    )

    def seed_fresh_vm(**kw) -> None:
        # What a real init does in this mode: brand-new VM disk, compose
        # seeded empty. The old fragments are GONE unless staged.
        (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )

    mocks["init"].side_effect = seed_fresh_vm
    staging = cfg.paths.host_data_dir / ".rebuild-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "coder-stamp.tar.zst").write_bytes(b"")
    sidecars = _stub_sidecar_reups(mocker)
    mocker.patch("agx_cli.ui.emit")
    warn = mocker.patch("agx_cli.ui.warn")

    vm.rebuild(yes=True, config_path=config_path)

    # Fragments staged host-side before the destroy...
    staged = json.loads((staging / "sidecar-services.json").read_text(encoding="utf-8"))
    assert staged["firecrawl-api"]["image"] == api_image
    # ...and restored into the fresh compose with the exact pin intact.
    doc = compose_mod.read_compose_doc(cfg)
    assert doc["services"]["firecrawl-api"]["image"] == api_image
    # The re-up phase then finds the stack and brings it back.
    sidecars["firecrawl_up"].assert_called_once()
    # The bulk-state loss is surfaced, never silent.
    warnings = [c.args[0] for c in warn.call_args_list]
    assert any("postgres starts empty" in w and "re-pulled" in w for w in warnings)


# ---------- bare-metal container-log rotation hint ----------


def test_bare_metal_init_warns_when_daemon_json_lacks_rotation(
    tmp_path: Path, mocker, monkeypatch
) -> None:
    """The verify-only bare-metal bootstrap can't write daemon.json the way
    the VM bootstrap does, so init must point out the gap before agents
    (and their unbounded logs) exist."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    warn = mocker.patch("agx_cli.ui.warn")
    vm._warn_bare_metal_log_rotation()
    assert warn.call_count == 1
    assert "max-size" in warn.call_args.args[0]


def test_bare_metal_init_quiet_when_daemon_json_rotates(
    tmp_path: Path, mocker, monkeypatch
) -> None:
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "daemon.json").write_text(
        '{"log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "3"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    warn = mocker.patch("agx_cli.ui.warn")
    vm._warn_bare_metal_log_rotation()
    warn.assert_not_called()
