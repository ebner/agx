"""``agents access`` end-to-end: grant/revoke/list, bare-metal no-op,
filesystem refusals, auto-grant hook.

Mocked at the ``permissions.run_*`` / ``probe_acl_support`` boundary
so tests don't actually try to sudo or write ACLs to the test runner's
filesystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents_cli import access_cmd
from agents_cli import config as cfg_mod
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import UserError

# ---------- fixtures ----------


@pytest.fixture
def cfg_with_coder(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text(
        "services:\n  hermes-coder:\n    image: x\n  openclaw-bob:\n    image: y\n",
        encoding="utf-8",
    )
    (data_dir / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    (data_dir / "coder").mkdir()
    (data_dir / "coder" / ".data").mkdir()
    (data_dir / "bob").mkdir()
    (data_dir / "bob" / ".openclaw").mkdir()
    (data_dir / "bob" / "workspace").mkdir()
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
def saved_config(cfg_with_coder: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg_with_coder, p)
    return p


@pytest.fixture
def acl_mocked(mocker: Any):
    """Stub the ACL probe and the two run paths so tests don't shell out."""
    return {
        "probe": mocker.patch(
            "agents_cli.permissions.probe_acl_support", return_value=(True, None)
        ),
        "run_sudo": mocker.patch("agents_cli.permissions.run_with_sudo"),
        "run_owned": mocker.patch("agents_cli.permissions.run_owned"),
    }


# ---------- grant ----------


