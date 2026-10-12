"""End-to-end smoke tests against a live, healthy deployment.

Run explicitly with::

    pytest -m multipass tests/integration/

Deselected by default (``addopts = -m 'not multipass'``); the session
gate in ``conftest.py`` skips everything unless ``agx doctor`` is
green. Every command goes through the ``agx`` console script as a
subprocess — full CLI wiring, ``--json`` stdout contract, exit codes —
and everything created here carries the reserved ``itest-`` prefix and
is torn down in finalizers. See ``conftest.py`` for the safety posture.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.integration.conftest import (
    CREATE_TIMEOUT,
    EXEC_TIMEOUT,
    MUTATE_TIMEOUT,
    QUICK_TIMEOUT,
    TEST_PREFIX,
    Cli,
    Deployment,
    DisposableAgent,
    parse_single_json,
)

pytestmark = pytest.mark.multipass


def test_doctor_green(deployment: Deployment) -> None:
    """`agx --json doctor` exits 0 and emits one JSON array of checks,
    none of them failing. (The run itself happens once, in the session
    gate — a red doctor skips the whole suite, so reaching this test at
    all means the deployment was healthy; the assertions here pin the
    exit code and the machine-readable shape.)"""
    assert deployment.doctor.returncode == 0
    checks = parse_single_json(deployment.doctor.stdout)
    assert isinstance(checks, list) and checks, "doctor emitted no checks"
    for check in checks:
        assert check["check"], f"row without a check name: {check!r}"
        assert check["status"] in ("ok", "warn"), f"failing check: {check!r}"


@pytest.mark.slow
def test_create_exec_remove_roundtrip(cli: Cli, disposable_agent: DisposableAgent) -> None:
    """The disposable Hermes agent (created by the session fixture, removed
    with `remove --yes` at session end) shows up in `agx list`, and
    `agx exec` runs commands inside its container with the in-container
    exit code propagated to the CLI's exit code."""
    info = disposable_agent.info
    assert info["name"] == disposable_agent.name
    assert info["type"] == "hermes"
    assert info["service"] == f"hermes-{disposable_agent.name}"

    listed = cli.run("--json", "list", timeout=QUICK_TIMEOUT)
    assert listed.returncode == 0, listed.stderr
    rows = parse_single_json(listed.stdout)
    match = [r for r in rows if r["name"] == disposable_agent.name]
    assert match, f"{disposable_agent.name!r} missing from `agx list`: {rows!r}"
    assert match[0]["type"] == "hermes"

    ok = cli.exec_in_agent(disposable_agent.name, "true")
    assert ok.returncode == 0, f"exec true failed: {ok.stderr}"
    fail = cli.exec_in_agent(disposable_agent.name, "false")
    assert fail.returncode != 0, "exec false must propagate a non-zero exit code"


@pytest.mark.slow
def test_secret_reaches_container_env(cli: Cli, disposable_agent: DisposableAgent) -> None:
    """`agx secret set` recreates the container with the key visible as
    process env (proves the per-agent .env + compose `env_file:` chain
    end-to-end); `unset` removes it from the env, the .env, and the Hermes
    `terminal.env_passthrough` list."""
    name = disposable_agent.name
    key = "ITEST_PROBE"
    value = f"itest-{uuid.uuid4().hex}"

    try:
        set_res = cli.run("--json", "secret", "set", name, key, value, timeout=MUTATE_TIMEOUT)
        assert set_res.returncode == 0, set_res.stderr
        assert parse_single_json(set_res.stdout)["status"] == "set"

        probe = cli.exec_in_agent(name, "sh", "-c", f"printenv {key}")
        assert probe.returncode == 0, f"{key} not in container env: {probe.stderr}"
        assert value in probe.stdout
    finally:
        unset_res = cli.run("--json", "secret", "unset", name, key, timeout=MUTATE_TIMEOUT)

    assert unset_res.returncode == 0, unset_res.stderr

    gone = cli.exec_in_agent(name, "sh", "-c", f"printenv {key}")
    assert gone.returncode != 0, f"{key} still in container env after unset"

    listed = cli.run("--json", "secret", "list", name, timeout=QUICK_TIMEOUT)
    assert listed.returncode == 0, listed.stderr
    assert key not in [row["key"] for row in parse_single_json(listed.stdout)]

    # env_passthrough cleanup: config.yaml is only readable through the
    # container (subuid-owned on the host), so `cat` it via exec.
    cfg_yaml = cli.exec_in_agent(name, "cat", "/opt/data/config.yaml")
    if cfg_yaml.returncode == 0:  # file exists once `secret set` wrote passthrough
        assert key not in cfg_yaml.stdout, "unset left the key in terminal.env_passthrough"


