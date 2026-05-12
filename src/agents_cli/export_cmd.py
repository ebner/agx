"""Export / import / host-cron integration with the user's host backup.

The user's host runs duplicity (or similar) over ``<data-dir>``. The
toolkit doesn't implement its own off-site replication — it just makes
sure each agent's data is a tidy, host-visible archive that the host
backup picks up.

- ``export <name>``: tar the agent's data dir into
  ``<data-dir>/backups/<name>-<utc>.tar.gz``. Under ``--data-dir-in-vm``,
  ``--to <host-path>`` is required and the toolkit ``multipass transfer``s
  the archive out.
- ``export --all``: loop.
- ``export-cron install``: install a host cron entry that runs
  ``agents export --all`` at the chosen time. Handles the first-ever
  empty crontab (where ``crontab -l`` exits 1 with "no crontab" on
  stderr). Idempotent — re-running with a new ``--time`` replaces the
  entry rather than appending.
- ``export-cron status``: report whether installed, the scheduled time,
  and the timestamp of the most-recent archive under ``<data-dir>/backups/``.
- ``import <name> <archive>``: extract into the agent's data dir.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from agents_cli import compose, ui
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import (
    AGENT_TYPES,
    _detect_type_from_compose,
    validate_name,
)
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError

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
) -> Path:
    validate_name(name)
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")

    if cfg.paths.data_dir_in_vm and to is None:
        raise UserError("--to <host-path> is required when running under --data-dir-in-vm")

    agent_dir = cfg.paths.host_data_dir / name
    if not agent_dir.exists():
        raise UserError(f"agent data dir for {name!r} not found at {agent_dir}")

    archive = _archive_dir(cfg, to) / f"{name}-{_utc_stamp()}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(agent_dir, arcname=name)

    ui.emit(
        {"name": name, "archive": str(archive), "size_bytes": archive.stat().st_size},
        human=f"exported {name!r} -> {archive}",
    )
    return archive


def export_all(
    *,
    to: Path | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> list[Path]:
    cfg = cfg_mod.load(config_path)
    doc = compose.read_compose_doc(cfg)
    archives: list[Path] = []
    for svc in doc.get("services") or {}:
        for at in AGENT_TYPES:
            prefix = f"{at}-"
            if svc.startswith(prefix):
                archives.append(export(svc[len(prefix) :], to=to, config_path=config_path))
    return archives


# ---------- import ----------


def import_archive(
    name: str,
    archive: Path,
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    validate_name(name)
    cfg = cfg_mod.load(config_path)
    if not archive.exists():
        raise UserError(f"archive not found: {archive}")
    target = cfg.paths.host_data_dir / name
    if (
        target.exists()
        and not yes
        and not ui.confirm(f"overwrite existing data dir at {target}?", default=False)
    ):
        raise UserError("aborted by user")
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(cfg.paths.host_data_dir, filter="data")
    ui.emit({"name": name, "archive": str(archive)}, human=f"imported {archive} into {target}")


# ---------- host-cron integration ----------


def export_cron_install(
    *,
    time: str = "02:30",
    binary: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Install a host cron entry that runs `agents export --all` daily.

    Idempotent: re-running replaces any existing toolkit-managed entry.
    The binary defaults to the current python's installed `agents` script.
    """
    cfg = cfg_mod.load(config_path)  # validates config first
    if binary is None:
        binary = shutil.which("agents") or sys.argv[0]
    hh, mm = _parse_hhmm(time)
    line = (
        f"{mm} {hh} * * * {binary} --yes export --all "
        f">> {cfg.paths.host_data_dir}/.export-cron.log 2>&1 {CRON_MARKER}"
    )

    existing = _read_crontab()
    filtered = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
    new = "\n".join([*filtered, line]).strip() + "\n"
    _write_crontab(new)

    # Update config (record installed state + chosen time)
    cfg.export_cron.enabled = True
    cfg.export_cron.time = time
    cfg_mod.save(cfg, config_path)

    ui.emit(
        {"installed": True, "time": time, "line": line},
        human=f"export-cron installed: daily at {time}",
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
    archives = sorted(backups_dir.glob("*.tar.gz")) if backups_dir.exists() else []
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
