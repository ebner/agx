"""config.py: schema validation, round-trip, data-dir refusal rules."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from agx_cli import config as cfg_mod
from agx_cli.config import AgentsConfig, PathsConfig, load, save
from agx_cli.errors import UserError


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


@pytest.mark.parametrize("bad", ["/", "/agents", "/home/ubuntu/../../etc"])
def test_vm_agents_root_too_shallow_or_climbing_rejected(tmp_path: Path, bad: str) -> None:
    """`agx remove` deletes `<vm_agents_root>/<name>` as root in the VM."""
    with pytest.raises(ValueError):
        PathsConfig(host_data_dir=str(tmp_path / "data"), vm_agents_root=bad)


def test_vm_backend_accepts_three_backends() -> None:
    from agx_cli.config import VmConfig

    for backend in ("multipass", "libvirt", "bare_metal"):
        cfg = VmConfig(backend=backend)
        assert cfg.backend == backend


def test_vm_backend_rejects_unknown() -> None:
    from agx_cli.config import VmConfig

    with pytest.raises(ValueError, match="bare_metal"):
        VmConfig(backend="podman")


def test_export_cron_time_validation() -> None:
    from agx_cli.config import ExportCronConfig

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


def test_upgrades_keep_generations_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        f'[paths]\nhost_data_dir = "{tmp_path / "data"}"\n\n[upgrades]\nkeep_generations = 5\n',
        encoding="utf-8",
    )
    c = load(p)
    assert c.upgrades.keep_generations == 5
    c.upgrades.keep_generations = 0
    save(c, p)
    assert load(p).upgrades.keep_generations == 0


def test_upgrades_section_defaults_when_absent(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(f'[paths]\nhost_data_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    assert load(p).upgrades.keep_generations == 2


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


# ---------- [ollama] URL localhost-refusal (host-vs-VM trap) ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://0.0.0.0:11434",
        "https://0.0.0.0:11434",
        # urlsplit().hostname strips IPv6 brackets — the old hand-rolled
        # parse yielded "[" for this one and let it through.
        "http://[::1]:11434",
        # ...and lowercases — the old compare was case-sensitive.
        "http://LOCALHOST:11434",
        "http://LocalHost:11434",
        # userinfo must not mask the host either.
        "http://user:pass@localhost:11434",
    ],
)
def test_ollama_url_loopback_variants_refused(url: str) -> None:
    from agx_cli.config import OllamaConfig

    with pytest.raises(ValueError, match="not reachable from inside the VM"):
        OllamaConfig(url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.20.30.1:11434",  # multipass NAT gateway
        "http://192.168.1.50:11434",  # LAN IP
        "https://api.example.com/v1",  # hosted OpenAI-compatible
        "http://gpu-box.tail1234.ts.net:11434",  # Tailscale hostname
    ],
)
def test_ollama_url_reachable_forms_accepted(url: str) -> None:
    from agx_cli.config import OllamaConfig

    assert OllamaConfig(url=url).url == url


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
    expected = Path.home() / ".config" / "agx" / "config.toml"
    assert expected == cfg_mod.DEFAULT_CONFIG_PATH


def test_migrate_legacy_config_dir_moves_once_and_links(tmp_path: Path) -> None:
    legacy = tmp_path / "agents" / "config.toml"
    new = tmp_path / "agx" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("x = 1\n")
    assert cfg_mod.migrate_legacy_config_dir(new, legacy) is True
    assert new.read_text() == "x = 1\n"
    assert (tmp_path / "agents").is_symlink()
    assert legacy.read_text() == "x = 1\n"  # via the compat link
    # second run: nothing to do
    assert cfg_mod.migrate_legacy_config_dir(new, legacy) is False


def test_migrate_legacy_config_dir_never_overwrites(tmp_path: Path) -> None:
    legacy = tmp_path / "agents" / "config.toml"
    new = tmp_path / "agx" / "config.toml"
    legacy.parent.mkdir()
    new.parent.mkdir()
    legacy.write_text("old\n")
    new.write_text("new\n")
    assert cfg_mod.migrate_legacy_config_dir(new, legacy) is False
    assert new.read_text() == "new\n"
    assert legacy.read_text() == "old\n"


def test_migrate_legacy_config_dir_noop_without_legacy(tmp_path: Path) -> None:
    assert not cfg_mod.migrate_legacy_config_dir(
        tmp_path / "agx" / "c.toml", tmp_path / "agents" / "c.toml"
    )
    assert not (tmp_path / "agx").exists()
