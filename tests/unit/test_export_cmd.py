"""export_cmd.py: export, import, host-cron round-trip."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from agents_cli import compose, export_cmd
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import create
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


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
def with_coder(cfg: AgentsConfig, saved_config: Path, mocker) -> Path:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    # Seed some agent data
    (cfg.paths.host_data_dir / "coder" / ".data" / "state.db").write_text(
        "fake-db", encoding="utf-8"
    )
    return saved_config


# ---------- export ----------


def test_export_writes_archive_into_backups_dir(with_coder: Path, cfg: AgentsConfig) -> None:
    archive = export_cmd.export("coder", config_path=with_coder)
    assert archive.parent == cfg.paths.host_data_dir / "backups"
    assert archive.suffix == ".gz"
    # Archive contains the seeded file
    with tarfile.open(archive, "r:gz") as tf:
        names = tf.getnames()
    assert any(n.endswith("state.db") for n in names)


def test_export_respects_to_directory(with_coder: Path, cfg: AgentsConfig, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    archive = export_cmd.export("coder", to=elsewhere, config_path=with_coder)
    assert archive.parent == elsewhere


def test_export_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        export_cmd.export("ghost", config_path=saved_config)


def test_export_under_data_dir_in_vm_requires_to(
    cfg: AgentsConfig, saved_config: Path, mocker
) -> None:
    """Toggling data_dir_in_vm in the saved config, export must demand --to."""
    from tests.conftest import install_vm_fs_simulation

    cfg.paths.data_dir_in_vm = True
    cfg_mod.save(cfg, saved_config)
    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    # Manually inject a service
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.add_service(doc, "hermes-coder", {"image": "x:y"})
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    with pytest.raises(UserError, match="--to"):
        export_cmd.export("coder", config_path=saved_config)


def test_export_all_iterates_every_agent(cfg: AgentsConfig, saved_config: Path, mocker) -> None:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    # Seed data
    (cfg.paths.host_data_dir / "coder" / ".data" / "x").write_text("c", encoding="utf-8")
    (cfg.paths.host_data_dir / "personal" / ".openclaw" / "x").write_text("p", encoding="utf-8")

    archives = export_cmd.export_all(config_path=saved_config)
    names = sorted(a.stem.split("-")[0] for a in archives)
    assert names == ["coder", "personal"]


# ---------- import ----------


def test_import_extracts_into_data_dir(with_coder: Path, cfg: AgentsConfig) -> None:
    archive = export_cmd.export("coder", config_path=with_coder)
    # Wipe and reimport
    import shutil

    shutil.rmtree(cfg.paths.host_data_dir / "coder")
    export_cmd.import_archive("coder", archive, yes=True, config_path=with_coder)
    assert (cfg.paths.host_data_dir / "coder" / ".data" / "state.db").read_text() == "fake-db"


def test_import_refuses_missing_archive(saved_config: Path, tmp_path: Path) -> None:
    with pytest.raises(UserError, match="archive not found"):
        export_cmd.import_archive("coder", tmp_path / "nothing.tar.gz", config_path=saved_config)


# ---------- host-cron ----------


class FakeCrontab:
    """In-memory crontab for tests; install via mocker.patch on the helpers."""

    def __init__(self, initial: str = "") -> None:
        self.body = initial
        self.raise_on_empty = False

    def read(self) -> str:
        return self.body

    def write(self, content: str) -> None:
        self.body = content


@pytest.fixture
def fake_crontab(mocker) -> FakeCrontab:
    ct = FakeCrontab()
    mocker.patch("agents_cli.export_cmd._read_crontab", side_effect=lambda: ct.read())
    mocker.patch(
        "agents_cli.export_cmd._write_crontab", side_effect=lambda content: ct.write(content)
    )
    return ct


def test_cron_install_adds_marker(saved_config: Path, fake_crontab: FakeCrontab) -> None:
    export_cmd.export_cron_install(time="03:15", config_path=saved_config)
    assert export_cmd.CRON_MARKER in fake_crontab.body
    assert "15 3 * * *" in fake_crontab.body


def test_cron_install_is_idempotent_and_replaces_existing(
    saved_config: Path, fake_crontab: FakeCrontab
) -> None:
    export_cmd.export_cron_install(time="02:30", config_path=saved_config)
    export_cmd.export_cron_install(time="04:00", config_path=saved_config)
    # Only one marker entry survives
    assert fake_crontab.body.count(export_cmd.CRON_MARKER) == 1
    assert "0 4 * * *" in fake_crontab.body


def test_cron_install_preserves_unrelated_entries(
    saved_config: Path, fake_crontab: FakeCrontab
) -> None:
    fake_crontab.body = "0 0 * * * /home/user/my-other-job\n"
    export_cmd.export_cron_install(time="02:30", config_path=saved_config)
    assert "my-other-job" in fake_crontab.body
    assert export_cmd.CRON_MARKER in fake_crontab.body


def test_cron_uninstall_removes_marker(saved_config: Path, fake_crontab: FakeCrontab) -> None:
    export_cmd.export_cron_install(time="02:30", config_path=saved_config)
    export_cmd.export_cron_uninstall(config_path=saved_config)
    assert export_cmd.CRON_MARKER not in fake_crontab.body


def test_cron_status_reports_installed(saved_config: Path, fake_crontab: FakeCrontab) -> None:
    export_cmd.export_cron_install(time="02:30", config_path=saved_config)
    info = export_cmd.export_cron_status(config_path=saved_config)
    assert info["installed"] is True
    assert info["scheduled_time"] == "02:30"


def test_cron_status_reports_uninstalled_when_no_marker(
    saved_config: Path, fake_crontab: FakeCrontab
) -> None:
    info = export_cmd.export_cron_status(config_path=saved_config)
    assert info["installed"] is False


def test_cron_install_handles_first_ever_empty_crontab(
    saved_config: Path, fake_crontab: FakeCrontab
) -> None:
    """`crontab -l` returns exit 1 with `no crontab for user` on stderr the
    first time. _read_crontab handles that by returning '' — test the install
    flow against a fully empty state."""
    fake_crontab.body = ""
    export_cmd.export_cron_install(time="02:30", config_path=saved_config)
    assert export_cmd.CRON_MARKER in fake_crontab.body


def test_invalid_time_rejected(saved_config: Path) -> None:
    with pytest.raises(UserError, match="HH:MM"):
        export_cmd.export_cron_install(time="2:30", config_path=saved_config)
    with pytest.raises(UserError, match="HH:MM"):
        export_cmd.export_cron_install(time="0230", config_path=saved_config)
    with pytest.raises(UserError, match="range"):
        export_cmd.export_cron_install(time="25:00", config_path=saved_config)


# ---------- crontab read/write helpers ----------


def test_read_crontab_handles_no_crontab_yet(mocker) -> None:
    class StubProc:
        returncode = 1
        stdout = b""
        stderr = b"no crontab for alice\n"

    mocker.patch("subprocess.run", return_value=StubProc())
    assert export_cmd._read_crontab() == ""


def test_read_crontab_returns_existing_lines(mocker) -> None:
    class StubProc:
        returncode = 0
        stdout = b"0 0 * * * job\n"
        stderr = b""

    mocker.patch("subprocess.run", return_value=StubProc())
    assert export_cmd._read_crontab() == "0 0 * * * job\n"


def test_read_crontab_raises_on_unexpected_failure(mocker) -> None:
    class StubProc:
        returncode = 1
        stdout = b""
        stderr = b"crontab: SELinux says no\n"

    mocker.patch("subprocess.run", return_value=StubProc())
    with pytest.raises(RuntimeFailure):
        export_cmd._read_crontab()
