"""migrate_cmd.py: preflight refusals, phase order, source-untouched
invariant, doctor-failure rollback guidance, journal resume.

Mocking style mirrors test_upgrades.py: patch at the ``mp`` /
``compose_runner`` / ``export_cmd`` / ``ops.doctor`` boundaries and
assert on the recorded argv/scripts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agx_cli import config as cfg_mod
from agx_cli import migrate_cmd, ui
from agx_cli.config import AgentsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agx_cli.errors import HealthCheckFailure, RuntimeFailure, UserError
from agx_cli.journal import Journal
from agx_cli.mp import MpResult

VM_ROOT = "/home/ubuntu/agents"
STAGING = f"{VM_ROOT}.migrating"


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail(stderr: str = "boom", rc: int = 1) -> MpResult:
    return MpResult(stdout="", stderr=stderr, returncode=rc)


def _make_cfg(tmp_path: Path, *, data_dir_in_vm: bool = False) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  hermes-coder:\n"
        "    image: nousresearch/hermes-agent:v1.0.0\n"
        "  openclaw-scout:\n"
        "    image: openclaw/openclaw:0.1.0\n",
        encoding="utf-8",
    )
    (data_dir / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    return AgentsConfig(
        paths=PathsConfig(
            host_data_dir=str(data_dir),
            vm_agents_root=VM_ROOT,
            data_dir_in_vm=data_dir_in_vm,
        ),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    return _make_cfg(tmp_path)


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


def _is_copy_script(script: str) -> bool:
    """The copy phase is the tar|tar pipeline rooted at the mount."""
    return f"tar -C {VM_ROOT} " in script


def _stub_happy_migration(
    mocker: Any,
    cfg: AgentsConfig,
    *,
    vm_state: str = "Running",
    du_kb: int = 1024,
    df_avail_kb: int = 50 * 1024 * 1024,
    initially_mounted: bool = True,
) -> list[tuple[str, Any]]:
    """Stub every boundary the migration touches; return the ordered
    call recorder. The ``mounted`` flag models the share: ``mountpoint
    -q`` reflects it and a successful umount clears it
    (``initially_mounted=False`` models a run resuming after the
    detach already happened).
    """
    calls: list[tuple[str, Any]] = []
    mounted = {"state": initially_mounted}

    def info_vm(vm: str, *, fmt: str = "json") -> MpResult:
        return _ok(json.dumps({"info": {cfg.vm.name: {"state": vm_state, "mounts": {}}}}))

    def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
        calls.append(("exec_in_vm", cmd))
        if cmd[:2] == ("mountpoint", "-q"):
            return _ok() if mounted["state"] else _fail("not a mountpoint", rc=32)
        return _ok()

    def exec_bash(
        vm: str, script: str, *, check: bool = True, timeout: float | None = None
    ) -> MpResult:
        calls.append(("exec_bash", script))
        if script.startswith("sudo du -sk"):
            return _ok(f"{du_kb}\t{VM_ROOT}\n")
        if script.startswith("df -Pk"):
            return _ok(
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"/dev/root 999999999 1234 {df_avail_kb} 10% /\n"
            )
        return _ok()

    def umount(vm: str, vm_path: str) -> MpResult:
        calls.append(("umount", vm_path))
        mounted["state"] = False
        return _ok()

    def fake_export(name: str, **kwargs: Any) -> Path:
        calls.append(("export", name))
        out = cfg.paths.host_data_dir / "backups" / f"{name}-20260101T000000Z.tar.zst"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(b"")
        return out

    def compose_stop(c: AgentsConfig, *services: str) -> MpResult:
        calls.append(("compose_stop", services))
        return _ok()

    def compose_up(c: AgentsConfig, *services: str, detach: bool = True) -> MpResult:
        calls.append(("compose_up", services))
        return _ok()

    def doctor(**kwargs: Any) -> Any:
        calls.append(("doctor", kwargs))
        from agx_cli.ops import DoctorReport

        report = DoctorReport()
        report.add("vm_reachable", "ok")
        report.add("disk_space", "warn", "14.0% free")
        return report

    mocker.patch("agx_cli.mp.info_vm", side_effect=info_vm)
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=exec_in_vm)
    mocker.patch("agx_cli.mp.exec_bash", side_effect=exec_bash)
    mocker.patch("agx_cli.mp.umount", side_effect=umount)
    mocker.patch("agx_cli.mp.stop_vm", return_value=_ok())
    mocker.patch("agx_cli.mp.start_vm", return_value=_ok())
    mocker.patch("agx_cli.export_cmd.export", side_effect=fake_export)
    mocker.patch("agx_cli.compose_runner.stop", side_effect=compose_stop)
    mocker.patch("agx_cli.compose_runner.up", side_effect=compose_up)
    mocker.patch("agx_cli.ops.doctor", side_effect=doctor)
    return calls


def _index(calls: list[tuple[str, Any]], kind: str, predicate: Any = None) -> int:
    for i, (k, payload) in enumerate(calls):
        if k == kind and (predicate is None or predicate(payload)):
            return i
    raise AssertionError(f"no call of kind {kind!r} matching predicate in {calls}")


# ---------- preflight refusals ----------


def test_refuses_bare_metal(mocker: Any, cfg: AgentsConfig, saved_config: Path) -> None:
    cfg.vm.backend = "bare_metal"
    cfg_mod.save(cfg, saved_config)
    with pytest.raises(UserError, match="bare_metal"):
        migrate_cmd.migrate_data(yes=True, config_path=saved_config)


def test_refuses_when_already_data_dir_in_vm(mocker: Any, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, data_dir_in_vm=True)
    config_path = tmp_path / "config.toml"
    cfg_mod.save(cfg, config_path)
    with pytest.raises(UserError, match="already"):
        migrate_cmd.migrate_data(yes=True, config_path=config_path)


def test_refuses_when_vm_not_running(mocker: Any, cfg: AgentsConfig, saved_config: Path) -> None:
    calls = _stub_happy_migration(mocker, cfg, vm_state="Stopped")
    with pytest.raises(UserError, match="not running"):
        migrate_cmd.migrate_data(yes=True, config_path=saved_config)
    assert not any(kind == "export" for kind, _ in calls)


def test_refuses_on_insufficient_disk_with_parsed_numbers(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    """du says 10 GiB used, df says 1 GiB available → refusal that
    surfaces all three numbers (used, needed-with-margin, available)."""
    calls = _stub_happy_migration(mocker, cfg, du_kb=10 * 1024 * 1024, df_avail_kb=1 * 1024 * 1024)
    with pytest.raises(UserError) as exc:
        migrate_cmd.migrate_data(yes=True, config_path=saved_config)
    msg = str(exc.value)
    assert "10.0 GiB" in msg  # parsed du
    assert "12.0 GiB" in msg  # +20% margin
    assert "1.0 GiB" in msg  # parsed df Available
    # Refused before anything ran: no exports, no stop, no copy.
    assert not any(kind in ("export", "compose_stop") for kind, _ in calls)
    assert not any(kind == "exec_bash" and _is_copy_script(payload) for kind, payload in calls)
    # The size estimate skips the same entries the copy will skip —
    # critically the libvirt VM disk, which is never copied and must
    # not be counted (it would refuse every libvirt migration).
    du_script = calls[_index(calls, "exec_bash", lambda s: s.startswith("sudo du -sk"))][1]
    for entry in migrate_cmd._COPY_EXCLUDES:
        assert f"--exclude={VM_ROOT}/{entry}" in du_script


# ---------- happy path ----------


def test_happy_path_phase_order_and_argv(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    calls = _stub_happy_migration(mocker, cfg)
    migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    i_export = _index(calls, "export")
    i_stop = _index(calls, "compose_stop")
    i_copy = _index(calls, "exec_bash", _is_copy_script)
    i_umount = _index(calls, "umount")
    i_swap = _index(calls, "exec_bash", lambda s: "sudo mv" in s)
    i_up = _index(calls, "compose_up")
    i_doctor = _index(calls, "doctor")
    assert i_export < i_stop < i_copy < i_umount < i_swap < i_up < i_doctor

    # Exact copy mechanism: VM-side root tar|tar pipeline from the
    # mounted root into the ext4 staging sibling, exclusions inline.
    copy_script = calls[i_copy][1]
    assert f"sudo tar -C {VM_ROOT} " in copy_script
    assert f"| sudo tar -C {STAGING} -xpf -" in copy_script
    # Swap moves the staging dir into the canonical root.
    swap_script = calls[i_swap][1]
    assert f"sudo mv {STAGING} {VM_ROOT}" in swap_script
    # The swap refuses while the share is still mounted, and moves a
    # non-empty leftover root aside instead of ever deleting it.
    assert f"mountpoint -q {VM_ROOT}" in swap_script
    assert f"{VM_ROOT}.replaced." in swap_script

    # Both exports ran (one per agent), stop/up are project-wide (no
    # service args → all services incl. sidecars), doctor is full-surface
    # and render-suppressed (one-object contract under --json).
    exported = sorted(p for k, p in calls if k == "export")
    assert exported == ["coder", "scout"]
    assert calls[i_stop][1] == ()
    assert calls[i_up][1] == ()
    assert calls[i_doctor][1]["target"] is None
    assert calls[i_doctor][1]["emit"] is False

    # Config flipped exactly like vm.init's --data-dir-in-vm branch:
    # flag true, vm_agents_root unchanged.
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.paths.data_dir_in_vm is True
    assert str(reloaded.paths.vm_agents_root) == VM_ROOT

    # Success state = no journal.
    assert (
        Journal.find_resumable(cfg.paths.host_data_dir, migrate_cmd.MIGRATE_OP, cfg.vm.name) is None
    )


def test_copy_excludes_are_applied_before_traversal(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    """The exclusion list rides on the producer tar (--exclude), never a
    post-copy prune: on libvirt the source contains the running VM's own
    qcow2 (.disks/), which must never be traversed — a copy-then-delete
    would copy the VM disk into itself before the delete could help."""
    calls = _stub_happy_migration(mocker, cfg)
    migrate_cmd.migrate_data(yes=True, config_path=saved_config)
    copy_script = calls[_index(calls, "exec_bash", _is_copy_script)][1]
    # Every host-only entry AND every libvirt backend-state dir is an
    # inline tar exclude...
    for entry in migrate_cmd._COPY_EXCLUDES:
        assert f"--exclude=./{entry}" in copy_script
    assert "--exclude=./.disks" in copy_script  # the qcow2 hazard, explicitly
    # ...positioned on the producer side of the pipeline (pre-traversal).
    producer = copy_script.split("|")[0]
    assert "--exclude=./.disks" in producer
    # No post-copy prune remains — nothing to rm at all.
    assert "rm " not in copy_script and "rm -rf" not in copy_script


def test_emits_exactly_one_object_with_doctor_summary(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    _stub_happy_migration(mocker, cfg)
    emit = mocker.spy(ui, "emit")
    migrate_cmd.migrate_data(yes=True, config_path=saved_config)
    assert emit.call_count == 1
    payload = emit.call_args.args[0]
    assert payload["migrated"] is True
    assert payload["vm_data_root"] == VM_ROOT
    assert len(payload["exports"]) == 2
    # The safe-to-archive list names the bulk host paths, and only ones
    # that exist (agent dirs weren't created in this fixture; compose was).
    assert (
        str(cfg.paths.host_data_dir / "docker-compose.yml")
        in (payload["host_paths_safe_to_archive"])
    )
    # Doctor's report is folded into the single payload (its own table
    # render is suppressed via emit=False).
    assert payload["doctor"]["ran"] is True
    assert payload["doctor"]["checks"] == 2
    assert payload["doctor"]["non_ok"] == [
        {"check": "disk_space", "status": "warn", "detail": "14.0% free"}
    ]


def test_superseded_list_keeps_host_authoritative_metadata(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    """The 'safe to archive/delete' list must never name paths that stay
    live host-side under data_dir_in_vm: docker-compose.override.yml
    (compose.override_file_args gates the extra -f on the HOST copy) and
    each agent's .mounts.json (mounts_cmd.manifest_path reads it
    host-side). Agent dirs are therefore listed via their bulk-state
    subdirs, never wholesale."""
    host = cfg.paths.host_data_dir
    (host / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    (host / "coder" / ".data").mkdir(parents=True)
    (host / "coder" / ".mounts.json").write_text("{}", encoding="utf-8")
    (host / "scout" / ".openclaw").mkdir(parents=True)
    (host / "scout" / "workspace").mkdir(parents=True)

    _stub_happy_migration(mocker, cfg)
    emit = mocker.spy(ui, "emit")
    migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    listed = emit.call_args.args[0]["host_paths_safe_to_archive"]
    assert str(host / "coder" / ".data") in listed
    assert str(host / "scout" / ".openclaw") in listed
    assert str(host / "scout" / "workspace") in listed
    assert str(host / "docker-compose.yml") in listed
    # Host-authoritative metadata stays off the list.
    assert str(host / "docker-compose.override.yml") not in listed
    assert str(host / "coder") not in listed  # never the agent dir wholesale
    assert not any(".mounts.json" in p for p in listed)
    # The human text says WHY the override and manifests must stay.
    human = emit.call_args.kwargs["human"]
    assert "docker-compose.override.yml" in human
    assert ".mounts.json" in human


# ---------- source-untouched invariant ----------


def test_no_destructive_argv_ever_targets_the_source(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    """Across every recorded VM call: no rm/unlink against the mounted
    data root or the host data dir. The single allowed op on the root
    path is a bare ``rmdir`` (refuses non-empty dirs by definition) in
    the swap script, and only AFTER the share was detached.
    """
    calls = _stub_happy_migration(mocker, cfg)
    migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    host_root = str(cfg.paths.host_data_dir)

    def _is_protected(token: str) -> bool:
        token = token.rstrip("/")
        return (
            token in (VM_ROOT, host_root)
            or token.startswith(VM_ROOT + "/")
            or token.startswith(host_root + "/")
        )

    i_umount = _index(calls, "umount")
    for i, (kind, payload) in enumerate(calls):
        if kind == "exec_in_vm":
            argv = list(payload)
            if argv and argv[0] in ("rm", "rmdir", "unlink"):
                assert not any(_is_protected(a) for a in argv[1:]), argv
        elif kind == "exec_bash":
            for line in payload.splitlines():
                tokens = line.split()
                if not any(t in ("rm", "unlink") for t in tokens):
                    continue
                assert not any(_is_protected(t) for t in tokens), line
            # rmdir of the (empty, already-detached) mountpoint stub is
            # the one sanctioned touch of the root path.
            if "rmdir" in payload:
                assert i > i_umount


# ---------- consent ----------


def test_consent_declined_before_downtime_aborts_and_clears_journal(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    calls = _stub_happy_migration(mocker, cfg)
    confirm = mocker.patch("agx_cli.ui.confirm", return_value=False)
    with pytest.raises(UserError, match="aborted by user"):
        migrate_cmd.migrate_data(yes=False, config_path=saved_config)
    confirm.assert_called_once()
    # Exports ran (they precede consent — they're the safety net), but
    # nothing was stopped or copied.
    assert any(kind == "export" for kind, _ in calls)
    assert not any(kind == "compose_stop" for kind, _ in calls)
    # Nothing mutated → no journal left to trip doctor's dangling check.
    assert (
        Journal.find_resumable(cfg.paths.host_data_dir, migrate_cmd.MIGRATE_OP, cfg.vm.name) is None
    )


def test_yes_bypasses_consent(mocker: Any, cfg: AgentsConfig, saved_config: Path) -> None:
    _stub_happy_migration(mocker, cfg)
    confirm = mocker.patch("agx_cli.ui.confirm")
    migrate_cmd.migrate_data(yes=True, config_path=saved_config)
    confirm.assert_not_called()


# ---------- doctor failure ----------


def test_doctor_failure_leaves_config_flipped_prints_rollback_keeps_journal(
    mocker: Any, cfg: AgentsConfig, saved_config: Path, capsys: Any
) -> None:
    """Chosen behavior: on a red doctor the config STAYS flipped (the
    data is already living in the VM), the exact flip-back steps are
    printed, the journal is kept for resume/diagnosis, and the
    HealthCheckFailure propagates (exit 3 via the CLI).
    """
    _stub_happy_migration(mocker, cfg)
    mocker.patch("agx_cli.ops.doctor", side_effect=HealthCheckFailure("2 checks failed"))

    with pytest.raises(HealthCheckFailure):
        migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    # Config left flipped...
    assert cfg_mod.load(saved_config).paths.data_dir_in_vm is True
    # ...journal kept...
    journal = Journal.find_resumable(cfg.paths.host_data_dir, migrate_cmd.MIGRATE_OP, cfg.vm.name)
    assert journal is not None
    assert journal.is_phase_done("config_flipped")
    assert not journal.is_phase_done("doctor_passed")
    # ...and the printed instructions match that state exactly. The
    # container lifecycle must sandwich the remount: stop BEFORE
    # `agx vm start` (libvirt live-attaches the share under running
    # containers), force-recreate AFTER (a plain start can keep the
    # pre-rollback bind-mount view).
    err = capsys.readouterr().err
    assert "data_dir_in_vm = false" in err
    assert str(saved_config) in err
    assert "never modified" in err
    for agent in ("coder", "scout"):
        i_stop = err.index(f"agx stop {agent}")
        i_remount = err.index("agx vm start")
        i_recreate = err.index(f"agx upgrade {agent} --recreate")
        assert i_stop < i_remount < i_recreate
    # A plain `agx start` must NOT be advertised as the rollback.
    assert "agx start coder" not in err


def test_pre_flip_failure_prints_resume_guidance_and_keeps_journal(
    mocker: Any, cfg: AgentsConfig, saved_config: Path, capsys: Any
) -> None:
    calls = _stub_happy_migration(mocker, cfg)

    def failing_exec_bash(
        vm: str, script: str, *, check: bool = True, timeout: float | None = None
    ) -> MpResult:
        calls.append(("exec_bash", script))
        if script.startswith("sudo du -sk"):
            return _ok(f"1024\t{VM_ROOT}\n")
        if script.startswith("df -Pk"):
            return _ok(
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 9 1 999999 1% /\n"
            )
        if _is_copy_script(script):
            raise RuntimeFailure("disk exploded mid-copy")
        return _ok()

    mocker.patch("agx_cli.mp.exec_bash", side_effect=failing_exec_bash)

    with pytest.raises(RuntimeFailure, match="disk exploded"):
        migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    # Config untouched, journal resumable at the failed phase.
    assert cfg_mod.load(saved_config).paths.data_dir_in_vm is False
    journal = Journal.find_resumable(cfg.paths.host_data_dir, migrate_cmd.MIGRATE_OP, cfg.vm.name)
    assert journal is not None
    assert journal.is_phase_done("services_stopped")
    assert not journal.is_phase_done("data_copied")
    err = capsys.readouterr().err
    assert "before the config flip" in err
    assert "re-run `agx vm migrate-data`" in err
    # Abandoning also sandwiches the remount with stop + force-recreate.
    assert err.index("agx stop coder") < err.index("agx vm start")
    assert err.index("agx vm start") < err.index("agx upgrade coder --recreate")


# ---------- resume ----------


def test_resume_skips_completed_phases(mocker: Any, cfg: AgentsConfig, saved_config: Path) -> None:
    calls = _stub_happy_migration(mocker, cfg)
    journal = Journal.begin(
        cfg.paths.host_data_dir,
        migrate_cmd.MIGRATE_OP,
        cfg.vm.name,
        migrate_cmd.MIGRATE_PHASES,
        meta={"exports": ["/backups/coder-old.tar.zst"]},
    )
    journal.advance("exports_taken")
    journal.advance("services_stopped")

    migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    # Completed phases were NOT re-run...
    assert not any(kind == "export" for kind, _ in calls)
    assert not any(kind == "compose_stop" for kind, _ in calls)
    # ...pending ones were.
    _index(calls, "exec_bash", _is_copy_script)
    _index(calls, "umount")
    _index(calls, "compose_up")
    _index(calls, "doctor")
    assert (
        Journal.find_resumable(cfg.paths.host_data_dir, migrate_cmd.MIGRATE_OP, cfg.vm.name) is None
    )


def test_resume_after_config_flip_never_recopies(
    mocker: Any, cfg: AgentsConfig, tmp_path: Path
) -> None:
    """Crash after the flip: config already says data_dir_in_vm=true.
    The in-flight journal both bypasses the "already migrated" refusal
    and guarantees the copy/detach/swap phases are skipped — nothing can
    copy backwards over the VM data.
    """
    flipped = _make_cfg(tmp_path, data_dir_in_vm=True)
    config_path = tmp_path / "config.toml"
    cfg_mod.save(flipped, config_path)

    # initially_mounted=False: post-flip reality — the share is already
    # detached, so journal reconciliation sees a CONSISTENT journal
    # (mount_detached done, mountpoint probe negative) and resumes.
    calls = _stub_happy_migration(mocker, flipped, initially_mounted=False)
    # data_dir_in_vm reads compose via transfer-from-VM; serve the host copy.
    compose_file = flipped.paths.host_data_dir / "docker-compose.yml"

    def transfer_from(vm: str, vm_path: str, host_path: Path) -> MpResult:
        host_path.write_text(compose_file.read_text(encoding="utf-8"), encoding="utf-8")
        return _ok()

    mocker.patch("agx_cli.mp.transfer_from_vm", side_effect=transfer_from)

    journal = Journal.begin(
        flipped.paths.host_data_dir,
        migrate_cmd.MIGRATE_OP,
        flipped.vm.name,
        migrate_cmd.MIGRATE_PHASES,
        meta={"exports": []},
    )
    for phase in (
        "exports_taken",
        "services_stopped",
        "data_copied",
        "mount_detached",
        "data_swapped",
        "config_flipped",
    ):
        journal.advance(phase)

    migrate_cmd.migrate_data(yes=True, config_path=config_path)

    assert not any(kind == "export" for kind, _ in calls)
    assert not any(kind == "umount" for kind, _ in calls)
    assert not any(kind == "exec_bash" and _is_copy_script(p) for kind, p in calls)
    assert not any(kind == "exec_bash" and "sudo mv" in p for kind, p in calls)
    _index(calls, "compose_up")
    _index(calls, "doctor")
    assert (
        Journal.find_resumable(flipped.paths.host_data_dir, migrate_cmd.MIGRATE_OP, flipped.vm.name)
        is None
    )


def test_stale_journal_after_rollback_is_invalidated_and_run_starts_fresh(
    mocker: Any, cfg: AgentsConfig, saved_config: Path, capsys: Any
) -> None:
    """A user who followed the printed rollback (flip config back, remount,
    recreate) leaves a journal claiming config_flipped while the config
    says false. Resuming from it would skip every mutation phase and
    report success against the host-mounted deployment. The journal must
    be detected as stale, discarded, and the migration re-run in full.
    """
    calls = _stub_happy_migration(mocker, cfg)
    stale = Journal.begin(
        cfg.paths.host_data_dir,
        migrate_cmd.MIGRATE_OP,
        cfg.vm.name,
        migrate_cmd.MIGRATE_PHASES,
        meta={"exports": []},
    )
    for phase in (
        "exports_taken",
        "services_stopped",
        "data_copied",
        "mount_detached",
        "data_swapped",
        "config_flipped",  # journal says flipped; saved_config says false
    ):
        stale.advance(phase)

    migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    assert "stale" in capsys.readouterr().err
    # A FULL fresh run happened — nothing was "resumed" from the lie.
    _index(calls, "export")
    _index(calls, "compose_stop")
    _index(calls, "exec_bash", _is_copy_script)
    _index(calls, "umount")
    # And the migration actually landed this time.
    assert cfg_mod.load(saved_config).paths.data_dir_in_vm is True
    assert (
        Journal.find_resumable(cfg.paths.host_data_dir, migrate_cmd.MIGRATE_OP, cfg.vm.name) is None
    )


def test_stale_journal_mount_divergence_is_invalidated(
    mocker: Any, cfg: AgentsConfig, saved_config: Path, capsys: Any
) -> None:
    """Partial rollback: the user abandoned pre-flip and `agx vm start`
    re-added the mount, but the journal still claims mount_detached. The
    mountpoint probe exposes the divergence → fresh start (re-copy from
    the remounted share, re-detach)."""
    calls = _stub_happy_migration(mocker, cfg)  # share mounted again
    stale = Journal.begin(
        cfg.paths.host_data_dir,
        migrate_cmd.MIGRATE_OP,
        cfg.vm.name,
        migrate_cmd.MIGRATE_PHASES,
        meta={"exports": []},
    )
    for phase in ("exports_taken", "services_stopped", "data_copied", "mount_detached"):
        stale.advance(phase)

    migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    assert "stale" in capsys.readouterr().err
    _index(calls, "export")  # exports re-ran → fresh journal, not a resume
    _index(calls, "exec_bash", _is_copy_script)
    _index(calls, "umount")  # the detach ran again against the remounted share
    assert cfg_mod.load(saved_config).paths.data_dir_in_vm is True


# ---------- mount detach fallback ----------


def test_umount_refusal_on_multipass_falls_back_to_stop_start_dance(
    mocker: Any, cfg: AgentsConfig, saved_config: Path
) -> None:
    """Live umount refused (native 9p) → mirror _ensure_mount's
    stop → umount → start dance, then verify the mount is gone."""
    calls = _stub_happy_migration(mocker, cfg)
    mounted = {"state": True}
    attempts = {"n": 0}

    def exec_in_vm(vm: str, *cmd: str, check: bool = True) -> MpResult:
        calls.append(("exec_in_vm", cmd))
        if cmd[:2] == ("mountpoint", "-q"):
            return _ok() if mounted["state"] else _fail("not a mountpoint", rc=32)
        return _ok()

    def umount(vm: str, vm_path: str) -> MpResult:
        calls.append(("umount", vm_path))
        attempts["n"] += 1
        if attempts["n"] == 1:
            # multipass backend raises on rc != 0 (mp() check=True).
            raise RuntimeFailure("cannot umount a native mount on a running VM")
        mounted["state"] = False
        return _ok()

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=exec_in_vm)
    mocker.patch("agx_cli.mp.umount", side_effect=umount)
    stop_vm = mocker.patch("agx_cli.mp.stop_vm", return_value=_ok())
    start_vm = mocker.patch("agx_cli.mp.start_vm", return_value=_ok())

    migrate_cmd.migrate_data(yes=True, config_path=saved_config)

    assert attempts["n"] == 2
    stop_vm.assert_called_once()
    start_vm.assert_called_once()
