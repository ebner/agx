"""Image upgrades with two-snapshot rollback.

A multipass snapshot captures the VM disk (rootless Docker image
cache, OS state). It does NOT capture the host-mounted data-dir, which
holds the compose file, the agent's config.yaml, and its persistent
state. So every upgrade takes BOTH:

- A host-data tarball at ``<data-dir>/.upgrades/<name>-<utc>.tar.gz``
  (compose + .env + the agent's data dir).
- A multipass snapshot named ``pre-upgrade-<name>-<utc>``.

Phases (resumable via journal): ``host_data_snapshot``,
``vm_snapshot``, ``tag_pinned``, ``pulled``, ``recreated``,
``doctor_passed``. On any phase failure after the snapshots, the
state machine reverts the compose tag, recreates the container with
the old image, and re-raises with the snapshot ids so a manual
``agents rollback`` can finish the job if needed.
"""

from __future__ import annotations

import datetime
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from agents_cli import archive as archive_mod
from agents_cli import compose, compose_runner, mp, ops, ui
from agents_cli import config as cfg_mod
from agents_cli import vm as vm_mod
from agents_cli.agents_cmd import _detect_type_from_compose
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.compose import HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.journal import Journal

CONFIG_MIGRATE_PHASE = "config_migrated"


UPGRADE_PHASES = [
    "host_data_snapshot",
    "vm_snapshot",
    "tag_pinned",
    "pulled",
    "recreated",
    CONFIG_MIGRATE_PHASE,
    "doctor_passed",
]


@dataclass(frozen=True)
class UpgradeOutcome:
    name: str
    old_tag: str
    new_tag: str
    host_snapshot: Path | None
    vm_snapshot: str | None


# ---------- pull ----------


