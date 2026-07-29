"""agents_cmd.py: create/list/remove/rename with journal-driven idempotency."""

from __future__ import annotations

import shutil
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
from agents_cli.errors import RuntimeFailure, UserError
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


def test_remove_resumes_after_crash_between_compose_update_and_data_delete(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """A removal that dies after ``compose_updated`` leaves the agent
    undetectable in compose. Re-running must resume from the journal
    (recovering the type from its meta), not refuse with "not found"."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    boom = mocker.patch(
        "agents_cli.agents_cmd._delete_data_dir",
        side_effect=RuntimeFailure("disk on fire"),
    )
    with pytest.raises(RuntimeFailure, match="disk on fire"):
        remove("coder", yes=True, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" not in (doc.get("services") or {})
    j = Journal.find_resumable(cfg.paths.host_data_dir, "remove", "coder")
    assert j is not None
    assert j.meta["agent_type"] == "hermes"
    mocker.stop(boom)
    remove("coder", yes=True, config_path=saved_config)
    assert not (cfg.paths.host_data_dir / "coder").exists()
    assert Journal.find_resumable(cfg.paths.host_data_dir, "remove", "coder") is None


def test_remove_resume_tolerates_pre_meta_journal(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """Journals written before remove recorded ``agent_type`` in meta
    must still resume — the fallback type is only cosmetic because a
    missing compose entry proves the container phases completed."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    j = Journal.begin(cfg.paths.host_data_dir, "remove", "coder", agents_cmd.REMOVE_PHASES)
    for phase in ["mounts_torn_down", "container_stopped", "compose_updated"]:
        j.advance(phase)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    compose.remove_service(doc, "hermes-coder")
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )
    remove("coder", yes=True, config_path=saved_config)
    assert not (cfg.paths.host_data_dir / "coder").exists()
    assert Journal.find_resumable(cfg.paths.host_data_dir, "remove", "coder") is None


