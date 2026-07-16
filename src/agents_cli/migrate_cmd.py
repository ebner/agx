"""``agents vm migrate-data`` — move agent state from the host mount onto
the VM disk, in place.

Why this exists: on host-mount deployments the Hermes entrypoint chmods
``/opt/data`` on every container start, which collapses the POSIX-ACL
mask that ``agents access grant`` set — so the host user keeps losing
read access (doctor's recurring ``host_access`` warning). And the s6
supervision tree (container-root) cannot write to the 9p/virtiofs share
at all, which is a whole class of "agent silently stops replying" bugs.
Both problems are structural to the host-mount layout; CLAUDE.md already
recommends ``--data-dir-in-vm`` for SQLite-heavy workloads. This command
converts an existing deployment without re-initialising anything.

Safety contract (non-negotiable): **the host data dir is never modified**
— no file under it is deleted, overwritten, or moved by this command.
The only host-side writes are additive metadata: fresh export archives
under ``<data-dir>/backups/`` and the phase journal under
``<data-dir>/.journal/``. The final step only *prints* which host paths
the user may archive/delete manually after verifying the migrated
deployment; the toolkit performs no deletion. Any failure leaves the
deployment restorable by flipping ``[paths] data_dir_in_vm`` back (the
exact steps are printed).

Mechanism, and why the target path is the *same* path:

- ``vm init --data-dir-in-vm``'s convention is that agent state lives at
  ``cfg.paths.vm_agents_root`` (default ``/home/ubuntu/agents``) on the
  VM's own ext4 disk — the same path host-mount mode mounts the share
  at, just with no mount behind it. Keeping that path is load-bearing:
  compose fragments are relative (resolved via ``--project-directory``),
  but the Codex-skill sandbox volume written into each Hermes agent's
  ``config.yaml`` (``<vm_agents_root>/.codex:/codex:ro``) and the
  toolkit's Codex auth sync target are *absolute* VM paths. Migrating to
  a side path would strand every one of those.
- So the copy goes mount → VM-local staging sibling
  (``<vm_agents_root>.migrating``), then the share is detached, then the
  staging dir is swapped into the original path. The swap refuses if the
  path is still a mountpoint, so it can never clobber a live share.
- The copy itself is a VM-side root ``tar | tar`` pipeline with
  ``--exclude`` applied *before traversal*: the 9p/virtiofs mount passes
  subuid ownership through to the guest, guest root can read every file
  regardless of subuid (the exact rationale behind ``export_cmd``'s
  in-VM ``sudo tar``), and tar-as-root preserves ownership/modes/
  symlinks onto ext4. Pre-traversal exclusion (not post-copy pruning)
  matters because on the libvirt backend the host data dir *contains
  the running VM's own qcow2 disk* (``.disks/``, plus ``.images/``,
  ``.seeds/``, ``.ssh/``) — a wholesale copy would recursively copy the
  VM's disk into itself. Both VM backends (multipass, libvirt) expose
  ``mp.exec_bash``, so the same script works on either; bare_metal is
  refused up front (no VM). Re-running the pipeline is idempotent (tar
  extraction overwrites), which is what makes the phase resumable.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from agents_cli import compose, compose_runner, export_cmd, mp, ops, ui
from agents_cli import config as cfg_mod
from agents_cli import vm as vm_mod
from agents_cli.agents_cmd import _agent_type_from_service, _name_from_service
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.journal import Journal

MIGRATE_OP = "migrate-data"

MIGRATE_PHASES = [
    "exports_taken",
    "services_stopped",
    "data_copied",
    "mount_detached",
    "data_swapped",
    "config_flipped",
    "services_started",
    "doctor_passed",
]

# Top-level entries that stay host-side and are excluded from the VM copy
# BEFORE traversal (tar --exclude — never a post-copy prune, see
# _copy_script). Each is either a host-filesystem primitive that the
# toolkit keeps reading/writing on the HOST data dir even under
# --data-dir-in-vm, or a transient staging area:
#
# - .agents.lock      flock primitive (rule #7) — every mutation locks the
#                     host file; a VM copy would be dead weight.
# - .journal          phase journals are host primitives (vm.init seeds the
#                     host .journal/ even in --data-dir-in-vm mode) —
#                     including THIS operation's own live journal.
# - .staging          multipass-transfer staging; swept host-side by
#                     compose.cleanup_stale_temps.
# - .rebuild-staging  vm rebuild's transient export staging.
# - backups           export archives must stay host-visible so duplicity
#                     keeps picking them up — that's the whole point of the
#                     export-cron recommendation below; export_cmd writes
#                     here (host-side) in both modes.
# - .upgrades         upgrade host-data snapshots; upgrades._host_data_snapshot
#                     and `agents rollback` read the HOST dir in both modes.
#
# NOT excluded (deliberately copied wholesale even though the host copy
# stays authoritative for some): .codex (the VM copy is the bind-mount
# source under --data-dir-in-vm; `agents codex login` re-syncs it),
# .firecrawl-state.json and .memory-state/ (host-read in both modes; the
# VM copies are inert but harmless), .toolkit/ (the cont-init hook's
# bind-mount source must exist VM-side after the flip).
_HOST_ONLY_TOP_LEVEL = (
    ".agents.lock",
    ".journal",
    ".staging",
    ".rebuild-staging",
    "backups",
    ".upgrades",
)

# libvirt-backend state that lives under the host data dir (see
# backends/libvirt.py `configure()` / `_vm_disk_path`): the running VM's
# own qcow2 disk, cached cloud images, cloud-init seed ISOs, and the
# backend's SSH keypair. Copying these would (a) copy the live VM disk
# into itself — unbounded growth + an inconsistent image — and (b) be
# useless in the VM anyway. Harmless no-ops on multipass (the dirs don't
# exist there), so the exclusion is unconditional.
_BACKEND_STATE_TOP_LEVEL = (
    ".disks",
    ".images",
    ".seeds",
    ".ssh",
)

# The single exclusion list both the copy (tar --exclude, pre-traversal)
# and the disk-headroom estimate (du --exclude) are built from.
_COPY_EXCLUDES = _HOST_ONLY_TOP_LEVEL + _BACKEND_STATE_TOP_LEVEL

# Copy needs the data size plus breathing room for image layers / logs
# that keep growing on the same VM disk.
_DISK_HEADROOM_FACTOR = 1.2


def migrate_data(
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Migrate a host-mounted deployment to ``--data-dir-in-vm`` in place.

    Phased + journaled (op ``migrate-data``); a crashed run resumes by
    re-running the command. See the module docstring for the safety
    contract and the copy mechanism.
    """
    cfg = cfg_mod.load(config_path)

    if cfg.vm.backend == "bare_metal":
        raise UserError(
            "vm migrate-data is meaningless on the bare_metal backend — there is "
            "no VM disk to migrate onto (the data dir is already host-native)."
        )

    # Unlocked journal peek (same pattern as upgrades): a run interrupted
    # AFTER the config flip resumes with data_dir_in_vm already true, so
    # only refuse when there is no migration in flight to resume. This is
    # also the guard that stops a re-run from ever "copying backwards":
    # with the flag set and no journal, there is nothing to do.
    peek = Journal.find_resumable(cfg.paths.host_data_dir, MIGRATE_OP, cfg.vm.name)
    if peek is None and cfg.paths.data_dir_in_vm:
        raise UserError(
            "data_dir_in_vm is already enabled — the agent state already lives "
            "on the VM disk; nothing to migrate."
        )

    if not vm_mod._vm_running(cfg.vm.name):
        raise UserError(f"VM {cfg.vm.name!r} is not running — run `agents vm start` first.")

    # AFTER the VM-running check (the mountpoint probe needs the VM):
    # cross-check the journal's completed phases against observed state.
    # A user who followed the printed rollback instructions leaves a
    # journal that says config_flipped/mount_detached while reality says
    # otherwise — resuming from it would skip every mutation phase and
    # report success without migrating anything.
    peek = _reconcile_journal(cfg, peek)

    src_root = str(cfg.paths.vm_agents_root)
    staging = f"{src_root}.migrating"

    # Disk headroom preflight — only meaningful while the copy is still
    # pending (afterwards the space is already consumed, and the mount
    # the `du` would walk may already be detached).
    if peek is None or not peek.is_phase_done("data_copied"):
        _check_disk_headroom(cfg)

    doc = compose.read_compose_doc(cfg)
    agent_specs: list[tuple[str, str]] = []
    for svc in doc.get("services") or {}:
        agent_type = _agent_type_from_service(svc)
        if agent_type is None:
            continue
        agent_specs.append((_name_from_service(svc, agent_type), agent_type))

    archives: list[Path] = []

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Re-find AND re-reconcile under the lock (mirrors upgrades'
        # relock pattern — the unlocked peek is only the fast path).
        journal = _reconcile_journal(
            cfg, Journal.find_resumable(cfg.paths.host_data_dir, MIGRATE_OP, cfg.vm.name)
        ) or Journal.begin(
            cfg.paths.host_data_dir,
            MIGRATE_OP,
            cfg.vm.name,
            MIGRATE_PHASES,
            meta={
                "host_data_dir": str(cfg.paths.host_data_dir),
                "vm_data_root": src_root,
            },
        )
        if journal.completed_phases:
            ui.progress(
                "resuming interrupted migrate-data "
                f"(already done: {', '.join(journal.completed_phases)})"
            )

        # Safety net FIRST: per-agent export archives to <data-dir>/backups/.
        # These are the user's recovery path (doctor-verified via `agents
        # import`) and, being host-side, the last state duplicity sees
        # before live state moves into the VM.
        if not journal.is_phase_done("exports_taken"):
            for name, _ in agent_specs:
                ui.progress(f"safety export: archiving {name!r} to <data-dir>/backups/ ...")
                archive = export_cmd.export(name, config_path=config_path, emit=False)
                ui.progress(f"safety export: {name!r} -> {archive}")
                archives.append(archive)
            journal.meta["exports"] = [str(p) for p in archives]
            journal.advance("exports_taken")
        else:
            archives = [Path(p) for p in journal.meta.get("exports", [])]

        names = ", ".join(n for n, _ in agent_specs) or "(none)"
        if not yes and not ui.confirm(
            f"migrate the data dir onto the VM disk? Plan:\n"
            f"  - STOP every agent + sidecar container "
            f"(downtime until the migration completes): {names}\n"
            f"  - copy everything under {cfg.paths.host_data_dir} "
            f"(via its VM mount) onto the VM disk\n"
            f"  - detach the host mount and flip [paths] data_dir_in_vm = true\n"
            f"  - restart all services from the VM-local copy and require a green doctor\n"
            f"The host data dir is NOT modified — it stays byte-for-byte in place "
            f"until YOU manually archive/delete it after verifying the migration.\n"
            f"Proceed?",
            default=False,
        ):
            if not journal.is_phase_done("services_stopped"):
                # Nothing was mutated yet — the exports above are harmless
                # extra archives. Drop the journal so a later run starts
                # fresh (with fresher safety exports).
                journal.complete()
                raise UserError("aborted by user")
            raise UserError(
                "aborted by user — a previous run already stopped your agents; "
                "re-run `agents vm migrate-data` to resume, or abandon via "
                "`agents vm start` + `agents start <name>` per agent."
            )

        mutation_started = journal.is_phase_done("services_stopped")
        # "ran": False only when a resumed run already passed doctor
        # before the crash (tiny window between advance and complete).
        doctor_summary: dict[str, Any] = {"ran": False}
        try:
            if not journal.is_phase_done("services_stopped"):
                mutation_started = True
                ui.progress("stopping every agent + sidecar container (docker compose stop)...")
                compose_runner.stop(cfg)
                journal.advance("services_stopped")

            if not journal.is_phase_done("data_copied"):
                ui.progress(
                    f"copying {src_root} (host mount) -> {staging} (VM disk) — "
                    f"can take minutes for large data dirs..."
                )
                mp.exec_bash(cfg.vm.name, _copy_script(src_root, staging))
                journal.advance("data_copied")

            if not journal.is_phase_done("mount_detached"):
                ui.progress(f"detaching the host-dir mount at {src_root}...")
                _detach_data_mount(cfg)
                journal.advance("mount_detached")

            if not journal.is_phase_done("data_swapped"):
                ui.progress(f"moving the VM-side copy into place at {src_root}...")
                mp.exec_bash(cfg.vm.name, _swap_script(src_root, staging))
                journal.advance("data_swapped")

            if not journal.is_phase_done("config_flipped"):
                ui.progress(f"flipping config: [paths] data_dir_in_vm = true ({config_path})")
                # Mirrors vm.init's --data-dir-in-vm branch exactly: the
                # flag flips, vm_agents_root stays put (the swap above put
                # the data at the very path init would have seeded).
                cfg.paths.data_dir_in_vm = True
                cfg_mod.save(cfg, config_path)
                journal.advance("config_flipped")

            # Re-load like vm.rebuild does after its init — everything
            # below must see the flipped mode.
            cfg = cfg_mod.load(config_path)

            if not journal.is_phase_done("services_started"):
                ui.progress("starting every agent + sidecar from the VM-local data dir...")
                compose_runner.up(cfg)
                journal.advance("services_started")

            if not journal.is_phase_done("doctor_passed"):
                ui.progress("running doctor across the migrated deployment...")
                # emit=False: doctor's table renders to stdout and would
                # break this command's one-object contract under --json.
                # The report is folded into the final payload instead; a
                # red doctor still raises HealthCheckFailure into the
                # recovery-hints handler below.
                report = ops.doctor(target=None, config_path=config_path, emit=False)
                doctor_summary = {
                    "ran": True,
                    "checks": len(report.checks),
                    "non_ok": [
                        {"check": c.name, "status": c.status, "detail": c.detail}
                        for c in report.checks
                        if c.status != "ok"
                    ],
                }
                journal.advance("doctor_passed")
        except BaseException as failure:
            # The journal stays for resume/diagnosis; the host data was
            # never touched, so print exactly how to flip back.
            if mutation_started:
                _print_recovery_hints(cfg, journal, agent_specs, config_path, failure)
            raise

        journal.complete()

    stale = _host_paths_superseded(cfg, agent_specs)
    export_lines = "\n".join(f"    {p}" for p in archives) or "    (no agents to export)"
    stale_lines = "\n".join(f"    {p}" for p in stale) or "    (none found)"
    non_ok = doctor_summary.get("non_ok") or []
    doctor_line = (
        f"  doctor: {doctor_summary.get('checks', 0)} checks, "
        f"{len(non_ok)} non-ok"
        + (f" ({', '.join(str(c['check']) for c in non_ok)})" if non_ok else "")
        if doctor_summary.get("ran")
        else "  doctor: already green from the interrupted run"
    )
    ui.emit(
        {
            "migrated": True,
            "vm": cfg.vm.name,
            "vm_data_root": src_root,
            "host_data_dir": str(cfg.paths.host_data_dir),
            "exports": [str(p) for p in archives],
            "host_paths_safe_to_archive": [str(p) for p in stale],
            "doctor": doctor_summary,
        },
        human=(
            f"data-dir migration complete.\n"
            f"  agent state now lives on the VM disk at {src_root} "
            f"(data_dir_in_vm = true).\n"
            f"{doctor_line}\n"
            f"  safety exports (your restore path — `agents import <name> <archive>`):\n"
            f"{export_lines}\n"
            f"  host paths now superseded by the VM copy — safe to archive/delete "
            f"MANUALLY\n"
            f"  once you have verified operation for a few days "
            f"(the toolkit will NEVER delete them):\n"
            f"{stale_lines}\n"
            f"  keep (still live host-side metadata):\n"
            f"    docker-compose.override.yml — compose only merges the override "
            f"while the HOST copy exists;\n"
            f"      deleting it drops every `agents mount` volume from future "
            f"compose calls\n"
            f"    <agent>/.mounts.json — `agents mount` reads its manifest "
            f"host-side in both modes\n"
            f"    .codex/ (live Codex auth home), .env, .firecrawl-state.json, "
            f".memory-state/,\n"
            f"    backups/ and .upgrades/\n"
            f"  IMPORTANT: live agent state is no longer host-visible. Install the "
            f"export cron so\n"
            f"  your host backup (duplicity) keeps seeing fresh archives:\n"
            f"    agents export-cron install --time 02:30,08:30,14:30,20:30"
        ),
    )


# ---------- phase helpers ----------


def _reconcile_journal(cfg: AgentsConfig, journal: Journal | None) -> Journal | None:
    """Drop a journal whose completed phases contradict observed state.

    The failure-path instructions tell the user to roll back by flipping
    ``data_dir_in_vm`` back and remounting via ``agents vm start`` —
    that leaves the journal claiming ``config_flipped`` (and/or
    ``mount_detached``) while reality says otherwise. Resuming from it
    would skip every mutation phase, run doctor against the *host-
    mounted* deployment, and report a successful migration that never
    happened. On divergence the journal is stale: warn, delete it, and
    start fresh — safe because the host source is untouched and every
    phase (exports, tar copy) is idempotent; the swap's move-aside
    fallback handles the previous attempt's leftover VM-local copy.
    """
    if journal is None:
        return None
    stale_reason: str | None = None
    if journal.is_phase_done("config_flipped") and not cfg.paths.data_dir_in_vm:
        stale_reason = (
            "it marks config_flipped done but [paths] data_dir_in_vm is false — "
            "the printed rollback was applied after a failure"
        )
    elif journal.is_phase_done("mount_detached") and _root_is_mountpoint(cfg):
        stale_reason = (
            f"it marks mount_detached done but the host share is mounted at "
            f"{cfg.paths.vm_agents_root} again — `agents vm start` re-added it"
        )
    if stale_reason is None:
        return journal
    ui.warn(
        f"discarding stale migrate-data journal ({stale_reason}). Starting the "
        f"migration over from scratch — safe: the host data dir is never modified "
        f"and every phase is idempotent."
    )
    journal.complete()
    return None


def _copy_script(src_root: str, staging: str) -> str:
    """VM-side copy: mount → ext4 staging sibling, minus ``_COPY_EXCLUDES``.

    A root ``tar -C <src> --exclude=./<entry> ... -cf - . | tar -C
    <staging> -xpf -`` pipeline instead of a bare ``cp -a``: exclusions
    apply BEFORE traversal, which is a correctness requirement, not an
    optimization — on libvirt the source contains the running VM's own
    qcow2 under ``.disks/``, and a copy-then-prune would recursively
    copy the VM's disk into itself (unbounded growth on that very
    disk, and an inconsistent image). ``--exclude=./<name>`` matches
    the top-level member names ``tar -C <src> ... .`` produces, and an
    excluded directory is never descended into.

    Extraction runs as root, so ownership (``--same-owner``) and modes
    (``-p``) are preserved onto ext4 — same fidelity as ``cp -a``, and
    idempotent on re-run (extraction overwrites). ``pipefail`` so a
    failing producer tar can't be masked by a successful consumer. The
    trailing ``mkdir`` mirrors ``vm._seed_vm_data_dir``'s skeleton so
    the migrated layout matches a fresh ``vm init --data-dir-in-vm``.
    """
    excludes = " ".join(f"--exclude=./{entry}" for entry in _COPY_EXCLUDES)
    return (
        f"set -eu\n"
        f"set -o pipefail\n"
        f"sudo mkdir -p {staging}\n"
        f"sudo tar -C {src_root} {excludes} -cf - . | sudo tar -C {staging} -xpf -\n"
        f"sudo mkdir -p {staging}/.codex {staging}/backups {staging}/.upgrades\n"
    )


def _swap_script(src_root: str, staging: str) -> str:
    """Move the staged copy into the canonical root after the mount is gone.

    Guards, in order:

    - refuse outright if the root is still a mountpoint (a swap over a
      live share would shadow-write the wrong filesystem);
    - ``rmdir`` (never ``rm -r``) the leftover mountpoint directory — an
      empty stub after detach. If it is NON-empty, the only way that
      happens is a previous migration attempt whose swap completed and
      was then rolled back per the printed instructions (`agents vm
      start` remounts the share on top, shadowing the old VM-local
      copy; the stale-journal reconciliation restarts such runs from
      scratch). Never delete it — move it aside to
      ``<root>.replaced.<epoch>`` so nothing is ever destroyed;
    - the whole block is skipped when the staging dir is already gone
      (crash between ``mv`` and the journal advance → re-run no-ops).
    """
    return (
        f"set -eu\n"
        f"if mountpoint -q {src_root}; then\n"
        f"  echo 'refusing swap: {src_root} is still a mounted share' >&2\n"
        f"  exit 9\n"
        f"fi\n"
        f"if [ -d {staging} ]; then\n"
        f"  if [ -d {src_root} ]; then\n"
        f"    sudo rmdir {src_root} 2>/dev/null || sudo mv {src_root} {src_root}.replaced.$(date +%s)\n"
        f"  fi\n"
        f"  sudo mv {staging} {src_root}\n"
        f"fi\n"
    )


def _detach_data_mount(cfg: AgentsConfig) -> None:
    """Detach the host-dir share from the VM; raise if it won't go.

    Detaching is mandatory in this design — the swap needs the
    mountpoint free (and its script refuses while anything is mounted
    there). Probe via in-guest ``mountpoint -q`` rather than
    ``multipass info`` because the libvirt backend's ``info_vm`` shim
    doesn't report mounts.

    multipass native (9p) mounts may refuse live changes — mirror
    ``vm._ensure_mount``'s stop → mutate → start dance in that case
    (libvirt's virtiofs detach works ``--live --config``, per its
    backend module, so it never needs the dance).
    """
    if not _root_is_mountpoint(cfg):
        return
    if not _try_umount(cfg) and cfg.vm.backend == "multipass":
        ui.progress(
            f"live umount refused; stopping {cfg.vm.name!r} to detach the "
            f"native mount (mirrors the mount-add dance)..."
        )
        mp.stop_vm(cfg.vm.name)
        try:
            _try_umount(cfg)
        finally:
            ui.progress(f"restarting {cfg.vm.name!r}...")
            mp.start_vm(cfg.vm.name)
    if _root_is_mountpoint(cfg):
        raise RuntimeFailure(
            f"could not detach the data mount at {cfg.paths.vm_agents_root} — "
            f"the copy on the VM disk is intact; re-run `agents vm migrate-data` "
            f"to retry from this phase."
        )


def _try_umount(cfg: AgentsConfig) -> bool:
    """One umount attempt. The multipass backend raises on rc != 0
    (its ``mp()`` helper defaults to check=True) while libvirt returns
    the rc — normalize both to a bool.
    """
    try:
        return mp.umount(cfg.vm.name, str(cfg.paths.vm_agents_root)).returncode == 0
    except RuntimeFailure:
        return False


def _root_is_mountpoint(cfg: AgentsConfig) -> bool:
    result = mp.exec_in_vm(
        cfg.vm.name, "mountpoint", "-q", str(cfg.paths.vm_agents_root), check=False
    )
    return result.returncode == 0


# ---------- preflight ----------


def _check_disk_headroom(cfg: AgentsConfig) -> None:
    """Refuse (with numbers) when the copy won't fit on the VM disk.

    ``sudo du`` because the agents' subuid-owned trees aren't readable
    by the ``ubuntu`` user (same reason export tars as root). ``df -P``
    for stable POSIX columns. Both parsed defensively — an unparseable
    probe is a RuntimeFailure, not a silent pass.
    """
    root = str(cfg.paths.vm_agents_root)
    # Same exclusion list as the copy itself — critically, on libvirt
    # the VM's own qcow2 under .disks/ must not count towards the size
    # (it isn't copied, and counting it would refuse every libvirt
    # migration outright). GNU du matches --exclude patterns containing
    # a "/" against the full traversal name, so absolute patterns pair
    # with the absolute root operand (verified empirically).
    du_excludes = " ".join(f"--exclude={root}/{entry}" for entry in _COPY_EXCLUDES)
    du = mp.exec_bash(cfg.vm.name, f"sudo du -sk {du_excludes} {root}", check=False)
    used_kb = _parse_first_int(du.stdout)
    if du.returncode != 0 or used_kb is None:
        raise RuntimeFailure(
            f"could not measure the data dir size in the VM "
            f"(du rc={du.returncode}): {du.stderr.strip() or du.stdout.strip()!r}"
        )
    parent = posixpath.dirname(root) or "/"
    df = mp.exec_bash(cfg.vm.name, f"df -Pk {parent}", check=False)
    avail_kb = _parse_df_available(df.stdout)
    if df.returncode != 0 or avail_kb is None:
        raise RuntimeFailure(
            f"could not determine free space on the VM filesystem at {parent} "
            f"(df rc={df.returncode}): {df.stderr.strip() or df.stdout.strip()!r}"
        )
    needed_kb = int(used_kb * _DISK_HEADROOM_FACTOR)
    if avail_kb < needed_kb:
        raise UserError(
            f"not enough space on the VM disk: the data dir holds "
            f"{_fmt_gib(used_kb)}, the copy needs ~{_fmt_gib(needed_kb)} "
            f"(size + 20% margin), but the target filesystem has only "
            f"{_fmt_gib(avail_kb)} available. Grow the VM disk "
            f"(`agents vm resize --disk <GB>`) and re-run."
        )


def _parse_first_int(stdout: str) -> int | None:
    """First whitespace-separated token of the first non-empty line, as
    int — the ``du -sk`` size column. None when the shape is off."""
    for line in stdout.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        try:
            return int(tokens[0])
        except ValueError:
            return None
    return None


def _parse_df_available(stdout: str) -> int | None:
    """The Available column (4th) of ``df -Pk``'s data line. POSIX ``-P``
    guarantees one header line + one line per filesystem, no wrapping."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[3])
    except ValueError:
        return None


def _fmt_gib(kb: int) -> str:
    return f"{kb / (1024 * 1024):.1f} GiB"


# ---------- failure guidance / summary ----------


def _print_recovery_hints(
    cfg: AgentsConfig,
    journal: Journal,
    agent_specs: list[tuple[str, str]],
    config_path: Path,
    failure: BaseException,
) -> None:
    """Exact restore steps, printed on any failure after downtime began.

    The host data dir is untouched at every phase, so restoring is
    always "point the toolkit back at it" — but the container lifecycle
    must sandwich the remount:

    - STOP the agents before remounting. On libvirt ``agents vm start``
      live-attaches the virtiofs share without a VM reboot, so a
      container that kept running would keep writing the VM-local copy
      hidden beneath the reattached mount.
    - FORCE-RECREATE (``agents upgrade <name> --recreate``, a real
      snapshot+recreate+doctor cycle at the current pin) after the
      remount, not a plain ``agents start`` — an existing container's
      bind mounts were materialized against the pre-rollback filesystem
      and a plain start on a live-remounted VM can retain that view.
    """
    stops = "\n".join(f"       agents stop {n}" for n, _ in agent_specs) or (
        "       (no agents in compose)"
    )
    recreates = "\n".join(f"       agents upgrade {n} --recreate" for n, _ in agent_specs) or (
        "       (no agents in compose)"
    )
    if journal.is_phase_done("config_flipped"):
        ui.warn(
            f"migrate-data failed after the config flip ({failure}).\n"
            f"Your host data dir was never modified — every byte is still at "
            f"{cfg.paths.host_data_dir}. To roll back to the host-mounted layout:\n"
            f"  1. stop the agents so none keeps writing the VM-local copy:\n{stops}\n"
            f"  2. edit {config_path}: set data_dir_in_vm = false under [paths]\n"
            f"  3. agents vm start   # re-adds + verifies the host mount at "
            f"{cfg.paths.vm_agents_root}\n"
            f"  4. force-recreate each container so its bind mounts point at the "
            f"host data again\n"
            f"     (a plain start is NOT enough after a live remount):\n{recreates}\n"
            f"Or re-run `agents vm migrate-data` to resume from the failed phase.\n"
            f"Run `agents doctor` for the full check report.\n"
            f"Journal kept for resume/diagnosis: {journal.path}"
        )
    else:
        ui.warn(
            f"migrate-data failed before the config flip ({failure}); the config "
            f"still points at the host mount and the host data dir was never "
            f"modified.\n"
            f"  - re-run `agents vm migrate-data` to resume, or\n"
            f"  - to abandon and go back to the host-mounted layout:\n"
            f"    1. stop any agents that are still running:\n{stops}\n"
            f"    2. agents vm start   # re-adds the host mount if it was detached\n"
            f"    3. force-recreate each container so its bind mounts point at the "
            f"host data again:\n{recreates}\n"
            f"Journal kept for resume/diagnosis: {journal.path}"
        )


def _host_paths_superseded(cfg: AgentsConfig, agent_specs: list[tuple[str, str]]) -> list[Path]:
    """Host paths whose contents now live (authoritatively) on the VM disk.

    Every entry was audited against the post-migration readers of
    ``cfg.paths.host_data_dir`` (grep for host-side consumers that stay
    host-side under ``data_dir_in_vm``):

    - ``<agent>/.data`` / ``<agent>/.openclaw`` / ``<agent>/workspace``:
      compose bind-mount sources, relative → resolved against the VM
      project dir after the flip. Listed as the bulk-state *subdirs*,
      never the agent dir wholesale — ``mounts_cmd.manifest_path``
      still reads ``<host>/<agent>/.mounts.json`` in data_dir_in_vm
      mode, so the agent dir itself must survive.
    - ``docker-compose.yml``: ``compose.read_compose_doc`` /
      ``compose_runner`` use the VM copy after the flip.
    - ``firecrawl/`` (postgres bind) and ``models/``: relative compose
      volumes → VM copies are live now.

    Deliberately NOT listed (still live host-side metadata):
    ``docker-compose.override.yml`` — ``compose.override_file_args``
    gates the extra ``-f`` on the HOST copy's existence, so deleting it
    would silently drop every ``agents mount`` volume from all future
    compose calls; ``<agent>/.mounts.json`` (see above); ``.codex``
    (live toolkit CODEX_HOME — ``agents codex login`` writes there and
    syncs into the VM); ``.env`` (project-level key writes still land
    host-side); ``.firecrawl-state.json`` and ``.memory-state/``
    (host-read in both modes); and the host primitives (lock, journals,
    backups/, .upgrades/) which stay in active use.
    """
    host = cfg.paths.host_data_dir
    candidates: list[Path] = []
    for name, agent_type in agent_specs:
        if agent_type == "hermes":
            candidates.append(host / name / ".data")
        else:
            candidates.append(host / name / ".openclaw")
            candidates.append(host / name / "workspace")
    candidates += [
        host / "docker-compose.yml",
        host / "firecrawl",
        host / "models",
    ]
    return [p for p in candidates if p.exists()]