@pytest.mark.slow
def test_mount_delivers_volume(
    cli: Cli, disposable_agent: DisposableAgent, itest_scratch_dir: Path
) -> None:
    """THE override-regression test. `agx mount add --target runtime`
    writes the volume ONLY to docker-compose.override.yml — a file that a
    past bug silently dropped from every compose invocation. Reading a
    host file back through `agx exec cat` after the `--restart`
    recreate proves Docker actually consumed the override, not merely
    that the file has the right content (which is all the unit suite can
    assert)."""
    name = disposable_agent.name
    host_dir = itest_scratch_dir / "shared"
    host_dir.mkdir()
    value = f"itest-mount-{uuid.uuid4().hex}"
    (host_dir / "probe.txt").write_text(value + "\n", encoding="utf-8")
    container_path = f"/workspace/itest-{uuid.uuid4().hex[:8]}"

    try:
        # Read-only default → no --rw safety prompt; --yes for belt and
        # braces. --target runtime keeps the test purely on the
        # override-file path (sandbox config.yaml is a separate layer).
        added = cli.run(
            "--json",
            "--yes",
            "mount",
            "add",
            name,
            "--host",
            str(host_dir),
            "--container",
            container_path,
            "--target",
            "runtime",
            "--restart",
            timeout=MUTATE_TIMEOUT,
        )
        assert added.returncode == 0, added.stderr
        payload = parse_single_json(added.stdout)
        assert payload["restarted"] is True
        assert payload["container_path"] == container_path

        seen = cli.exec_in_agent(name, "cat", f"{container_path}/probe.txt")
        assert seen.returncode == 0, (
            f"mounted file not visible in container — the override volume "
            f"was NOT delivered: {seen.stderr}"
        )
        assert value in seen.stdout
    finally:
        removed = cli.run(
            "--json",
            "mount",
            "remove",
            name,
            "--container",
            container_path,
            "--restart",
            timeout=MUTATE_TIMEOUT,
        )

    assert removed.returncode == 0, removed.stderr
    gone = cli.exec_in_agent(name, "cat", f"{container_path}/probe.txt")
    assert gone.returncode != 0, "file still visible after mount remove + recreate"


@pytest.mark.slow
def test_export_import_roundtrip(
    cli: Cli, disposable_agent: DisposableAgent, itest_scratch_dir: Path
) -> None:
    """A sentinel written inside the container survives export → delete →
    import: proves the archive carries real content (not the silent
    stub-tarball failure mode export once had) and that import restores
    it with ownership the running container can read.

    Same-name roundtrip by design: `agx import` extracts the archive's
    embedded top-level dir (always the source agent's name) — importing
    under a second name is not supported by the CLI, so a second-name
    test would assert on behavior that doesn't exist. `--to` points at a
    scratch dir so retention pruning of real backups is never touched."""
    name = disposable_agent.name
    sentinel = "/opt/data/itest-sentinel.txt"
    value = f"itest-export-{uuid.uuid4().hex}"

    wrote = cli.exec_in_agent(name, "sh", "-c", f"printf '%s\\n' {value} > {sentinel}")
    assert wrote.returncode == 0, wrote.stderr

    out_dir = itest_scratch_dir / "exports"
    exported = cli.run("--json", "export", name, "--to", str(out_dir), timeout=MUTATE_TIMEOUT)
    assert exported.returncode == 0, exported.stderr
    payload = parse_single_json(exported.stdout)
    archive = Path(payload["archive"])
    assert archive.parent == out_dir, "archive landed outside the --to dir"
    assert "pruned" not in payload, "custom --to must never trigger retention pruning"
    assert archive.is_file() and archive.stat().st_size > 0

    # Damage the live data dir, then restore from the archive.
    removed = cli.exec_in_agent(name, "rm", "-f", sentinel)
    assert removed.returncode == 0, removed.stderr
    gone = cli.exec_in_agent(name, "cat", sentinel)
    assert gone.returncode != 0, "sentinel still present after rm"

    imported = cli.run("--json", "--yes", "import", name, str(archive), timeout=MUTATE_TIMEOUT)
    assert imported.returncode == 0, imported.stderr

    back = cli.exec_in_agent(name, "cat", sentinel)
    assert back.returncode == 0, f"sentinel not restored by import: {back.stderr}"
    assert value in back.stdout

    cli.exec_in_agent(name, "rm", "-f", sentinel)  # leave the agent tidy


