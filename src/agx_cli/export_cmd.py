"""Export / import / host-cron integration with the user's host backup.

The user's host runs duplicity (or similar) over ``<data-dir>``. The
toolkit doesn't implement its own off-site replication — it just makes
sure each agent's data is a tidy, host-visible archive that the host
backup picks up.

- ``export <name>``: tar the agent's data dir into
  ``<data-dir>/backups/<name>-<utc>.tar.zst``. Under ``--data-dir-in-vm``,
  the tar runs inside the VM and the toolkit ``multipass transfer``s
  the archive out. Default output is the same ``<data-dir>/backups/``
  in either mode; pass ``--to <host-path>`` only when you want a
  different destination.
- ``export --all``: loop.
- ``export-cron install``: install a host cron entry that runs
  ``agx export --all`` at the chosen time. Handles the first-ever
  empty crontab (where ``crontab -l`` exits 1 with "no crontab" on
  stderr). Idempotent — re-running with a new ``--time`` replaces the
  entry rather than appending.
- ``export-cron status``: report whether installed, the scheduled time,
  and the timestamp of the most-recent archive under ``<data-dir>/backups/``.
- ``import <name> <archive>``: extract into the agent's data dir.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from agx_cli import archive as archive_mod
from agx_cli import compose, ui
from agx_cli import config as cfg_mod
from agx_cli.agents_cmd import (
    AGENT_TYPES,
    _detect_type_from_compose,
    validate_name,
)
from agx_cli.config import AgentsConfig
from agx_cli.errors import RuntimeFailure, UserError

CRON_MARKER = "# agents toolkit — managed export-cron entry"


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _archive_dir(cfg: AgentsConfig, to: Path | None) -> Path:
    if to is not None:
        to.mkdir(parents=True, exist_ok=True)
        return to
    target = cfg.paths.host_data_dir / "backups"
    target.mkdir(exist_ok=True)
    return target


# ---------- export ----------


def export(
    name: str,
    *,
    to: Path | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
    emit: bool = True,
) -> Path:
    """Archive one agent's data dir; returns the archive path.

    ``emit=False`` skips the final :func:`ui.emit` so composite
    commands (``vm rebuild``, ``vm destroy --export-first``, ``remove
    --export-first``) can embed an export while still writing exactly
    one JSON object to stdout themselves. Those callers narrate via
    ``ui.progress`` instead.
    """
    validate_name(name)
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")

    # In both modes the default output dir is `<data-dir>/backups/`. Under
    # ``--data-dir-in-vm`` the data dir on the host is mostly empty (the
    # agent state lives inside the VM), but `<data-dir>/backups/` is still
    # a valid host path that the host backup (duplicity etc.) sees; the
    # toolkit ``multipass transfer``s the archive there from the VM.
    archive_name = f"{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"

    if cfg.paths.data_dir_in_vm:
        # Tar+zstd inside the VM, then multipass transfer the archive out.
        from agx_cli import mp

        check = mp.exec_in_vm(
            cfg.vm.name,
            "test",
            "-d",
            f"{cfg.paths.vm_agents_root}/{name}",
            check=False,
        )
        if check.returncode != 0:
            raise UserError(f"agent data dir for {name!r} not found in VM")
        vm_archive = f"/tmp/agents-export-{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"
        # As root, like the host-mount branch below: the agent's state
        # subtree is subuid-owned under mode 700 on the VM disk too, and
        # an unprivileged tar with ``--ignore-failed-read`` skips it
        # without an error — a "successful" archive with no agent data.
        mp.exec_bash(
            cfg.vm.name,
            (
                f"set -eu\n"
                f"cd {cfg.paths.vm_agents_root}\n"
                f"sudo tar --zstd --warning=no-file-changed --ignore-failed-read "
                f"-cf {vm_archive} {name}\n"
                f'sudo chown "$(id -u):$(id -g)" {vm_archive}\n'
            ),
        )
        target_dir = _archive_dir(cfg, to)
        out = target_dir / archive_name
        mp.transfer_from_vm(cfg.vm.name, vm_archive, out)
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_archive, check=False)
    else:
        agent_dir = cfg.paths.host_data_dir / name
        if not agent_dir.exists():
            raise UserError(f"agent data dir for {name!r} not found at {agent_dir}")
        # Host-mount mode: the agent's ``.data/`` subtree is owned by
        # a rootless-Docker subuid (host uid 100999) under mode 700.
        # A host-side ``write_tar_zst([(agent_dir, name)])`` walks the
        # tree as the toolkit user (uid 1000), which can't even ``opendir``
        # ``.data``. Worse, Python's tarfile suppresses per-file errors
        # and writes an archive containing JUST the readable directory
        # stubs — silently produces ~200-byte "successful" tarballs
        # missing every actual file. ``agx export-cron`` has been
        # running 4x/day, faithfully producing useless archives that
        # duplicity then backs up — silent data loss disguised as
        # working backups.
        #
        # Fix: same pattern as ``upgrades._host_data_snapshot`` —
        # tar inside the VM as root (virtiofs passes file ownership
        # through; root can read any uid), chown the archive back to
        # ubuntu so SFTP can transfer it, then transfer out and clean
        # up the VM-side temp.
        from agx_cli import mp

        out = _archive_dir(cfg, to) / archive_name
        vm_archive = f"/tmp/agents-export-{name}-{_utc_stamp()}{archive_mod.ARCHIVE_SUFFIX}"
        # ``--warning=no-file-changed`` demotes GNU tar's rc=1 exit on
        # "file changed as we read it" (the running agent rewriting
        # state.db / kanban.db / WAL files during the walk) to a
        # silent ok — without this, ``set -eu`` aborts before the
        # chown and we get a root-owned archive that SFTP can't read
        # back. ``--ignore-failed-read`` similarly handles files that
        # vanish mid-walk. For crash-consistent backups (duplicity-
        # style) this is the right trade-off; for point-in-time
        # SQLite snapshots use ``sqlite3 .backup`` instead.
        mp.exec_bash(
            cfg.vm.name,
            (
                f"set -eu\n"
                f"cd {cfg.paths.vm_agents_root}\n"
                f"sudo tar --zstd --warning=no-file-changed --ignore-failed-read "
                f"-cf {vm_archive} {name}\n"
                f'sudo chown "$(id -u):$(id -g)" {vm_archive}\n'
            ),
        )
        mp.transfer_from_vm(cfg.vm.name, vm_archive, out)
        mp.exec_in_vm(cfg.vm.name, "rm", "-f", vm_archive, check=False)

    # Auto-prune old archives in the default backups dir. Only fires
    # when the user didn't pass a custom ``--to`` location (custom
    # destinations are user-managed; we never delete from paths the
    # user explicitly named).
    pruned: list[Path] = []
    if to is None:
        pruned = _prune_old_archives(name, out.parent, cfg.export_cron.keep_last)

    payload: dict[str, object] = {
        "name": name,
        "archive": str(out),
        "size_bytes": out.stat().st_size,
    }
    if pruned:
        payload["pruned"] = [str(p) for p in pruned]
    if emit:
        ui.emit(
            payload,
            human=(
                f"exported {name!r} -> {out}"
                + (f"\n  pruned {len(pruned)} older archive(s)" if pruned else "")
            ),
        )
    return out


def _prune_old_archives(agent_name: str, archive_dir: Path, keep_last: int) -> list[Path]:
    """Delete this agent's oldest archives in ``archive_dir``, keeping
    only the newest ``keep_last``. Returns the deleted paths.

    Only files matching the toolkit's own naming pattern
    (``<agent>-<utc>.tar.zst``, plus the legacy ``.tar.gz`` form) are
    candidates — anything else in the dir is left alone (user files,
    other agents' archives, the ``.export-cron.log``).

    Sort is on filename: ISO-8601 UTC timestamps in the format
    ``YYYYMMDDTHHMMSSZ`` sort lexicographically the same as
    chronologically, so we don't need to stat each file.

    ``keep_last == 0`` disables pruning entirely — useful for users
    who want unbounded history and will clean up by hand.
    """
    if keep_last <= 0 or not archive_dir.is_dir():
        return []
    candidates = sorted(
        list(archive_dir.glob(f"{agent_name}-*.tar.zst"))
        + list(archive_dir.glob(f"{agent_name}-*.tar.gz")),
        key=lambda p: p.name,
    )
    if len(candidates) <= keep_last:
        return []
    to_delete = candidates[:-keep_last]
    deleted: list[Path] = []
    for p in to_delete:
        try:
            p.unlink()
            deleted.append(p)
        except OSError as e:
            ui.warn(f"could not delete old archive {p}: {e}")
    return deleted


def export_all(
    *,
    to: Path | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
    emit: bool = True,
) -> list[Path]:
    """Export every agent in compose. One final :func:`ui.emit` summary
    (unless ``emit=False`` — used by the ``vm destroy`` export offer);
    per-agent completion is narrated via ``ui.progress`` on stderr so
    ``--json`` stdout stays a single object.
    """
    cfg = cfg_mod.load(config_path)
    doc = compose.read_compose_doc(cfg)
    archives: list[Path] = []
    for svc in doc.get("services") or {}:
        for at in AGENT_TYPES:
            prefix = f"{at}-"
            if svc.startswith(prefix):
                name = svc[len(prefix) :]
                archive = export(name, to=to, config_path=config_path, emit=False)
                ui.progress(f"exported {name!r} -> {archive}")
                archives.append(archive)
    if emit:
        ui.emit(
            {"exported": [str(p) for p in archives], "count": len(archives)},
            human=(
                "\n".join(f"exported -> {p}" for p in archives)
                if archives
                else "no agents to export"
            ),
        )
    return archives


# ---------- import ----------


def _vm_import_temp_path(name: str) -> str:
    """VM-side temp path for a transferred import archive.

    Built exclusively from safe generated parts: the agent name (already
    ``validate_name``-checked, ``[a-z0-9_-]`` only), the pid, and a uuid4
    hex. The user-supplied archive filename is deliberately excluded —
    both import branches interpolate this path into ``mp.exec_bash``
    scripts (one of them sudo'd), so a hostile filename would be shell
    injection. ``tar -xf`` detects compression by content, so the fixed
    ``.tar`` extension is only cosmetic.
    """
    return f"/tmp/agents-import-{name}-{os.getpid()}-{uuid.uuid4().hex[:8]}.tar"


def import_archive(
    name: str,
    archive_path: Path,
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
    emit: bool = True,
) -> None:
    """Extract an export archive into the agent's data dir.

    ``emit=False`` suppresses the final :func:`ui.emit` for composite
    callers (``vm rebuild``) that own the command's single stdout object.
    """
    validate_name(name)
    cfg = cfg_mod.load(config_path)
    if not archive_path.exists():
        raise UserError(f"archive not found: {archive_path}")

    if cfg.paths.data_dir_in_vm:
        from agx_cli import mp

        check = mp.exec_in_vm(
            cfg.vm.name, "test", "-d", f"{cfg.paths.vm_agents_root}/{name}", check=False
        )
        if (
            check.returncode == 0
            and not yes
            and not ui.confirm(
                f"overwrite existing data dir at {cfg.paths.vm_agents_root}/{name} (in VM)?",
                default=False,
            )
        ):
            raise UserError("aborted by user")
        # tar -xf auto-detects compression by content (zst, gz, etc), so
        # the VM-side temp name needs no real extension. Built ONLY from
        # generated parts (validated name + pid + uuid) — the archive
        # filename is user-supplied and must never reach shell text.
        vm_tmp = _vm_import_temp_path(name)
        mp.transfer_to_vm(archive_path, cfg.vm.name, vm_tmp)
        # As root so the members' subuid ownership is restored (an
        # unprivileged extract lands everything as ``ubuntu``).
        mp.exec_bash(
            cfg.vm.name,
            (
                f"set -eu\n"
                f"cd {cfg.paths.vm_agents_root}\n"
                f"sudo tar -xf {vm_tmp} --numeric-owner\n"
                f"rm -f {vm_tmp}\n"
            ),
        )
        if emit:
            ui.emit(
                {"name": name, "archive": str(archive_path)},
                human=f"imported {archive_path} into {cfg.paths.vm_agents_root}/{name} (in VM)",
            )
        return

    target = cfg.paths.host_data_dir / name
    if (
        target.exists()
        and not yes
        and not ui.confirm(f"overwrite existing data dir at {target}?", default=False)
    ):
        raise UserError("aborted by user")
    # Extract inside the VM as root for the symmetric reason export
    # tars there: the archive members carry their original
    # ownership (rootless-Docker subuid 100999, mode 700). The host
    # user can't chown to that uid (out of its subuid range without
    # CAP_SETUID) and a host-side ``tarfile.extractall`` either
    # silently strips ownership (landing files as uid 1000, which
    # the container would then EACCES on read) or fails outright on
    # symlinks pointing inside the subuid-owned subtree. Root
    # inside the VM has none of these problems — virtiofs passes
    # the ownership back through to the host, and the container
    # sees its own files unchanged.
    from agx_cli import mp

    # Same rule as the --data-dir-in-vm branch: never interpolate the
    # user-supplied archive filename into the (sudo'd) shell script.
    vm_archive = _vm_import_temp_path(name)
    mp.transfer_to_vm(archive_path, cfg.vm.name, vm_archive)
    mp.exec_bash(
        cfg.vm.name,
        (
            f"set -eu\n"
            f"cd {cfg.paths.vm_agents_root}\n"
            f"sudo tar -xf {vm_archive}\n"
            f"rm -f {vm_archive}\n"
        ),
    )
    _surface_imported_mounts_metadata(cfg, name)
    if emit:
        ui.emit(
            {"name": name, "archive": str(archive_path)},
            human=f"imported {archive_path} into {target}",
        )


def _surface_imported_mounts_metadata(cfg: AgentsConfig, name: str) -> None:
    """The archive may carry the source's ``.mounts.json``. Per the
    design decision (issue #5 follow-up): we keep the file as metadata
    but DO NOT re-mount on this host — host paths are likely different
    here, and re-running ``multipass mount`` for stale paths would
    fail mid-import. Just surface the list so the user knows what to
    re-add manually.
    """
    from agx_cli import mounts_cmd

    entries = mounts_cmd.load_manifest(cfg, name)
    if not entries:
        return
    lines = ["the imported archive declares these host mounts (NOT re-applied here):"]
    for e in entries:
        lines.append(
            f"  agx mount add {name} --host {e.host_path} "
            f"--container {e.container_path}"
            + ("" if e.readonly else " --rw")
            + (f" --target {e.target}" if e.target != "both" else "")
        )
    lines.append("Re-add the ones that still apply on this host.")
    ui.warn("\n".join(lines))
    # Drop the manifest so the surfaced commands above are the
    # source of truth — keeping a stale .mounts.json that doesn't
    # match the actual compose-override state would mislead
    # subsequent `agx mount list` / `agx remove`.
    mounts_cmd.manifest_path(cfg, name).unlink(missing_ok=True)


# ---------- host-cron integration ----------


def export_cron_install(
    *,
    time: str = "02:30",
    binary: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Install host cron entries that run `agx export --all` at one
    or more times each day.

    ``time`` accepts a single HH:MM (`"02:30"`) or a comma-separated
    list (`"02:30,08:30,14:30,20:30"`) for multi-times-a-day backups.
    One crontab line per time; all share a single marker so uninstall
    removes them together.

    Idempotent: re-running replaces any existing toolkit-managed
    entries with the new schedule.
    """
    cfg = cfg_mod.load(config_path)  # validates config first
    if binary is None:
        binary = shutil.which("agx") or sys.argv[0]
    times = [t.strip() for t in time.split(",") if t.strip()]
    if not times:
        raise UserError("at least one HH:MM time is required")
    lines = []
    for t in times:
        hh, mm = _parse_hhmm(t)
        lines.append(
            f"{mm} {hh} * * * {binary} --yes export --all "
            f">> {cfg.paths.host_data_dir}/.export-cron.log 2>&1 {CRON_MARKER}"
        )

    existing = _read_crontab()
    filtered = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
    new = "\n".join([*filtered, *lines]).strip() + "\n"
    _write_crontab(new)

    cfg.export_cron.enabled = True
    cfg.export_cron.time = ",".join(times)
    cfg_mod.save(cfg, config_path)

    ui.emit(
        {"installed": True, "times": times, "lines": lines},
        human=(
            f"export-cron installed: {len(times)} daily entr"
            f"{'y' if len(times) == 1 else 'ies'} at {', '.join(times)}"
        ),
    )


def export_cron_uninstall(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    cfg = cfg_mod.load(config_path)
    existing = _read_crontab()
    filtered = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
    new = "\n".join(filtered).strip()
    new = (new + "\n") if new else ""
    _write_crontab(new)
    cfg.export_cron.enabled = False
    cfg_mod.save(cfg, config_path)
    ui.emit({"installed": False}, human="export-cron uninstalled")


def export_cron_status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, object]:
    cfg = cfg_mod.load(config_path)
    existing = _read_crontab()
    installed = any(CRON_MARKER in ln for ln in existing.splitlines())
    backups_dir = cfg.paths.host_data_dir / "backups"
    archives = (
        sorted(
            list(backups_dir.glob(f"*{archive_mod.ARCHIVE_SUFFIX}"))
            + list(backups_dir.glob("*.tar.gz"))
        )
        if backups_dir.exists()
        else []
    )
    last_archive = archives[-1] if archives else None
    info: dict[str, object] = {
        "installed": installed,
        "scheduled_time": cfg.export_cron.time if installed else None,
        "archives_count": len(archives),
        "last_archive": str(last_archive) if last_archive else None,
        "last_archive_mtime": (
            datetime.datetime.fromtimestamp(last_archive.stat().st_mtime, datetime.UTC).isoformat()
            if last_archive
            else None
        ),
    }
    ui.emit(
        info,
        human=(
            f"installed: {info['installed']}\n"
            f"scheduled: {info['scheduled_time'] or '(not installed)'}\n"
            f"archives:  {info['archives_count']}\n"
            f"latest:    {info['last_archive'] or '(none)'}"
        ),
    )
    return info


# ---------- crontab helpers ----------


def _read_crontab() -> str:
    """Read the current user's crontab. Handles the first-ever empty case
    where ``crontab -l`` exits 1 with ``no crontab for ...`` on stderr.
    """
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, check=False)
    except FileNotFoundError as e:
        raise RuntimeFailure("crontab binary not found on host") from e
    if result.returncode == 0:
        return result.stdout.decode("utf-8", errors="replace")
    err = result.stderr.decode("utf-8", errors="replace")
    if "no crontab" in err.lower():
        return ""
    raise RuntimeFailure(f"crontab -l failed (rc={result.returncode}): {err.strip()}")


def _write_crontab(content: str) -> None:
    """Pipe ``content`` into ``crontab -``."""
    try:
        proc = subprocess.run(
            ["crontab", "-"],
            input=content.encode("utf-8"),
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeFailure("crontab binary not found on host") from e
    if proc.returncode != 0:
        raise RuntimeFailure(
            f"crontab update failed: {proc.stderr.decode('utf-8', errors='replace').strip()}"
        )


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts):
        raise UserError(f"time must be HH:MM (zero-padded), got {value!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise UserError(f"time out of range: {value!r}")
    return hh, mm