def test_grant_hermes_targets_data_dir_with_ro_acl(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    """Default grant is read-only; setfacl spec must be ``u:<huid>:rX``,
    NOT ``rwX``. RO is the safe default — the user's likely use case
    is "I just want to read auth.json / SOUL.md".
    """
    access_cmd.grant(agent="coder", writable=False, yes=True, config_path=saved_config)
    acl_mocked["run_sudo"].assert_called_once()
    cmds = acl_mocked["run_sudo"].call_args.args[0]
    # Both -m (recurse) and -d -m (default) for the .data dir.
    assert any("u:1000:rX" in arg for cmd in cmds for arg in cmd)
    assert not any("rwX" in arg for cmd in cmds for arg in cmd)
    paths_targeted = {c[-1] for c in cmds}
    assert str(cfg_with_coder.paths.host_data_dir / "coder" / ".data") in paths_targeted


def test_grant_with_rw_uses_rwx_spec(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    access_cmd.grant(agent="coder", writable=True, yes=True, config_path=saved_config)
    cmds = acl_mocked["run_sudo"].call_args.args[0]
    assert any("u:1000:rwX" in arg for cmd in cmds for arg in cmd)


def test_grant_openclaw_targets_both_workspace_and_state(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    """OpenClaw stores state under .openclaw AND workspace. Granting
    both is the consistent default — peeking at workspace is the
    obvious case but state-dir access matters too (the user may want
    to inspect logs or saved sessions).
    """
    access_cmd.grant(agent="bob", writable=False, yes=True, config_path=saved_config)
    cmds = acl_mocked["run_sudo"].call_args.args[0]
    paths_targeted = {c[-1] for c in cmds}
    assert str(cfg_with_coder.paths.host_data_dir / "bob" / ".openclaw") in paths_targeted
    assert str(cfg_with_coder.paths.host_data_dir / "bob" / "workspace") in paths_targeted


def test_grant_refuses_when_filesystem_lacks_acl(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """A polite hard failure beats a silent partial grant that leaves
    the user confused about why they can't read their files."""
    mocker.patch(
        "agents_cli.permissions.probe_acl_support",
        return_value=(False, "Operation not supported"),
    )
    mocker.patch("agents_cli.permissions.run_with_sudo")
    with pytest.raises(UserError, match="doesn't support ACLs"):
        access_cmd.grant(agent="coder", writable=False, yes=True, config_path=saved_config)


def test_grant_refuses_unknown_agent(saved_config: Path, acl_mocked) -> None:
    with pytest.raises(UserError, match="no agent named"):
        access_cmd.grant(agent="ghost", writable=False, yes=True, config_path=saved_config)


def test_grant_refuses_when_data_dir_missing(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    """If the agent was created but never started, .data doesn't exist
    yet — better to surface "start it first" than mysteriously fail."""
    import shutil

    shutil.rmtree(cfg_with_coder.paths.host_data_dir / "coder" / ".data")
    with pytest.raises(UserError, match="bring it up first"):
        access_cmd.grant(agent="coder", writable=False, yes=True, config_path=saved_config)


def test_grant_no_op_on_bare_metal(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    """Container uid maps to host user on bare-metal — files are
    already host-owned, ACL grant would be a sudoed no-op."""
    cfg_with_coder.vm.backend = "bare_metal"
    cfg_mod.save(cfg_with_coder, saved_config)
    access_cmd.grant(agent="coder", writable=False, yes=True, config_path=saved_config)
    acl_mocked["run_sudo"].assert_not_called()


def test_grant_refuses_in_data_dir_in_vm_mode(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    """Host ACLs don't reach into the VM disk. Refuse with hint to
    use `agents export` instead."""
    cfg_with_coder.paths = PathsConfig(
        host_data_dir=str(cfg_with_coder.paths.host_data_dir),
        data_dir_in_vm=True,
    )
    cfg_mod.save(cfg_with_coder, saved_config)
    with pytest.raises(UserError, match="data-dir-in-vm"):
        access_cmd.grant(agent="coder", writable=False, yes=True, config_path=saved_config)


# ---------- revoke ----------


def test_revoke_emits_x_form_for_correct_uid(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    access_cmd.revoke(agent="coder", yes=True, config_path=saved_config)
    cmds = acl_mocked["run_sudo"].call_args.args[0]
    assert all("-x" in c for c in cmds)
    assert all("u:1000" in arg for cmd in cmds for arg in cmd if arg.startswith("u:"))


def test_revoke_no_op_on_bare_metal(
    cfg_with_coder: AgentsConfig, saved_config: Path, acl_mocked
) -> None:
    cfg_with_coder.vm.backend = "bare_metal"
    cfg_mod.save(cfg_with_coder, saved_config)
    access_cmd.revoke(agent="coder", yes=True, config_path=saved_config)
    acl_mocked["run_sudo"].assert_not_called()


# ---------- list ----------


def test_list_returns_rows_with_status_per_path(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """Every probed path produces a row with ``agent``, ``path``,
    ``owner_uid``, ``host_uid``, ``status``. The status comes from
    permissions.assess_host_access — mock it to return a controlled mix.
    """

    def fake_assess(p: Path, uid: int) -> str:
        # Match on basename so the test isn't fooled by parent dirs
        # named "data" (tmp_path here contains "data" higher up).
        return "ok" if Path(p).name == ".data" else "missing"

    mocker.patch("agents_cli.permissions.assess_host_access", side_effect=fake_assess)
    rows = access_cmd.list_access(agent=None, config_path=saved_config)
    statuses_by_path = {r["path"]: r["status"] for r in rows}
    # coder/.data → ok; bob/.openclaw + bob/workspace → missing
    assert statuses_by_path[str(cfg_with_coder.paths.host_data_dir / "coder" / ".data")] == "ok"
    assert (
        statuses_by_path[str(cfg_with_coder.paths.host_data_dir / "bob" / ".openclaw")]
        == "missing"
    )
    assert (
        statuses_by_path[str(cfg_with_coder.paths.host_data_dir / "bob" / "workspace")]
        == "missing"
    )


def test_list_includes_mode_column_per_acl_perms(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """The mode column tells the user whether they granted r or rw —
    the ok/missing status alone doesn't say which permission set is
    in effect. Mirrors the coder ergonomics issue from 2026-05-17.
    """
    mocker.patch("agents_cli.permissions.assess_host_access", return_value="ok")
    # The fixture creates dirs owned by uid 1000 (the test runner);
    # force them to look subuid-owned so the ACL path runs.
    from unittest.mock import MagicMock

    mocker.patch.object(Path, "stat", lambda self: MagicMock(st_uid=100999))

    def fake_perms(p: Path, uid: int) -> str | None:
        # coder granted r-only, bob/.openclaw granted rwx, bob/workspace not granted.
        if p.name == ".data":
            return "r-x"
        if p.name == ".openclaw":
            return "rwx"
        return None

    mocker.patch("agents_cli.permissions.get_user_acl_perms", side_effect=fake_perms)
    rows = access_cmd.list_access(agent=None, config_path=saved_config)
    modes = {r["path"]: r["mode"] for r in rows}
    assert modes[str(cfg_with_coder.paths.host_data_dir / "coder" / ".data")] == "r"
    assert modes[str(cfg_with_coder.paths.host_data_dir / "bob" / ".openclaw")] == "rw"
    assert modes[str(cfg_with_coder.paths.host_data_dir / "bob" / "workspace")] == "—"


def test_list_marks_owner_when_host_user_owns_path(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """No ACL is needed when the user owns the dir — show 'owner' so
    the user isn't confused about whether they need to run grant."""

    def fake_stat(self: Path) -> Any:
        from unittest.mock import MagicMock

        # Pretend the dir is owned by uid 1000 (the host user).
        return MagicMock(st_uid=1000)

    mocker.patch.object(Path, "stat", fake_stat)
    mocker.patch("agents_cli.permissions.assess_host_access", return_value="ok")
    perms_mock = mocker.patch("agents_cli.permissions.get_user_acl_perms")
    rows = access_cmd.list_access(agent="coder", config_path=saved_config)
    assert rows
    for r in rows:
        assert r["mode"] == "owner"
    # When the user already owns the path, no ACL lookup should happen
    # — owner mode is the answer.
    perms_mock.assert_not_called()


def test_list_emits_plain_english_access_column(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """The user-facing column answers "can I read this?" in plain
    English: 'yes' for ok, 'no' for both missing AND masked. The
    internal ``status`` field keeps the distinction so the doctor +
    remediation hint can differ.
    """
    from unittest.mock import MagicMock

    mocker.patch.object(Path, "stat", lambda self: MagicMock(st_uid=100999))

    def fake_assess(p: Path, uid: int) -> str:
        # Three dirs → three different statuses.
        if p.name == ".data":
            return "ok"
        if p.name == ".openclaw":
            return "masked"
        return "missing"

    mocker.patch("agents_cli.permissions.assess_host_access", side_effect=fake_assess)
    mocker.patch("agents_cli.permissions.get_user_acl_perms", return_value="r-x")
    rows = access_cmd.list_access(agent=None, config_path=saved_config)
    access_by_path = {r["path"]: r["access"] for r in rows}
    assert access_by_path[str(cfg_with_coder.paths.host_data_dir / "coder" / ".data")] == "yes"
    assert access_by_path[str(cfg_with_coder.paths.host_data_dir / "bob" / ".openclaw")] == "no"
    assert access_by_path[str(cfg_with_coder.paths.host_data_dir / "bob" / "workspace")] == "no"


def test_list_prints_remediation_hint_for_masked_state(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """The exact coder symptom — mode shows 'rw' but reads fail —
    must come with a one-line remediation telling the user to re-run
    the grant (which is what applies the m::rwx mask reset).
    """
    from unittest.mock import MagicMock

    mocker.patch.object(Path, "stat", lambda self: MagicMock(st_uid=100999))
    mocker.patch("agents_cli.permissions.assess_host_access", return_value="masked")
    mocker.patch("agents_cli.permissions.get_user_acl_perms", return_value="rwx")
    warn_mock = mocker.patch("agents_cli.ui.warn")
    access_cmd.list_access(agent="coder", config_path=saved_config)
    warn_calls = " ".join(call.args[0] for call in warn_mock.call_args_list)
    assert "re-run" in warn_calls.lower()
    assert "agents access grant coder" in warn_calls


def test_list_prints_remediation_hint_for_missing_state(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    from unittest.mock import MagicMock

    mocker.patch.object(Path, "stat", lambda self: MagicMock(st_uid=100999))
    mocker.patch("agents_cli.permissions.assess_host_access", return_value="missing")
    mocker.patch("agents_cli.permissions.get_user_acl_perms", return_value=None)
    warn_mock = mocker.patch("agents_cli.ui.warn")
    access_cmd.list_access(agent="coder", config_path=saved_config)
    warn_calls = " ".join(call.args[0] for call in warn_mock.call_args_list)
    assert "agents access grant coder" in warn_calls


def test_list_omits_hint_when_everything_ok(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """No noise below the table when there's nothing to fix."""
    from unittest.mock import MagicMock

    mocker.patch.object(Path, "stat", lambda self: MagicMock(st_uid=100999))
    mocker.patch("agents_cli.permissions.assess_host_access", return_value="ok")
    mocker.patch("agents_cli.permissions.get_user_acl_perms", return_value="r-x")
    warn_mock = mocker.patch("agents_cli.ui.warn")
    access_cmd.list_access(agent="coder", config_path=saved_config)
    warn_mock.assert_not_called()


def test_list_filtered_to_single_agent(
    cfg_with_coder: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    mocker.patch("agents_cli.permissions.assess_host_access", return_value="ok")
    rows = access_cmd.list_access(agent="coder", config_path=saved_config)
    assert {r["agent"] for r in rows} == {"coder"}


# ---------- auto_grant_on_create hook ----------


def test_auto_grant_on_create_sets_default_acl_only(
    cfg_with_coder: AgentsConfig, acl_mocked
) -> None:
    """At create time the dir is empty — recurse is pointless but the
    default ACL is the whole point. Verifies the toolkit emits the
    -d -m form so future container-written files inherit access.
    """
    access_cmd.auto_grant_on_create(cfg_with_coder, "coder", "hermes")
    acl_mocked["run_owned"].assert_called_once()
    cmds = acl_mocked["run_owned"].call_args.args[0]
    # Must include the -d form for both recipients (host user + subuid).
    default_cmds = [c for c in cmds if c[1] == "-d"]
    assert default_cmds, "must emit at least one -d -m form so future files inherit ACL"
    # And no -R (recurse) form since the dir is empty.
    assert not any("-R" in c for c in cmds)


def test_auto_grant_on_create_skips_on_bare_metal(
    cfg_with_coder: AgentsConfig, acl_mocked
) -> None:
    cfg_with_coder.vm.backend = "bare_metal"
    access_cmd.auto_grant_on_create(cfg_with_coder, "coder", "hermes")
    acl_mocked["run_owned"].assert_not_called()


def test_auto_grant_on_create_skips_in_data_dir_in_vm_mode(
    cfg_with_coder: AgentsConfig, acl_mocked
) -> None:
    cfg_with_coder.paths = PathsConfig(
        host_data_dir=str(cfg_with_coder.paths.host_data_dir),
        data_dir_in_vm=True,
    )
    access_cmd.auto_grant_on_create(cfg_with_coder, "coder", "hermes")
    acl_mocked["run_owned"].assert_not_called()


def test_auto_grant_on_create_warns_on_unsupported_fs(
    cfg_with_coder: AgentsConfig, mocker: Any
) -> None:
    """Lack of ACL support shouldn't break `agents create` — surface
    a warning, let creation proceed. The user can still `agents
    export` for host-side reads."""
    mocker.patch(
        "agents_cli.permissions.probe_acl_support",
        return_value=(False, "Operation not supported"),
    )
    run_owned = mocker.patch("agents_cli.permissions.run_owned")
    access_cmd.auto_grant_on_create(cfg_with_coder, "coder", "hermes")
    run_owned.assert_not_called()


def test_auto_grant_on_create_swallows_run_errors(
    cfg_with_coder: AgentsConfig, mocker: Any
) -> None:
    """If setfacl itself errors at runtime, don't break the whole
    create — it's a best-effort convenience hook. The user can later
    `agents access grant <name>` to retry."""
    mocker.patch("agents_cli.permissions.probe_acl_support", return_value=(True, None))
    mocker.patch(
        "agents_cli.permissions.run_owned",
        side_effect=RuntimeError("simulated setfacl crash"),
    )
    # Must not raise.
    access_cmd.auto_grant_on_create(cfg_with_coder, "coder", "hermes")
