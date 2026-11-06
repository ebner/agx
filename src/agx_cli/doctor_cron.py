"""``agx doctor-cron {install,uninstall,status,run}`` — host cron that
runs ``agx doctor`` and mails on STATE CHANGE.

Why: the failures that matter most are the ones you only discover when
you need the agent — the canonical case is a Hermes agent whose
ChatGPT-subscription Codex OAuth chain died overnight ("Provider
authentication failed"). There is no reliable pre-warning for that
class (see ``ops._check_hermes_credential_pool``), so the next best
thing is prompt detection: poll doctor every N minutes and notify
within one interval.

Design:

- One toolkit-managed crontab line (marker-tagged like ``export-cron``)
  running ``agx --yes doctor-cron run`` every ``every_minutes``.
- ``run`` executes the full doctor (``ops.doctor(emit=False)``), reduces
  it to the set of failing checks, and compares with the previous run's
  set persisted at ``<data-dir>/.doctor-cron/state.json``. Mail is sent
  only when that set changes: new failures ⇒ a "failing" mail listing
  every failing check with its detail; failures gone ⇒ one "recovered"
  mail. A persistently red setup therefore produces exactly one mail,
  not one per interval. Warnings are reported in the body for context
  but never trigger a mail on their own.
- Upstream releases: at most every ``update_check_hours`` (default 24,
  0 = off) ``run`` also mails, once per new release per agent, which
  agents can be upgraded, with condensed release notes
  (``update_notify``). Separate mail, separate state keys.
- Mail goes through the host's ``sendmail -t`` (postfix/msmtp/…) —
  the toolkit carries no SMTP configuration. ``install`` refuses when
  no ``sendmail`` binary is on PATH so a silent no-op can't be
  installed.
- ``run`` never exits non-zero for a red doctor (cron would otherwise
  mail the raw output through its own MAILTO path); it exits non-zero
  only when the run itself could not complete (config missing, mail
  delivery failed) so the ``.doctor-cron.log`` shows it.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import shutil
import subprocess
import sys
import textwrap
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from agx_cli import config as cfg_mod
from agx_cli import ui
from agx_cli.errors import RuntimeFailure, UserError

CRON_MARKER = "# agents toolkit — managed doctor-cron entry"
STATE_DIRNAME = ".doctor-cron"
LOG_FILENAME = ".doctor-cron.log"


# ---------- install / uninstall / status ----------


def install(
    *,
    every_minutes: int = 15,
    mail_to: list[str] | None = None,
    update_check_hours: int | None = None,
    binary: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Install (or replace) the toolkit-managed doctor-cron entry.

    ``update_check_hours`` ``None`` keeps the configured value."""
    from agx_cli.export_cmd import _read_crontab, _write_crontab

    cfg = cfg_mod.load(config_path)
    recipients = list(mail_to) if mail_to else list(cfg.doctor_cron.mail_to)
    if not recipients:
        raise UserError("at least one --mail recipient is required")
    # Validate through the config model so a bad address / interval is
    # rejected before anything lands in the crontab.
    if update_check_hours is None:
        update_check_hours = cfg.doctor_cron.update_check_hours
    cfg.doctor_cron = cfg_mod.DoctorCronConfig(
        enabled=True,
        every_minutes=every_minutes,
        mail_to=recipients,
        update_check_hours=update_check_hours,
    )
    if _sendmail_path() is None:
        raise UserError(
            "no `sendmail` binary on PATH — doctor-cron mails through the host "
            "MTA (postfix, msmtp, …); install/configure one first"
        )
    if binary is None:
        binary = shutil.which("agx") or sys.argv[0]

    line = (
        f"*/{every_minutes} * * * * {binary} --yes doctor-cron run "
        f">> {cfg.paths.host_data_dir}/{LOG_FILENAME} 2>&1 {CRON_MARKER}"
    )
    existing = _read_crontab()
    filtered = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
    _write_crontab("\n".join([*filtered, line]).strip() + "\n")
    cfg_mod.save(cfg, config_path)

    ui.emit(
        {
            "installed": True,
            "every_minutes": every_minutes,
            "mail_to": recipients,
            "update_check_hours": update_check_hours,
            "line": line,
        },
        human=(
            f"doctor-cron installed: every {every_minutes} min, "
            f"mail on state change to {', '.join(recipients)}; "
            + (
                f"upstream release check every {update_check_hours} h"
                if update_check_hours
                else "upstream release check off"
            )
        ),
    )


