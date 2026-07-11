"""export_cmd.py: export, import, host-cron round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents_cli import config as cfg_mod
from agents_cli import export_cmd
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
    from agents_cli.archive import ARCHIVE_SUFFIX, extract_tar_zst

    archive = export_cmd.export("coder", config_path=with_coder)
    assert archive.parent == cfg.paths.host_data_dir / "backups"
    assert archive.name.endswith(ARCHIVE_SUFFIX)
    # Archive contains the seeded file
    extract_dest = cfg.paths.host_data_dir / ".test-extract"
    extract_dest.mkdir(exist_ok=True)
    extract_tar_zst(archive, extract_dest)
    extracted = list(extract_dest.rglob("*"))
    assert any(p.name == "state.db" for p in extracted)


def test_export_respects_to_directory(with_coder: Path, cfg: AgentsConfig, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    archive = export_cmd.export("coder", to=elsewhere, config_path=with_coder)
    assert archive.parent == elsewhere


def test_export_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        export_cmd.export("ghost", config_path=saved_config)


def test_prune_keeps_newest_n_and_deletes_rest(tmp_path: Path) -> None:
    """Auto-prune sorts on the ``<agent>-<utc>.tar.zst`` filename
    (ISO timestamps sort chronologically) and deletes everything
    older than the newest N. Anything not matching the pattern is
    left alone — user files, other agents' archives, the cron log.
    """
    d = tmp_path / "backups"
    d.mkdir()
    # Mixed bag of files: coder archives across many days, one bob
    # archive (different agent), plus untouchable user content.
    coder_stamps = [
        "20260518T003001Z",
        "20260518T063001Z",
        "20260519T003001Z",
        "20260519T063001Z",
        "20260520T003001Z",
        "20260520T063001Z",
    ]
    for s in coder_stamps:
        (d / f"coder-{s}.tar.zst").write_bytes(b"x")
    (d / "bob-20260518T003001Z.tar.zst").write_bytes(b"x")
    (d / ".export-cron.log").write_text("log", encoding="utf-8")
    (d / "user-manual-archive.tar.zst").write_bytes(b"x")

    deleted = export_cmd._prune_old_archives("coder", d, keep_last=3)

    # Newest 3 coder archives kept; older 3 deleted.
    kept = sorted(p.name for p in d.iterdir())
    assert "coder-20260520T063001Z.tar.zst" in kept
    assert "coder-20260520T003001Z.tar.zst" in kept
    assert "coder-20260519T063001Z.tar.zst" in kept
    assert "coder-20260518T003001Z.tar.zst" not in kept
    # Bob's archive untouched (per-agent isolation).
    assert "bob-20260518T003001Z.tar.zst" in kept
    # User files untouched (only the toolkit naming pattern is pruned).
    assert ".export-cron.log" in kept
    assert "user-manual-archive.tar.zst" in kept
    assert len(deleted) == 3


def test_prune_disabled_when_keep_last_is_zero(tmp_path: Path) -> None:
    """``keep_last = 0`` opts out of auto-prune. Useful for users who
    want unbounded history (e.g. relying on filesystem snapshots
    instead of duplicity for long history)."""
    d = tmp_path / "backups"
    d.mkdir()
    for s in ("20260518T003001Z", "20260519T003001Z", "20260520T003001Z"):
        (d / f"coder-{s}.tar.zst").write_bytes(b"x")
    deleted = export_cmd._prune_old_archives("coder", d, keep_last=0)
    assert deleted == []
    assert len(list(d.iterdir())) == 3


def test_prune_no_op_when_under_threshold(tmp_path: Path) -> None:
    """Fewer-or-equal-than-N archives → nothing to delete."""
    d = tmp_path / "backups"
    d.mkdir()
    for s in ("20260520T003001Z", "20260520T063001Z"):
        (d / f"coder-{s}.tar.zst").write_bytes(b"x")
    deleted = export_cmd._prune_old_archives("coder", d, keep_last=14)
    assert deleted == []
    assert len(list(d.iterdir())) == 2


def test_prune_handles_legacy_tar_gz_archives(tmp_path: Path) -> None:
    """Pre-G6 archives are .tar.gz, not .tar.zst. Auto-prune must
    treat both as toolkit-owned so users migrating from older
    versions don't end up with an unbounded old-format pile."""
    d = tmp_path / "backups"
    d.mkdir()
    (d / "coder-20260518T003001Z.tar.gz").write_bytes(b"x")  # legacy
    (d / "coder-20260519T003001Z.tar.zst").write_bytes(b"x")
    (d / "coder-20260520T003001Z.tar.zst").write_bytes(b"x")
    deleted = export_cmd._prune_old_archives("coder", d, keep_last=2)
    # The legacy .tar.gz is the oldest → gets deleted.
    assert len(deleted) == 1
    assert deleted[0].name == "coder-20260518T003001Z.tar.gz"


