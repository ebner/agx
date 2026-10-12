"""Exit-code mapping and CLI exception hierarchy.

Exit codes are part of the public CLI contract:

- 0: success
- 1: user error — bad arguments, conflicting state, refused dangerous op
- 2: runtime failure — VM unreachable, Docker failure, network error
- 3: health-check failure — `doctor` found something broken
"""

from __future__ import annotations


class CliError(Exception):
    """Base for all CLI-raised errors. Subclasses set their own exit code."""

    exit_code: int = 2


class UserError(CliError):
    """Bad arguments, conflicting state, missing prerequisites the user controls."""

    exit_code = 1


class RuntimeFailure(CliError):
    """Multipass/Docker/network failures the user did not cause directly."""

    exit_code = 2


class HealthCheckFailure(CliError):
    """A `doctor` check failed."""

    exit_code = 3