def uninstall(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    from agx_cli.export_cmd import _read_crontab, _write_crontab

    cfg = cfg_mod.load(config_path)
    existing = _read_crontab()
    filtered = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
    new = "\n".join(filtered).strip()
    _write_crontab((new + "\n") if new else "")
    cfg.doctor_cron.enabled = False
    cfg_mod.save(cfg, config_path)
    ui.emit({"installed": False}, human="doctor-cron uninstalled")


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    from agx_cli.export_cmd import _read_crontab

    cfg = cfg_mod.load(config_path)
    installed = any(CRON_MARKER in ln for ln in _read_crontab().splitlines())
    state = _read_state(cfg)
    info: dict[str, Any] = {
        "installed": installed,
        "every_minutes": cfg.doctor_cron.every_minutes if installed else None,
        "mail_to": cfg.doctor_cron.mail_to,
        "last_run": state.get("checked_at"),
        "failing": state.get("failing", []),
        "last_notified": state.get("notified_at"),
        "update_check_hours": cfg.doctor_cron.update_check_hours,
        "updates_checked_at": state.get("updates_checked_at"),
        "updates_notified": state.get("updates_notified") or {},
    }
    failing = info["failing"]
    ui.emit(
        info,
        human=(
            f"installed: {installed}\n"
            f"every:     {info['every_minutes'] or '(not installed)'} min\n"
            f"mail to:   {', '.join(cfg.doctor_cron.mail_to) or '(none)'}\n"
            f"last run:  {info['last_run'] or '(never)'}\n"
            f"failing:   {', '.join(failing) if failing else '(none)'}\n"
            f"notified:  {info['last_notified'] or '(never)'}\n"
            f"updates:   "
            + (
                f"checked every {info['update_check_hours']} h, last "
                f"{info['updates_checked_at'] or '(never)'}"
                if info["update_check_hours"]
                else "check off"
            )
            + "".join(
                f"\n  {agent}: mailed about {tag}"
                for agent, tag in sorted(info["updates_notified"].items())
            )
        ),
    )
    return info


# ---------- run ----------


def run(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """One poll: doctor → diff against the last state → mail on change."""
    from agx_cli import ops

    cfg = cfg_mod.load(config_path)
    report = ops.doctor(config_path=config_path, emit=False, raise_on_fail=False)

    failing = {c.name: c.detail for c in report.checks if c.status == "fail"}
    warning = {c.name: c.detail for c in report.checks if c.status == "warn"}
    now_dt = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    now = now_dt.isoformat()

    previous = _read_state(cfg)
    prev_failing = set(previous.get("failing", []))
    changed = set(failing) != prev_failing

    notified = False
    if changed and cfg.doctor_cron.mail_to:
        subject = _mail_subject(failing)
        body = _mail_body(cfg, failing=failing, warning=warning, previous=prev_failing, when=now)
        _send_mail(cfg.doctor_cron.mail_to, subject, body)
        notified = True

    state = {
        "checked_at": now,
        "failing": sorted(failing),
        "notified_at": now if notified else previous.get("notified_at"),
        "updates_checked_at": previous.get("updates_checked_at"),
        "updates_notified": previous.get("updates_notified") or {},
    }
    # Doctor state is durable before the (network-bound) update check.
    _write_state(cfg, state)
    update_mailed = _check_updates(cfg, state, now_dt)

    result = {
        "status": "fail" if failing else "ok",
        "failing": sorted(failing),
        "warning": sorted(warning),
        "changed": changed,
        "notified": notified,
        "update_notified": update_mailed,
    }
    ui.emit(
        result,
        human=(
            f"doctor-cron: {result['status']} "
            f"(failing={len(failing)}, warn={len(warning)}, "
            f"{'mailed' if notified else 'no change'})"
            + (f"; update mail for {', '.join(update_mailed)}" if update_mailed else "")
        ),
    )
    return result


def _check_updates(
    cfg: cfg_mod.AgentsConfig, state: dict[str, Any], now: datetime.datetime
) -> list[str]:
    """Mail newly available upstream releases (see ``update_notify``);
    returns the agents mailed about. At most one check per
    ``update_check_hours``; an incomplete check (GitHub unreachable for
    some agent type) is retried on the next run, and the per-agent
    ``updates_notified`` tag keeps any retry from mailing twice."""
    from agx_cli import update_notify

    hours = cfg.doctor_cron.update_check_hours
    if hours == 0 or not cfg.doctor_cron.mail_to:
        return []
    last = _parse_time(state.get("updates_checked_at"))
    if last is not None and now - last < datetime.timedelta(hours=hours):
        return []

    notified: dict[str, str] = dict(state.get("updates_notified") or {})
    updates, reached = update_notify.check(cfg, notified)
    mailed: list[str] = []
    if updates:
        _send_mail(
            cfg.doctor_cron.mail_to,
            update_notify.mail_subject(updates),
            update_notify.mail_body(cfg, updates, now.isoformat()),
        )
        for update in updates:
            for agent in update.agents:
                notified[agent] = update.latest
                mailed.append(agent)
    if reached:
        state["updates_checked_at"] = now.isoformat()
    state["updates_notified"] = notified
    _write_state(cfg, state)
    return sorted(mailed)


def _parse_time(value: Any) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ---------- helpers ----------


def _state_path(cfg: cfg_mod.AgentsConfig) -> Path:
    return cfg.paths.host_data_dir / STATE_DIRNAME / "state.json"


def _read_state(cfg: cfg_mod.AgentsConfig) -> dict[str, Any]:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(cfg: cfg_mod.AgentsConfig, state: dict[str, Any]) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


MAIL_WIDTH = 76


def _mail_subject(failing: dict[str, str]) -> str:
    """ASCII-only (no RFC 2047 encoding) and names the failing checks."""
    if not failing:
        return "[agents] doctor: recovered - all checks green"
    names = sorted(failing)
    shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
    return f"[agents] doctor: {len(names)} check(s) failing ({shown})"


def _mail_body(
    cfg: cfg_mod.AgentsConfig,
    *,
    failing: dict[str, str],
    warning: dict[str, str],
    previous: set[str],
    when: str,
) -> str:
    """Plain-text body: one titled section per state, one block per check,
    detail wrapped below the check name (multi-line details keep their
    ``- item`` lines, each wrapped with a hanging indent)."""
    lines = [
        f"agx doctor on VM {cfg.vm.name}",
        f"data dir: {cfg.paths.host_data_dir}",
        f"checked:  {when}",
    ]

    def section(title: str, checks: dict[str, str]) -> None:
        lines.extend(["", title, "=" * len(title)])
        for name, detail in sorted(checks.items()):
            lines.extend(["", name])
            for para in (detail or "").splitlines():
                lines.extend(
                    textwrap.wrap(
                        para,
                        width=MAIL_WIDTH,
                        initial_indent="    ",
                        subsequent_indent="      " if para.startswith("- ") else "    ",
                        break_on_hyphens=False,
                    )
                )

    if failing:
        section(f"FAILING ({len(failing)})", failing)
    else:
        lines.extend(["", "All checks green."])
    recovered = sorted(previous - set(failing))
    if recovered:
        section(f"RECOVERED since last notice ({len(recovered)})", dict.fromkeys(recovered, ""))
    if warning:
        section(f"Warnings ({len(warning)}, informational: never mailed on their own)", warning)
    lines.extend(["", "-- ", "Full report: agx doctor"])
    return "\n".join(lines) + "\n"


def _sendmail_path() -> str | None:
    return shutil.which("sendmail") or (
        "/usr/sbin/sendmail" if Path("/usr/sbin/sendmail").exists() else None
    )


def _send_mail(recipients: list[str], subject: str, body: str) -> None:
    """Deliver via the host MTA's ``sendmail -t`` (headers in the message)."""
    sendmail = _sendmail_path()
    if sendmail is None:
        raise RuntimeFailure("no `sendmail` binary on PATH; cannot deliver doctor-cron mail")
    # EmailMessage adds MIME-Version + a Content-Transfer-Encoding that
    # matches the body (the hand-built header block declared neither).
    message = EmailMessage()
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message.set_content(body, charset="utf-8")
    proc = subprocess.run(
        [sendmail, "-t", "-oi"],
        input=message.as_bytes(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeFailure(
            f"sendmail failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