def test_export_skips_prune_when_to_is_custom(
    with_coder: Path, cfg: AgentsConfig, tmp_path: Path
) -> None:
    """User-provided ``--to <path>`` is treated as a destination the
    user manages themselves — never auto-pruned. Otherwise a user
    pointing ``--to`` at a shared dir (a NAS mount, a different
    project's backup folder) would have their other files deleted
    surprise-style.
    """
    elsewhere = tmp_path / "elsewhere"
    # Pre-seed older archives at the custom location.
    elsewhere.mkdir()
    for s in ("20260518T003001Z", "20260519T003001Z", "20260520T003001Z"):
        (elsewhere / f"coder-{s}.tar.zst").write_bytes(b"x")
    cfg.export_cron.keep_last = 1  # would prune to 1 if not custom-dir
    cfg_mod.save(cfg, with_coder)

    export_cmd.export("coder", to=elsewhere, config_path=with_coder)

    # All 3 pre-existing archives still there + 1 new one = 4 total.
    matching = sorted(p.name for p in elsewhere.glob("coder-*.tar.zst"))
    assert len(matching) == 4


def test_archive_dir_defaults_to_backups_in_both_modes(cfg: AgentsConfig, tmp_path: Path) -> None:
    """``--to`` was previously required under ``--data-dir-in-vm`` —
    forcing the user to type a path on every invocation. The default
    ``<data-dir>/backups/`` is a valid host path in both modes (the
    host backup tool sees it either way), so the toolkit defaults
    there in both modes.
    """
    cfg.paths.data_dir_in_vm = False
    assert export_cmd._archive_dir(cfg, None) == cfg.paths.host_data_dir / "backups"
    cfg.paths.data_dir_in_vm = True
    assert export_cmd._archive_dir(cfg, None) == cfg.paths.host_data_dir / "backups"
    # --to overrides the default regardless of mode.
    override = tmp_path / "elsewhere"
    cfg.paths.data_dir_in_vm = False
    assert export_cmd._archive_dir(cfg, override) == override
    cfg.paths.data_dir_in_vm = True
    assert export_cmd._archive_dir(cfg, override) == override


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


HOSTILE_ARCHIVE_NAME = "a;rm -rf $HOME;.tar.zst"


def test_import_vm_temp_path_excludes_user_filename(with_coder: Path, cfg: AgentsConfig) -> None:
    """The VM-side temp path lands inside (sudo'd) mp.exec_bash script
    text — interpolating the user-supplied archive filename there is
    shell injection. The temp name must be built only from generated
    parts (validated agent name + pid + uuid), and the import must
    still work end-to-end with a hostile filename."""
    import re
    import shutil

    from agents_cli import mp

    archive = export_cmd.export("coder", config_path=with_coder)
    hostile = archive.parent / HOSTILE_ARCHIVE_NAME
    archive.rename(hostile)
    shutil.rmtree(cfg.paths.host_data_dir / "coder")

    export_cmd.import_archive("coder", hostile, yes=True, config_path=with_coder)

    # End-to-end restore still works despite the hostile name.
    assert (cfg.paths.host_data_dir / "coder" / ".data" / "state.db").read_text() == "fake-db"
    # No shell text ever saw the filename.
    scripts = [c.args[1] for c in mp.exec_bash.call_args_list if "tar -xf" in c.args[1]]
    assert scripts, "expected an extraction script"
    for script in scripts:
        assert "rm -rf $HOME" not in script
        assert HOSTILE_ARCHIVE_NAME not in script
        assert re.search(r"/tmp/agents-import-coder-\d+-[0-9a-f]{8}\.tar\b", script)
    # The transfer destination (also a shell-adjacent VM path) is clean too.
    vm_dst = mp.transfer_to_vm.call_args.args[2]
    assert "rm -rf" not in vm_dst and HOSTILE_ARCHIVE_NAME not in vm_dst


def test_import_in_vm_mode_temp_path_excludes_user_filename(
    cfg: AgentsConfig, saved_config: Path, tmp_path: Path, mocker
) -> None:
    """Same guarantee for the --data-dir-in-vm branch (non-sudo'd but
    still shell text)."""
    import re

    from tests.conftest import install_vm_fs_simulation

    mocks = install_vm_fs_simulation(mocker, cfg)
    cfg.paths.data_dir_in_vm = True
    cfg_mod.save(cfg, saved_config)
    hostile = tmp_path / HOSTILE_ARCHIVE_NAME
    hostile.write_bytes(b"not-really-a-tar")

    export_cmd.import_archive("coder", hostile, yes=True, config_path=saved_config)

    script = mocks["exec_bash"].call_args.args[1]
    assert "rm -rf $HOME" not in script
    assert HOSTILE_ARCHIVE_NAME not in script
    assert re.search(r"/tmp/agents-import-coder-\d+-[0-9a-f]{8}\.tar\b", script)


