"""Output and prompts.

Every command writes through this module so `--json` and `--yes` work
uniformly. The flags live on the click root context and are read via
``click.get_current_context``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any

import click

from agents_cli.errors import CliError, UserError

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def _flag(name: str) -> bool:
    ctx = click.get_current_context(silent=True)
    return bool(ctx and ctx.obj and ctx.obj.get(name))


def is_json() -> bool:
    return _flag("json")


def is_yes() -> bool:
    return _flag("yes")


def is_verbose() -> bool:
    return _flag("verbose")


def emit(payload: Mapping[str, Any] | str, *, human: str | None = None) -> None:
    """Write a result to stdout.

    Under ``--json`` always emits a single JSON object. Otherwise prefers
    ``human`` (when given) and falls back to a pretty-printed JSON dump.
    """
    if is_json():
        body: dict[str, Any]
        body = {"message": payload} if isinstance(payload, str) else dict(payload)
        click.echo(json.dumps(body, default=str, sort_keys=True))
        return

    if human is not None:
        click.echo(human)
        return

    if isinstance(payload, str):
        click.echo(payload)
        return

    click.echo(json.dumps(payload, indent=2, default=str, sort_keys=True))


def warn(message: str) -> None:
    """Side-channel warning. Goes to stderr; under --json wraps as a warning event."""
    if is_json():
        click.echo(json.dumps({"warning": message}, sort_keys=True), err=True)
    else:
        click.echo(f"warning: {message}", err=True)


def info(message: str) -> None:
    """Verbose-only informational message to stderr."""
    if not is_verbose():
        return
    click.echo(message, err=True)


def confirm(prompt: str, *, default: bool = False) -> bool:
    """Prompt for yes/no. Bypassed by --yes. In --json mode without --yes, refuses."""
    if is_yes():
        return True
    if is_json():
        raise UserError(
            f"Confirmation required ({prompt!r}). Re-run with --yes for non-interactive mode."
        )
    return click.confirm(prompt, default=default)


def table(rows: Iterable[Mapping[str, Any]], columns: list[str]) -> None:
    """Print rows as a table (human) or a JSON array (`--json`)."""
    row_list = [dict(r) for r in rows]
    if is_json():
        click.echo(json.dumps(row_list, indent=2, default=str, sort_keys=True))
        return

    widths = {
        c: max(len(c), *(len(str(r.get(c, ""))) for r in row_list)) if row_list else len(c)
        for c in columns
    }
    click.echo("  ".join(c.ljust(widths[c]) for c in columns))
    click.echo("  ".join("-" * widths[c] for c in columns))
    for r in row_list:
        click.echo("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def handle_cli_errors(exc: BaseException) -> int:
    """Translate an exception into an exit code with a user-visible message.

    Called from ``cli.main`` (and tests). Returns the exit code; does not
    call ``sys.exit`` itself so callers can chain behavior in tests.
    """
    if isinstance(exc, CliError):
        emit({"error": str(exc), "kind": type(exc).__name__}, human=f"error: {exc}")
        return exc.exit_code
    if isinstance(exc, click.UsageError):
        # Click usage errors are always the user's fault → exit 1.
        emit(
            {"error": exc.format_message(), "kind": "UsageError"},
            human=f"error: {exc.format_message()}",
        )
        return 1
    if isinstance(exc, KeyboardInterrupt):
        emit({"error": "interrupted"}, human="interrupted")
        return 130

    # Any other exception is an unhandled runtime failure.
    emit(
        {"error": str(exc), "kind": type(exc).__name__},
        human=f"unexpected error: {exc} ({type(exc).__name__})",
    )
    if is_verbose():
        import traceback

        traceback.print_exception(exc, file=sys.stderr)
    return 2