@pytest.mark.slow
def test_remove_deletes_subuid_owned_state(
    cli: Cli, deployment: Deployment, request: pytest.FixtureRequest
) -> None:
    """`agx remove` must leave nothing behind on the host, although the
    started container has filled `<name>/.data` with files owned by a
    rootless-Docker subuid under mode 700 that the host user cannot even
    list. Unit tests can only assert the argv; this proves the delete
    (root inside the VM, or rootlesskit on bare-metal) really works on
    this backend's share — without any host sudo prompt (`--json --yes`
    has no TTY to prompt on)."""
    if deployment.config["paths"].get("data_dir_in_vm"):
        pytest.skip("data dir lives in the VM — nothing to observe from the host")
    name = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
    agent_dir = deployment.host_data_dir / name
    # Safety net only; the assertion below is the real removal.
    request.addfinalizer(
        lambda: (
            cli.run("--json", "--yes", "remove", name, timeout=MUTATE_TIMEOUT)
            if agent_dir.exists()
            else None
        )
    )

    created = cli.run("--json", "--yes", "create", name, "--type", "hermes", timeout=CREATE_TIMEOUT)
    assert created.returncode == 0, created.stderr
    seeded = cli.exec_in_agent(
        name,
        "sh",
        "-c",
        "mkdir -m 700 /opt/data/itest-private && touch /opt/data/itest-private/f "
        "&& chmod 600 /opt/data/itest-private/f",
    )
    assert seeded.returncode == 0, seeded.stderr
    assert (agent_dir / ".data").stat().st_uid != deployment.host_data_dir.stat().st_uid

    removed = cli.run("--json", "--yes", "remove", name, timeout=MUTATE_TIMEOUT)
    assert removed.returncode == 0, removed.stderr
    assert parse_single_json(removed.stdout)["status"] == "removed"
    assert not agent_dir.exists()


def test_logs_returns_content(cli: Cli, disposable_agent: DisposableAgent) -> None:
    """Non-follow `agx logs` exits 0 with non-empty output (the agent's
    container has booted at least once by now, so s6/entrypoint output
    must be present)."""
    res = cli.run("--json", "logs", disposable_agent.name, timeout=EXEC_TIMEOUT)
    assert res.returncode == 0, res.stderr
    payload = parse_single_json(res.stdout)
    assert payload["name"] == disposable_agent.name
    assert payload["logs"].strip(), "logs came back empty"


def test_json_contract_single_object(cli: Cli, deployment: Deployment) -> None:
    """Under --json, stdout of read-only commands parses as exactly ONE
    JSON document (json.loads raises on trailing data — progress/warning
    events must stay on stderr). Covers doctor (captured by the session
    gate) plus fresh list / firecrawl status / config runs."""
    parse_single_json(deployment.doctor.stdout)

    for argv in (["list"], ["firecrawl", "status"], ["config"]):
        res = cli.run("--json", *argv, timeout=EXEC_TIMEOUT)
        assert res.returncode == 0, f"agx --json {' '.join(argv)}: {res.stderr}"
        parse_single_json(res.stdout)
