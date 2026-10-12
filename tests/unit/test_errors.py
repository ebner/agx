"""Exit-code semantics for the CLI exception hierarchy."""

from __future__ import annotations

from agx_cli.errors import CliError, HealthCheckFailure, RuntimeFailure, UserError


def test_exit_codes_are_load_bearing() -> None:
    assert UserError("x").exit_code == 1
    assert RuntimeFailure("x").exit_code == 2
    assert HealthCheckFailure("x").exit_code == 3


def test_base_class_defaults_to_runtime() -> None:
    assert CliError("x").exit_code == 2


def test_inheritance() -> None:
    assert isinstance(UserError("x"), CliError)
    assert isinstance(RuntimeFailure("x"), CliError)
    assert isinstance(HealthCheckFailure("x"), CliError)