def test_remove_repairs_acl_mask_and_retries_on_permission_error(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """PermissionError from rmtree (the entrypoint's chmod clobbered the
    ACL mask) triggers a sudo setfacl repair — with the ``m::rwx`` mask
    reset — and a retry."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    target = cfg.paths.host_data_dir / "coder"
    real_rmtree = shutil.rmtree
    state = {"denied": False}

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path) == target and not state["denied"]:
            state["denied"] = True
            raise PermissionError(13, "Permission denied", str(path))
        return real_rmtree(path, *args, **kwargs)

    mocker.patch("agents_cli.agents_cmd.shutil.rmtree", side_effect=flaky_rmtree)
    sudo = mocker.patch("agents_cli.agents_cmd.permissions.run_with_sudo")
    remove("coder", yes=True, config_path=saved_config)
    assert not target.exists()
    sudo.assert_called_once()
    joined = [" ".join(argv) for argv in sudo.call_args.args[0]]
    assert any("setfacl" in c and "m::rwx" in c and str(target) in c for c in joined)
    assert sudo.call_args.kwargs == {"yes": True}


def test_remove_acl_repair_failure_keeps_journal_and_hints_manual_cleanup(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """If the sudo repair is declined or fails, remove surfaces the
    manual ``sudo rm -rf`` remediation and leaves the journal resumable."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    target = cfg.paths.host_data_dir / "coder"
    real_rmtree = shutil.rmtree

    def denying_rmtree(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return real_rmtree(path, *args, **kwargs)

    mocker.patch("agents_cli.agents_cmd.shutil.rmtree", side_effect=denying_rmtree)
    mocker.patch(
        "agents_cli.agents_cmd.permissions.run_with_sudo",
        side_effect=UserError("aborted by user"),
    )
    with pytest.raises(RuntimeFailure, match="sudo rm -rf"):
        remove("coder", yes=True, config_path=saved_config)
    assert target.exists()
    assert Journal.find_resumable(cfg.paths.host_data_dir, "remove", "coder") is not None


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


# ---------- rename: dir_renamed phase must fail loudly, resume idempotently ----------


def _flip_to_vm_mode(cfg: AgentsConfig, saved_config: Path) -> None:
    """Re-save the config in ``--data-dir-in-vm`` mode with
    ``vm_agents_root`` aliased onto the host data dir, so a locally
    executed bash script (see ``_local_bash``) acts on the exact tree
    the assertions inspect."""
    cfg.paths.data_dir_in_vm = True
    cfg.paths.vm_agents_root = cfg.paths.host_data_dir
    cfg_mod.save(cfg, saved_config)


def _local_bash(vm_name: str, script: str, *, check: bool = True, timeout=None) -> MpResult:
    """Execute the VM-bound bash script on the host. With
    ``vm_agents_root`` aliased to the host data dir this runs the REAL
    script (the branch logic under test) against a real filesystem
    instead of hand-simulating its outcome."""
    import subprocess

    proc = subprocess.run(["bash", "-s"], input=script.encode(), capture_output=True)
    return MpResult(
        stdout=proc.stdout.decode(), stderr=proc.stderr.decode(), returncode=proc.returncode
    )


def test_rename_vm_mode_mv_failure_raises_and_resume_retries_move(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """A transient VM/mv failure in --data-dir-in-vm mode must raise
    BEFORE the journal advances: a journaled-but-unmoved dir would make
    the compose validation fail (env_file points at ./<new>/) while
    every retry skips the dir phase — unrecoverable without hand-editing
    the journal. The resumed run must retry the move and complete."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    _flip_to_vm_mode(cfg, saved_config)

    def failing_bash(vm_name, script, *, check=True, timeout=None):
        if "mv " in script:
            return MpResult(stdout="", stderr="mv: cannot move: I/O error", returncode=1)
        return _local_bash(vm_name, script, check=check, timeout=timeout)

    mocker.patch("agents_cli.mp.exec_bash", side_effect=failing_bash)
    with pytest.raises(RuntimeFailure, match="could not move VM data dir"):
        rename("coder", "writer", yes=True, config_path=saved_config)

    journal = Journal.find_resumable(cfg.paths.host_data_dir, "rename", "coder")
    assert journal is not None, "journal must survive for the resume"
    assert "dir_renamed" not in journal.completed_phases
    # Nothing moved, compose untouched.
    assert (cfg.paths.host_data_dir / "coder").is_dir()
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" in doc["services"]

    # Resume with the transient failure gone: the move is retried and
    # the rename completes end-to-end.
    executed: list[str] = []

    def working_bash(vm_name, script, *, check=True, timeout=None):
        executed.append(script)
        return _local_bash(vm_name, script, check=check, timeout=timeout)

    mocker.patch("agents_cli.mp.exec_bash", side_effect=working_bash)
    rename("coder", "writer", yes=True, config_path=saved_config)
    assert any("mv " in s for s in executed), "resume must retry the move"
    assert (cfg.paths.host_data_dir / "writer").is_dir()
    assert not (cfg.paths.host_data_dir / "coder").exists()
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-writer" in doc["services"]
    assert Journal.find_resumable(cfg.paths.host_data_dir, "rename", "coder") is None


def test_rename_vm_mode_resume_skips_mv_when_move_already_landed(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """Crash window: a previous run's mv landed but the journal never
    advanced. The re-run must treat "new exists, old gone" as success
    WITHOUT executing a second mv (which would fail on the now-missing
    source)."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    _flip_to_vm_mode(cfg, saved_config)
    (cfg.paths.host_data_dir / "coder").rename(cfg.paths.host_data_dir / "writer")
    Journal.begin(cfg.paths.host_data_dir, "rename", "coder", agents_cmd.RENAME_PHASES)

    traces: list[str] = []

    def tracing_bash(vm_name, script, *, check=True, timeout=None):
        import subprocess

        # `bash -x` so the xtrace proves which branch actually executed.
        proc = subprocess.run(["bash", "-x", "-s"], input=script.encode(), capture_output=True)
        traces.append(proc.stderr.decode())
        return MpResult(
            stdout=proc.stdout.decode(), stderr=proc.stderr.decode(), returncode=proc.returncode
        )

    mocker.patch("agents_cli.mp.exec_bash", side_effect=tracing_bash)
    rename("coder", "writer", yes=True, config_path=saved_config)

    assert (cfg.paths.host_data_dir / "writer").is_dir()
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-writer" in doc["services"]
    # The early-exit branch ran; no second mv was attempted.
    assert not any("+ mv" in t for t in traces)
    assert Journal.find_resumable(cfg.paths.host_data_dir, "rename", "coder") is None


def test_rename_vm_mode_missing_source_fails_loudly(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """No source dir AND no target dir is not a resume — it's a broken
    state. Advancing the journal would strand the rename; raise instead."""
    import shutil

    create(name="coder", agent_type="hermes", config_path=saved_config)
    _flip_to_vm_mode(cfg, saved_config)
    shutil.rmtree(cfg.paths.host_data_dir / "coder")

    mocker.patch("agents_cli.mp.exec_bash", side_effect=_local_bash)
    with pytest.raises(RuntimeFailure, match="is missing"):
        rename("coder", "writer", yes=True, config_path=saved_config)
    journal = Journal.find_resumable(cfg.paths.host_data_dir, "rename", "coder")
    assert journal is not None
    assert "dir_renamed" not in journal.completed_phases


def test_rename_host_mode_missing_source_fails_loudly(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """Host-mount mode shares the phase contract: missing source with no
    target must not silently advance."""
    import shutil

    create(name="coder", agent_type="hermes", config_path=saved_config)
    shutil.rmtree(cfg.paths.host_data_dir / "coder")
    with pytest.raises(RuntimeFailure, match="missing"):
        rename("coder", "writer", yes=True, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-coder" in doc["services"]


def test_rename_host_mode_resumes_when_move_already_landed(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """Host-mount twin of the VM resume case: dir already at the new
    name + old gone counts as success, not an error."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    (cfg.paths.host_data_dir / "coder").rename(cfg.paths.host_data_dir / "writer")
    rename("coder", "writer", yes=True, config_path=saved_config)
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    assert "hermes-writer" in doc["services"]


def test_rename_host_mode_stray_target_dir_fails_loudly(
    cfg: AgentsConfig, saved_config: Path, stub_runners
) -> None:
    """Source AND target both present: renaming would nest the old dir
    inside the stray target (mv semantics) — refuse instead."""
    create(name="coder", agent_type="hermes", config_path=saved_config)
    (cfg.paths.host_data_dir / "writer").mkdir()
    with pytest.raises(RuntimeFailure, match="already exists"):
        rename("coder", "writer", yes=True, config_path=saved_config)
    assert (cfg.paths.host_data_dir / "coder").is_dir()


# ---------- progress narration ----------


def _progress_lines(spy) -> list[str]:
    return [c.args[0] for c in spy.call_args_list]


def test_ensure_hermes_image_pulled_narrates_pull(cfg: AgentsConfig, mocker) -> None:
    """A first-create pull is multi-GB; the narration must be always-on
    (ui.progress, not --verbose-gated ui.info) with a duration hint,
    plus a completion line."""
    from agents_cli.agents_cmd import _ensure_hermes_image_pulled

    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_build_pull_runner(inspect_rc=1, pull_rc=0),
    )
    spy = mocker.spy(agents_cmd.ui, "progress")

    _ensure_hermes_image_pulled(cfg, "v1.0.0")

    lines = _progress_lines(spy)
    assert any("pulling nousresearch/hermes-agent:v1.0.0" in ln and "minutes" in ln for ln in lines)
    assert any(ln.startswith("pulled ") for ln in lines)


def test_ensure_hermes_image_pulled_silent_when_cached(cfg: AgentsConfig, mocker) -> None:
    """Image already local (inspect rc=0): nothing slow happens, so no
    pull narration should appear."""
    from agents_cli.agents_cmd import _ensure_hermes_image_pulled

    mocker.patch(
        "agents_cli.mp.exec_in_vm",
        side_effect=_build_pull_runner(inspect_rc=0, pull_rc=0),
    )
    spy = mocker.spy(agents_cmd.ui, "progress")

    _ensure_hermes_image_pulled(cfg, "v1.0.0")

    assert not any("pull" in ln for ln in _progress_lines(spy))


def test_create_narrates_long_steps(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """create's slow sub-steps (compose up, ACL grant, install-path
    chown) each get an always-on progress line."""
    spy = mocker.spy(agents_cmd.ui, "progress")

    create(name="coder", agent_type="hermes", config_path=saved_config)

    lines = _progress_lines(spy)
    assert any("granting host-user ACL" in ln for ln in lines)
    assert any("starting container hermes-coder" in ln for ln in lines)
    assert any("install paths" in ln for ln in lines)
    # firecrawl isn't installed — the wiring no-op must stay silent.
    assert not any("firecrawl" in ln for ln in lines)


def test_reseat_narrates_rewrite_recreate_and_perms(
    cfg: AgentsConfig, saved_config: Path, stub_runners, mocker
) -> None:
    """reseat runs for a while (fragment rewrite → force-recreate →
    chown dance); each stage gets an always-on progress line."""
    create(name="coder", agent_type="hermes", config_path=saved_config)

    sim_runner = stub_runners["exec_in_vm"].side_effect

    def runner(vm_name: str, *cmd: str, check: bool = True):
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok("1000\n")
        return sim_runner(vm_name, *cmd, check=check)

    mocker.patch("agents_cli.mp.exec_in_vm", side_effect=runner)
    spy = mocker.spy(agents_cmd.ui, "progress")

    agents_cmd.reseat("coder", yes=True, config_path=saved_config)

    lines = _progress_lines(spy)
    assert any("rewriting hermes-coder" in ln and "template" in ln for ln in lines)
    assert any("force-recreating hermes-coder" in ln for ln in lines)
    assert any("chown -R" in ln and "/opt/data" in ln for ln in lines)
    assert any("install paths" in ln for ln in lines)
