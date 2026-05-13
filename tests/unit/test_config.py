"""config.py: schema validation, round-trip, data-dir refusal rules."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from agents_cli import config as cfg_mod
from agents_cli.config import AgentsConfig, PathsConfig, load, save
from agents_cli.errors import UserError


def test_minimal_config_loads(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        tomlkit.dumps(
            {
                "paths": {"host_data_dir": str(tmp_path / "data")},
            }
        ),
        encoding="utf-8",
    )
    c = load(p)
    assert c.paths.host_data_dir == tmp_path / "data"
    assert c.vm.name == "agents"
    assert c.vm.ubuntu_release == "24.04"


def test_load_missing_raises_user_error(tmp_path: Path) -> None:
    with pytest.raises(UserError):
        load(tmp_path / "does-not-exist.toml")


def test_load_malformed_raises_user_error(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("not = valid = toml", encoding="utf-8")
    with pytest.raises(UserError):
        load(p)


def test_load_extra_keys_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        tomlkit.dumps(
            {
                "paths": {"host_data_dir": str(tmp_path / "data")},
                "unknown_section": {"x": 1},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(UserError):
        load(p)


def test_data_dir_under_var_snap_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PathsConfig(host_data_dir="/var/snap/multipass/data")


def test_data_dir_under_home_snap_rejected() -> None:
    with pytest.raises(ValueError):
        PathsConfig(host_data_dir="/home/alice/snap/multipass/data")


def test_data_dir_relative_path_rejected() -> None:
    with pytest.raises(ValueError):
        PathsConfig(host_data_dir="data/agents")


def test_data_dir_normal_absolute_path_ok(tmp_path: Path) -> None:
    p = PathsConfig(host_data_dir=str(tmp_path / "data"))
    assert p.host_data_dir == tmp_path / "data"


def test_export_cron_time_validation() -> None:
    from agents_cli.config import ExportCronConfig

    ExportCronConfig(time="02:30")
    ExportCronConfig(time="23:59")
    # Comma-separated multi-time schedules are valid.
    ExportCronConfig(time="02:30,08:30,14:30,20:30")
    ExportCronConfig(time="06:00,18:00")
    with pytest.raises(ValueError):
        ExportCronConfig(time="25:00")
    with pytest.raises(ValueError):
        ExportCronConfig(time="2:30")  # not zero-padded splits into 1+2 char halves, still digits
    with pytest.raises(ValueError):
        ExportCronConfig(time="x:y")
    with pytest.raises(ValueError):
        ExportCronConfig(time="0230")
    # If ANY time in the list is invalid, the whole value is rejected.
    with pytest.raises(ValueError):
        ExportCronConfig(time="02:30,25:00")
    with pytest.raises(ValueError):
        ExportCronConfig(time="02:30,2:30")


def test_round_trip_preserves_comments(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    initial = (
        "# user added this comment\n"
        "[paths]\n"
        f'host_data_dir = "{tmp_path / "data"}"\n'
        "\n"
        "[defaults]\n"
        "# pinned for stability\n"
        'hermes_image_tag = "v1.0.0"\n'
    )
    p.write_text(initial, encoding="utf-8")

    c = load(p)
    c.defaults.hermes_image_tag = "v1.0.1"
    save(c, p)

    after = p.read_text(encoding="utf-8")
    assert "# user added this comment" in after
    assert "# pinned for stability" in after
    assert 'hermes_image_tag = "v1.0.1"' in after


def test_vm_runtime_is_populated_flag() -> None:
    c = AgentsConfig(paths=PathsConfig(host_data_dir="/tmp/data"))
    assert not c.vm_runtime.is_populated()
    c.vm_runtime.docker_socket_path = "/run/user/1000/docker.sock"
    c.vm_runtime.ubuntu_uid = 1000
    assert c.vm_runtime.is_populated()


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "dir" / "config.toml"
    c = AgentsConfig(paths=PathsConfig(host_data_dir=str(tmp_path / "data")))
    save(c, p)
    assert p.exists()


def test_default_config_path_is_user_config_dir() -> None:
    expected = Path.home() / ".config" / "agents" / "config.toml"
    assert expected == cfg_mod.DEFAULT_CONFIG_PATH
