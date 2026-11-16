"""Image upgrades with two-snapshot rollback.

A multipass snapshot captures the VM disk (rootless Docker image
cache, OS state). It does NOT capture the host-mounted data-dir, which
holds the compose file, the agent's config.yaml, and its persistent
state. So every upgrade takes BOTH:

- A host-data tarball at ``<data-dir>/.upgrades/<name>-<utc>.tar.zst``
  (compose + .env + the agent's data dir).
- A multipass snapshot named ``pre-upgrade-<name>-<utc>``.

Phases (resumable via journal): ``host_data_snapshot``,
``vm_snapshot``, ``tag_pinned``, ``pulled``, ``recreated``,
``doctor_passed``. On any phase failure after the snapshots, the
state machine reverts the compose tag, recreates the container with
the old image, and re-raises with the snapshot ids so a manual
``agx rollback`` can finish the job if needed.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Runtime import stays lazy (inside upgrade()) — prune_cmd imports
    # this module for the archive/snapshot name patterns.
    from agx_cli import prune_cmd as prune_cmd_mod

from agx_cli import archive as archive_mod
from agx_cli import backup_tags, compose, compose_runner, mp, ops, ui
from agx_cli import config as cfg_mod
from agx_cli import vm as vm_mod
from agx_cli.agents_cmd import _detect_type_from_compose
from agx_cli.agents_cmd import service_name as _service_name_for
from agx_cli.compose import HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE
from agx_cli.config import AgentsConfig
from agx_cli.errors import HealthCheckFailure, RuntimeFailure, UserError
from agx_cli.journal import Journal

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
        # Multi-minute operation on fresh image caches — narrate so a
        # silent terminal doesn't read as a hang (same rationale as the
        # upgrade phases).
        ui.progress("pulling images for all services (large images take minutes)...")
        t0 = time.monotonic()
        compose_runner.pull(cfg)
        ui.progress(f"pull complete ({_fmt_elapsed(t0)})")
        ui.emit({"pulled": "all"}, human="pulled all images")
        return
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    svc = _service_name_for(name, agent_type)
    ui.progress(f"pulling {svc} (large images take minutes)...")
    t0 = time.monotonic()
    compose_runner.pull(cfg, svc)
    ui.progress(f"pulled {svc} ({_fmt_elapsed(t0)})")
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
    pinned_tag = current_image.split(":", 1)[1] if ":" in current_image else ""
    if not pinned_tag:
        raise UserError(f"current image for {name!r} has no tag: {current_image!r}")

    # An interrupted upgrade may already have mutated the compose pin
    # (``tag_pinned`` runs before pull/recreate), so the pin is NOT a
    # trustworthy "currently running version" when a resumable journal
    # exists. Check for one BEFORE the no-op short-circuit below:
    # without this, a retry after a SIGKILL-post-tag_pinned reads the
    # NEW tag back out of compose, concludes "already at vNEW —
    # nothing to do", and leaves the container running vOLD forever.
    # Resume with the tags the journal recorded at begin-time so the
    # retry completes the ORIGINAL upgrade faithfully — including
    # skipping the latest-fetch, which could otherwise silently
    # retarget the resume at a release that appeared upstream after
    # the crash. Journals from before the meta field existed carry no
    # tags; those fall through to the normal resolution path.
    resumable = Journal.find_resumable(cfg.paths.host_data_dir, "upgrade", name)
    resume_tags: tuple[str, str] | None = None
    if resumable is not None and resumable.meta.get("old_tag") and resumable.meta.get("new_tag"):
        resume_tags = (str(resumable.meta["old_tag"]), str(resumable.meta["new_tag"]))

    # Deferred defaults bump (item: never re-baseline `agx create`
    # onto a tag a failed upgrade never proved out). Resolution stays
    # here; persistence happens only on the success paths — the no-op
    # return (the agent demonstrably runs that tag already) and after
    # ``journal.complete()``.
    resolved_latest: str | None = None
    if resume_tags is not None:
        old_tag, new_tag = resume_tags
        ui.progress(
            f"interrupted upgrade journal found for {name!r} ({old_tag} -> {new_tag}); resuming it"
        )
    else:
        old_tag = pinned_tag
        # Bare ``agx upgrade <name>`` (no ``--tag``, no ``--latest``,
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
            from agx_cli import outdated_cmd

            ui.progress(f"resolving latest upstream {agent_type} tag from GitHub Releases...")
            latest = outdated_cmd.fetch_latest_tag(agent_type, include_prerelease=False)
            if not latest:
                raise UserError(
                    f"could not resolve latest upstream tag for {agent_type!r} "
                    f"— GitHub API unreachable? Pass --tag <T> to pin explicitly, "
                    f"or --recreate to force-refresh at the current pin."
                )
            if outdated_cmd.image_tag_published(agent_type, latest) is False:
                # Refuse BEFORE snapshots/stop: the pull would fail anyway.
                raise UserError(
                    f"latest {agent_type} release {latest!r} has no image tag {latest!r} "
                    f"on Docker Hub (not published yet, or upstream only tagged it "
                    f"under floating names like :stable). Re-run later, or pin an "
                    f"existing tag explicitly with --tag <T>."
                )
            new_tag = latest
            resolved_latest = latest
        else:
            from agx_cli import outdated_cmd

            # explicit tag wins (in the upstream's image-tag form); otherwise
            # refresh in place
            new_tag = outdated_cmd.normalize_image_tag(agent_type, tag) if tag else old_tag

        # No-op short-circuit when the target tag matches the current pin.
        # Covers both:
        # - bare ``agx upgrade <name>`` after the implicit-latest fetch
        #   resolved to the same tag the agent is already on
        # - ``--tag X`` where X happens to equal the current pin
        # Without this, the upgrade would do ~30-60s of avoidable agent
        # downtime (``vm.take_snapshot`` on libvirt+virtiofs needs the
        # VM stopped), print a misleading ``vX.Y.Z -> vX.Y.Z`` summary,
        # and look like a no-op anyway. ``--recreate`` is the explicit
        # opt-in for the legitimate force-refresh use cases (env-file
        # changes, wedged container, re-running doctor with the snapshot
        # safety net intact). Skipped entirely when resuming a journal —
        # the mutated pin is exactly what makes this check lie.
        if new_tag == old_tag and not recreate:
            _persist_default_tag(cfg, agent_type, resolved_latest, config_path)
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
    base = HERMES_IMAGE_BASE if agent_type == "hermes" else OPENCLAW_IMAGE_BASE

    # Progress numbering runs over the phases this invocation can
    # actually execute — snapshots drop out under --no-snapshot, and the
    # config-migrate phase is a silent no-op for OpenClaw — so "[2/5]"
    # never refers to work the user won't see happen. The journal keeps
    # the full canonical phase list regardless; only the narration
    # adapts.
    unplanned: set[str] = set()
    if no_snapshot:
        unplanned |= {"host_data_snapshot", "vm_snapshot"}
    if agent_type != "hermes":
        unplanned.add(CONFIG_MIGRATE_PHASE)
    planned = [p for p in UPGRADE_PHASES if p not in unplanned]

    def _step(phase: str, message: str) -> None:
        ui.progress(f"[{planned.index(phase) + 1}/{len(planned)}] {message}")

    ui.progress(f"upgrading {name!r}: {old_tag} -> {new_tag} ({len(planned)} phases)")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Re-find under the lock (the peek above ran unlocked; a
        # parallel invocation may have begun or completed a journal
        # in between).
        journal = Journal.find_resumable(cfg.paths.host_data_dir, "upgrade", name)
        if journal is None:
            journal = Journal.begin(
                cfg.paths.host_data_dir,
                "upgrade",
                name,
                UPGRADE_PHASES,
                meta={"old_tag": old_tag, "new_tag": new_tag},
            )
        else:
            # A journal found HERE is authoritative over whatever the
            # unlocked peek decided. If a parallel invocation began a
            # v1->v2 upgrade (and died after tag_pinned) while this one
            # resolved v3 waiting for the lock, keeping the local v3
            # tags would skip the pin (tag_pinned already done), pull
            # v3, recreate from the compose file still pinned to v2,
            # and report v3 as deployed. Adopt the journal's tags for
            # everything downstream — pin, pull, emit, and the defaults
            # bump. The pre-lock peek remains only the fast path that
            # skips the no-op short-circuit.
            if journal.meta.get("old_tag") and journal.meta.get("new_tag"):
                adopted = (str(journal.meta["old_tag"]), str(journal.meta["new_tag"]))
                if adopted != (old_tag, new_tag):
                    old_tag, new_tag = adopted
                    # The locally resolved "latest" was never proven out
                    # by the upgrade this run is now completing — it must
                    # not re-baseline future ``agx create``.
                    resolved_latest = None
                    ui.progress(
                        f"interrupted upgrade journal found under the lock for {name!r} "
                        f"({old_tag} -> {new_tag}); completing it instead"
                    )
            if journal.completed_phases:
                ui.progress(
                    "resuming interrupted upgrade "
                    f"(already done: {', '.join(journal.completed_phases)})"
                )

        host_snap: Path | None = None
        vm_snap: str | None = None
        pruned: prune_cmd_mod.PruneResult | None = None
        try:
            if not no_snapshot and not journal.is_phase_done("host_data_snapshot"):
                _step(
                    "host_data_snapshot",
                    "host data snapshot: tarring compose + .env + agent dir...",
                )
                t0 = time.monotonic()
                host_snap = _host_data_snapshot(cfg, name, agent_type)
                _step(
                    "host_data_snapshot",
                    f"host data snapshot: {host_snap.name} "
                    f"({_fmt_size(host_snap.stat().st_size)}, {_fmt_elapsed(t0)})",
                )
                journal.advance("host_data_snapshot")
            if not no_snapshot and not journal.is_phase_done("vm_snapshot"):
                _step(
                    "vm_snapshot",
                    "VM snapshot (may stop + restart the VM — all agents pause briefly)...",
                )
                t0 = time.monotonic()
                vm_snap = _vm_snapshot(cfg, name, config_path=config_path)
                _step("vm_snapshot", f"VM snapshot: {vm_snap} ({_fmt_elapsed(t0)})")
                journal.advance("vm_snapshot")

            if not journal.is_phase_done("tag_pinned"):
                _step("tag_pinned", f"pinning {base}:{new_tag} in compose")
                _set_image_tag(cfg, svc, base, new_tag)
                journal.advance("tag_pinned")

            if not journal.is_phase_done("pulled"):
                _step("pulled", f"pulling {base}:{new_tag} (large images take minutes)...")
                t0 = time.monotonic()
                compose_runner.pull(cfg, svc)
                inspect = compose_runner.docker_image_inspect(cfg, f"{base}:{new_tag}")
                if inspect.returncode != 0:
                    raise RuntimeFailure(
                        f"docker image inspect failed for {base}:{new_tag} "
                        f"(rc={inspect.returncode}): {inspect.stderr.strip()}"
                    )
                _step("pulled", f"pulled {base}:{new_tag} ({_fmt_elapsed(t0)})")
                journal.advance("pulled")

            if not journal.is_phase_done("recreated"):
                _step("recreated", "recreating the container on the new image...")
                t0 = time.monotonic()
                if agent_type == "hermes":
                    # Materialize the cont-init.d perms hook so a reseated
                    # agent's bind mount has its source. AND re-apply the
                    # install-path chown directly: agents whose fragment
                    # predates the hook (not yet reseated) get recreated here
                    # with the overlay fs reset to the image-baked uid; without
                    # this call upgrade would be the one recreate path that
                    # never runs the fixer, and the perms would re-break after
                    # every upgrade. Belt-and-suspenders for reseated ones.
                    compose.ensure_cont_init_script(cfg)
                compose_runner.up(cfg, svc)
                if agent_type == "hermes":
                    from agx_cli import fixperms_cmd

                    try:
                        huid = fixperms_cmd.container_hermes_uid(cfg, svc)
                    except RuntimeFailure:
                        huid = 1000  # best-effort; entrypoint usually maps to 1000
                    fixperms_cmd.chown_hermes_install_paths(cfg, svc, huid)
                _step("recreated", f"container recreated ({_fmt_elapsed(t0)})")
                journal.advance("recreated")

            if not journal.is_phase_done(CONFIG_MIGRATE_PHASE):
                if agent_type == "hermes":
                    _step(
                        CONFIG_MIGRATE_PHASE,
                        "running `hermes config migrate` in the container...",
                    )
                _run_config_migrate(cfg, svc, agent_type, name=name)
                journal.advance(CONFIG_MIGRATE_PHASE)

            if not journal.is_phase_done("doctor_passed"):
                _step("doctor_passed", "running doctor health checks on the upgraded agent...")
                # emit=False: upgrade owns this command's single stdout
                # object — doctor's own table/array would stack a second
                # JSON document under --json. raise_on_fail=False so the
                # failing checks can be NAMED in the error: a bare "1
                # doctor check(s) failed" left the user no way to know
                # what rolled the upgrade back, since the revert replaces
                # the container the report was about.
                report = ops.doctor(
                    target=name, config_path=config_path, emit=False, raise_on_fail=False
                )
                if report.failed:
                    failed_checks = [c for c in report.checks if c.status == "fail"]
                    raise HealthCheckFailure(
                        f"{len(failed_checks)} doctor check(s) failed on the upgraded agent: "
                        + "; ".join(f"{c.name}: {c.detail or '(no detail)'}" for c in failed_checks)
                    )
                journal.advance("doctor_passed")

            journal.complete()

            # Post-upgrade retention (best-effort, inside the lock we
            # already hold — auto_prune_after_upgrade must NOT re-acquire
            # it). Fully swallowed on error: a prune hiccup must never
            # route a landed upgrade into the revert handler below.
            if cfg.upgrades.keep_generations > 0:
                from agx_cli import prune_cmd

                # Prune against the FUTURE defaults baseline: image retention
                # protects defaults.<type>_image_tag, and at this point
                # config.toml still holds the OUTGOING tag — counting it as
                # the baseline retains one extra image per --latest upgrade
                # (permanently at keep_generations=1). A mutated COPY, not
                # cfg itself: _persist_default_tag below skips its save when
                # the in-memory value already matches, so mutating cfg here
                # would silently drop the on-disk defaults refresh.
                prune_cfg = cfg
                if resolved_latest is not None:
                    prune_cfg = cfg.model_copy(deep=True)
                    if agent_type == "hermes":
                        prune_cfg.defaults.hermes_image_tag = resolved_latest
                    else:
                        prune_cfg.defaults.openclaw_image_tag = resolved_latest
                try:
                    pruned = prune_cmd.auto_prune_after_upgrade(prune_cfg, name)
                except Exception as prune_err:
                    ui.warn(f"post-upgrade prune failed (non-fatal): {prune_err}")
                else:
                    if pruned is not None and pruned.failed:
                        for item in pruned.failed:
                            ui.warn(
                                f"prune: could not delete {item['kind']} "
                                f"{item['ref']}: {item['error']}"
                            )
        except Exception as failure:
            ui.warn(f"upgrade failed: {failure}; reverting compose tag")
            # The revert itself talks to the VM (compose write via
            # transfer + docker compose up) — and an unreachable VM is
            # often the very reason we're in this handler. Guard every
            # step so a failing revert can't replace the original
            # exception or skip the journal-clear and the snapshot
            # hint below.
            try:
                _set_image_tag(cfg, svc, base, old_tag)
            except Exception as revert_err:
                ui.warn(f"could not revert compose tag to {old_tag!r}: {revert_err}")
            else:
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
                f"run `agx rollback {name}` if the live state is broken"
            )
            raise

    # Only now — after the upgrade demonstrably landed — is the
    # resolved-latest tag a sane baseline for future ``agx create``.
    # Persisting at resolve time meant a failed-and-rolled-back upgrade
    # still re-baselined new agents onto the bad tag. Outside the
    # try/except above so a config-save hiccup can't trigger the revert
    # handler on an upgrade that already succeeded.
    _persist_default_tag(cfg, agent_type, resolved_latest, config_path)

    pruned_counts = (
        f"{len(pruned.archives)} tarball(s), {len(pruned.snapshots)} snapshot(s), "
        f"{len(pruned.images)} image tag(s)"
        if pruned is not None
        else None
    )
    ui.emit(
        {
            "name": name,
            "old_tag": old_tag,
            "new_tag": new_tag,
            "host_snapshot": str(host_snap) if host_snap else None,
            "vm_snapshot": vm_snap,
            "pruned": pruned.payload() if pruned is not None else None,
        },
        human=(
            f"upgraded {name!r}: {old_tag} -> {new_tag}\n"
            f"  host snapshot: {host_snap or '(skipped)'}\n"
            f"  vm snapshot: {vm_snap or '(skipped)'}\n"
            f"  pruned: {pruned_counts or '(disabled)'}"
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

    if len(names) > 1:
        ui.progress(f"upgrading {len(names)} agents: {', '.join(names)}")
    outcomes: list[UpgradeOutcome] = []
    for i, n in enumerate(names, 1):
        if len(names) > 1:
            ui.progress(f"--- {n} ({i}/{len(names)}) ---")
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
    ``<data-dir>/.upgrades/<name>-*.tar.zst`` is used and the matching
    multipass VM snapshot is restored. With an explicit ``snapshot_id``:

    - If it is an archive filename under ``.upgrades/``: that archive.
    - If it starts with ``pre-upgrade-``: treated as a multipass snapshot
      name; ONLY the VM is restored (host data unchanged).
    - Otherwise: matched by suffix against archive names; if exactly one
      matches, that's used.

    Confirms unless ``--yes``.

    Never discards the present silently: everything the agent learned or
    changed since the snapshot (skills, memories, sessions, config, and
    rotated credentials) would otherwise be gone for good. Before
    overwriting, the current state is exported to
    ``<data-dir>/backups/pre-rollback/`` (restorable via ``agx import``,
    never auto-pruned, seen by the host backup) — for the agent itself,
    or for EVERY agent when a VM-snapshot restore will revert agent data
    on the VM disk (``--data-dir-in-vm``). The rollback aborts if that
    safety export fails.
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
        pattern = _host_archive_re(name)
        candidates = (
            sorted(p for p in upgrades_dir.iterdir() if pattern.fullmatch(p.name))
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

    vm_snap_name = snapshot_id if vm_only else _latest_vm_snapshot(cfg.vm.name, name)
    # A VM-snapshot restore reverts agent data too when it lives on the
    # VM disk — for every agent in the VM, not just this one.
    vm_reverts_all_data = bool(vm_snap_name) and cfg.paths.data_dir_in_vm
    prompt_target = archive.name if archive else snapshot_id
    age = _snapshot_age(prompt_target or "")
    if not yes and not ui.confirm(
        f"restore {name!r} from {prompt_target!r}{f' (taken {age})' if age else ''}? "
        + (
            "The data of EVERY agent in the VM reverts to the VM snapshot. "
            if vm_reverts_all_data
            else "compose, .env, and the agent's data dir will be overwritten — "
            "everything it learned or changed since then is replaced. "
        )
        + f"The current state is saved first under {_PRE_ROLLBACK_DIR}/. Restored "
        "credentials may be stale (OAuth refresh tokens are single-use): expect "
        "to re-authenticate providers that rotated since the snapshot.",
        default=False,
    ):
        raise UserError("aborted by user")

    # Rollback rewrites docker-compose.yml, .env, and the agent's data
    # dir — a mutation like any other; hold the flock so it serializes
    # against parallel create/upgrade/reseat invocations.
    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Resolve the agent's service so the container can be stopped
        # before extraction and recreated afterwards. Rolling back a
        # REMOVED agent's archive is legitimate — there's simply no
        # container to stop, so skip that part with a warning.
        agent_type = _detect_type_from_compose(cfg, name)
        svc = _service_name_for(name, agent_type) if agent_type is not None else None
        if svc is None:
            ui.warn(f"agent {name!r} not in compose — restoring without container stop/recreate")

        # Once the live container is stopped, ANY failure in the restore
        # steps below (invalid archive, extraction error, VM snapshot
        # restore failure) would otherwise raise before the final
        # ``compose_runner.up`` — leaving the agent stopped after a
        # merely failed rollback ATTEMPT. Track the stop and best-effort restart the service
        # before re-raising the original error.
        stopped_svc: str | None = None
        safety: list[Path] = []
        try:
            if archive is not None and svc is not None:
                # Extracting over a LIVE container's SQLite/WAL files
                # (state.db, kanban.db, ...) is a straight corruption
                # path — stop the container first. Stopped BEFORE the
                # safety export, so that archive is consistent too.
                ui.progress(f"stopping {svc} before restore...")
                compose_runner.stop(cfg, svc)
                stopped_svc = svc
            safety = _save_pre_rollback_state(
                cfg,
                name,
                all_agents=vm_reverts_all_data,
                agent_exists=svc is not None,
                config_path=config_path,
            )
            if archive is not None:
                ui.progress(f"restoring host data from {archive.name}...")
                _restore_host_snapshot(cfg, archive)

            if vm_snap_name:
                ui.progress(
                    f"restoring VM snapshot {vm_snap_name} "
                    f"(stop -> restore -> start; all agents pause)..."
                )
                # vm_mod.restore_snapshot owns the restore orchestration:
                # ACPI-stop + wait-for-stopped where the backend needs it,
                # a returncode check on the restore itself (the raw
                # mp.restore call this replaces never checked its result),
                # and restart + SSH wait. ``yes=True`` — the user already
                # confirmed the rollback above.
                # ``emit=False``: rollback narrates via ui.progress and
                # emits its own single stdout object at the end — the
                # restore helper's "restored: ..." emit would be a
                # second object mid-command.
                vm_mod.restore_snapshot(
                    vm_snap_name, destructive=True, yes=True, config_path=config_path, emit=False
                )
                ui.progress("VM restored and restarted")
        except Exception as restore_err:
            if stopped_svc is not None:
                ui.warn(
                    f"rollback failed after {stopped_svc} was stopped ({restore_err}); "
                    f"restarting it with the pre-rollback state..."
                )
                try:
                    compose_runner.up(cfg, stopped_svc)
                except Exception as up_err:
                    ui.warn(f"could not restart {stopped_svc}: {up_err}")
            raise

        if archive is not None and svc is not None:
            ui.progress(f"recreating {svc} from the restored compose...")
            compose_runner.up(cfg, svc)

    ui.emit(
        {
            "name": name,
            "restored": {
                "host": str(archive) if archive else None,
                "vm_snapshot": vm_snap_name,
            },
            "pre_rollback_archives": [str(p) for p in safety],
        },
        human=(
            f"rolled back {name!r}\n"
            f"  host: {archive or '(skipped — vm-only)'}\n"
            f"  vm snapshot: {vm_snap_name or '(none found)'}"
            + "".join(f"\n  state before rollback: {p} (`agx import` to restore)" for p in safety)
        ),
    )


# Where rollback parks the state it is about to overwrite. A subdir of
# ``backups/`` on purpose: outside ``.upgrades/`` (rollback must never
# pick its own safety copy as the "latest snapshot", and prune must not
# count it as an upgrade generation), outside export's rolling-window
# prune (which only touches the top-level ``backups/``), inside what the
# host backup already covers.
_PRE_ROLLBACK_DIR = "backups/pre-rollback"


def _save_pre_rollback_state(
    cfg: AgentsConfig,
    name: str,
    *,
    all_agents: bool,
    agent_exists: bool,
    config_path: Path,
) -> list[Path]:
    """Export the state a rollback is about to overwrite; see
    :func:`rollback`. Raises (aborting the rollback) on failure."""
    from agx_cli import export_cmd

    target = cfg.paths.host_data_dir / _PRE_ROLLBACK_DIR
    if all_agents:
        ui.progress(f"saving every agent's current state to {target}/ before the VM restore...")
        return export_cmd.export_all(to=target, config_path=config_path, emit=False)
    if not agent_exists:
        return []  # a removed agent's archive: nothing live to preserve
    ui.progress(f"saving {name!r}'s current state to {target}/ before overwriting it...")
    return [export_cmd.export(name, to=target, config_path=config_path, emit=False)]


def _snapshot_age(snapshot_name: str) -> str:
    """Human age of a snapshot from the UTC stamp in its name, or ``""``."""
    m = re.search(r"(\d{8}T\d{6}Z)", snapshot_name)
    if not m:
        return ""
    taken = datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.UTC)
    hours = (datetime.datetime.now(datetime.UTC) - taken).total_seconds() / 3600
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return f"{hours:.0f} hours ago"
    return f"{hours / 24:.0f} days ago"


# ---------- helpers ----------


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


# The stamp format emitted by ``_utc_stamp`` above (and journal.py's
# twin). Snapshot lookups must match ``<name>-<stamp>`` STRICTLY: a
# prefix glob like ``foo-*`` also catches agent ``foo-bar``'s
# artifacts, and since letters sort after digits, ``foo-bar``'s
# newest would beat ``foo``'s own in a ``sorted(...)[-1]`` pick.
_UTC_STAMP_RE = archive_mod.UTC_STAMP_RE


def _host_archive_re(name: str) -> re.Pattern[str]:
    """Match ``<name>-<UTC stamp>`` + the archive suffix, exactly."""
    return archive_mod.archive_name_re(name)


def _vm_snapshot_re(agent_name: str) -> re.Pattern[str]:
    """Match ``pre-upgrade-<name>-<UTC stamp>`` exactly, allowing the
    ``-<n>`` counter suffix that ``vm._unique_snapshot_name`` appends
    on collision."""
    return re.compile("pre-upgrade-" + re.escape(agent_name) + "-" + _UTC_STAMP_RE + r"(?:-\d+)?")


def _persist_default_tag(
    cfg: AgentsConfig,
    agent_type: str,
    latest: str | None,
    config_path: Path,
) -> None:
    """Refresh the toolkit-wide default so future ``agx create`` of
    this type starts on the latest baseline rather than the ``vm
    init``-time snapshot.

    Deliberately called only from the success paths (the no-op return
    — the agent demonstrably already runs the tag — and after
    ``journal.complete()``): persisting at resolve time re-baselined
    new agents onto a tag that a failed-and-rolled-back upgrade never
    proved out.
    """
    cfg_mod.persist_default_image_tag(cfg, agent_type, latest, config_path)


def _fmt_elapsed(start: float) -> str:
    """Elapsed since ``start`` (a ``time.monotonic()`` value), humanized."""
    seconds = time.monotonic() - start
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.1f}m"


def _fmt_size(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GiB"
    return f"{n / 1024**2:.0f} MiB"


def _host_data_snapshot(cfg: AgentsConfig, name: str, agent_type: str | None = None) -> Path:
    """Tar+zstd the agent's data dir + compose + .env.

    Always tarred inside the VM as root, in both data-dir modes:
    rootless Docker writes the agent's state at a subuid (e.g. host uid
    100999 for container uid 1000) under mode 0700, so neither the host
    user nor the VM's ``ubuntu`` user can walk into ``<name>/.data`` —
    and host ACLs can't be relied on to change that (a ``mkdir(0700)``
    under a default ACL is born with ``mask::---``). The share passes
    ownership through to the guest, and root in the guest can read
    everything regardless of subuid.

    Bare-metal: no VM, so the ``sudo tar`` runs on the host directly
    (prompting the user).

    Package-manager caches are left out (``archive.cache_exclude_args``).
    The restore extracts OVER the live dir without wiping it, so a
    rollback keeps whatever caches exist and re-downloads the rest.
    """
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    upgrades_dir.mkdir(parents=True, exist_ok=True)
    backup_tags.tag_dir(upgrades_dir)
    archive_path = upgrades_dir / f"{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"

    # Both data-dir modes: tar inside the VM as root so subuid-owned
    # files are readable. (The in-VM mode used to run an unprivileged
    # tar; with ``--ignore-failed-read`` that silently skipped the whole
    # mode-700 ``.data`` subtree and produced a hollow snapshot.) The
    # archive's owner inside the VM is root after the tar runs; chown
    # it back to the invoking user so the SFTP/SCP transfer to the host
    # (which runs as that user) doesn't hit EACCES. After transfer the archive lands at
    # ``<host_data_dir>/.upgrades/`` owned by the toolkit user
    # (host-side default — ``shutil.copy`` semantics), which is what
    # the rollback path expects.
    vm_archive = f"/tmp/agx-upgrade-snap-{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"
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
            f"{archive_mod.cache_exclude_args(name, agent_type)} "
            f"-cf {vm_archive} "
            f"$([ -d {name} ] && echo {name} || true) "
            f"$([ -f docker-compose.yml ] && echo docker-compose.yml || true) "
            f"$([ -f .env ] && echo .env || true)\n"
            f'sudo chown "$(id -u):$(id -g)" {vm_archive}\n'
        ),
    )
    mp.transfer_from_vm(cfg.vm.name, vm_archive, archive_path)
    mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_archive, check=False)
    return archive_path


def _restore_host_snapshot(cfg: AgentsConfig, archive_path: Path) -> None:
    """Extract a pre-upgrade snapshot back over the data dir.

    Always inside the VM as root, in both data-dir modes — the mirror
    image of :func:`_host_data_snapshot`. The archive members carry
    subuid ownership under mode-700 parents: a host-side extraction
    can't even enter ``<name>/.data`` (and couldn't restore the owners
    if it did), and an unprivileged in-VM one has the same problem.
    ``tar -xf`` auto-detects the zstd compression.
    """
    vm_archive = f"/tmp/agx-restore-{os.getpid()}-{uuid.uuid4().hex[:8]}.tar"
    mp.transfer_to_vm(archive_path, cfg.vm.name, vm_archive)
    try:
        mp.exec_bash(
            cfg.vm.name,
            (
                f"set -eu\n"
                f"cd {cfg.paths.vm_agents_root}\n"
                f"sudo tar -xf {vm_archive} --numeric-owner\n"
            ),
        )
    finally:
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_archive, check=False)


def _latest_host_snapshot(cfg: AgentsConfig, name: str) -> Path | None:
    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    if not upgrades_dir.exists():
        return None
    # Strict match (no ``foo-*`` prefix glob that would pick up agent
    # ``foo-bar``'s archives for agent ``foo``).
    pattern = _host_archive_re(name)
    candidates = sorted(p for p in upgrades_dir.iterdir() if pattern.fullmatch(p.name))
    return candidates[-1] if candidates else None


def _run_config_migrate(cfg: AgentsConfig, svc: str, agent_type: str, *, name: str) -> None:
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
    AGENTS.md's Hermes-vs-OpenClaw matrix). Any config migration on
    that side is the user's responsibility via ``openclaw configure``.

    After a successful migrate, ``hermes config check`` runs as a
    verification pass. Rationale (upstream "Updating & Uninstalling"
    doc): the native ``hermes update`` flow prompts INTERACTIVELY for
    each new config option, but this phase runs migrate with
    ``no_tty`` — options that wanted an interactive answer may be left
    unset with only their defaults. ``config check`` is upstream's
    documented way to surface exactly that; when it reports drift we
    warn with the interactive remedy (``agx exec <name> -- hermes
    config migrate``, which allocates a TTY). The check is skipped
    when migrate itself failed — an image old enough to lack
    ``migrate`` lacks ``check`` too, and one warning is enough.

    Best-effort: a non-zero exit (e.g. older Hermes builds that lack
    the ``migrate`` subcommand, or the container not being healthy
    enough to exec into yet) emits a warning and lets the upgrade
    proceed. Doctor runs next and will catch any leftover
    misconfiguration. Caveat: this command operates on
    ``config.yaml`` only — it does NOT migrate data-directory
    layouts (e.g. the v2026.5.7 → v2026.5.16 ``platforms/pairing/``
    → ``pairing/`` move). Such migrations remain the user's responsibility
    until Hermes adds them upstream.
    """
    if agent_type != "hermes":
        return
    from agx_cli.ops import _compose_exec_in_vm_argv

    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "hermes", "config", "migrate", no_tty=True),
        check=False,
    )
    if result.returncode != 0:
        ui.warn(
            f"hermes config migrate failed (rc={result.returncode}); upgrade continues. "
            f"stderr tail: {(result.stderr or '').strip().splitlines()[-3:] if result.stderr else '(empty)'}. "
            f"Run `agx exec {name} -- hermes config check` after the upgrade "
            "completes to inspect any drift."
        )
        return

    check = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "hermes", "config", "check", no_tty=True),
        check=False,
    )
    # rc!=0 is the drift signal (checkers report problems via exit
    # status). Deliberately NOT substring-matching the output — with
    # an undocumented format, probing for words like "missing" would
    # false-positive on healthy text such as "no missing options".
    if check.returncode != 0:
        tail = "\n".join((check.stdout or check.stderr or "").strip().splitlines()[-6:])
        ui.warn(
            f"hermes config check reports options the non-interactive migrate "
            f"could not fill:\n{tail}\n"
            f"Run `agx exec {name} -- hermes config migrate` for the "
            "interactive pass (it prompts for each new option)."
        )


