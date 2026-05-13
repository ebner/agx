"""vm.py: orchestration tests with all Incus calls mocked at the mp.* boundary.

The unit suite does not touch a real VM. Integration coverage lives behind
the `multipass` pytest marker (kept as the name for historical compat;
the marker now means "actually drives Incus end-to-end").
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents_cli import config as cfg_mod
from agents_cli import vm
from agents_cli.config import AgentsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import HealthCheckFailure, RuntimeFailure, UserError
from agents_cli.mp import MpResult

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
    mocker.patch("agents_cli.mp.find", return_value=_ok(json.dumps(catalog)))
    assert vm._select_ubuntu_image("26.04") == "26.04"


def test_select_ubuntu_image_falls_back_to_24_04_when_missing(mocker) -> None:
    catalog = {"images": {"x": {"aliases": ["24.04", "noble"]}}}
    mocker.patch("agents_cli.mp.find", return_value=_ok(json.dumps(catalog)))
    assert vm._select_ubuntu_image("26.04") == "24.04"


def test_select_ubuntu_image_returns_requested_when_find_fails(mocker) -> None:
    mocker.patch("agents_cli.mp.find", side_effect=RuntimeFailure("network"))
    assert vm._select_ubuntu_image("26.04") == "26.04"


def test_select_ubuntu_image_handles_bad_json(mocker) -> None:
    mocker.patch("agents_cli.mp.find", return_value=_ok("not json"))
    assert vm._select_ubuntu_image("26.04") == "26.04"


# ---------- _vm_exists ----------


def test_vm_exists_true(mocker) -> None:
    listing = {"list": [{"name": "agents"}, {"name": "other"}]}
    mocker.patch("agents_cli.mp.list_vms", return_value=_ok(json.dumps(listing)))
    assert vm._vm_exists("agents") is True


def test_vm_exists_false(mocker) -> None:
    listing = {"list": [{"name": "other"}]}
    mocker.patch("agents_cli.mp.list_vms", return_value=_ok(json.dumps(listing)))
    assert vm._vm_exists("agents") is False


def test_vm_exists_handles_list_failure(mocker) -> None:
    mocker.patch("agents_cli.mp.list_vms", side_effect=RuntimeFailure("x"))
    assert vm._vm_exists("agents") is False


# ---------- _unique_snapshot_name ----------


def test_unique_snapshot_name_returns_base_when_no_collision(mocker) -> None:
    info = {"info": {"agents": {"snapshots": {"foo": {}}}}}
    mocker.patch("agents_cli.mp.info_vm", return_value=_ok(json.dumps(info)))
    assert vm._unique_snapshot_name("agents", "bar") == "bar"


def test_unique_snapshot_name_appends_counter_on_collision(mocker) -> None:
    info = {"info": {"agents": {"snapshots": {"snap": {}, "snap-2": {}}}}}
    mocker.patch("agents_cli.mp.info_vm", return_value=_ok(json.dumps(info)))
    assert vm._unique_snapshot_name("agents", "snap") == "snap-3"


def test_unique_snapshot_name_handles_info_failure(mocker) -> None:
    mocker.patch("agents_cli.mp.info_vm", side_effect=RuntimeFailure("x"))
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

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=fake_runner)
    vm._mount_probe(cfg)


def test_mount_probe_raises_when_round_trip_fails(mocker, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.paths.host_data_dir.mkdir()
    mocker.patch(
        "agents_cli.mp.exec_in_vm",
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
    # info_vm returns the mount already present (since launch sets it
    # up inline on Incus). `state: Running` keeps `_wait_for_agent` and
    # `_vm_running` happy.
    running_info = json.dumps(
        {
            "info": {
                "agents": {
                    "state": "Running",
                    "mounts": {"/home/ubuntu/agents": {"source_path": "/data"}},
                    "snapshots": {},
                    "ipv4": [],
                }
            }
        }
    )
    return {
        "find": mocker.patch("agents_cli.mp.find", return_value=_ok(catalog)),
        "list_vms": mocker.patch(
            "agents_cli.mp.list_vms", return_value=_ok(json.dumps({"list": []}))
        ),
        "launch": mocker.patch("agents_cli.mp.launch", return_value=_ok()),
        "info_vm": mocker.patch(
            "agents_cli.mp.info_vm",
            return_value=_ok(running_info),
        ),
        "mount": mocker.patch("agents_cli.mp.mount", return_value=_ok()),
        # _wait_for_agent polls exec_in_vm. Returning success on first
        # call keeps tests fast.
        "exec_in_vm": mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok()),
        "bootstrap": mocker.patch(
            "agents_cli.mp.exec_bash", return_value=_ok(stdout=bootstrap_output)
        ),
        "fetch": mocker.patch("agents_cli.vm._fetch_latest_tag", return_value=None),
    }


def test_init_full_flow(mocker, tmp_path: Path) -> None:
    mocks = _stub_all_mp(mocker)
    data_dir = tmp_path / "data"
    config_path = _saved_config(tmp_path)

    vm.init(data_dir=data_dir, config_path=config_path)

    # Launch wires up the host-dir share inline (raw.idmap + disk device
    # in the launch YAML). Standalone mp.mount is NOT called during
    # init — it's only the fallback for drift in `_ensure_mount`.
    mocks["launch"].assert_called_once()
    _, launch_kwargs = mocks["launch"].call_args
    assert launch_kwargs["host_data_dir"] == data_dir
    assert launch_kwargs["vm_data_path"] == "/home/ubuntu/agents"
    assert launch_kwargs["host_uid"] is not None
    assert launch_kwargs["host_gid"] is not None
    mocks["mount"].assert_not_called()
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

    vm.init(data_dir=tmp_path / "data", config_path=_saved_config(tmp_path))

    mocks["launch"].assert_not_called()
    # info_vm reports the mount is already present (fresh re-init on an
    # existing VM whose device persisted), so _ensure_mount short-circuits
    # and mp.mount isn't called.
    mocks["mount"].assert_not_called()


def test_init_uses_baked_in_fallback_tags_when_github_unreachable(mocker, tmp_path: Path) -> None:
    _stub_all_mp(mocker)  # `_fetch_latest_tag` already stubbed to None
    config_path = _saved_config(tmp_path)
    vm.init(data_dir=tmp_path / "data", config_path=config_path)
    cfg = cfg_mod.load(config_path)
    from agents_cli.templates import FALLBACK_HERMES_IMAGE_TAG, FALLBACK_OPENCLAW_IMAGE_TAG

    assert cfg.defaults.hermes_image_tag == FALLBACK_HERMES_IMAGE_TAG
    assert cfg.defaults.openclaw_image_tag == FALLBACK_OPENCLAW_IMAGE_TAG


def test_init_uses_live_tags_when_github_succeeds(mocker, tmp_path: Path) -> None:
    mocks = _stub_all_mp(mocker)
    mocks["fetch"].side_effect = lambda repo: f"v{repo}.live"
    config_path = _saved_config(tmp_path)
    vm.init(data_dir=tmp_path / "data", config_path=config_path)
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
        vm.init(data_dir=tmp_path / "data", config_path=_saved_config(tmp_path))


def test_init_falls_back_when_release_not_in_catalog(mocker, tmp_path: Path) -> None:
    mocks = _stub_all_mp(mocker)
    mocks["find"].return_value = _ok(json.dumps({"images": {"x": {"aliases": ["24.04"]}}}))
    config_path = _saved_config(tmp_path)
    vm.init(data_dir=tmp_path / "data", config_path=config_path)
    cfg = cfg_mod.load(config_path)
    assert cfg.vm.ubuntu_release == "24.04"


def test_init_respects_ubuntu_release_override(mocker, tmp_path: Path) -> None:
    """`--ubuntu-release 26.04` should be honored when the catalog lists it."""
    mocks = _stub_all_mp(mocker)
    catalog = json.dumps({"images": {"x": {"aliases": ["24.04", "26.04"]}}})
    mocks["find"].return_value = _ok(catalog)
    config_path = _saved_config(tmp_path)
    vm.init(data_dir=tmp_path / "data", ubuntu_release="26.04", config_path=config_path)
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
        vm.init(data_dir=tmp_path / "data", config_path=_saved_config(tmp_path))
    msg = str(exc.value)
    assert "curl: (28) Connection timed out" in msg
    assert "bootstrap stdout" in msg


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
            data_dir=tmp_path / "data",
            ubuntu_release="26.04",
            config_path=_saved_config(tmp_path),
        )


# ---------- destroy ----------


def test_destroy_requires_confirmation(mocker, tmp_path: Path) -> None:
    config_path = _saved_config(tmp_path)
    cfg_mod.save(_make_config(tmp_path), config_path)
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    delete = mocker.patch("agents_cli.mp.delete_vm")
    with pytest.raises(UserError, match="aborted"):
        vm.destroy(config_path=config_path)
    delete.assert_not_called()


def test_destroy_with_yes_skips_prompt_and_purges(mocker, tmp_path: Path) -> None:
    config_path = _saved_config(tmp_path)
    cfg_mod.save(_make_config(tmp_path), config_path)
    delete = mocker.patch("agents_cli.mp.delete_vm", return_value=_ok())
    vm.destroy(yes=True, config_path=config_path)
    delete.assert_called_once()
    _, kwargs = delete.call_args
    assert kwargs.get("purge") is True


# ---------- resize ----------


def _stub_resize_mp(mocker) -> dict:
    return {
        "stop": mocker.patch("agents_cli.mp.stop_vm", return_value=_ok()),
        "start": mocker.patch("agents_cli.mp.start_vm", return_value=_ok()),
        "set_cpus": mocker.patch("agents_cli.mp.set_cpus", return_value=_ok()),
        "set_memory_gb": mocker.patch("agents_cli.mp.set_memory_gb", return_value=_ok()),
        "set_disk_gb": mocker.patch("agents_cli.mp.set_disk_gb", return_value=_ok()),
        "info_vm": mocker.patch(
            "agents_cli.mp.info_vm",
            return_value=_ok(
                json.dumps(
                    {
                        "info": {
                            "agents": {
                                "state": "Running",
                                "mounts": {"/home/ubuntu/agents": {}},
                                "snapshots": {},
                                "ipv4": [],
                            }
                        }
                    }
                )
            ),
        ),
        "exec_in_vm": mocker.patch(
            "agents_cli.mp.exec_in_vm",
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
    mocks["set_cpus"].assert_called_once_with("agents", 8)
    mocks["set_memory_gb"].assert_called_once_with("agents", 16)
    mocks["set_disk_gb"].assert_called_once_with("agents", 50)

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

    mocks["set_cpus"].assert_called_once_with("agents", 12)
    mocks["set_memory_gb"].assert_not_called()
    mocks["set_disk_gb"].assert_not_called()

    reloaded = cfg_mod.load(config_path)
    assert reloaded.vm.cpus == 12
    # memory_gb / disk_gb unchanged from the saved config
    assert reloaded.vm.memory_gb == cfg.vm.memory_gb
    assert reloaded.vm.disk_gb == cfg.vm.disk_gb


def test_resize_starts_vm_even_if_set_raises(mocker, tmp_path: Path) -> None:
    """A failed ``incus config set`` must not leave the VM stopped."""
    cfg = _make_config(tmp_path)
    config_path = _saved_config(tmp_path)
    cfg_mod.save(cfg, config_path)
    cfg.paths.host_data_dir.mkdir(exist_ok=True)

    from agents_cli.errors import RuntimeFailure

    mocks = _stub_resize_mp(mocker)
    mocks["set_cpus"].side_effect = RuntimeFailure("incus config set failed")

    with pytest.raises(RuntimeFailure):
        vm.resize(cpus=8, config_path=config_path)

    mocks["stop"].assert_called_once()
    mocks["start"].assert_called_once()  # cleanup ran
