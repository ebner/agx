"""agents_cmd.py: create/list/remove/rename with journal-driven idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents_cli import agents_cmd, compose
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import (
    create,
    list_agents,
    remove,
    rename,
    service_name,
    validate_name,
)
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import UserError
from agents_cli.journal import Journal
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
        defaults=DefaultsConfig(
            hermes_image_tag="v1.0.0",
            openclaw_image_tag="0.1.0",
        ),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


@pytest.fixture
def stub_runners(mocker, cfg: AgentsConfig):
    """Stub every multipass / docker-compose interaction with VM-fs simulation.

    Returns the merged mock dict so tests can introspect specific calls
    (e.g. ``stub_runners["exec_in_vm_interactive"].assert_called_once()``).
    """
    from tests.conftest import install_vm_fs_simulation

    mocks = dict(install_vm_fs_simulation(mocker, cfg))
    mocks["compose_up"] = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    mocks["compose_down"] = mocker.patch("agents_cli.compose_runner.down", return_value=_ok())
    mocks["compose_stop"] = mocker.patch("agents_cli.compose_runner.stop", return_value=_ok())
    return mocks


# ---------- validate_name ----------


@pytest.mark.parametrize("good", ["coder", "personal", "x", "a1", "my-bot", "my_bot"])
def test_validate_name_accepts(good: str) -> None:
    validate_name(good)


# ---------- _ensure_hermes_image_pulled ----------


def _build_pull_runner(inspect_rc: int, pull_rc: int, pull_stderr: str = ""):
    """Stub for mp.exec_in_vm that fakes a `docker image inspect` then
    `docker pull` sequence. Other docker subcommands return success.
    """
    from agents_cli.mp import MpResult

    def runner(vm_name: str, *cmd: str, check: bool = True):
        if "image" in cmd and "inspect" in cmd:
            return MpResult(stdout="", stderr="", returncode=inspect_rc)
        if "pull" in cmd:
            return MpResult(stdout="", stderr=pull_stderr, returncode=pull_rc)
        return MpResult(stdout="", stderr="", returncode=0)

    return runner


def test_ensure_hermes_image_pulled_skips_if_already_local(cfg: AgentsConfig, mocker) -> None:
    """`docker image inspect` succeeding means the image is already cached;
    no `docker pull` should be issued."""
    from agents_cli.agents_cmd import _ensure_hermes_image_pulled

    calls: list[tuple[str, ...]] = []

    def runner(vm_name: str, *cmd: str, check: bool = True):
        from agents_cli.mp import MpResult

        calls.append(cmd)
        if "image" in cmd and "inspect" in cmd:
            return MpResult(stdout="", stderr="", returncode=0)
        return MpResult(stdout="", stderr="", returncode=0)

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    _ensure_hermes_image_pulled(cfg, "v1.0.0")
    assert not any("pull" in c for c in calls), "pull should be skipped when inspect succeeds"


def test_ensure_hermes_image_pulled_pulls_when_missing(cfg: AgentsConfig, mocker) -> None:
    """Inspect rc=1 (missing) triggers a pull. Success is silent."""
    from agents_cli.agents_cmd import _ensure_hermes_image_pulled

    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_build_pull_runner(inspect_rc=1, pull_rc=0),
    )
    _ensure_hermes_image_pulled(cfg, "v1.0.0")  # no raise


def test_ensure_hermes_image_pulled_raises_on_pull_failure(cfg: AgentsConfig, mocker) -> None:
    """Inspect rc=1, pull rc=1 -> UserError with the stderr."""
    from agents_cli.agents_cmd import _ensure_hermes_image_pulled

    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_build_pull_runner(inspect_rc=1, pull_rc=1, pull_stderr="rate limit"),
    )
    with pytest.raises(UserError, match="could not pull"):
        _ensure_hermes_image_pulled(cfg, "v1.0.0")


@pytest.mark.parametrize(
    "bad", ["", "Coder", "coder!", "-leading-dash", "with space", "a" * 51, "../slashy"]
)
def test_validate_name_rejects(bad: str) -> None:
    with pytest.raises(UserError):
        validate_name(bad)


def test_service_name_hermes() -> None:
    assert service_name("coder", "hermes") == "hermes-coder"


def test_service_name_openclaw() -> None:
    assert service_name("personal", "openclaw") == "openclaw-personal"


def test_service_name_unknown_type() -> None:
    with pytest.raises(UserError):
        service_name("x", "bogus")


# ---------- create ----------


def test_create_hermes_writes_compose_and_dirs(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    # Data dir + .data created
    assert (cfg.paths.host_data_dir / "coder").is_dir()
    assert (cfg.paths.host_data_dir / "coder" / ".data").is_dir()
    # Compose has the service with the right shape
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" in doc["services"]
    svc = doc["services"]["hermes-coder"]
    assert svc["image"] == "nousresearch/hermes-agent:v1.0.0"
    assert any("/.codex:/home/hermes/.codex" in v for v in svc["volumes"])


def test_create_openclaw_uses_per_agent_paths(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    assert (cfg.paths.host_data_dir / "personal" / ".openclaw").is_dir()
    assert (cfg.paths.host_data_dir / "personal" / "workspace").is_dir()
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    svc = doc["services"]["openclaw-personal"]
    assert any("./personal/.openclaw" in v for v in svc["volumes"])


def test_create_refuses_unknown_type(saved_config: Path) -> None:
    with pytest.raises(UserError, match="unknown agent type"):
        create(name="coder", agent_type="bogus", config_path=saved_config)


def test_create_refuses_existing_data_dir_without_reuse(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    with pytest.raises(UserError, match="already exists"):
        create(name="coder", agent_type="hermes", config_path=saved_config)


def test_create_resumes_when_journal_exists_despite_existing_data_dir(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """A resumable journal means a prior run crashed mid-op. Re-running must
    pick up where it left off — the data-dir-exists guard must not block this
    path, which is the whole point of the journal.
    """
    j = Journal.begin(cfg.paths.host_data_dir, "create", "coder", agents_cmd.CREATE_PHASES)
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    j.advance("data_dir_created")

    create(name="coder", agent_type="hermes", config_path=saved_config)

    assert (
        "hermes-coder"
        in compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")["services"]
    )
    assert Journal.find_resumable(cfg.paths.host_data_dir, "create", "coder") is None


def test_create_with_reuse_data_attaches_to_existing_dir(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    create(name="coder", agent_type="hermes", reuse_data=True, config_path=saved_config)
    assert (
        "hermes-coder"
        in compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")["services"]
    )


def test_create_refuses_when_service_already_in_compose(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    with pytest.raises(UserError, match="already exists"):
        create(name="coder", agent_type="hermes", config_path=saved_config)


def test_create_picks_next_free_port_when_none_given(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    mocker.patch("agents_cli.compose.used_ports", return_value={8642, 8643})
    create(name="coder", agent_type="hermes", config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["ports"] == ["127.0.0.1:8644:8642"]


def test_create_respects_explicit_port(cfg: AgentsConfig, saved_config: Path, stub_runners) -> None:
    create(name="coder", agent_type="hermes", port=9999, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["ports"] == ["127.0.0.1:9999:8642"]


def test_create_respects_explicit_image_tag(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", image_tag="v2.0.0-rc1", config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert doc["services"]["hermes-coder"]["image"].endswith(":v2.0.0-rc1")


def test_create_completes_journal(cfg: AgentsConfig, saved_config: Path, stub_runners) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    assert Journal.find_resumable(cfg.paths.host_data_dir, "create", "coder") is None


def test_create_resumes_from_partial_journal(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """Pre-seed a journal as if a previous run completed two of four phases.
    Re-running should pick up at service_up and skip the already-done phases.
    """
    j = Journal.begin(cfg.paths.host_data_dir, "create", "coder", agents_cmd.CREATE_PHASES)
    # Simulate data dir + compose already done
    (cfg.paths.host_data_dir / "coder" / ".data").mkdir(parents=True)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.add_service(
        doc,
        "hermes-coder",
        compose.hermes_service_fragment(name="coder", cfg=cfg, port=8642, image_tag="v1.0.0"),
    )
    compose_path = cfg.paths.host_data_dir / "docker-compose.yml"
    compose_path.write_text(compose.dump_compose(doc), encoding="utf-8")
    j.advance("data_dir_created")
    j.advance("compose_updated")

    create_data_spy = mocker.spy(agents_cmd, "_create_data_dirs")
    add_service_spy = mocker.spy(agents_cmd, "_add_service_to_compose")
    up_spy = mocker.patch("agents_cli.compose_runner.up", return_value=_ok())

    create(name="coder", agent_type="hermes", reuse_data=True, config_path=saved_config)

    # Skipped finished phases
    create_data_spy.assert_not_called()
    add_service_spy.assert_not_called()
    # Ran remaining phase
    up_spy.assert_called_once()


def test_create_with_setup_runs_wizard(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    setup_spy = mocker.spy(agents_cmd, "_run_agent_setup")
    create(name="coder", agent_type="hermes", run_setup=True, config_path=saved_config)
    setup_spy.assert_called_once()


def test_create_with_setup_uses_interactive_runner(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    # The setup wizard needs stdin/stdout/stderr inherited from the parent
    # process. The non-interactive runner captures output and would silently
    # swallow prompts (user sees "setup wizard ran" without ever being
    # asked anything). Assert the interactive helper is the one called.
    create(name="coder", agent_type="hermes", run_setup=True, config_path=saved_config)
    stub_runners["exec_in_vm_interactive"].assert_called_once()


def test_create_with_setup_raises_on_wizard_failure(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    # Non-zero exit from the wizard must surface as a UserError pointing
    # at `agents setup <name>` for retry — not silent success.
    stub_runners["exec_in_vm_interactive"].return_value = 1
    with pytest.raises(UserError, match="agents setup"):
        create(name="coder", agent_type="hermes", run_setup=True, config_path=saved_config)


# ---------- list ----------


def test_list_agents_returns_rows_for_each_service(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    rows = list_agents(config_path=saved_config)
    names = {r["name"] for r in rows}
    assert names == {"coder", "personal"}
    types = {r["type"] for r in rows}
    assert types == {"hermes", "openclaw"}


def test_list_agents_ignores_non_agent_services(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.add_service(doc, "random-helper", {"image": "x:y"})
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )
    rows = list_agents(config_path=saved_config)
    assert rows == []


# ---------- remove ----------


def test_remove_stops_and_drops_service(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    remove("coder", yes=True, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" not in (doc.get("services") or {})
    assert not (cfg.paths.host_data_dir / "coder").exists()


def test_remove_preserves_data_when_delete_data_false(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    remove("coder", yes=True, delete_data=False, config_path=saved_config)
    assert (cfg.paths.host_data_dir / "coder").exists()


def test_remove_unknown_agent_raises(saved_config: Path, stub_runners) -> None:
    with pytest.raises(UserError, match="not found"):
        remove("ghost", yes=True, config_path=saved_config)


def test_remove_aborts_without_confirmation(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    with pytest.raises(UserError, match="aborted"):
        remove("coder", config_path=saved_config)


# ---------- rename ----------


def test_rename_updates_compose_and_dir(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    rename("coder", "writer", yes=True, config_path=saved_config)

    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-writer" in doc["services"]
    assert "hermes-coder" not in doc["services"]
    # Volume paths rewritten
    vols = doc["services"]["hermes-writer"]["volumes"]
    assert any("./writer/.data:/opt/data" in v for v in vols)
    assert not any("./coder/" in v for v in vols)
    # env_file rewritten too — a stale `./coder/.env` would break EVERY
    # compose op for EVERY agent once the old dir is gone.
    assert doc["services"]["hermes-writer"]["env_file"] == ["./writer/.env"]
    # Container name rewritten
    assert doc["services"]["hermes-writer"]["container_name"] == "hermes-writer"
    # Dir renamed
    assert (cfg.paths.host_data_dir / "writer").is_dir()
    assert not (cfg.paths.host_data_dir / "coder").exists()


def test_rename_rewrites_openclaw_env_file_and_volumes(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """Both fragments carry `env_file: [./<name>/.env]` — the OpenClaw
    block must round-trip through rename just like the Hermes one."""
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    rename("personal", "friend", yes=True, config_path=saved_config)

    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    svc = doc["services"]["openclaw-friend"]
    assert svc["env_file"] == ["./friend/.env"]
    assert any("./friend/.openclaw" in v for v in svc["volumes"])
    assert any("./friend/workspace" in v for v in svc["volumes"])
    flat = str(dict(svc))
    assert "./personal/" not in flat


def test_rename_rewrites_string_form_env_file(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """Compose accepts `env_file: ./<name>/.env` (bare string) as well as
    the list form — hand-edited blocks use it. Rename must rewrite both."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    compose_path = cfg.paths.host_data_dir / "docker-compose.yml"
    doc = compose.load_compose(compose_path)
    doc["services"]["hermes-coder"]["env_file"] = "./coder/.env"
    compose_path.write_text(compose.dump_compose(doc), encoding="utf-8")

    rename("coder", "writer", yes=True, config_path=saved_config)

    doc = compose.load_compose(compose_path)
    assert doc["services"]["hermes-writer"]["env_file"] == "./writer/.env"


