"""permissions.py: subuid math, ACL argv generation, probe, sudo posture."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents_cli import permissions
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.permissions import AclRecipient


@pytest.fixture(autouse=True)
def _reset_subuid_cache() -> None:
    """Each test starts with a clean subuid cache."""
    permissions._subuid_cache.clear()


# ---------- subuid math ----------


def test_container_uid_0_maps_to_host_uid(mocker: Any) -> None:
    """Rootless Docker: container uid 0 IS the host user. No /etc/subuid lookup."""
    read_text = mocker.patch.object(
        Path, "read_text", side_effect=AssertionError("should not read")
    )
    assert permissions.compute_container_subuid(1000, container_uid=0) == 1000
    read_text.assert_not_called()


def test_container_uid_1000_maps_to_subuid_base_plus_999(mocker: Any) -> None:
    """Container uid N>0 → subuid_base + N - 1. For Hermes (N=1000) and
    subuid base 100000 (Ubuntu default), that's 100999.
    """
    mocker.patch("pwd.getpwuid").return_value = MagicMock(pw_name="alice")
    mocker.patch.object(
        Path,
        "read_text",
        return_value="alice:100000:65536\nroot:100000000:1000000000\n",
    )
    assert permissions.compute_container_subuid(1000, container_uid=1000) == 100999


def test_subuid_cache_avoids_reread(mocker: Any) -> None:
    mocker.patch("pwd.getpwuid").return_value = MagicMock(pw_name="alice")
    read = mocker.patch.object(Path, "read_text", return_value="alice:100000:65536\n")
    permissions.compute_container_subuid(1000)
    permissions.compute_container_subuid(1000)
    permissions.compute_container_subuid(1000)
    assert read.call_count == 1


def test_missing_subuid_entry_raises_user_error(mocker: Any) -> None:
    mocker.patch("pwd.getpwuid").return_value = MagicMock(pw_name="alice")
    mocker.patch.object(Path, "read_text", return_value="someone-else:100000:65536\n")
    with pytest.raises(UserError, match="no /etc/subuid entry"):
        permissions.compute_container_subuid(1000)


def test_subuid_skips_comments_and_blank_lines(mocker: Any) -> None:
    mocker.patch("pwd.getpwuid").return_value = MagicMock(pw_name="alice")
    mocker.patch.object(
        Path,
        "read_text",
        return_value="# header\n\n# another\nalice:200000:65536\n",
    )
    assert permissions.compute_container_subuid(1000) == 200999


# ---------- apply_acl_argv ----------


def test_apply_acl_argv_emits_recurse_and_default_when_both_set(tmp_path: Path) -> None:
    cmds = permissions.apply_acl_argv(
        tmp_path,
        recipients=[AclRecipient(uid=1000, perms="rX")],
        recurse=True,
        defaults=True,
    )
    assert len(cmds) == 2
    # Mask MUST be in the spec — see the long comment in
    # apply_acl_argv. Without it, coder-style stuck-mask state
    # leaves the new entry masked to ---.
    assert cmds[0] == ["setfacl", "-R", "-m", "u:1000:rX,m::rwx", str(tmp_path)]
    # Critical: the default-ACL form must NOT carry the `d:` prefix —
    # `setfacl -d -m` rejects `d:u:…:rX` as "Invalid argument".
    assert cmds[1] == ["setfacl", "-d", "-m", "u:1000:rX,m::rwx", str(tmp_path)]


def test_apply_acl_argv_skips_recurse_when_false(tmp_path: Path) -> None:
    cmds = permissions.apply_acl_argv(
        tmp_path,
        recipients=[AclRecipient(uid=1000, perms="rwX")],
        recurse=False,
        defaults=False,
    )
    assert cmds == [["setfacl", "-m", "u:1000:rwX,m::rwx", str(tmp_path)]]


def test_apply_acl_argv_emits_only_default_when_recurse_off(tmp_path: Path) -> None:
    """Used by ``access_cmd.auto_grant_on_create``: dir is empty so
    recurse is pointless, but the default ACL is the whole point.
    """
    cmds = permissions.apply_acl_argv(
        tmp_path,
        recipients=[AclRecipient(uid=1000, perms="rwX")],
        recurse=False,
        defaults=True,
    )
    assert cmds == [
        ["setfacl", "-m", "u:1000:rwX,m::rwx", str(tmp_path)],
        ["setfacl", "-d", "-m", "u:1000:rwX,m::rwx", str(tmp_path)],
    ]


def test_apply_acl_argv_handles_multiple_recipients_in_one_call(tmp_path: Path) -> None:
    """Multiple recipients on the same path are batched into one
    ``-m`` flag (comma-separated specs) — fewer subprocess hops.
    The ``m::rwx`` reset is appended once at the end regardless of
    how many recipients there are.
    """
    cmds = permissions.apply_acl_argv(
        tmp_path,
        recipients=[
            AclRecipient(uid=1000, perms="rwX"),
            AclRecipient(uid=100999, perms="rwX"),
        ],
        recurse=False,
        defaults=False,
    )
    assert cmds == [["setfacl", "-m", "u:1000:rwX,u:100999:rwX,m::rwx", str(tmp_path)]]


def test_apply_acl_argv_empty_recipients_returns_empty(tmp_path: Path) -> None:
    """No-op when there's nothing to grant — callers shouldn't have to guard."""
    assert permissions.apply_acl_argv(tmp_path, recipients=[]) == []


# ---------- remove_acl_argv ----------


def test_remove_acl_argv_uses_x_and_no_perms_spec(tmp_path: Path) -> None:
    """``setfacl -x`` takes ``u:<uid>`` (no perms) and deletes only
    that uid's entry — other recipients' ACLs untouched."""
    cmds = permissions.remove_acl_argv(
        tmp_path,
        recipients=[AclRecipient(uid=1000, perms="")],
        recurse=True,
        defaults=True,
    )
    assert cmds == [
        ["setfacl", "-R", "-x", "u:1000", str(tmp_path)],
        ["setfacl", "-d", "-x", "u:1000", str(tmp_path)],
    ]


def test_remove_acl_argv_empty_recipients_returns_empty(tmp_path: Path) -> None:
    assert permissions.remove_acl_argv(tmp_path, recipients=[]) == []


# ---------- probe_acl_support ----------


def test_probe_acl_support_returns_false_when_path_missing(tmp_path: Path) -> None:
    ok, err = permissions.probe_acl_support(tmp_path / "nope")
    assert ok is False
    assert err is not None and "does not exist" in err


def test_probe_acl_support_returns_false_when_setfacl_missing(tmp_path: Path, mocker: Any) -> None:
    mocker.patch("shutil.which", return_value=None)
    ok, err = permissions.probe_acl_support(tmp_path)
    assert ok is False
    assert err is not None and "setfacl" in err


def test_probe_acl_support_returns_false_when_setfacl_fails(tmp_path: Path, mocker: Any) -> None:
    mocker.patch("shutil.which", return_value="/usr/bin/setfacl")
    mocker.patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stderr=b"setfacl: Operation not supported"),
    )
    ok, err = permissions.probe_acl_support(tmp_path)
    assert ok is False
    assert err is not None and "not supported" in err


def test_probe_acl_support_returns_true_on_real_ext4(tmp_path: Path) -> None:
    """Live probe on the test runner's filesystem. tmp_path is usually
    on the same ext4 partition that runs the toolkit's CI; if this
    test fails the runner's fs lacks ACL support which is worth knowing
    independently of the toolkit.
    """
    ok, err = permissions.probe_acl_support(tmp_path)
    assert ok, f"probe failed unexpectedly: {err}"


# ---------- run_owned / run_with_sudo ----------


def test_run_owned_executes_each_argv_and_raises_on_nonzero(mocker: Any) -> None:
    proc_ok = MagicMock(returncode=0, stderr=b"")
    proc_fail = MagicMock(returncode=1, stderr=b"permission denied")
    run = mocker.patch("subprocess.run", side_effect=[proc_ok, proc_fail])
    with pytest.raises(RuntimeFailure, match="permission denied"):
        permissions.run_owned(
            [["setfacl", "-m", "u:1000:rwX", "/p1"], ["setfacl", "-m", "x", "/p2"]]
        )
    assert run.call_count == 2


def test_run_with_sudo_skips_confirmation_with_yes_true(mocker: Any) -> None:
    mocker.patch("shutil.which", return_value="/usr/bin/sudo")
    confirm = mocker.patch("agents_cli.ui.confirm")
    run = mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))
    permissions.run_with_sudo([["setfacl", "-m", "u:1000:rwX", "/x"]], yes=True)
    confirm.assert_not_called()
    assert run.call_args.args[0] == ["sudo", "setfacl", "-m", "u:1000:rwX", "/x"]


def test_run_with_sudo_aborts_on_negative_confirmation(mocker: Any) -> None:
    mocker.patch("shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    run = mocker.patch("subprocess.run")
    with pytest.raises(UserError, match="aborted by user"):
        permissions.run_with_sudo([["setfacl", "-m", "u:1000:rwX", "/x"]], yes=False)
    run.assert_not_called()


def test_run_with_sudo_refuses_when_sudo_missing(mocker: Any) -> None:
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(UserError, match="sudo not available"):
        permissions.run_with_sudo([["setfacl", "-m", "u:1000:rwX", "/x"]], yes=True)


def test_run_with_sudo_empty_argvs_is_noop(mocker: Any) -> None:
    run = mocker.patch("subprocess.run")
    permissions.run_with_sudo([], yes=True)
    run.assert_not_called()


# ---------- has_user_acl / assess_host_access ----------


def test_has_user_acl_recognizes_access_and_default_entries(mocker: Any, tmp_path: Path) -> None:
    mocker.patch(
        "agents_cli.permissions.getfacl",
        return_value=(
            "user::rwx\nuser:1000:rwx\ngroup::r-x\nmask::rwx\nother::r-x\n"
            "default:user::rwx\ndefault:user:1000:rwx\n"
        ),
    )
    assert permissions.has_user_acl(tmp_path, 1000) is True
    assert permissions.has_user_acl(tmp_path, 9999) is False


def test_getfacl_passes_numeric_flag(mocker: Any, tmp_path: Path) -> None:
    """Regression: without --numeric, getfacl resolves uids to login
    names (``user:alice:r-x``) and ``has_user_acl``'s
    ``user:<uid>:`` substring check fails to ever match — the doctor
    keeps reporting ``missing`` even after a successful grant.
    """
    mocker.patch("shutil.which", return_value="/usr/bin/getfacl")
    run = mocker.patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"", stderr=b""),
    )
    permissions.getfacl(tmp_path)
    argv = run.call_args.args[0]
    assert "--numeric" in argv


def test_get_user_acl_perms_returns_perm_string(mocker: Any, tmp_path: Path) -> None:
    """``agents access list`` needs to tell the user whether they
    granted read-only or read-write — the binary has_user_acl signal
    isn't enough.
    """
    mocker.patch(
        "agents_cli.permissions.getfacl",
        return_value="user::rwx\nuser:1000:r-x\ngroup::---\nmask::rwx\nother::---\n",
    )
    assert permissions.get_user_acl_perms(tmp_path, 1000) == "r-x"


def test_get_user_acl_perms_returns_none_when_no_entry(mocker: Any, tmp_path: Path) -> None:
    mocker.patch(
        "agents_cli.permissions.getfacl",
        return_value="user::rwx\ngroup::---\nmask::---\nother::---\n",
    )
    assert permissions.get_user_acl_perms(tmp_path, 1000) is None


def test_get_user_acl_perms_ignores_default_namespace(mocker: Any, tmp_path: Path) -> None:
    """The default ACL governs future files; the access ACL governs
    the dir itself. ``list`` reports current readability, which means
    the access entry — not the default one."""
    mocker.patch(
        "agents_cli.permissions.getfacl",
        return_value=(
            "user::rwx\ngroup::---\nmask::---\nother::---\n"
            "default:user:1000:rwx\n"  # only in default, not access
        ),
    )
    assert permissions.get_user_acl_perms(tmp_path, 1000) is None


def test_summarize_acl_mode_labels() -> None:
    assert permissions.summarize_acl_mode(None) == "—"
    assert permissions.summarize_acl_mode("r-x") == "r"
    assert permissions.summarize_acl_mode("r--") == "r"
    assert permissions.summarize_acl_mode("---") == "r"  # masked but entry exists
    assert permissions.summarize_acl_mode("rwx") == "rw"
    assert permissions.summarize_acl_mode("rw-") == "rw"


def test_has_user_acl_handles_real_getfacl_numeric_output(mocker: Any, tmp_path: Path) -> None:
    """End-to-end style: feed an actual ``getfacl --numeric`` payload
    (uids, not names) and verify the substring matcher recognises it.
    Mirrors what ``access grant`` produces in the field — caught the
    bug where coder kept showing ``missing`` after a successful grant.
    """
    mocker.patch("shutil.which", return_value="/usr/bin/getfacl")
    mocker.patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0,
            stdout=(
                b"user::rwx\n"
                b"user:1000:r-x\n"
                b"group::---\n"
                b"mask::r-x\n"
                b"other::---\n"
                b"default:user::rwx\n"
                b"default:user:1000:r-x\n"
            ),
            stderr=b"",
        ),
    )
    assert permissions.has_user_acl(tmp_path, 1000) is True


def test_assess_host_access_returns_ok_when_owner_matches(tmp_path: Path) -> None:
    """If the host user owns the path, no ACL needed."""
    assert permissions.assess_host_access(tmp_path, tmp_path.stat().st_uid) == "ok"


def test_assess_host_access_returns_missing_for_foreign_uid_no_acl(
    tmp_path: Path, mocker: Any
) -> None:
    """Foreign owner + os.access fails + no ACL entry → ``missing``."""
    mocker.patch.object(Path, "stat", return_value=MagicMock(st_uid=100999))
    mocker.patch.object(Path, "is_dir", return_value=True)
    mocker.patch("os.access", return_value=False)
    mocker.patch("agents_cli.permissions.has_user_acl", return_value=False)
    assert permissions.assess_host_access(tmp_path, 1000) == "missing"


def test_assess_host_access_returns_ok_when_os_access_passes(tmp_path: Path, mocker: Any) -> None:
    """Foreign owner but ``os.access`` says the kernel grants the user
    read+traverse → ``ok``. This is the authoritative signal:
    ``has_user_acl`` returning True is necessary but not sufficient
    (the entry could be masked to ``---``)."""
    mocker.patch.object(Path, "stat", return_value=MagicMock(st_uid=100999))
    mocker.patch.object(Path, "is_dir", return_value=True)
    mocker.patch("os.access", return_value=True)
    assert permissions.assess_host_access(tmp_path, 1000) == "ok"


def test_assess_host_access_returns_masked_when_entry_exists_but_zeroed(
    tmp_path: Path, mocker: Any
) -> None:
    """The exact coder state from 2026-05-17: ``getfacl`` shows
    ``user:1000:r-x`` but ``mask::---`` zeroes the effective perms.
    Doctor should say ``masked`` (with the "re-run grant" hint) not
    ``ok``, because reads actually fail.
    """
    mocker.patch.object(Path, "stat", return_value=MagicMock(st_uid=100999))
    mocker.patch.object(Path, "is_dir", return_value=True)
    mocker.patch("os.access", return_value=False)
    mocker.patch("agents_cli.permissions.has_user_acl", return_value=True)
    assert permissions.assess_host_access(tmp_path, 1000) == "masked"


def test_assess_host_access_returns_path_missing_when_dir_gone(tmp_path: Path) -> None:
    assert permissions.assess_host_access(tmp_path / "nope", 1000) == "path-missing"


# ---------- convenience recipients ----------


def test_host_user_recipient_ro_vs_rw() -> None:
    ro = permissions.host_user_recipient(1000, writable=False)
    rw = permissions.host_user_recipient(1000, writable=True)
    assert ro.to_spec() == "u:1000:rX"
    assert rw.to_spec() == "u:1000:rwX"


def test_container_recipient_uses_subuid_mapping(mocker: Any) -> None:
    mocker.patch("agents_cli.permissions.compute_container_subuid", return_value=100999)
    r = permissions.container_recipient(1000, writable=True)
    assert r.to_spec() == "u:100999:rwX"


def test_run_with_sudo_prints_each_argv_via_progress_before_confirm(mocker: Any) -> None:
    """SECURITY-UX: the docstring, CLI help, and CLAUDE.md promise the
    exact argvs are printed before sudo runs. That display must be
    always-on (ui.progress), NOT --verbose-gated ui.info — a user can't
    audit a sudo grant they never saw — and must land before the
    confirmation prompt."""
    mocker.patch("shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))
    progress_spy = mocker.spy(permissions.ui, "progress")
    info_spy = mocker.spy(permissions.ui, "info")

    progress_count_at_confirm: list[int] = []

    def fake_confirm(prompt: str, *, default: bool = False) -> bool:
        progress_count_at_confirm.append(progress_spy.call_count)
        return True

    mocker.patch("agents_cli.ui.confirm", side_effect=fake_confirm)

    argvs = [
        ["setfacl", "-R", "-m", "u:1000:rX,m::rwx", "/data/coder/.data"],
        ["setfacl", "-d", "-m", "u:1000:rX,m::rwx", "/data/coder/.data"],
    ]
    permissions.run_with_sudo(argvs, yes=False)

    lines = [c.args[0] for c in progress_spy.call_args_list]
    for argv in argvs:
        assert any("sudo " + " ".join(argv) in ln for ln in lines), f"argv not printed: {argv}"
    # Header + both argv lines printed BEFORE the confirm ran.
    assert progress_count_at_confirm == [3]
    # Nothing about the command display goes through the verbose channel.
    assert not any("setfacl" in c.args[0] for c in info_spy.call_args_list)


def test_run_with_sudo_prints_argvs_with_yes_too(mocker: Any) -> None:
    """--yes skips the confirmation but NOT the display — the audit
    trail of what ran under sudo must always appear."""
    mocker.patch("shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))
    progress_spy = mocker.spy(permissions.ui, "progress")

    permissions.run_with_sudo([["setfacl", "-m", "u:1000:rwX", "/x"]], yes=True)

    lines = [c.args[0] for c in progress_spy.call_args_list]
    assert any("sudo setfacl -m u:1000:rwX /x" in ln for ln in lines)
