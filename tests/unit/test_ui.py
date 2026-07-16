"""ui.py behavior: --json vs human output, --yes bypass, exit-code mapping."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from agents_cli import ui
from agents_cli.errors import HealthCheckFailure, RuntimeFailure, UserError


def _invoke(flags: dict[str, bool], body):
    """Run `body()` inside a click context that mimics the root group's flags."""

    @click.command()
    @click.pass_context
    def cmd(ctx: click.Context) -> None:
        ctx.obj = dict(flags)
        body(ctx)

    runner = CliRunner()
    return runner.invoke(cmd, [], catch_exceptions=False)


def test_emit_human_uses_human_string() -> None:
    def body(ctx: click.Context) -> None:
        ui.emit({"x": 1}, human="hello")

    result = _invoke({"json": False, "yes": False, "verbose": False}, body)
    assert result.exit_code == 0
    assert result.output.strip() == "hello"


def test_emit_json_always_emits_json() -> None:
    def body(ctx: click.Context) -> None:
        ui.emit({"x": 1}, human="hello")

    result = _invoke({"json": True, "yes": False, "verbose": False}, body)
    payload = json.loads(result.output.strip())
    assert payload == {"x": 1}


def test_emit_string_payload_under_json_wraps_as_message() -> None:
    def body(ctx: click.Context) -> None:
        ui.emit("hi")

    result = _invoke({"json": True, "yes": False, "verbose": False}, body)
    payload = json.loads(result.output.strip())
    assert payload == {"message": "hi"}


def test_progress_human_goes_to_stderr_not_stdout() -> None:
    """Progress lines must stay off stdout so piping a command's result
    (or capturing its single --json object) never picks up narration."""

    def body(ctx: click.Context) -> None:
        ui.progress("[1/7] pulling image...")

    result = _invoke({"json": False, "yes": False, "verbose": False}, body)
    assert "[1/7] pulling image..." in result.stderr
    assert result.stdout == ""


def test_progress_json_emits_structured_event_on_stderr() -> None:
    def body(ctx: click.Context) -> None:
        ui.progress("step one")

    result = _invoke({"json": True, "yes": False, "verbose": False}, body)
    assert json.loads(result.stderr.strip()) == {"progress": "step one"}
    assert result.stdout == ""


def test_progress_is_not_gated_on_verbose() -> None:
    """Unlike ui.info — progress exists precisely for default-mode UX
    during minutes-long operations."""

    def body(ctx: click.Context) -> None:
        ui.info("hidden")
        ui.progress("shown")

    result = _invoke({"json": False, "yes": False, "verbose": False}, body)
    assert "shown" in result.stderr
    assert "hidden" not in result.stderr


def test_echo_human_writes_plain_line_to_stdout() -> None:
    """echo_human exists so renderers with custom layouts (doctor's
    wrapped table) don't call click.echo directly outside ui.py."""

    def body(ctx: click.Context) -> None:
        ui.echo_human("plain layout line")

    result = _invoke({"json": False, "yes": False, "verbose": False}, body)
    assert "plain layout line" in result.stdout
    assert result.stderr == ""


def test_confirm_with_yes_flag_returns_true_without_prompting() -> None:
    captured = []

    def body(ctx: click.Context) -> None:
        captured.append(ui.confirm("delete everything?"))

    result = _invoke({"json": False, "yes": True, "verbose": False}, body)
    assert result.exit_code == 0
    assert captured == [True]
    assert "delete everything" not in result.output


def test_confirm_in_json_mode_without_yes_raises_user_error() -> None:
    def body(ctx: click.Context) -> None:
        with pytest.raises(UserError):
            ui.confirm("delete?")

    _invoke({"json": True, "yes": False, "verbose": False}, body)


def test_table_human_shows_header_and_rows() -> None:
    def body(ctx: click.Context) -> None:
        ui.table(
            [{"name": "coder", "status": "running"}, {"name": "personal", "status": "stopped"}],
            ["name", "status"],
        )

    result = _invoke({"json": False, "yes": False, "verbose": False}, body)
    assert "name" in result.output
    assert "coder" in result.output
    assert "personal" in result.output


def test_table_json_emits_array() -> None:
    def body(ctx: click.Context) -> None:
        ui.table([{"name": "coder"}], ["name"])

    result = _invoke({"json": True, "yes": False, "verbose": False}, body)
    assert json.loads(result.output.strip()) == [{"name": "coder"}]


def test_handle_cli_errors_user_error_returns_1() -> None:
    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(UserError("bad arg")) == 1

    _invoke({"json": False, "yes": False, "verbose": False}, body)


def test_handle_cli_errors_click_usage_error_returns_1() -> None:
    """click hardwires UsageError to exit 2 in standalone mode; the
    toolkit's contract says user error = 1. cli.main routes click's
    exceptions here so the mapping is applied."""

    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(click.UsageError("missing argument")) == 1

    _invoke({"json": False, "yes": False, "verbose": False}, body)


def test_handle_cli_errors_generic_click_exception_returns_1() -> None:
    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(click.ClickException("nope")) == 1

    _invoke({"json": False, "yes": False, "verbose": False}, body)


def test_handle_cli_errors_runtime_returns_2() -> None:
    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(RuntimeFailure("vm down")) == 2

    _invoke({"json": False, "yes": False, "verbose": False}, body)


def test_handle_cli_errors_health_returns_3() -> None:
    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(HealthCheckFailure("doctor failed")) == 3

    _invoke({"json": False, "yes": False, "verbose": False}, body)


def test_handle_cli_errors_unknown_returns_2() -> None:
    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(ValueError("oops")) == 2

    _invoke({"json": False, "yes": False, "verbose": False}, body)


def test_handle_cli_errors_keyboard_interrupt_returns_130() -> None:
    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(KeyboardInterrupt()) == 130

    _invoke({"json": False, "yes": False, "verbose": False}, body)


def test_handle_cli_errors_click_abort_returns_130() -> None:
    """click converts Ctrl-C/EOF at prompts into Abort — same user
    action as KeyboardInterrupt, same interrupt exit code, NOT the
    generic exit-2 runtime failure (Abort subclasses RuntimeError)."""

    def body(ctx: click.Context) -> None:
        assert ui.handle_cli_errors(click.Abort()) == 130

    _invoke({"json": False, "yes": False, "verbose": False}, body)
