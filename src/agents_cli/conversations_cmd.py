"""``agents conversations``: stats + transcript-export for a Hermes agent.

Two subcommands:

- ``status <name>`` — counts active / open / total sessions, per-platform
  breakdown, top-N active table. "Active" means within the
  ``agent.gateway_auto_continue_freshness`` window (the gateway keeps
  these hot; older open sessions are cold and trigger context
  recompression on next message).
- ``export <name> --to <dir>`` — writes one Markdown transcript per
  session under ``<dir>/<session_id>.md``. Used for migration / archival
  (the OpenClaw side of a Hermes→OpenClaw cutover; see
  ``docs/migration.md``).

Reads the in-container SQLite session store at ``/opt/data/state.db``.
Hermes-only. OpenClaw has its own session schema; out of scope unless we
ever add an equivalent reader.

Both subcommands run a single ``python3 -c`` inside the agent's
container (via ``docker compose exec``) and return one JSON blob the
host parses + renders. Read-only on the container side: no flock, no
mutations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents_cli import config as cfg_mod
from agents_cli import mp, ui
from agents_cli.agents_cmd import _detect_type_from_compose
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.ops import _compose_exec_in_vm_argv

# Used only as a fallback if the agent's config.yaml is unreadable or
# doesn't carry ``agent.gateway_auto_continue_freshness``. Matches the
# Hermes upstream default at time of writing.
DEFAULT_FRESHNESS_S = 3600

# Run inside the agent's container. Emits one JSON object on stdout
# describing all sessions + which are within the freshness window.
# Reads ``/opt/data/config.yaml`` to pick up the agent's configured
# freshness window (falls back to ``DEFAULT_FRESHNESS_S`` if unparseable).
# Kept as a single string so we can pass it through ``python3 -c`` via
# argv without staging a temp file — the script is read-only and tiny.
_QUERY_SCRIPT = """\
import json, sqlite3, time
try:
    import yaml
    cfg = yaml.safe_load(open("/opt/data/config.yaml")) or {}
    fresh = int((cfg.get("agent") or {}).get(
        "gateway_auto_continue_freshness", 3600))
except Exception:
    fresh = 3600
now = time.time()
c = sqlite3.connect("/opt/data/state.db")
c.row_factory = sqlite3.Row
rows = list(c.execute('''
  select s.id, s.source, s.title, s.message_count,
         s.input_tokens, s.output_tokens, s.cache_read_tokens,
         s.estimated_cost_usd, s.ended_at,
         coalesce(s.ended_at,
           (select max(timestamp) from messages m
             where m.session_id = s.id)) as last_ts
  from sessions s
  order by last_ts desc nulls last
'''))
def d(r):
    return {k: r[k] for k in r.keys()}
open_rows = [r for r in rows if r["ended_at"] is None]
active_rows = [r for r in open_rows
               if r["last_ts"] and (now - r["last_ts"]) < fresh]
by_platform = {}
for r in rows:
    src = r["source"] or "unknown"
    p = by_platform.setdefault(src, {"total": 0, "open": 0, "active": 0})
    p["total"] += 1
    if r["ended_at"] is None:
        p["open"] += 1
        if r["last_ts"] and (now - r["last_ts"]) < fresh:
            p["active"] += 1
out = {
    "freshness_window_s": fresh,
    "total": len(rows),
    "open": len(open_rows),
    "ended": len(rows) - len(open_rows),
    "active": len(active_rows),
    "by_platform": by_platform,
    "active_sessions": [
        {**d(r), "age_s": int(now - r["last_ts"])} for r in active_rows
    ],
}
print(json.dumps(out))
"""


_EXPORT_SCRIPT = """\
import json, sqlite3
c = sqlite3.connect("/opt/data/state.db")
c.row_factory = sqlite3.Row
sessions = []
for s in c.execute('''
  select id, source, title, started_at, ended_at, message_count,
         input_tokens, output_tokens, model
  from sessions order by started_at
'''):
    msgs = []
    for m in c.execute(
        "select role, content, timestamp, tool_name, tool_call_id "
        "from messages where session_id = ? order by id",
        (s["id"],),
    ):
        msgs.append({k: m[k] for k in m.keys()})
    sessions.append({**{k: s[k] for k in s.keys()}, "messages": msgs})
