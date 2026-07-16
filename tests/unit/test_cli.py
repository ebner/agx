"""cli.py: click wiring — exit-code contract, stdout discipline, prompt hygiene.

First CliRunner harness for the command tree. Backends are covered by
their own module tests; here the command-module functions are mocked
and the assertions target the *wiring*: exit codes (user error = 1,
never click's standalone-mode 2), exactly one JSON object on stdout
under ``--json``, and prompts that refuse instead of hanging in
non-interactive mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from agents_cli import cli as cli_mod
from agents_cli import ui
from agents_cli import vm as vm_mod
from agents_cli.config import AgentsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.mp import MpResult


def _make_cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
    )


def invoke(
    mocker: Any,
    args: list[str],
    *,
    cfg: AgentsConfig | None = None,
    input: str | None = None,
) -> Result:
    """Invoke the real command tree with the root callback's config load
    neutralized, so tests never read (or write) ``~/.config/agents/``.

    Pass ``cfg`` when the command under test loads config itself; the
    default raises like a first-run machine with no config, which the
    root callback treats as "dispatcher defaults to multipass".
    """
    if cfg is None:
        mocker.patch(
            "agents_cli.config.load",
            side_effect=FileNotFoundError("no config in tests"),
        )
    else:
        mocker.patch("agents_cli.config.load", return_value=cfg)
    return CliRunner().invoke(cli_mod.main, args, input=input)


def single_json_object(result: Result) -> dict[str, Any]:
    """Parse stdout as exactly one JSON object — json.loads raises on
    trailing data, so a second emitted object fails the test."""
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


# ---------- exit-code contract (item: standalone_mode) ----------


def test_missing_argument_exits_1_not_2(mocker: Any) -> None:
    """click's own parse errors are user errors → exit 1 per errors.py,
    not click's standalone-mode default of 2 (reserved for runtime
    failures)."""
    result = invoke(mocker, ["remove"])
    assert result.exit_code == 1
    assert "NAME" in result.output


def test_unknown_option_exits_1(mocker: Any) -> None:
    result = invoke(mocker, ["list", "--bogus-flag"])
    assert result.exit_code == 1


def test_body_raised_usage_error_exits_1(mocker: Any) -> None:
    """`raise click.UsageError` inside a command body (upgrade with
    neither NAME nor --all) takes the same route."""
    result = invoke(mocker, ["upgrade"])
    assert result.exit_code == 1
    assert "provide NAME or --all" in result.output


def test_parse_error_under_json_emits_error_object(mocker: Any) -> None:
    """Parse errors carry their click context, so the root --json flag
    is honored and the error surfaces as a JSON object."""
    result = invoke(mocker, ["--json", "remove"])
    assert result.exit_code == 1
    payload = single_json_object(result)
    assert "NAME" in payload["error"]
    assert payload["kind"] == "MissingParameter"


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--json", "--bogus"], id="unknown-root-option"),
        pytest.param(["--json", "bogus"], id="unknown-command"),
        pytest.param(["--json"], id="missing-command"),
    ],
)
def test_root_level_parse_errors_honor_json(mocker: Any, args: list[str]) -> None:
    """Parse errors that fire before (or instead of) the root callback
    leave ctx.obj unset — the handler must reconstruct the root flags
    (from click's parsed params or the raw argv) so --json still yields
    a single JSON error object, not human text."""
    result = invoke(mocker, args)
    assert result.exit_code == 1
    payload = single_json_object(result)
    assert payload["error"]
    assert payload["kind"]


def test_help_exits_0(mocker: Any) -> None:
    result = invoke(mocker, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_subcommand_help_exits_0(mocker: Any) -> None:
    result = invoke(mocker, ["vm", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_version_exits_0(mocker: Any) -> None:
    result = invoke(mocker, ["--version"])
    assert result.exit_code == 0


def test_keyboard_interrupt_in_command_exits_130(mocker: Any) -> None:
    mocker.patch("agents_cli.ops.doctor", side_effect=KeyboardInterrupt)
    result = invoke(mocker, ["doctor"])
    assert result.exit_code == 130


def test_prompt_eof_inside_run_body_exits_130_as_interrupt(mocker: Any) -> None:
    """Ctrl-C/EOF at a prompt inside a _run-wrapped body surfaces as
    click.Abort (click converts it before the prompt call returns) —
    that's an interruption (130), not an unexpected runtime failure (2)."""
    set_secret = mocker.patch("agents_cli.secrets_cmd.set_secret")
    # Empty stdin -> the hidden prompt hits EOF -> click.Abort.
    result = invoke(mocker, ["secret", "set", "coder", "API_KEY"], input="")
    assert result.exit_code == 130
    assert "interrupted" in result.output
    set_secret.assert_not_called()


def test_cli_error_exit_codes_pass_through(mocker: Any) -> None:
    """CliError semantics are untouched by the non-standalone switch:
    a HealthCheckFailure still maps to its declared exit code 3."""
    from agents_cli.errors import HealthCheckFailure

    mocker.patch("agents_cli.ops.doctor", side_effect=HealthCheckFailure("2 checks failed"))
    result = invoke(mocker, ["doctor"])
    assert result.exit_code == 3


# ---------- secret set prompt hygiene (item: no hidden prompt under --json) ----------


def test_secret_set_json_without_value_errors_instead_of_prompting(mocker: Any) -> None:
    set_secret = mocker.patch("agents_cli.secrets_cmd.set_secret")
    # input="" means an accidental prompt would hit EOF instead of
    # hanging — belt and suspenders for the regression this test pins.
    result = invoke(mocker, ["--json", "secret", "set", "coder", "API_KEY"], input="")
    assert result.exit_code == 1
    payload = single_json_object(result)
    assert "secret value required" in payload["error"]
    set_secret.assert_not_called()


def test_secret_set_with_value_argument_works_under_json(mocker: Any) -> None:
    set_secret = mocker.patch("agents_cli.secrets_cmd.set_secret")
    result = invoke(mocker, ["--json", "secret", "set", "coder", "API_KEY", "sk-x"])
    assert result.exit_code == 0
    set_secret.assert_called_once_with("coder", "API_KEY", "sk-x")


def test_secret_set_human_mode_prompts_hidden_when_value_omitted(mocker: Any) -> None:
    set_secret = mocker.patch("agents_cli.secrets_cmd.set_secret")
    result = invoke(mocker, ["secret", "set", "coder", "API_KEY"], input="sk-typed\n")
    assert result.exit_code == 0
    set_secret.assert_called_once_with("coder", "API_KEY", "sk-typed")
    # Hidden prompt: the typed value never appears in the output.
    assert "sk-typed" not in result.output


# ---------- remove --export-first (item: one stdout object) ----------


def test_remove_export_first_emits_exactly_one_json_object(mocker: Any) -> None:
    """The embedded export runs with emit=False; the removal result is
    the command's single stdout object."""

    def fake_export(name: str, **kwargs: Any) -> Path:
        # Faithful stand-in: the real export only emits when asked to.
        if kwargs.get("emit", True):
            ui.emit({"name": name, "archive": "/backups/x.tar.zst"})
        return Path("/backups/x.tar.zst")

    export = mocker.patch("agents_cli.export_cmd.export", side_effect=fake_export)
    mocker.patch(
        "agents_cli.agents_cmd.remove",
        side_effect=lambda name, **kw: ui.emit({"removed": name}),
    )

    result = invoke(mocker, ["--json", "--yes", "remove", "coder", "--export-first"])

    assert result.exit_code == 0, result.output
    payload = single_json_object(result)  # raises if export added a second object
    assert payload == {"removed": "coder"}
    assert export.call_args.kwargs["emit"] is False
    # The archive path still surfaces — as narration on stderr.
    assert "x.tar.zst" in result.stderr


# ---------- vm init (item: launch line is narration, ready payload is the object) ----------


_GOOD_BOOTSTRAP = (
    "BOOTSTRAP_OK uidmap=ok dbus=ok lingering=ok docker_host=ok daemon=ok "
    "user_socket_active=ok net=ok ubuntu_uid=1000 ubuntu_gid=1000 "
    "socket_path=/run/user/1000/docker.sock"
)


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _stub_init_mp(mocker: Any) -> None:
    """Stub every mp primitive vm.init touches (mirrors test_vm's stub)."""
    catalog = json.dumps({"images": {"x": {"aliases": ["24.04"]}}})
    mocker.patch("agents_cli.mp.find", return_value=_ok(catalog))
    mocker.patch("agents_cli.mp.list_vms", return_value=_ok(json.dumps({"list": []})))
    mocker.patch("agents_cli.mp.launch", return_value=_ok())
    mocker.patch(
        "agents_cli.mp.info_vm",
        return_value=_ok(json.dumps({"info": {"agents": {"mounts": {}}}})),
    )
    mocker.patch("agents_cli.mp.mount", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_bash", return_value=_ok(stdout=_GOOD_BOOTSTRAP))
    mocker.patch("agents_cli.vm._fetch_latest_tag", return_value=None)


def test_vm_init_emits_exactly_one_json_object(mocker: Any, tmp_path: Path) -> None:
    """End-to-end through the CLI: all narration (launch, bootstrap,
    mount, seed) lands on stderr; stdout carries only the final ready
    payload."""
    _stub_init_mp(mocker)
    # Re-target the real init at a throwaway config so the test never
    # touches ~/.config/agents/config.toml (the CLI command can't take
    # a config path).
    real_init = vm_mod.init
    config_path = tmp_path / "config.toml"
    mocker.patch(
        "agents_cli.vm.init",
        side_effect=lambda **kw: real_init(**{**kw, "config_path": config_path}),
    )

    result = invoke(mocker, ["--json", "vm", "init", "--data-dir", str(tmp_path / "data")])

    assert result.exit_code == 0, result.output
    payload = single_json_object(result)  # a second object would fail the parse
    assert payload["vm"] == "agents"
    assert payload["docker_socket"] == "/run/user/1000/docker.sock"
    # Narration went to stderr as structured progress events.
    assert "launching VM" in result.stderr
    assert "provisioning rootless Docker" in result.stderr


# ---------- ollama clear (item: confirm like sibling wipe commands) ----------


def test_ollama_clear_json_without_yes_refuses(mocker: Any, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    save = mocker.patch("agents_cli.config.save")

    result = invoke(mocker, ["--json", "ollama", "clear"], cfg=cfg)

    assert result.exit_code == 1
    payload = single_json_object(result)
    assert "Confirmation required" in payload["error"]
    save.assert_not_called()  # nothing wiped


def test_ollama_clear_with_yes_proceeds(mocker: Any, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    save = mocker.patch("agents_cli.config.save")

    result = invoke(mocker, ["--json", "ollama", "clear", "--yes"], cfg=cfg)

    assert result.exit_code == 0, result.output
    payload = single_json_object(result)
    assert payload == {"cleared": True}
    save.assert_called_once()


# ---------- vm migrate-data (item: wiring under the vm group) ----------


def test_vm_migrate_data_is_wired_under_vm_group_and_emits_one_object(mocker: Any) -> None:
    """Command exists under the vm group, ``--yes`` flows through as the
    keyword the backend expects, and stdout carries exactly one JSON
    object (the backend's own emit)."""
    migrate = mocker.patch(
        "agents_cli.migrate_cmd.migrate_data",
        side_effect=lambda **kw: ui.emit({"migrated": True}),
    )

    result = invoke(mocker, ["--json", "vm", "migrate-data", "--yes"])

    assert result.exit_code == 0, result.output
    payload = single_json_object(result)
    assert payload == {"migrated": True}
    assert migrate.call_args.kwargs["yes"] is True


def test_vm_migrate_data_local_yes_flag_parses(mocker: Any) -> None:
    migrate = mocker.patch("agents_cli.migrate_cmd.migrate_data")
    result = invoke(mocker, ["vm", "migrate-data", "-y"])
    assert result.exit_code == 0, result.output
    assert migrate.call_args.kwargs["yes"] is True


def test_vm_migrate_data_without_yes_passes_yes_false(mocker: Any) -> None:
    migrate = mocker.patch("agents_cli.migrate_cmd.migrate_data")
    result = invoke(mocker, ["vm", "migrate-data"])
    assert result.exit_code == 0, result.output
    assert migrate.call_args.kwargs["yes"] is False


def test_vm_migrate_data_json_emits_one_object_with_doctor_summary(
    mocker: Any, tmp_path: Path
) -> None:
    """End-to-end through the CLI with the REAL migrate_data: doctor runs
    with emit=False (its table render would be a second top-level JSON
    document on stdout), and its per-check summary rides inside the
    single migration object instead."""
    from agents_cli import config as cfg_mod
    from agents_cli import migrate_cmd
    from tests.unit.test_migrate_cmd import _make_cfg, _stub_happy_migration

    cfg = _make_cfg(tmp_path)
    config_path = tmp_path / "config.toml"
    cfg_mod.save(cfg, config_path)
    _stub_happy_migration(mocker, cfg)

    # Re-target the real implementation at a throwaway config so the test
    # never touches ~/.config/agents/ (same trick as the vm-init test).
    real = migrate_cmd.migrate_data
    mocker.patch(
        "agents_cli.migrate_cmd.migrate_data",
        side_effect=lambda **kw: real(**{**kw, "config_path": config_path}),
    )

    result = invoke(mocker, ["--json", "vm", "migrate-data", "--yes"], cfg=cfg)

    assert result.exit_code == 0, result.output
    payload = single_json_object(result)  # a leaked doctor table fails the parse
    assert payload["migrated"] is True
    assert payload["doctor"]["ran"] is True
    assert payload["doctor"]["non_ok"] == [
        {"check": "disk_space", "status": "warn", "detail": "14.0% free"}
    ]


def test_vm_migrate_data_maps_cli_error_exit_codes(mocker: Any) -> None:
    """UserError → 1 and HealthCheckFailure → 3 pass through _run
    untouched (the doctor-gate failure mode of the migration)."""
    from agents_cli.errors import HealthCheckFailure, UserError

    mocker.patch(
        "agents_cli.migrate_cmd.migrate_data",
        side_effect=UserError("data_dir_in_vm is already enabled"),
    )
    result = invoke(mocker, ["--json", "vm", "migrate-data", "--yes"])
    assert result.exit_code == 1
    assert "already enabled" in single_json_object(result)["error"]

    mocker.patch(
        "agents_cli.migrate_cmd.migrate_data",
        side_effect=HealthCheckFailure("doctor found 2 failures"),
    )
    result = invoke(mocker, ["vm", "migrate-data", "--yes"])
    assert result.exit_code == 3


# ---------- mount add --rw (item: ui.confirm semantics, not click.Abort) ----------


def test_mount_add_rw_json_without_yes_refuses_cleanly(mocker: Any, tmp_path: Path) -> None:
    add = mocker.patch("agents_cli.mounts_cmd.add")

    result = invoke(
        mocker,
        [
            "--json",
            "mount",
            "add",
            "coder",
            "--host",
            str(tmp_path),
            "--container",
            "/workspace/x",
            "--rw",
        ],
    )

    assert result.exit_code == 1
    payload = single_json_object(result)
    assert "Confirmation required" in payload["error"]
    add.assert_not_called()


def test_mount_add_rw_declined_exits_1_as_user_error(mocker: Any, tmp_path: Path) -> None:
    """Declining the --rw prompt is a user decision → UserError exit 1
    (previously click.Abort)."""
    add = mocker.patch("agents_cli.mounts_cmd.add")

    result = invoke(
        mocker,
        ["mount", "add", "coder", "--host", str(tmp_path), "--container", "/w", "--rw"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "aborted by user" in result.output
    add.assert_not_called()