def pull(
    name: str | None = None,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    cfg = cfg_mod.load(config_path)
    if name is None:
        compose_runner.pull(cfg)
        ui.emit({"pulled": "all"}, human="pulled all images")
        return
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    svc = _service_name_for(name, agent_type)
    compose_runner.pull(cfg, svc)
    ui.emit({"pulled": svc}, human=f"pulled {svc}")


# ---------- upgrade ----------


def upgrade(
    name: str,
    *,
    tag: str | None = None,
    to_latest: bool = False,
    no_snapshot: bool = False,
    recreate: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> UpgradeOutcome:
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    svc = _service_name_for(name, agent_type)

    doc = compose.read_compose_doc(cfg)
    current_image = doc["services"][svc].get("image", "")
    old_tag = current_image.split(":", 1)[1] if ":" in current_image else ""
    if not old_tag:
        raise UserError(f"current image for {name!r} has no tag: {current_image!r}")

    # Bare ``agents upgrade <name>`` (no ``--tag``, no ``--latest``,
    # no ``--recreate``) implicitly fetches the latest upstream tag —
    # matches the convention from ``apt upgrade`` / ``brew upgrade`` /
    # ``npm update``: if the user types ``upgrade`` without a version
    # selector, they want to be brought to the newest available. The
    # explicit ``--latest`` flag becomes documentation rather than
    # behavior-changing. ``--recreate`` is the opt-out — it signals
    # "refresh in place at the current tag", so we skip the fetch.
    implicit_latest = tag is None and not to_latest and not recreate
    if to_latest or implicit_latest:
        # Resolve latest upstream tag via the same /releases/latest path
        # `outdated` uses. Refuses if the network fetch fails — bumping
        # to "whatever we had before" silently would defeat the intent.
        from agents_cli import outdated_cmd

        latest = outdated_cmd.fetch_latest_tag(agent_type, include_prerelease=False)
        if not latest:
            raise UserError(
                f"could not resolve latest upstream tag for {agent_type!r} "
                f"— GitHub API unreachable? Pass --tag <T> to pin explicitly, "
                f"or --recreate to force-refresh at the current pin."
            )
        new_tag = latest
        # Refresh the toolkit-wide default so future `agents create` of
        # this type starts on the latest baseline rather than the
        # `vm init`-time snapshot.
        if agent_type == "hermes" and cfg.defaults.hermes_image_tag != latest:
            cfg.defaults.hermes_image_tag = latest
            cfg_mod.save(cfg, config_path)
        elif agent_type == "openclaw" and cfg.defaults.openclaw_image_tag != latest:
            cfg.defaults.openclaw_image_tag = latest
            cfg_mod.save(cfg, config_path)
    else:
        new_tag = tag or old_tag  # explicit tag wins; otherwise refresh in place
    base = HERMES_IMAGE_BASE if agent_type == "hermes" else OPENCLAW_IMAGE_BASE

    # No-op short-circuit when the target tag matches the current pin.
    # Covers both:
    # - bare ``agents upgrade <name>`` after the implicit-latest fetch
    #   resolved to the same tag the agent is already on
    # - ``--tag X`` where X happens to equal the current pin
    # Without this, the upgrade would do ~30-60s of avoidable agent
    # downtime (``vm.take_snapshot`` on libvirt+virtiofs needs the
    # VM stopped), print a misleading ``vX.Y.Z -> vX.Y.Z`` summary,
    # and look like a no-op anyway. ``--recreate`` is the explicit
    # opt-in for the legitimate force-refresh use cases (env-file
    # changes, wedged container, re-running doctor with the snapshot
    # safety net intact).
    if new_tag == old_tag and not recreate:
        ui.emit(
            {
                "name": name,
                "old_tag": old_tag,
                "new_tag": new_tag,
                "noop": True,
            },
            human=(
                f"{name!r}: already at {new_tag} — nothing to do "
                f"(pass --recreate to force-refresh anyway)"
            ),
        )
        return UpgradeOutcome(
            name=name,
            old_tag=old_tag,
            new_tag=new_tag,
            host_snapshot=None,
            vm_snapshot=None,
        )

    with compose.acquire_lock(cfg.paths.host_data_dir):
        journal = Journal.find_resumable(cfg.paths.host_data_dir, "upgrade", name) or Journal.begin(
            cfg.paths.host_data_dir, "upgrade", name, UPGRADE_PHASES
        )

        host_snap: Path | None = None
        vm_snap: str | None = None
        try:
            if not no_snapshot and not journal.is_phase_done("host_data_snapshot"):
                host_snap = _host_data_snapshot(cfg, name)
                journal.advance("host_data_snapshot")
            if not no_snapshot and not journal.is_phase_done("vm_snapshot"):
                vm_snap = _vm_snapshot(cfg, name)
                journal.advance("vm_snapshot")

            if not journal.is_phase_done("tag_pinned"):
                _set_image_tag(cfg, svc, base, new_tag)
                journal.advance("tag_pinned")

            if not journal.is_phase_done("pulled"):
                compose_runner.pull(cfg, svc)
                inspect = compose_runner.docker_image_inspect(cfg, f"{base}:{new_tag}")
                if inspect.returncode != 0:
                    raise RuntimeFailure(
                        f"docker image inspect failed for {base}:{new_tag} "
                        f"(rc={inspect.returncode}): {inspect.stderr.strip()}"
                    )
                journal.advance("pulled")

            if not journal.is_phase_done("recreated"):
                compose_runner.up(cfg, svc)
                journal.advance("recreated")

            if not journal.is_phase_done(CONFIG_MIGRATE_PHASE):
                _run_config_migrate(cfg, svc, agent_type)
                journal.advance(CONFIG_MIGRATE_PHASE)

            if not journal.is_phase_done("doctor_passed"):
                ops.doctor(target=name, config_path=config_path)
                journal.advance("doctor_passed")

            journal.complete()
        except Exception as failure:
            ui.warn(f"upgrade failed: {failure}; reverting compose tag")
            _set_image_tag(cfg, svc, base, old_tag)
            try:
                compose_runner.up(cfg, svc)
            except Exception as recreate_err:
                ui.warn(f"could not recreate with old tag: {recreate_err}")
            # The rollback just undid the new-tag side of every
            # phase the state machine had advanced past (tag, pull,
            # recreate). Leaving the journal with those phases marked
            # "done" would cause the next retry to skip them — doctor
            # would then run against the old tag and report success,
            # making the upgrade look like it landed when it didn't.
            # Erase the journal so retry starts from a clean slate.
            try:
                journal.path.unlink(missing_ok=True)
            except OSError as journal_err:
                ui.warn(f"could not clear journal {journal.path}: {journal_err}")
            ui.warn(
                f"snapshots preserved: host={host_snap}, vm={vm_snap} — "
                f"run `agents rollback {name}` if the live state is broken"
            )
            raise

    ui.emit(
        {
            "name": name,
            "old_tag": old_tag,
            "new_tag": new_tag,
            "host_snapshot": str(host_snap) if host_snap else None,
            "vm_snapshot": vm_snap,
        },
        human=(
            f"upgraded {name!r}: {old_tag} -> {new_tag}\n"
            f"  host snapshot: {host_snap or '(skipped)'}\n"
            f"  vm snapshot: {vm_snap or '(skipped)'}"
        ),
    )
    return UpgradeOutcome(
        name=name,
        old_tag=old_tag,
        new_tag=new_tag,
        host_snapshot=host_snap,
        vm_snapshot=vm_snap,
    )


def upgrade_all(
    *,
    canary: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> list[UpgradeOutcome]:
    cfg = cfg_mod.load(config_path)
    doc = compose.read_compose_doc(cfg)
    services = list((doc.get("services") or {}).keys())

    # Names sorted, optional canary first.
    names: list[str] = []
    for svc_name in services:
        if svc_name.startswith("hermes-"):
            names.append(svc_name[len("hermes-") :])
        elif svc_name.startswith("openclaw-"):
            names.append(svc_name[len("openclaw-") :])
    names.sort()
    if canary and canary in names:
        names.remove(canary)
        names.insert(0, canary)

    outcomes: list[UpgradeOutcome] = []
    for n in names:
        outcomes.append(upgrade(n, config_path=config_path))
    return outcomes


# ---------- rollback ----------


def rollback(
    name: str,
    *,
    snapshot_id: str | None = None,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Restore an agent from a pre-upgrade snapshot.

    Without ``snapshot_id``, the latest archive under
    ``<data-dir>/.upgrades/<name>-*.tar.gz`` is used and the matching
    multipass VM snapshot is restored. With an explicit ``snapshot_id``:

    - If it ends in ``.tar.gz``: treated as a host-side archive filename
      under ``.upgrades/``.
    - If it starts with ``pre-upgrade-``: treated as a multipass snapshot
      name; ONLY the VM is restored (host data unchanged).
    - Otherwise: matched by suffix against archive names; if exactly one
      matches, that's used.

    Confirms unless ``--yes``.
    """
    cfg = cfg_mod.load(config_path)

    archive: Path | None = None
    vm_only = False

    if snapshot_id is None:
        archive = _latest_host_snapshot(cfg, name)
        if archive is None:
            raise UserError(f"no host snapshot found for {name!r} under .upgrades/")
    elif snapshot_id.startswith("pre-upgrade-"):
        # Multipass-snapshot form — restore the VM only.
        vm_only = True
    else:
        upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
        candidates = (
            sorted(
                list(upgrades_dir.glob(f"{name}-*{archive_mod.ARCHIVE_SUFFIX}"))
                + list(upgrades_dir.glob(f"{name}-*.tar.gz"))
            )
            if upgrades_dir.exists()
            else []
        )
        # Exact filename or suffix match
        exact = next((p for p in candidates if p.name == snapshot_id), None)
        if exact:
            archive = exact
        else:
            partial = [p for p in candidates if snapshot_id in p.name]
            if len(partial) == 1:
                archive = partial[0]
            elif len(partial) > 1:
                raise UserError(
                    f"snapshot id {snapshot_id!r} is ambiguous; matches "
                    + ", ".join(p.name for p in partial)
                )
            else:
                raise UserError(f"no host snapshot matches {snapshot_id!r} for {name!r}")

    prompt_target = archive.name if archive else snapshot_id
    if not yes and not ui.confirm(
        f"restore {name!r} from {prompt_target!r}? "
        "compose, .env, and the agent's data dir will be overwritten.",
        default=False,
    ):
        raise UserError("aborted by user")

    if archive is not None:
        _restore_host_snapshot(cfg, archive)

    vm_snap_name = snapshot_id if vm_only else _latest_vm_snapshot(cfg.vm.name, name)
    if vm_snap_name:
        mp.stop_vm(cfg.vm.name)
        mp.restore(f"{cfg.vm.name}.{vm_snap_name}", destructive=True)
        mp.start_vm(cfg.vm.name)

    ui.emit(
        {
            "name": name,
            "restored": {
                "host": str(archive) if archive else None,
                "vm_snapshot": vm_snap_name,
            },
        },
        human=(
            f"rolled back {name!r}\n"
            f"  host: {archive or '(skipped — vm-only)'}\n"
            f"  vm snapshot: {vm_snap_name or '(none found)'}"
        ),
    )


# ---------- helpers ----------


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _host_data_snapshot(cfg: AgentsConfig, name: str) -> Path:
    """Tar+zstd the agent's data dir + compose + .env.

    The naïve "stream through zstandard on the host" path doesn't
    work in general: rootless-Docker writes the agent's files at a
    subuid (e.g. host uid 100999 for container uid 1000), mode 0700.
    The host user owns the *directory layout* but cannot read the
    contents — every ``walk`` into the agent's ``.data/`` hits
    EACCES. The toolkit's ``agents access grant`` lifts the mask via
    POSIX ACLs, but the grant gets clobbered every time the Hermes
    entrypoint chmods ``/opt/data`` on container restart (the chmod
    resets the mask back to ``---``), so even a freshly-granted
    agent goes back to unreadable after the next ``compose up``.

    Tarring inside the VM with sudo sidesteps the whole problem:
    virtiofs passes file ownership through to the guest, and root
    in the guest can read everything regardless of subuid. Same
    pattern as the existing ``--data-dir-in-vm`` branch — just
    works on the host-mount layout too.

    Bare-metal: no VM, runs sudo on the host directly. Best to
    `agents access grant` first so sudo isn't needed at all; we
    still attempt sudo as a fallback so a one-shot upgrade isn't
    blocked by a fresh-but-clobbered ACL.
    """
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(parents=True, exist_ok=True)
    archive_path = upgrades_dir / f"{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"

    if cfg.paths.data_dir_in_vm:
        vm_archive = f"/tmp/agents-upgrade-snap-{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"
        mp.exec_bash(
            cfg.vm.name,
            (
                f"set -eu\n"
                f"cd {cfg.paths.vm_agents_root}\n"
                f"tar --zstd --warning=no-file-changed --ignore-failed-read "
                f"-cf {vm_archive} "
                f"$([ -d {name} ] && echo {name} || true) "
                f"$([ -f docker-compose.yml ] && echo docker-compose.yml || true) "
                f"$([ -f .env ] && echo .env || true)\n"
            ),
        )
        mp.transfer_from_vm(cfg.vm.name, vm_archive, archive_path)
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_archive, check=False)
        return archive_path

    # Host-mount mode: tar inside the VM as root so subuid-owned
    # files are readable. The archive's owner inside the VM is root
    # after the tar runs; chown to ``ubuntu`` so the SFTP/SCP
    # transfer back to the host (which runs as ``ubuntu``) doesn't
    # hit EACCES. After transfer the archive lands at
    # ``<host_data_dir>/.upgrades/`` owned by the toolkit user
    # (host-side default — ``shutil.copy`` semantics), which is what
    # the rollback path expects.
    vm_archive = f"/tmp/agents-upgrade-snap-{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"
    # ``--warning=no-file-changed`` + ``--ignore-failed-read`` so a
    # live agent rewriting state.db / kanban.db / WAL files mid-walk
    # doesn't trip ``set -eu`` (GNU tar exits rc=1 on "file changed
    # as we read it" otherwise). See export_cmd.py for the longer
    # rationale.
    mp.exec_bash(
        cfg.vm.name,
        (
            f"set -eu\n"
            f"cd {cfg.paths.vm_agents_root}\n"
            f"sudo tar --zstd --warning=no-file-changed --ignore-failed-read "
            f"-cf {vm_archive} "
            f"$([ -d {name} ] && echo {name} || true) "
            f"$([ -f docker-compose.yml ] && echo docker-compose.yml || true) "
            f"$([ -f .env ] && echo .env || true)\n"
            f"sudo chown ubuntu:ubuntu {vm_archive}\n"
        ),
    )
    mp.transfer_from_vm(cfg.vm.name, vm_archive, archive_path)
    mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_archive, check=False)
    return archive_path


def _restore_host_snapshot(cfg: AgentsConfig, archive_path: Path) -> None:
    if cfg.paths.data_dir_in_vm:
        vm_archive = f"/tmp/agents-restore-{archive_path.name}"
        mp.transfer_to_vm(archive_path, cfg.vm.name, vm_archive)
        # `tar -xf` auto-detects compression by content (gz, zst, etc).
        mp.exec_bash(
            cfg.vm.name,
            f"set -eu\ncd {cfg.paths.vm_agents_root} && tar -xf {vm_archive} && rm -f {vm_archive}\n",
        )
        return
    if archive_path.suffix == ".zst":
        archive_mod.extract_tar_zst(archive_path, cfg.paths.host_data_dir)
    else:
        # Legacy .tar.gz path (archives from pre-G6 versions).
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(cfg.paths.host_data_dir, filter="data")


def _latest_host_snapshot(cfg: AgentsConfig, name: str) -> Path | None:
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    if not upgrades_dir.exists():
        return None
    # Look for both the canonical .tar.zst and legacy .tar.gz archives so
    # rollbacks of older upgrades keep working.
    candidates = sorted(
        list(upgrades_dir.glob(f"{name}-*{archive_mod.ARCHIVE_SUFFIX}"))
        + list(upgrades_dir.glob(f"{name}-*.tar.gz"))
    )
    return candidates[-1] if candidates else None


def _run_config_migrate(cfg: AgentsConfig, svc: str, agent_type: str) -> None:
    """Run the in-container config-schema migration after recreate.

    Hermes: ``hermes config migrate``. Newer Hermes versions sometimes
    bump the ``config.yaml`` schema (add fields, rename keys, deprecate
    sections). Without this hook, an upgraded container would run with
    the previous schema — some new gateway features stay dormant, and
    a future ``config check`` would surface drift the user has to fix
    manually. Idempotent: when no changes are needed the command just
    prints "Config version: N ✓" and returns 0.

    OpenClaw: skipped. OpenClaw owns its own plugin lifecycle and the
    toolkit deliberately stays out of its config surface (see
    CLAUDE.md's Hermes-vs-OpenClaw matrix). Any config migration on
    that side is the user's responsibility via ``openclaw configure``.

    Best-effort: a non-zero exit (e.g. older Hermes builds that lack
    the ``migrate`` subcommand, or the container not being healthy
    enough to exec into yet) emits a warning and lets the upgrade
    proceed. Doctor runs next and will catch any leftover
    misconfiguration. Caveat: this command operates on
    ``config.yaml`` only — it does NOT migrate data-directory
    layouts (e.g. the v2026.5.7 → v2026.5.16 ``platforms/pairing/``
    → ``pairing/`` move that ``agents upgrade coder`` famously hit
    on 2026-05-22). Such migrations remain the user's responsibility
    until Hermes adds them upstream.
    """
    if agent_type != "hermes":
        return
    from agents_cli.ops import _compose_exec_in_vm_argv

    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "hermes", "config", "migrate", no_tty=True),
        check=False,
    )
    if result.returncode != 0:
        ui.warn(
            f"hermes config migrate failed (rc={result.returncode}); upgrade continues. "
            f"stderr tail: {(result.stderr or '').strip().splitlines()[-3:] if result.stderr else '(empty)'}. "
            "Run `agents exec <name> -- hermes config check` after the upgrade "
            "completes to inspect any drift."
        )


def _vm_snapshot(cfg: AgentsConfig, name: str) -> str:
    """Take a pre-upgrade VM snapshot.

    Delegates to :func:`vm_mod.take_snapshot` so we inherit its full
    orchestration: ACPI-shutdown-then-wait-for-stopped (libvirt's
    ``virsh shutdown`` returns before the domain actually halts —
    racing the snapshot against an active virtiofs vhost-user
    backend, which fails with VHOST_USER_PROTOCOL_F_LOG_SHMFD), the
    returncode check on the snapshot itself (libvirt's ``-virsh
    snapshot-create-as`` is called with check=False so a failure
    would otherwise be silently swallowed and the journal would
    advance past a snapshot that doesn't actually exist), and
    wait_for_ssh after start_vm so the next phase's compose-write-
    via-scp doesn't race DHCP.

    ``yes=True`` because ``agents upgrade`` is the caller — the
    user already opted in to downtime by running ``upgrade``; an
    additional snapshot-stop confirmation here would be redundant.
    """
    base_name = f"pre-upgrade-{name}-{_utc_stamp()}"
    return vm_mod.take_snapshot(base_name, comment=f"agents upgrade {name}", yes=True)


def _latest_vm_snapshot(vm_name: str, agent_name: str) -> str | None:
    result = mp.info_vm(vm_name, fmt="json")
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    snapshots = info.get("info", {}).get(vm_name, {}).get("snapshots", {})
    candidates = sorted(n for n in snapshots if n.startswith(f"pre-upgrade-{agent_name}-"))
    return candidates[-1] if candidates else None


def _set_image_tag(cfg: AgentsConfig, svc: str, base: str, tag: str) -> None:
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    if svc not in services:
        raise UserError(f"service {svc!r} not in compose")
    services[svc]["image"] = f"{base}:{tag}"
    compose.write_compose_atomically(cfg, doc)