def _vm_snapshot(cfg: AgentsConfig, name: str, *, config_path: Path) -> str:
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

    ``yes=True`` because ``agx upgrade`` is the caller — the
    user already opted in to downtime by running ``upgrade``; an
    additional snapshot-stop confirmation here would be redundant.
    ``emit=False`` because upgrade already narrates this phase via
    ``ui.progress`` and owns the command's single stdout object —
    take_snapshot's own emit would land a second one mid-upgrade.
    """
    base_name = f"pre-upgrade-{name}-{_utc_stamp()}"
    # config_path MUST be threaded through: take_snapshot defaults to
    # DEFAULT_CONFIG_PATH, so omitting it makes the snapshot load a
    # DIFFERENT config than the upgrade is operating on whenever the
    # caller passed an alternate path — wrong VM name, wrong backend.
    # (Surfaced in CI: the unit suite was silently reading the
    # developer's real ~/.config/agx/config.toml through this call.)
    return vm_mod.take_snapshot(
        base_name,
        comment=f"agx upgrade {name}",
        yes=True,
        emit=False,
        config_path=config_path,
    )


def _latest_vm_snapshot(vm_name: str, agent_name: str) -> str | None:
    result = mp.info_vm(vm_name, fmt="json")
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    snapshots = info.get("info", {}).get(vm_name, {}).get("snapshots", {})
    pattern = _vm_snapshot_re(agent_name)
    candidates = sorted(n for n in snapshots if pattern.fullmatch(n))
    return candidates[-1] if candidates else None


def _set_image_tag(cfg: AgentsConfig, svc: str, base: str, tag: str) -> None:
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    if svc not in services:
        raise UserError(f"service {svc!r} not in compose")
    services[svc]["image"] = f"{base}:{tag}"
    compose.write_compose_atomically(cfg, doc)