def test_rename_renames_dir_before_compose_write(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """The rewritten block points env_file at ./<new>/.env, and the
    compose write validates via `docker compose config -q` — which
    errors on a missing env file. The dir rename must land first or
    the rewritten block can never validate in production."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    real_write = compose.write_compose_atomically
    new_env_present_at_write: list[bool] = []

    def wrapper(cfg_arg, doc):
        new_env_present_at_write.append((cfg.paths.host_data_dir / "writer" / ".env").exists())
        return real_write(cfg_arg, doc)

    mocker.patch("agents_cli.compose.write_compose_atomically", side_effect=wrapper)
    rename("coder", "writer", yes=True, config_path=saved_config)
    assert new_env_present_at_write and all(new_env_present_at_write)


def test_rename_refuses_when_target_exists(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    create(name="writer", agent_type="hermes", config_path=saved_config)
    with pytest.raises(UserError, match="already exists"):
        rename("coder", "writer", config_path=saved_config)


def test_rename_refuses_unknown_source(saved_config: Path, stub_runners) -> None:
    with pytest.raises(UserError, match="not in compose"):
        rename("ghost", "x", config_path=saved_config)


def test_rename_refuses_openviking_sidecar_in_compose(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """A sibling `openviking-<name>` service can't be migrated by rename
    (container_name, volume path, DNS endpoint in config.yaml all carry
    the old name) — refuse rather than half-rename."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    compose_path = cfg.paths.host_data_dir / "docker-compose.yml"
    doc = compose.load_compose(compose_path)
    compose.add_service(doc, "openviking-coder", {"image": "volcengine/openviking:v0.1"})
    doc["services"]["hermes-coder"]["depends_on"] = ["openviking-coder"]
    compose_path.write_text(compose.dump_compose(doc), encoding="utf-8")

    with pytest.raises(UserError, match=r"[Dd]isable memory"):
        rename("coder", "writer", yes=True, config_path=saved_config)
    # Nothing was touched.
    doc = compose.load_compose(compose_path)
    assert "hermes-coder" in doc["services"]
    assert (cfg.paths.host_data_dir / "coder").is_dir()


def test_rename_refuses_openviking_via_memory_state_file(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """Detection also fires off the memory state file — covers a state
    where the sidecar's compose block was lost (e.g. VM rebuild)."""
    import json

    create(name="coder", agent_type="hermes", config_path=saved_config)
    state_dir = cfg.paths.host_data_dir / ".memory-state"
    state_dir.mkdir()
    (state_dir / "coder.json").write_text(
        json.dumps({"plugin": "openviking", "sidecar_service": "openviking-coder"}),
        encoding="utf-8",
    )
    with pytest.raises(UserError, match=r"[Dd]isable memory"):
        rename("coder", "writer", yes=True, config_path=saved_config)


def test_rename_allows_non_openviking_memory_state(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """In-process backends (holographic, mem0) have no sibling service —
    the refusal must not over-trigger on them."""
    import json

    create(name="coder", agent_type="hermes", config_path=saved_config)
    state_dir = cfg.paths.host_data_dir / ".memory-state"
    state_dir.mkdir()
    (state_dir / "coder.json").write_text(json.dumps({"plugin": "holographic"}), encoding="utf-8")
    rename("coder", "writer", yes=True, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-writer" in doc["services"]


def test_rename_aborts_without_confirmation(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """Rename force-recreates a running container — it must confirm the
    downtime like reseat does."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    with pytest.raises(UserError, match="aborted"):
        rename("coder", "writer", config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" in doc["services"]
    assert (cfg.paths.host_data_dir / "coder").is_dir()


def test_rename_yes_skips_confirmation(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    create(name="coder", agent_type="hermes", config_path=saved_config)
    confirm_spy = mocker.spy(agents_cmd.ui, "confirm")
    rename("coder", "writer", yes=True, config_path=saved_config)
    confirm_spy.assert_not_called()


def test_rename_cli_yes_flag_wires_through(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """`agents rename -y old new` must skip the prompt (cli.py wiring)."""
    from click.testing import CliRunner

    from agents_cli import cli as cli_mod

    create(name="coder", agent_type="hermes", config_path=saved_config)
    mocker.patch("agents_cli.config.load", return_value=cfg)
    confirm_spy = mocker.spy(agents_cmd.ui, "confirm")
    result = CliRunner().invoke(cli_mod.main, ["rename", "coder", "writer", "-y"])
    assert result.exit_code == 0, result.output
    confirm_spy.assert_not_called()
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-writer" in doc["services"]