# ---------- remove --export-first (CLI ordering) ----------


def test_remove_export_first_declined_leaves_no_archive(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    """The confirmation must precede the export: a declined removal used
    to leave a fresh tarball (and an extra JSON object) behind."""
    from click.testing import CliRunner

    from agents_cli import cli as cli_mod

    mocker.patch("agents_cli.config.load", return_value=cfg)
    confirm = mocker.patch("agents_cli.ui.confirm", return_value=False)

    result = CliRunner().invoke(cli_mod.main, ["remove", "coder", "--export-first"])

    assert result.exit_code == 1
    confirm.assert_called_once()
    backups = cfg.paths.host_data_dir / "backups"
    assert not backups.exists() or not list(backups.glob("coder-*"))
    # Agent untouched.
    assert (cfg.paths.host_data_dir / "coder").is_dir()


def test_remove_export_first_confirms_then_exports_then_removes(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    from click.testing import CliRunner

    from agents_cli import cli as cli_mod

    mocker.patch("agents_cli.config.load", return_value=cfg)
    order: list[str] = []
    mocker.patch(
        "agents_cli.ui.confirm",
        side_effect=lambda *a, **k: order.append("confirm") or True,
    )
    mocker.patch(
        "agents_cli.export_cmd.export",
        side_effect=lambda *a, **k: order.append("export"),
    )
    remove_mock = mocker.patch(
        "agents_cli.agents_cmd.remove",
        side_effect=lambda *a, **k: order.append("remove"),
    )

    result = CliRunner().invoke(cli_mod.main, ["remove", "coder", "--export-first"])

    assert result.exit_code == 0, result.output
    assert order == ["confirm", "export", "remove"]
    # Consent already given — must not prompt a second time downstream.
    assert remove_mock.call_args.kwargs["yes"] is True


def test_remove_export_first_with_yes_skips_prompt_but_still_exports(
    with_coder: Path, cfg: AgentsConfig, mocker
) -> None:
    from click.testing import CliRunner

    from agents_cli import cli as cli_mod

    mocker.patch("agents_cli.config.load", return_value=cfg)
    confirm = mocker.patch("agents_cli.ui.confirm", return_value=True)
    export_mock = mocker.patch("agents_cli.export_cmd.export")
    remove_mock = mocker.patch("agents_cli.agents_cmd.remove")

    result = CliRunner().invoke(cli_mod.main, ["remove", "coder", "--export-first", "-y"])

    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    export_mock.assert_called_once()
    assert remove_mock.call_args.kwargs["yes"] is True


def test_remove_export_first_unknown_agent_fails_before_prompt_and_export(
    cfg: AgentsConfig, saved_config: Path, mocker
) -> None:
    from click.testing import CliRunner

    from agents_cli import cli as cli_mod

    mocker.patch("agents_cli.config.load", return_value=cfg)
    confirm = mocker.patch("agents_cli.ui.confirm", return_value=True)
    export_mock = mocker.patch("agents_cli.export_cmd.export")

    result = CliRunner().invoke(cli_mod.main, ["remove", "ghost", "--export-first"])

    assert result.exit_code == 1
    confirm.assert_not_called()
    export_mock.assert_not_called()


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


def test_cron_install_accepts_comma_separated_times(
    saved_config: Path, fake_crontab: FakeCrontab
) -> None:
    """Multi-times-a-day backups: comma-separated --time installs one
    crontab line per time, all carrying the same marker."""
    export_cmd.export_cron_install(time="02:30,08:30,14:30,20:30", config_path=saved_config)
    # Four marker lines, one per time.
    assert fake_crontab.body.count(export_cmd.CRON_MARKER) == 4
    assert "30 2 * * *" in fake_crontab.body
    assert "30 8 * * *" in fake_crontab.body
    assert "30 14 * * *" in fake_crontab.body
    assert "30 20 * * *" in fake_crontab.body


def test_cron_install_replaces_all_existing_marker_entries(
    saved_config: Path, fake_crontab: FakeCrontab
) -> None:
    """Re-installing with a new schedule must replace ALL prior toolkit
    entries, not append. Otherwise running install twice doubles them."""
    export_cmd.export_cron_install(time="02:30,14:30", config_path=saved_config)
    export_cmd.export_cron_install(time="06:00", config_path=saved_config)
    assert fake_crontab.body.count(export_cmd.CRON_MARKER) == 1
    assert "0 6 * * *" in fake_crontab.body
    assert "30 2" not in fake_crontab.body  # the old 02:30 is gone


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