print(json.dumps({"sessions": sessions}))
"""


def status(
    name: str,
    *,
    limit: int = 20,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    if agent_type != "hermes":
        raise UserError(
            f"conversations is Hermes-only — {name!r} is type "
            f"{agent_type!r}. OpenClaw has its own session storage; "
            f"out of scope."
        )
    svc = _service_name_for(name, agent_type)

    payload = _query(cfg, svc)
    payload["agent"] = name
    _emit(payload, limit=limit)
    return payload


def _query(cfg: AgentsConfig, svc: str) -> dict[str, Any]:
    return _query_with_script(cfg, svc, _QUERY_SCRIPT)


def _query_with_script(cfg: AgentsConfig, svc: str, script: str) -> dict[str, Any]:
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "python3", "-c", script, no_tty=True, as_root=True),
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or "").lower()
        if "is not running" in err or "no such" in err:
            raise UserError(f"container for {svc!r} is not running")
        raise RuntimeFailure(
            f"state.db query failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip() or 'no stderr'}"
        )
    try:
        data: dict[str, Any] = json.loads(result.stdout)
        return data
    except json.JSONDecodeError as e:
        raise RuntimeFailure(
            f"could not parse session query output: {e}; got: {result.stdout[:200]!r}"
        ) from e


def export(
    name: str,
    *,
    to_dir: Path,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Write one Markdown transcript per session to ``to_dir``.

    Use case: migration to OpenClaw (or any other agent runtime). The
    Hermes ``state.db`` schema doesn't port, but plain-text transcripts
    are universally useful for archival and for seeding the destination
    agent with relevant context manually.

    Idempotent: existing files with the same session_id are overwritten.
    Creates ``to_dir`` if missing. Open (un-ended) sessions are exported
    too — their transcripts reflect the state at export time.
    """
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    if agent_type != "hermes":
        raise UserError(
            f"conversations export is Hermes-only — {name!r} is type "
            f"{agent_type!r}. OpenClaw uses a different session schema."
        )
    svc = _service_name_for(name, agent_type)

    data = _query_with_script(cfg, svc, _EXPORT_SCRIPT)
    sessions = data.get("sessions") or []

    to_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for s in sessions:
        path = to_dir / f"{s['id']}.md"
        path.write_text(_render_session_md(s), encoding="utf-8")
        written.append(path.name)

    payload = {
        "agent": name,
        "to_dir": str(to_dir),
        "sessions_written": len(written),
        "files": written,
    }
    ui.emit(
        payload,
        human=(
            f"{name}: exported {len(written)} session transcript(s) to {to_dir}\n"
            f"  (one Markdown file per session; safe to grep / archive / hand-seed "
            f"into another agent's context)"
        ),
    )
    return payload


def _render_session_md(s: dict[str, Any]) -> str:
    """Render one session's metadata + messages as Markdown.

    Format: front-matter-style metadata block, then one section per
    message with role + timestamp header. Tool calls / tool results
    are surfaced explicitly so they're useful when re-seeding another
    agent.
    """
    import datetime as _dt

    def ts(epoch: float | None) -> str:
        if not epoch:
            return "—"
        return _dt.datetime.fromtimestamp(epoch, tz=_dt.UTC).isoformat()

    title = s.get("title") or "(no title)"
    lines = [
        f"# {title}",
        "",
        f"- **Session ID**: `{s['id']}`",
        f"- **Source**: {s.get('source') or '—'}",
        f"- **Model**: {s.get('model') or '—'}",
        f"- **Started**: {ts(s.get('started_at'))}",
        f"- **Ended**: {ts(s.get('ended_at')) if s.get('ended_at') else 'open'}",
        f"- **Messages**: {s.get('message_count') or len(s.get('messages') or [])}",
        f"- **Tokens**: in={s.get('input_tokens') or 0}, out={s.get('output_tokens') or 0}",
        "",
        "---",
        "",
    ]
    for m in s.get("messages") or []:
        role = (m.get("role") or "?").lower()
        tname = m.get("tool_name")
        header = f"## {role}"
        if tname:
            header += f" — tool `{tname}`"
        header += f"  ·  {ts(m.get('timestamp'))}"
        lines.append(header)
        lines.append("")
        content = m.get("content") or ""
        if content:
            lines.append(content if not content.startswith("#") else "\\" + content)
        lines.append("")
    return "\n".join(lines)


def _emit(payload: dict[str, Any], *, limit: int) -> None:
    agent = payload["agent"]
    active = payload["active"]
    open_n = payload["open"]
    total = payload["total"]
    fresh = payload["freshness_window_s"]
    lines = [
        f"{agent}: {active} active / {open_n} open / {total} total "
        f"conversations  (freshness: {fresh}s)",
    ]
    plats = payload.get("by_platform") or {}
    if plats:
        lines.append("")
        lines.append("per-platform:")
        pad = max(len(s) for s in plats)
        for src in sorted(plats, key=lambda s: -plats[s]["total"]):
            p = plats[src]
            lines.append(
                f"  {src:<{pad}}  {p['total']:>3} sessions  "
                f"({p['open']} open, {p['active']} active)"
            )

    lines.append("")
    lines.append("active (within freshness window):")
    sessions = payload.get("active_sessions") or []
    if not sessions:
        lines.append("  (none)")
    else:
        lines.append(f"  {'id':<28} {'src':<10} {'msgs':>5} {'in':>8} {'out':>7} {'age':>6}  title")
        lines.append("  " + "-" * 90)
        for s in sessions[:limit]:
            age = s["age_s"]
            age_str = f"{age // 60}m" if age < 3600 else f"{age // 3600}h{(age % 3600) // 60}m"
            lines.append(
                f"  {s['id'][:28]:<28} {(s['source'] or '-'):<10} "
                f"{s['message_count']:>5} {s['input_tokens']:>8} "
                f"{s['output_tokens']:>7} {age_str:>6}  "
                f"{(s['title'] or '-')[:50]}"
            )
        if len(sessions) > limit:
            lines.append(f"  ... ({len(sessions) - limit} more)")

    ui.emit(payload, human="\n".join(lines))
