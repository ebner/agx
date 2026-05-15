"""``agents conversations <name>``: stats about active Hermes conversations.

Reads the in-container SQLite session store at ``/opt/data/state.db``
(populated by Hermes' gateway as messages flow) and reports:

- Total sessions (open + ended)
- Active context: sessions whose last message timestamp is within
  ``agent.gateway_auto_continue_freshness`` seconds. The gateway keeps
  these "hot" — the next message in the same channel resumes the
  context without recompression. Sessions older than the window are
  still resumable but cold; they trigger a context recompression /
  reload on next message.
- Per-platform breakdown (total / open / active per source).
- Active session list (id / source / title / msgs / tokens / age).

Hermes-only. OpenClaw has its own session storage which doesn't share
this schema; out of scope unless we ever add an equivalent reader.

The query runs as a single ``python3 -c`` inside the agent's container
(via ``docker compose exec``) — keeps the SQLite client work in-VM and
returns one JSON blob over stdout that the host parses and renders.
Read-only: no flock, no mutations.
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
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "python3", "-c", _QUERY_SCRIPT, no_tty=True, as_root=True
        ),
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
