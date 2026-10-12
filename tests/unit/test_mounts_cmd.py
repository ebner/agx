"""``agx mount`` — add/remove/list, per-backend dispatch, rollback.

Tests are mocked at the ``mp`` boundary like the rest of the suite:
``mp.mount`` / ``mp.umount`` are stubbed so we exercise the toolkit's
orchestration without touching a real VM. The compose-override and
``terminal.docker_volumes`` writes go to a real on-disk tmp data-dir,
which lets the round-trip + idempotency tests confirm actual file
state, not just the mock call shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from agx_cli import config as cfg_mod
from agx_cli import mounts_cmd
from agx_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agx_cli.errors import UserError
from agx_cli.mounts_cmd import MountEntry
from agx_cli.mp import MpResult

# ---------- fixtures ----------


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


@pytest.fixture
def host_folder(tmp_path: Path) -> Path:
    p = tmp_path / "shared" / "projects-x"
    p.mkdir(parents=True)
    (p / "README.md").write_text("hi", encoding="utf-8")
    return p


@pytest.fixture
def cfg_with_agent(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # A minimal compose doc with one hermes agent + one openclaw agent.
    (data_dir / "docker-compose.yml").write_text(
        """services:
  hermes-coder:
    image: nousresearch/hermes-agent:v1.0.0
  openclaw-bob:
    image: openclaw/openclaw:0.1.0
