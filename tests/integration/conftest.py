"""Fixtures for the opt-in integration smoke suite (``pytest -m multipass``).

Invocation style: **subprocess of the installed ``agx`` console
script**, not the Python API. The console script exercises the full
user-facing surface — click parsing in ``cli.py``, the ``_RootGroup``
exit-code mapping, ``ui.emit``'s one-JSON-document-per-command contract —
which is exactly the wiring unit tests can't prove. (The audit's worst
bug lived in that gap: unit tests asserted the override file's CONTENT,
never that a real ``docker compose`` invocation consumed it.)

Safety posture (hard rules — do not relax):

- **Deselected by default.** ``pyproject.toml``'s ``addopts`` carries
  ``-m 'not multipass'``; bare ``pytest`` / ``pytest tests/unit`` never
  run this suite. Opt in with ``pytest -m multipass``.
- **Requires an already-healthy deployment.** The session gate runs
  ``agx --json doctor`` and SKIPS the whole suite when the console
  script is missing, the config doesn't exist, the VM isn't running, or
  doctor reports any failure.
- **Never destructive.** No ``vm init/destroy/rebuild``, no upgrades,
  no writes to pre-existing agents' data. Everything the suite creates
  carries the reserved ``itest-`` prefix and is torn down in fixture
  finalizers even on assertion failure.
- **Backend-agnostic.** Everything goes through the ``agx`` CLI
  against the REAL default config — never ``multipass``/``virsh``
  directly. Scratch dirs live under ``<data-dir>/.itest/`` (NOT /tmp:
  snap-confined multipass cannot mount or SFTP from /tmp, while the
  data dir is reachable on every backend — same reasoning as
  ``compose.host_staging_dir``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# Reserved prefix for every agent (and scratch dir) this suite creates.
TEST_PREFIX = "itest-"

# Explicit time-boxes (seconds) — a hung backend must fail the test run,
# never wedge it.
QUICK_TIMEOUT = 180.0  # list / config / secret list
EXEC_TIMEOUT = 300.0  # docker compose exec round-trip
MUTATE_TIMEOUT = 900.0  # secret set/unset, mount add/remove, export/import
DOCTOR_TIMEOUT = 1500.0  # doctor probes every check + agent
CREATE_TIMEOUT = 3600.0  # agx create pulls the Hermes image on first run


def parse_single_json(stdout: str) -> Any:
    """Parse stdout as exactly one JSON document.

    ``json.loads`` raises ``JSONDecodeError`` ("Extra data") on trailing
    content, so a successful parse *is* the single-document assertion.
    """
    return json.loads(stdout)


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


@dataclass(frozen=True)
class Cli:
    """Thin wrapper around the ``agx`` console script."""

    binary: str

    def run(self, *args: str, timeout: float = QUICK_TIMEOUT) -> subprocess.CompletedProcess[str]:
        """Run ``agx <args...>`` with captured output and closed stdin.

        stdin is /dev/null so nothing can block on a prompt — commands
        that would prompt must be invoked with ``--yes`` (or fail fast).
        """
        return subprocess.run(
            [self.binary, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def exec_in_agent(
        self, agent: str, *cmd: str, timeout: float = EXEC_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """``agx exec <agent> -- <cmd...>`` — exit code is the in-container
        command's exit code (``ops.run_in_agent`` propagates it via
        ``sys.exit``)."""
        return self.run("exec", agent, "--", *cmd, timeout=timeout)


@dataclass(frozen=True)
class Deployment:
    """Snapshot of the healthy deployment the session gate verified."""

    config: dict[str, Any]
    host_data_dir: Path
    doctor: subprocess.CompletedProcess[str]


@dataclass(frozen=True)
class DisposableAgent:
    """The one throwaway Hermes agent shared by the whole session."""

    name: str
    info: dict[str, Any]


@pytest.fixture(scope="session")
def cli() -> Cli:
    """Resolve the ``agx`` console script, preferring the running venv.

    The console-script wrapper does ``sys.exit(main())`` — required for
    exit-code assertions (calling the click group in-process discards
    the return value).
    """
    candidate = Path(sys.executable).parent / "agx"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return Cli(binary=str(candidate))
    found = shutil.which("agx")
    if found:
        return Cli(binary=found)
    pytest.skip(
        "`agx` console script not found (looked next to the running "
        "python and on PATH) — install the package first (`pip install -e .`)"
    )


@pytest.fixture(scope="session")
def deployment(cli: Cli) -> Deployment:
    """Session gate: skip the whole suite unless the deployment is healthy.

    Two probes, both read-only:

    1. ``agx --json config`` — proves the CLI is wired to a real
       config (skips on missing/broken ``~/.config/agx/config.toml``).
    2. ``agx --json doctor`` — proves the VM is up and every doctor
       check passes (a stopped VM or any red check exits non-zero).
    """
    cfg_res = cli.run("--json", "config", timeout=QUICK_TIMEOUT)
    if cfg_res.returncode != 0:
        pytest.skip(
            "agx config not readable — no deployment to test against "
            f"(exit {cfg_res.returncode}): {_tail(cfg_res.stdout or cfg_res.stderr, 3)}"
        )
    config = parse_single_json(cfg_res.stdout)
    host_data_dir = Path(config["paths"]["host_data_dir"])

    doctor = cli.run("--json", "doctor", timeout=DOCTOR_TIMEOUT)
    if doctor.returncode != 0:
        try:
            checks = parse_single_json(doctor.stdout)
            failing = ", ".join(
                f"{c['check']}={c['detail'] or 'fail'}" for c in checks if c.get("status") == "fail"
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            failing = _tail(doctor.stdout or doctor.stderr, 5)
        pytest.skip(
            f"deployment not healthy — `agx doctor` exited "
            f"{doctor.returncode}; failing: {failing or '(unparsed)'}"
        )
    return Deployment(config=config, host_data_dir=host_data_dir, doctor=doctor)


@pytest.fixture(scope="session")
def disposable_agent(
    cli: Cli, deployment: Deployment, request: pytest.FixtureRequest
) -> DisposableAgent:
    """Create ONE throwaway Hermes agent for the whole session.

    Pulls the pinned Hermes image on first run (hence the generous
    timeout). The remove finalizer is registered BEFORE the create so a
    half-created agent (journaled mid-phase) is still torn down.
    """
    name = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"

    def teardown() -> None:
        res = cli.run("--json", "--yes", "remove", name, timeout=MUTATE_TIMEOUT)
        if res.returncode != 0:
            # Never mask test results with teardown noise — but do surface it.
            sys.stderr.write(
                f"\n[integration teardown] `agx remove {name}` exited "
                f"{res.returncode}: {_tail(res.stdout or res.stderr, 5)}\n"
                f"[integration teardown] clean up manually with: "
                f"agx remove {name} --yes\n"
            )

    request.addfinalizer(teardown)

    created = cli.run("--json", "--yes", "create", name, "--type", "hermes", timeout=CREATE_TIMEOUT)
    if created.returncode != 0:
        pytest.fail(
            f"could not create disposable agent {name!r} on a deployment "
            f"doctor reported healthy (exit {created.returncode}):\n"
            f"stdout: {_tail(created.stdout, 5)}\nstderr: {_tail(created.stderr, 12)}"
        )
    return DisposableAgent(name=name, info=parse_single_json(created.stdout))


@pytest.fixture
def itest_scratch_dir(deployment: Deployment) -> Iterator[Path]:
    """Per-test scratch dir under ``<data-dir>/.itest/``.

    Lives on the host data dir — the one host location every backend can
    mount from and transfer to (snap-multipass refuses /tmp and dotfile
    paths under $HOME; the data dir is already shared with the VM).
    """
    scratch = deployment.host_data_dir / ".itest" / uuid.uuid4().hex[:8]
    scratch.mkdir(parents=True)
    yield scratch
    shutil.rmtree(scratch, ignore_errors=True)
    parent = scratch.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
