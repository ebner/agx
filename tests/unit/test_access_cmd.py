"""``agents access``: idmapped host views — mount / unmount / list, the
state file, lifecycle guards and the doctor check. (Plus the legacy
create-time auto-grant hook until it is removed.)

``sudo`` and ``/proc/self/mountinfo`` are faked by a tiny in-memory
mount table, so nothing here mounts anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents_cli import access_cmd, agents_cmd, ops
from agents_cli import config as cfg_mod
from agents_cli import idmap_view as iv
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import RuntimeFailure, UserError

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
        paths=PathsConfig(host_data_dir=str(data_dir), host_view_dir=str(tmp_path / "views")),
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


class FakeMounts:
    """In-memory mount table standing in for sudo + /proc/self/mountinfo."""

    def __init__(self, mocker: Any) -> None:
        self.entries: list[iv.MountEntry] = [iv.MountEntry("/", "/", ("rw",), "ext4", "/dev/x")]
        self.sudo_calls: list[list[str]] = []
        self.fail_on: str | None = None  # substring of a view path whose mount fails
        self.lie = False  # mount "succeeds" but nothing shows up
        mocker.patch("agents_cli.idmap_view.read_mountinfo", side_effect=lambda: list(self.entries))
        mocker.patch("agents_cli.permissions.run_with_sudo", side_effect=self._sudo)
        mocker.patch("agents_cli.access_cmd.subprocess.run", side_effect=self._raw_sudo)
        mocker.patch(
            "agents_cli.access_cmd._owner_ids",
            side_effect=lambda src: (1000, 1000) if src.name == "workspace" else (100999, 100999),
        )

    def _sudo(self, argvs: list[list[str]], *, yes: bool) -> None:
        for argv in argvs:
            self.sudo_calls.append(list(argv))
            if argv[0] == "mount":
                source, view = argv[-2], argv[-1]
                if self.fail_on and self.fail_on in view:
                    raise RuntimeFailure("mount: wrong fs type")
                if not self.lie:
                    opts = tuple(o for o in argv[3].split(",") if not o.startswith("X-mount"))
                    self.entries.append(
                        iv.MountEntry(view, source, (*opts, "idmapped"), "ext4", "/dev/x")
                    )
            elif argv[0] == "umount":
                self.entries = [e for e in self.entries if e.mount_point != argv[-1]]

    def _raw_sudo(self, argv: list[str], **_: Any) -> Any:
        self._sudo([argv[1:]], yes=True)

    def mounted(self) -> list[str]:
        return [e.mount_point for e in self.entries if e.mount_point != "/"]


@pytest.fixture
def mounts(mocker: Any) -> FakeMounts:
    return FakeMounts(mocker)


def _state(cfg: AgentsConfig) -> dict[str, Any]:
    path = cfg.paths.host_data_dir / access_cmd.STATE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


# ---------- mount ----------


def test_mount_hermes_is_read_only_hardened_and_idmapped_from_disk_owner(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts, tmp_path: Path
) -> None:
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    (argv,) = mounts.sudo_calls
    source = cfg_with_coder.paths.host_data_dir / "coder" / ".data"
    view = tmp_path / "views" / "coder"
    assert argv[:3] == ["mount", "--bind", "-o"] and argv[4:] == [str(source), str(view)]
    opts = argv[3].split(",")
    assert opts[:5] == ["ro", "nosuid", "nodev", "noexec", "nosymfollow"]
    assert "u:1000:100999:1 u:100999:1000:1" in argv[3]  # from the on-disk owner
    assert view.is_dir() and (tmp_path / "views").stat().st_mode & 0o777 == 0o700
    assert _state(cfg_with_coder) == {
        "views": {"coder": [{"source": str(source), "view": str(view), "rw": False}]}
    }


def test_mount_emits_an_escaped_nofail_fstab_line(
    saved_config: Path, mounts: FakeMounts, mocker: Any
) -> None:
    emit = mocker.patch("agents_cli.ui.emit")
    access_cmd.mount(agent="coder", writable=True, yes=True, config_path=saved_config)
    payload = emit.call_args.args[0]
    (line,) = payload["fstab_lines"]
    fields = line.split(" ")
    assert len(fields) == 6 and fields[2] == "none"
    assert fields[3].startswith("bind,nofail,rw,nosuid,nodev,noexec,nosymfollow,X-mount.idmap=")
    assert "\\040" in fields[3]
    assert "exclude" in emit.call_args.kwargs["human"]


def test_mount_openclaw_views_state_dir_and_skips_host_owned_workspace(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts, tmp_path: Path
) -> None:
    access_cmd.mount(agent="bob", writable=False, yes=True, config_path=saved_config)
    assert mounts.mounted() == [str(tmp_path / "views" / "bob" / "openclaw")]
    assert [v["view"] for v in _state(cfg_with_coder)["views"]["bob"]] == mounts.mounted()


def test_mount_is_idempotent_and_never_stacks(saved_config: Path, mounts: FakeMounts) -> None:
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    assert len(mounts.sudo_calls) == 1 and len(mounts.mounted()) == 1


def test_mount_refuses_to_silently_change_mode(saved_config: Path, mounts: FakeMounts) -> None:
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    with pytest.raises(UserError, match="already mounted read-only"):
        access_cmd.mount(agent="coder", writable=True, yes=True, config_path=saved_config)


def test_mount_refuses_foreign_mount_non_empty_dir_and_symlinked_view(
    saved_config: Path, mounts: FakeMounts, tmp_path: Path
) -> None:
    view = tmp_path / "views" / "coder"
    mounts.entries.append(iv.MountEntry(str(view), "/somewhere/else", ("rw",), "ext4", "/dev/x"))
    with pytest.raises(UserError, match="foreign"):
        access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    mounts.entries.pop()

    view.mkdir(parents=True)
    (view / "leftover").write_text("x")
    with pytest.raises(UserError, match="non-empty"):
        access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    (view / "leftover").unlink()
    view.rmdir()

    view.symlink_to(tmp_path)
    with pytest.raises(UserError, match="symlink"):
        access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    assert mounts.sudo_calls == []


def test_mount_failure_rolls_back_and_registers_nothing(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts, tmp_path: Path
) -> None:
    mounts.fail_on = "coder"
    with pytest.raises(RuntimeFailure, match="wrong fs type"):
        access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    assert mounts.mounted() == [] and _state(cfg_with_coder) == {}
    assert not (tmp_path / "views" / "coder").exists()


def test_mount_verifies_the_view_is_really_there(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts
) -> None:
    mounts.lie = True
    with pytest.raises(RuntimeFailure, match="no idmapped view"):
        access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    assert _state(cfg_with_coder) == {}


def test_mount_refusals(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts, tmp_path: Path
) -> None:
    with pytest.raises(UserError, match="no agent named"):
        access_cmd.mount(agent="ghost", writable=False, yes=True, config_path=saved_config)
    cfg_with_coder.paths.host_view_dir = cfg_with_coder.paths.host_data_dir / "views"
    cfg_mod.save(cfg_with_coder, saved_config)
    with pytest.raises(UserError, match="must not contain, or be inside"):
        access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    cfg_with_coder.paths.host_view_dir = tmp_path / "views"
    cfg_with_coder.paths.data_dir_in_vm = True
    cfg_mod.save(cfg_with_coder, saved_config)
    with pytest.raises(UserError, match="data-dir-in-vm"):
        access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)


def test_mount_is_supported_on_bare_metal(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts
) -> None:
    """Rootless Docker on the host: agent files are subuid-owned there too."""
    cfg_with_coder.vm = cfg_with_coder.vm.model_copy(update={"backend": "bare_metal"})
    cfg_mod.save(cfg_with_coder, saved_config)
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    assert len(mounts.mounted()) == 1


def test_default_view_root_is_not_persisted_to_config(tmp_path: Path) -> None:
    cfg = AgentsConfig(paths=PathsConfig(host_data_dir=str(tmp_path / "data")))
    path = tmp_path / "c.toml"
    cfg_mod.save(cfg, path)
    assert "host_view_dir" not in path.read_text()
    assert cfg_mod.load(path).paths.host_view_dir is None
    assert access_cmd.view_root(cfg) == Path("~/agents-view").expanduser().resolve()


# ---------- unmount ----------


def test_unmount_umounts_then_plain_rmdirs_and_deregisters(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts, tmp_path: Path
) -> None:
    access_cmd.mount(agent="bob", writable=False, yes=True, config_path=saved_config)
    access_cmd.unmount(agent="bob", yes=True, config_path=saved_config)
    assert mounts.sudo_calls[-1] == ["umount", str(tmp_path / "views" / "bob" / "openclaw")]
    assert mounts.mounted() == [] and _state(cfg_with_coder) == {}
    assert not (tmp_path / "views" / "bob").exists()  # empty parents go too
    assert (tmp_path / "views").exists()


def test_unmount_never_deletes_through_a_view_that_failed_to_unmount(
    saved_config: Path, mounts: FakeMounts, tmp_path: Path, mocker: Any
) -> None:
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    view = tmp_path / "views" / "coder"
    (view / "state.db").write_text("the real data, seen through the view")
    mocker.patch("agents_cli.permissions.run_with_sudo")  # umount "runs" but does nothing
    access_cmd.unmount(agent="coder", yes=True, config_path=saved_config)
    assert (view / "state.db").exists()


def test_unmount_works_for_an_agent_already_gone_from_compose(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts
) -> None:
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    (cfg_with_coder.paths.host_data_dir / "docker-compose.yml").write_text("services: {}\n")
    access_cmd.unmount(agent="coder", yes=True, config_path=saved_config)
    assert mounts.mounted() == [] and _state(cfg_with_coder) == {}


# ---------- list + doctor ----------


def test_list_reports_status_and_fstab_line(
    saved_config: Path, mounts: FakeMounts, mocker: Any
) -> None:
    assert access_cmd.list_views(config_path=saved_config) == []
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    (row,) = access_cmd.list_views(config_path=saved_config)
    assert (row["agent"], row["mode"], row["status"]) == ("coder", "ro", "mounted")
    assert row["fstab_line"].split(" ")[3].startswith("bind,nofail,ro,")
    mounts.entries = mounts.entries[:1]  # host rebooted without the fstab line
    (row,) = access_cmd.list_views(agent="coder", config_path=saved_config)
    assert row["status"] == "not-mounted"


def test_doctor_is_silent_without_views_and_warns_on_unmounted_or_stale(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts
) -> None:
    report = ops.DoctorReport()
    ops._check_access_views(cfg_with_coder, report)
    assert report.checks == []

    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    ops._check_access_views(cfg_with_coder, report)
    assert report.checks == []

    view = mounts.entries[-1]
    mounts.entries[-1] = iv.MountEntry(
        view.mount_point, view.root + "//deleted", view.options, view.fstype, view.source
    )
    ops._check_access_views(cfg_with_coder, report)
    mounts.entries = mounts.entries[:1]
    ops._check_access_views(cfg_with_coder, report)
    assert [(c.name, c.status) for c in report.checks] == [
        ("coder.host_view", "warn"),
        ("coder.host_view", "warn"),
    ]
    assert "stale" in report.checks[0].detail and "fstab" in report.checks[1].detail


# ---------- lifecycle guards ----------


def test_remove_and_rename_refuse_while_a_view_is_registered(
    cfg_with_coder: AgentsConfig, saved_config: Path, mounts: FakeMounts, mocker: Any
) -> None:
    access_cmd.mount(agent="coder", writable=False, yes=True, config_path=saved_config)
    stop = mocker.patch("agents_cli.compose_runner.stop")
    with pytest.raises(UserError, match="agents access unmount coder"):
        agents_cmd.remove("coder", yes=True, config_path=saved_config)
    with pytest.raises(UserError, match="agents access unmount coder"):
        agents_cmd.rename("coder", "alfie", yes=True, config_path=saved_config)
    stop.assert_not_called()
    # --keep-data leaves the directory (and so the view) intact: allowed past the guard.
    access_cmd.refuse_if_view_registered(cfg_with_coder, "bob", "remove 'bob'")


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