""",
        encoding="utf-8",
    )
    (data_dir / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    (data_dir / "coder").mkdir()
    (data_dir / "coder" / ".data").mkdir()
    (data_dir / "coder" / ".data" / "config.yaml").write_text(
        "terminal:\n  backend: docker\n  docker_volumes: []\n",
        encoding="utf-8",
    )
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
def saved_config(cfg_with_agent: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg_with_agent, p)
    return p


@pytest.fixture
def mounts_mocked(mocker, cfg_with_agent: AgentsConfig):
    """Stub the VM-mount + hermes_config.write + auto-ACL paths so
    add/remove exercise only the orchestration + on-disk side effects.

    The ACL helpers are stubbed (rather than running real ``setfacl``)
    because (a) we don't want tests touching real ACL state and (b)
    most failure paths in mounts_cmd are independent of the ACL
    plumbing. There are dedicated tests for the ACL wiring below.

    ``install_vm_fs_simulation`` is installed so ``read_doc`` (which
    routes through ``docker compose exec cat`` in both modes since
    the 2026-05-22 subuid-readability fix) finds the host-side
    config.yaml. The simulator translates ``cat /opt/data/X`` to a
    host read at ``<data-dir>/<agent>/.data/X``.
    """
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg_with_agent)
    mounts = {
        "mp_mount": mocker.patch("agx_cli.mp.mount", return_value=_ok()),
        "mp_umount": mocker.patch("agx_cli.mp.umount", return_value=_ok()),
        # hermes_config.write_atomically goes through `docker compose exec`
        # which the unit tests stub out — replace it with a direct file
        # write so the test can read the resulting config.yaml back.
        "hermes_write": mocker.patch(
            "agx_cli.hermes_config.write_atomically",
            side_effect=_fake_hermes_write,
        ),
        "acl_probe": mocker.patch(
            "agx_cli.permissions.probe_acl_support", return_value=(True, None)
        ),
        "acl_run_owned": mocker.patch("agx_cli.permissions.run_owned"),
        "acl_subuid": mocker.patch(
            "agx_cli.permissions.compute_container_subuid", return_value=100999
        ),
    }
    return mounts


def _fake_hermes_write(cfg, agent_name, doc):
    """Stand-in for the real `docker compose exec`-driven write — just
    dumps the doc to the agent's config.yaml on the host. Mirrors the
    test_codex / test_hermes_config patterns.
    """
    import io

    from agx_cli import hermes_config

    p = cfg.paths.host_data_dir / agent_name / ".data" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    YAML().dump(doc, buf)
    p.write_text(buf.getvalue(), encoding="utf-8")
    _ = hermes_config  # silence unused-import lint


# ---------- MountEntry / vm_path ----------


def test_vm_path_is_deterministic_and_isolated_per_agent() -> None:
    e = MountEntry(host_path="/h/x", container_path="/workspace/x", readonly=True, target="both")
    p_a = e.vm_path("coder")
    p_b = e.vm_path("bob")
    assert p_a != p_b
    assert "/host-extras/coder/" in p_a
    assert "/host-extras/bob/" in p_b
    # Stable across calls.
    assert e.vm_path("coder") == p_a


def test_vm_path_basename_collisions_are_disambiguated_by_hash() -> None:
    """Two mounts with the same basename but different container paths
    must NOT collide on the VM side — that's the whole point of the
    8-hex hash prefix."""
    e1 = MountEntry(
        host_path="/a/x", container_path="/workspace/x", readonly=True, target="runtime"
    )
    e2 = MountEntry(host_path="/b/x", container_path="/projects/x", readonly=True, target="runtime")
    assert e1.vm_path("coder") != e2.vm_path("coder")
    # ...but the basename suffix is preserved for human grokability.
    assert e1.vm_path("coder").endswith("-x")
    assert e2.vm_path("coder").endswith("-x")


# ---------- validation ----------


def test_add_rejects_relative_host_path(saved_config: Path) -> None:
    with pytest.raises(UserError, match="must be absolute"):
        mounts_cmd.add(
            agent="coder",
            host="relative/path",
            container="/workspace/x",
            readonly=True,
            target=None,
            restart=False,
            config_path=saved_config,
        )


def test_add_rejects_nonexistent_host_path(saved_config: Path) -> None:
    with pytest.raises(UserError, match="does not exist"):
        mounts_cmd.add(
            agent="coder",
            host="/nope/does/not/exist",
            container="/workspace/x",
            readonly=True,
            target=None,
            restart=False,
            config_path=saved_config,
        )


def test_add_rejects_relative_container_path(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    with pytest.raises(UserError, match=r"container.*absolute"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="workspace/x",
            readonly=True,
            target=None,
            restart=False,
            config_path=saved_config,
        )


@pytest.mark.parametrize(
    "reserved",
    ["/opt/data", "/home/hermes/.codex", "/proc", "/etc", "/run", "/"],
)
def test_add_rejects_reserved_container_paths(
    host_folder: Path, saved_config: Path, mounts_mocked, reserved: str
) -> None:
    with pytest.raises(UserError, match="reserved"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container=reserved,
            readonly=True,
            target=None,
            restart=False,
            config_path=saved_config,
        )


def test_add_rejects_shadowing_reserved_prefix(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    with pytest.raises(UserError, match="shadow"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/opt/data/danger",
            readonly=True,
            target=None,
            restart=False,
            config_path=saved_config,
        )


def test_add_refuses_sandbox_target_on_openclaw(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    with pytest.raises(UserError, match="Hermes-only"):
        mounts_cmd.add(
            agent="bob",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="sandbox",
            restart=False,
            config_path=saved_config,
        )


def test_add_refuses_unknown_agent(host_folder: Path, saved_config: Path, mounts_mocked) -> None:
    with pytest.raises(UserError, match="no agent named"):
        mounts_cmd.add(
            agent="ghost",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target=None,
            restart=False,
            config_path=saved_config,
        )


def test_add_refuses_duplicate_container_path(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    with pytest.raises(UserError, match="already has a mount"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="runtime",
            restart=False,
            config_path=saved_config,
        )


# ---------- add: end-to-end side effects ----------


def test_add_runtime_writes_compose_override(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """Mount with --target runtime must add a volume entry to the
    docker-compose.override.yml for the hermes-coder service.
    Override file is created on demand if absent.
    """
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )

    override_path = cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml"
    assert override_path.exists(), "override file should have been created"
    doc = YAML().load(override_path.read_text(encoding="utf-8"))
    volumes = doc["services"]["hermes-coder"]["volumes"]
    assert len(volumes) == 1
    assert "/workspace/x:ro" in volumes[0]
    assert "/host-extras/coder/" in volumes[0]


def test_add_sandbox_writes_terminal_docker_volumes(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="sandbox",
        restart=False,
        config_path=saved_config,
    )
    config_path = cfg_with_agent.paths.host_data_dir / "coder" / ".data" / "config.yaml"
    doc = YAML().load(config_path.read_text(encoding="utf-8"))
    volumes = doc["terminal"]["docker_volumes"]
    assert len(volumes) == 1
    assert "/workspace/x:ro" in volumes[0]


def test_add_both_default_for_hermes_touches_runtime_and_sandbox(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """Hermes' default ``target=None`` resolves to 'both' — both
    override and config.yaml should gain the entry.
    """
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target=None,
        restart=False,
        config_path=saved_config,
    )
    override = YAML().load(
        (cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml").read_text(
            encoding="utf-8"
        )
    )
    assert override["services"]["hermes-coder"]["volumes"]
    config = YAML().load(
        (cfg_with_agent.paths.host_data_dir / "coder" / ".data" / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert config["terminal"]["docker_volumes"]


def test_add_runtime_default_for_openclaw_skips_sandbox(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """OpenClaw's default ``target=None`` resolves to 'runtime' — only
    the override is touched; OpenClaw has no terminal.docker_volumes
    concept and we must not invent one.
    """
    mounts_cmd.add(
        agent="bob",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target=None,
        restart=False,
        config_path=saved_config,
    )
    # The hermes_config write helper must NOT have been called for an
    # openclaw mount.
    mounts_mocked["hermes_write"].assert_not_called()


def test_add_calls_mp_mount_on_libvirt_backend(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """The VM-mount phase must invoke ``mp.mount`` with the deterministic
    VM path so the libvirt/multipass backend actually attaches the share.
    """
    cfg_with_agent.vm.backend = "libvirt"
    cfg_mod.save(cfg_with_agent, saved_config)
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    mounts_mocked["mp_mount"].assert_called_once()
    args = mounts_mocked["mp_mount"].call_args
    assert args.kwargs["host_uid"] == 1000
    assert args.kwargs["host_gid"] == 1000
    assert args.args[2].startswith("/home/ubuntu/host-extras/coder/")


def test_add_skips_mp_mount_on_bare_metal(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """Bare-metal: the host folder is already addressable to the rootless
    Docker daemon, so the VM-mount step is a no-op."""
    cfg_with_agent.vm.backend = "bare_metal"
    cfg_mod.save(cfg_with_agent, saved_config)
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    mounts_mocked["mp_mount"].assert_not_called()


def test_add_rolls_back_vm_mount_on_compose_failure(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
    mocker,
) -> None:
    """If the compose-override write fails after the VM mount succeeds,
    we MUST call mp.umount to roll back — otherwise the next add (or a
    later prune) trips over the orphan."""
    mocker.patch(
        "agx_cli.compose.write_override_atomically",
        side_effect=OSError("simulated disk full"),
    )
    with pytest.raises(OSError, match="disk full"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="runtime",
            restart=False,
            config_path=saved_config,
        )
    mounts_mocked["mp_umount"].assert_called_once()


def test_add_failure_rolls_back_override_entry_and_acls(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
) -> None:
    """Phase-3 (sandbox config) failure after phase-2 wrote the override:
    the manifest is only written in the final phase, so remove() can
    never see these orphans — the rollback itself must revert the
    override volume entry, the VM mount, AND the phase-0 ACLs."""
    mounts_mocked["hermes_write"].side_effect = OSError("simulated config write failure")
    with pytest.raises(OSError, match="config write failure"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="both",
            restart=False,
            config_path=saved_config,
        )
    # Manifest never persisted.
    assert mounts_cmd.load_manifest(cfg_with_agent, "coder") == []
    # Override entry reverted (file may exist with an empty doc).
    override_path = cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml"
    if override_path.exists():
        doc = YAML().load(override_path.read_text(encoding="utf-8"))
        services = (doc or {}).get("services") or {}
        assert "hermes-coder" not in services or not services["hermes-coder"].get("volumes")
    # VM mount rolled back.
    mounts_mocked["mp_umount"].assert_called_once()
    # ACLs stripped (a `-x` setfacl ran) — no other manifest references
    # the host folder, so the strip is safe.
    flat = [
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    ]
    assert "-x" in flat


def test_add_failure_rollback_preserves_acls_shared_with_other_agent(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
) -> None:
    """If another agent's manifest already references the same host
    folder, the failed add's rollback must NOT strip the shared ACLs —
    all agents map to the same container subuid."""
    mounts_cmd.add(
        agent="bob",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    mounts_mocked["acl_run_owned"].reset_mock()
    mounts_mocked["hermes_write"].side_effect = OSError("simulated failure")
    with pytest.raises(OSError):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="both",
            restart=False,
            config_path=saved_config,
        )
    flat = [
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    ]
    assert "-x" not in flat, "shared ACLs must survive the rollback"


# ---------- rollback removes ONLY what this call inserted (rule #10) ----------


def _coder_volume(container_path: str, *, readonly: bool) -> str:
    """The exact override/sandbox volume string add() would write for
    coder at ``container_path`` (vm_path is a pure function of the
    container path + agent name)."""
    entry = MountEntry(
        host_path="/irrelevant", container_path=container_path, readonly=readonly, target="both"
    )
    suffix = ":ro" if readonly else ""
    return f"{entry.vm_path('coder')}:{container_path}{suffix}"


def test_add_failure_rollback_preserves_preexisting_override_entry(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
) -> None:
    """The user may have hand-added the IDENTICAL volume entry to
    docker-compose.override.yml before running `agx mount add`. The
    override is sacred (rule #10): a later-phase failure must roll back
    only entries THIS call inserted — the pre-existing one stays."""
    volume = _coder_volume("/workspace/x", readonly=True)
    override_path = cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml"
    override_path.write_text(
        f"services:\n  hermes-coder:\n    volumes:\n      - {volume}\n",
        encoding="utf-8",
    )

    mounts_mocked["hermes_write"].side_effect = OSError("simulated config write failure")
    with pytest.raises(OSError, match="config write failure"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="both",
            restart=False,
            config_path=saved_config,
        )
    doc = YAML().load(override_path.read_text(encoding="utf-8"))
    assert doc["services"]["hermes-coder"]["volumes"] == [volume], (
        "user-added override entry must survive the rollback"
    )


def test_add_failure_rollback_removes_inserted_entry_but_keeps_user_entries(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
) -> None:
    """Contrast case: the entry THIS call inserted is rolled back, while
    an unrelated user-added volume in the same service block survives."""
    user_volume = "/srv/user-thing:/data/user-thing:ro"
    override_path = cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml"
    override_path.write_text(
        f"services:\n  hermes-coder:\n    volumes:\n      - {user_volume}\n",
        encoding="utf-8",
    )

    mounts_mocked["hermes_write"].side_effect = OSError("simulated config write failure")
    with pytest.raises(OSError, match="config write failure"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="both",
            restart=False,
            config_path=saved_config,
        )
    doc = YAML().load(override_path.read_text(encoding="utf-8"))
    assert doc["services"]["hermes-coder"]["volumes"] == [user_volume], (
        "the inserted entry is removed; the user's other entry stays"
    )


def test_add_failure_rollback_keeps_user_flipped_suffix_twin(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
) -> None:
    """remove()'s flipped-:ro-suffix tolerance must NOT leak into add()'s
    rollback: when the user hand-added the rw twin of the entry this
    call inserts as ro, the rollback removes only the exact ro form."""
    flipped_twin = _coder_volume("/workspace/x", readonly=False)
    override_path = cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml"
    override_path.write_text(
        f"services:\n  hermes-coder:\n    volumes:\n      - {flipped_twin}\n",
        encoding="utf-8",
    )

    mounts_mocked["hermes_write"].side_effect = OSError("simulated config write failure")
    with pytest.raises(OSError, match="config write failure"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="both",
            restart=False,
            config_path=saved_config,
        )
    doc = YAML().load(override_path.read_text(encoding="utf-8"))
    assert doc["services"]["hermes-coder"]["volumes"] == [flipped_twin], (
        "the user's rw twin must survive; only the ro entry this call inserted is removed"
    )


def test_add_failure_rollback_preserves_preexisting_sandbox_entry(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
    mocker,
) -> None:
    """Same actually-inserted rule for terminal.docker_volumes: a user-
    added identical sandbox entry survives a later-phase failure — the
    rollback must not even rewrite config.yaml."""
    volume = _coder_volume("/workspace/x", readonly=True)
    config_yaml = cfg_with_agent.paths.host_data_dir / "coder" / ".data" / "config.yaml"
    config_yaml.write_text(
        f"terminal:\n  backend: docker\n  docker_volumes:\n    - {volume}\n",
        encoding="utf-8",
    )
    mocker.patch(
        "agx_cli.mounts_cmd.save_manifest",
        side_effect=OSError("simulated manifest failure"),
    )
    with pytest.raises(OSError, match="manifest failure"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="both",
            restart=False,
            config_path=saved_config,
        )
    # Neither the add (entry pre-existed) nor the rollback wrote the file.
    mounts_mocked["hermes_write"].assert_not_called()
    doc = YAML().load(config_yaml.read_text(encoding="utf-8"))
    assert doc["terminal"]["docker_volumes"] == [volume]


def test_add_failure_rollback_preserves_preexisting_acl_grant(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
    mocker,
) -> None:
    """A subuid ACL grant that predates this call (user-set, or drifted
    outside any manifest) must survive the rollback even though no other
    manifest references the folder. The apply still runs (setfacl is
    idempotent and converges recursion/mask) but the strip must not."""
    mocker.patch("agx_cli.permissions.has_user_acl", return_value=True)
    mounts_mocked["hermes_write"].side_effect = OSError("simulated config write failure")
    with pytest.raises(OSError, match="config write failure"):
        mounts_cmd.add(
            agent="coder",
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="both",
            restart=False,
            config_path=saved_config,
        )
    flat = [
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    ]
    assert "-m" in flat, "the grant is still applied (converges recursion/mask)"
    assert "-x" not in flat, "a pre-existing grant must survive the rollback"


def test_add_rw_flag_drops_ro_suffix(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=False,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    override = YAML().load(
        (cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml").read_text(
            encoding="utf-8"
        )
    )
    vol = override["services"]["hermes-coder"]["volumes"][0]
    assert not vol.endswith(":ro")


# ---------- --restart: recreate, not `compose restart` ----------


def test_add_restart_recreates_via_compose_up(
    host_folder: Path, saved_config: Path, mounts_mocked, mocker
) -> None:
    """`docker compose restart` never reloads compose config — routing
    --restart through it silently left the new mount unapplied. It must
    go through `compose up -d <svc>`, which recreates on config drift
    (same trap secrets_cmd documents for env_file changes)."""
    up = mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    restart = mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=True,
        config_path=saved_config,
    )
    up.assert_called_once()
    assert up.call_args.args[1] == "hermes-coder"
    restart.assert_not_called()


def test_remove_restart_recreates_via_compose_up(
    host_folder: Path, saved_config: Path, mounts_mocked, mocker
) -> None:
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    up = mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    restart = mocker.patch("agx_cli.compose_runner.restart", return_value=_ok())
    mounts_cmd.remove(
        agent="coder",
        container="/workspace/x",
        restart=True,
        config_path=saved_config,
    )
    up.assert_called_once()
    assert up.call_args.args[1] == "hermes-coder"
    restart.assert_not_called()


def test_add_without_restart_warns_with_recreate_hint(
    host_folder: Path, saved_config: Path, mounts_mocked, mocker
) -> None:
    """The manual hint must not point at `agx restart` as the fix —
    that command doesn't reload compose config either."""
    warn = mocker.spy(mounts_cmd.ui, "warn")
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    hints = [c.args[0] for c in warn.call_args_list if "recreate" in c.args[0]]
    assert hints, "expected a recreate-required warning"
    assert "--restart" in hints[0]
    assert "won't reload compose config" in hints[0]


# ---------- list ----------


def test_list_returns_per_agent_entries(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    rows = mounts_cmd.list_mounts(agent="coder", config_path=saved_config)
    assert len(rows) == 1
    assert rows[0]["container"] == "/workspace/x"
    assert rows[0]["readonly"] is True
    assert rows[0]["agent"] == "coder"


def test_list_all_agents(host_folder: Path, saved_config: Path, mounts_mocked) -> None:
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    rows = mounts_cmd.list_mounts(agent=None, config_path=saved_config)
    assert {r["agent"] for r in rows} == {"coder"}


# ---------- remove ----------


def test_remove_clears_manifest_compose_and_terminal(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
) -> None:
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="both",
        restart=False,
        config_path=saved_config,
    )
    mounts_cmd.remove(
        agent="coder",
        container="/workspace/x",
        restart=False,
        config_path=saved_config,
    )
    assert mounts_cmd.load_manifest(cfg_with_agent, "coder") == []
    override = YAML().load(
        (cfg_with_agent.paths.host_data_dir / "docker-compose.override.yml").read_text(
            encoding="utf-8"
        )
    )
    services = override.get("services") or {}
    # Either the service block is gone or its volumes list is empty.
    assert "hermes-coder" not in services or not services["hermes-coder"].get("volumes")
    config = YAML().load(
        (cfg_with_agent.paths.host_data_dir / "coder" / ".data" / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert not config["terminal"].get("docker_volumes")


def test_remove_invokes_mp_umount_on_libvirt(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
) -> None:
    cfg_with_agent.vm.backend = "libvirt"
    cfg_mod.save(cfg_with_agent, saved_config)
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    mounts_cmd.remove(
        agent="coder",
        container="/workspace/x",
        restart=False,
        config_path=saved_config,
    )
    mounts_mocked["mp_umount"].assert_called_once()


def test_remove_unknown_container_raises(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    with pytest.raises(UserError, match="no mount at container path"):
        mounts_cmd.remove(
            agent="coder",
            container="/workspace/never-added",
            restart=False,
            config_path=saved_config,
        )


# ---------- remove_all_for_agent ----------


def test_remove_all_for_agent_clears_every_mount(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
    tmp_path: Path,
) -> None:
    """Two mounts on one agent — both get torn down; the manifest is gone."""
    second = tmp_path / "shared" / "projects-y"
    second.mkdir(parents=True)
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    mounts_cmd.add(
        agent="coder",
        host=str(second),
        container="/workspace/y",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    cfg_with_agent.vm.backend = "libvirt"
    mounts_cmd.remove_all_for_agent(cfg_with_agent, "coder")
    assert not mounts_cmd.manifest_path(cfg_with_agent, "coder").exists()
    assert mounts_mocked["mp_umount"].call_count == 2


def test_remove_all_for_agent_missing_manifest_is_noop(
    cfg_with_agent: AgentsConfig, mounts_mocked
) -> None:
    """The hook in agents_cmd.remove must tolerate agents that never
    added a mount — manifest absence is the common case."""
    mounts_cmd.remove_all_for_agent(cfg_with_agent, "coder")
    mounts_mocked["mp_umount"].assert_not_called()


# ---------- manifest I/O ----------


def test_manifest_round_trip(cfg_with_agent: AgentsConfig) -> None:
    entries = [
        MountEntry(host_path="/h/x", container_path="/workspace/x", readonly=True, target="both"),
    ]
    mounts_cmd.save_manifest(cfg_with_agent, "coder", entries)
    loaded = mounts_cmd.load_manifest(cfg_with_agent, "coder")
    assert loaded == entries


def test_add_auto_acls_host_folder_for_subuid_read(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    """A read-only mount must auto-grant the agent's subuid `rX` on
    the host folder. Without this, the agent can't read files in the
    folder unless they're world-readable (which they typically
    aren't for, e.g., mode-600 SSH keys or .env files)."""
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    # Expect at least one setfacl invocation with the subuid + rX.
    flat_argv = " ".join(
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    )
    assert "u:100999:rX" in flat_argv


def test_add_rw_acls_bidirectionally(host_folder: Path, saved_config: Path, mounts_mocked) -> None:
    """`--rw` mount must (a) grant subuid rwX (existing files + new)
    AND (b) set default ACL granting the host user rwx on files the
    agent writes back — otherwise we get back to the original problem
    in the other direction.
    """
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=False,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    flat_argv = " ".join(
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    )
    # Both recipients with rwX.
    assert "u:100999:rwX" in flat_argv  # subuid for agent
    assert "u:1000:rwX" in flat_argv  # host user for default-ACL


def test_add_warns_but_proceeds_on_filesystem_without_acl(
    host_folder: Path, saved_config: Path, mounts_mocked, mocker
) -> None:
    """If the host folder is on FAT/exFAT/network-mount that lacks
    ACL support, the mount itself still works (POSIX-permissive
    files will be readable) — we warn and proceed instead of
    refusing. The agent will hit EACCES on mode-restrictive files
    but that's a clear runtime signal, not a silent breakage.
    """
    mounts_mocked["acl_probe"].return_value = (False, "Operation not supported")
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    # No ACL commands run.
    mounts_mocked["acl_run_owned"].assert_not_called()


def test_add_applies_acl_on_bare_metal(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """Bare-metal runs rootless Docker too: the agent is a subuid on the
    host (NOT a 1:1 map to the host user), so the folder needs the same
    ACL grant as on the VM backends or restrictive files stay unreadable."""
    cfg_with_agent.vm.backend = "bare_metal"
    cfg_mod.save(cfg_with_agent, saved_config)
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    flat_argv = " ".join(
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    )
    assert "u:100999:rX" in flat_argv


def test_add_on_bare_metal_binds_the_host_folder_directly(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """Bare-metal has no ``host-extras`` VM mount behind ``vm_path`` —
    the daemon runs on the host, so both the override volume and the
    sandbox ``docker_volumes`` entry must name the host folder itself."""
    cfg_with_agent.vm.backend = "bare_metal"
    cfg_mod.save(cfg_with_agent, saved_config)
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="both",
        restart=False,
        config_path=saved_config,
    )
    data_dir = cfg_with_agent.paths.host_data_dir
    override = YAML().load((data_dir / "docker-compose.override.yml").read_text(encoding="utf-8"))
    assert override["services"]["hermes-coder"]["volumes"] == [f"{host_folder}:/workspace/x:ro"]
    config = YAML().load(
        (data_dir / "coder" / ".data" / "config.yaml").read_text(encoding="utf-8")
    )
    assert config["terminal"]["docker_volumes"] == [f"{host_folder}:/workspace/x:ro"]

    rows = mounts_cmd.list_mounts(agent="coder", config_path=saved_config)
    assert rows[0]["source"] == str(host_folder)
    assert rows[0]["vm"] is None


def test_remove_on_bare_metal_also_strips_legacy_vm_path_entries(
    host_folder: Path, cfg_with_agent: AgentsConfig, saved_config: Path, mounts_mocked
) -> None:
    """Older toolkit versions wrote the (non-existent) VM-side path as the
    bind source on bare-metal. ``remove`` must still clean those up."""
    cfg_with_agent.vm.backend = "bare_metal"
    cfg_mod.save(cfg_with_agent, saved_config)
    entry = MountEntry(
        host_path=str(host_folder), container_path="/workspace/x", readonly=True, target="runtime"
    )
    mounts_cmd.save_manifest(cfg_with_agent, "coder", [entry])
    data_dir = cfg_with_agent.paths.host_data_dir
    legacy = f"{entry.vm_path('coder')}:/workspace/x:ro"
    (data_dir / "docker-compose.override.yml").write_text(
        f"services:\n  hermes-coder:\n    volumes:\n      - {legacy}\n", encoding="utf-8"
    )

    mounts_cmd.remove(
        agent="coder", container="/workspace/x", restart=False, config_path=saved_config
    )

    override = YAML().load((data_dir / "docker-compose.override.yml").read_text(encoding="utf-8"))
    assert "hermes-coder" not in (override.get("services") or {})
    mounts_mocked["mp_umount"].assert_not_called()


def test_remove_strips_the_acl_entries_we_added(
    host_folder: Path, saved_config: Path, mounts_mocked
) -> None:
    """Symmetric to add: when a mount is removed, its ACL entries
    on the host folder are taken back out (so the user's view of
    that folder is back to whatever they originally had)."""
    mounts_cmd.add(
        agent="coder",
        host=str(host_folder),
        container="/workspace/x",
        readonly=True,
        target="runtime",
        restart=False,
        config_path=saved_config,
    )
    add_call_count = mounts_mocked["acl_run_owned"].call_count
    mounts_cmd.remove(
        agent="coder",
        container="/workspace/x",
        restart=False,
        config_path=saved_config,
    )
    # `-x` (remove) calls should have run on top of the add ones.
    assert mounts_mocked["acl_run_owned"].call_count > add_call_count
    last_argv = mounts_mocked["acl_run_owned"].call_args_list[-1].args[0]
    assert any("-x" in c for c in last_argv)


def test_remove_preserves_acl_shared_with_another_agent(
    host_folder: Path,
    cfg_with_agent: AgentsConfig,
    saved_config: Path,
    mounts_mocked,
    mocker,
) -> None:
    """coder and bob both mount the same host folder. Removing coder's
    mount must NOT strip the folder's ACL entries — every agent maps to
    the SAME container subuid, so bob's mount depends on the very same
    entries. Removing the last reference strips as usual."""
    for agent in ("coder", "bob"):
        mounts_cmd.add(
            agent=agent,
            host=str(host_folder),
            container="/workspace/x",
            readonly=True,
            target="runtime",
            restart=False,
            config_path=saved_config,
        )
    mounts_mocked["acl_run_owned"].reset_mock()
    info_spy = mocker.spy(mounts_cmd.ui, "info")

    mounts_cmd.remove(
        agent="coder",
        container="/workspace/x",
        restart=False,
        config_path=saved_config,
    )
    flat = [
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    ]
    assert "-x" not in flat, "ACL strip must be skipped while bob still references the folder"
    notes = [c.args[0] for c in info_spy.call_args_list if "still referenced" in c.args[0]]
    assert notes and "bob" in notes[0]

    # Last reference gone → strip proceeds.
    mounts_cmd.remove(
        agent="bob",
        container="/workspace/x",
        restart=False,
        config_path=saved_config,
    )
    flat = [
        arg
        for call in mounts_mocked["acl_run_owned"].call_args_list
        for cmd in call.args[0]
        for arg in cmd
    ]
    assert "-x" in flat


def test_manifest_corrupt_raises_user_error(cfg_with_agent: AgentsConfig) -> None:
    p = mounts_cmd.manifest_path(cfg_with_agent, "coder")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(UserError, match="not valid JSON"):
        mounts_cmd.load_manifest(cfg_with_agent, "coder")
