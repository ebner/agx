"""Memory backend plugins for Hermes agents.

The toolkit exposes ``agents memory {enable,disable,status,list} ...`` as
a unified surface; per-backend logic lives in ``memory/<plugin>.py``.
Each plugin module implements three callables — :func:`enable`,
:func:`disable`, :func:`status` — and is registered by name in
:data:`PLUGINS`.

Tiers
-----

- **In-process** (``holographic``, ``mem0``): no sibling docker service.
  Enabling them is a config flip on the agent's ``config.yaml`` plus a
  restart. Mem0 also wires Ollama endpoints when configured.
- **Sibling-service** (``openviking``, ``hindsight``): adds a second
  compose service alongside the Hermes container, on the same docker
  network. The toolkit owns the lifecycle (image pinning, compose
  fragment, volume layout, .env entries).

Implementation contract for each plugin
---------------------------------------

The :class:`MemoryPlugin` protocol below documents the shape every
plugin must satisfy. The CLI dispatches through :func:`enable` /
:func:`disable` / :func:`status` here; those funnel into the registered
plugin module.

Common invariants
-----------------

- All mutations hold ``<data-dir>/.agents.lock`` via
  :func:`compose.acquire_lock`.
- Plugin state lives at ``<data-dir>/.memory-state/<agent>.json`` so it
  survives ``--data-dir-in-vm`` exports (under the host data dir, not the
  in-VM agent dir).
- Hermes ``config.yaml`` writes go through
  :func:`hermes_config.write_atomically` — dashboard-binding refusal +
  ``hermes config validate`` apply automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from agents_cli import compose, hermes_config, ui
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import _detect_type_from_compose
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError


class MemoryPlugin(Protocol):
    """Surface every plugin module exposes.

    Implementations live in sibling modules (``memory/holographic.py``
    etc.) and the registry keys them by short name.
    """

    name: str

    def enable(self, cfg: AgentsConfig, agent_name: str, **opts: Any) -> dict[str, Any]: ...

    def disable(self, cfg: AgentsConfig, agent_name: str) -> None: ...

    def status(self, cfg: AgentsConfig, agent_name: str) -> dict[str, Any]: ...


def state_path(cfg: AgentsConfig, agent_name: str) -> Path:
    """Where the toolkit records which memory backend is enabled for an
    agent. Sits under the host data-dir so it works in both modes (the
    per-agent data dir doesn't exist on the host under ``--data-dir-in-vm``).
    """
    return cfg.paths.host_data_dir / ".memory-state" / f"{agent_name}.json"


def read_state(cfg: AgentsConfig, agent_name: str) -> dict[str, Any] | None:
    p = state_path(cfg, agent_name)
    if not p.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError):
        return None


def write_state(cfg: AgentsConfig, agent_name: str, payload: dict[str, Any]) -> None:
    p = state_path(cfg, agent_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def clear_state(cfg: AgentsConfig, agent_name: str) -> None:
    p = state_path(cfg, agent_name)
    p.unlink(missing_ok=True)


def _require_hermes(cfg: AgentsConfig, agent_name: str) -> None:
    agent_type = _detect_type_from_compose(cfg, agent_name)
    if agent_type is None:
        raise UserError(f"agent {agent_name!r} not in compose")
    if agent_type != "hermes":
        raise UserError(
            f"agents memory only applies to Hermes agents — "
            f"{agent_name!r} is type {agent_type!r}. "
            "OpenClaw uses OpenViking via its own bundled memory layer."
        )


# ---------- registry ----------


def _load_plugins() -> dict[str, Any]:
    """Import plugin modules lazily to keep the package import cost flat
    and to let plugins fail-import without breaking the whole CLI.
    """
    from agents_cli.memory import holographic, mem0, openviking

    return {
        holographic.NAME: holographic,
        mem0.NAME: mem0,
        openviking.NAME: openviking,
    }


def list_plugins() -> list[dict[str, Any]]:
    plugins = _load_plugins()
    return [
        {
            "name": p.NAME,
            "tier": p.TIER,
            "summary": p.SUMMARY,
        }
        for p in plugins.values()
    ]


def _get_plugin(plugin_name: str) -> Any:
    plugins = _load_plugins()
    if plugin_name not in plugins:
        raise UserError(f"unknown memory plugin {plugin_name!r}; known: {sorted(plugins)}")
    return plugins[plugin_name]


# ---------- CLI entry points ----------


def enable(
    plugin_name: str,
    agent_name: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
    **opts: Any,
) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    _require_hermes(cfg, agent_name)
    plugin = _get_plugin(plugin_name)
    with compose.acquire_lock(cfg.paths.host_data_dir):
        result: dict[str, Any] = plugin.enable(cfg, agent_name, **opts)
    ui.emit(
        {"plugin": plugin_name, "agent": agent_name, **result},
        human=f"memory[{plugin_name}] enabled for {agent_name!r}",
    )
    return result


def disable(
    agent_name: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    cfg = cfg_mod.load(config_path)
    _require_hermes(cfg, agent_name)
    state = read_state(cfg, agent_name)
    if state is None:
        raise UserError(f"no memory backend enabled for {agent_name!r}; nothing to disable")
    plugin = _get_plugin(state["plugin"])
    with compose.acquire_lock(cfg.paths.host_data_dir):
        plugin.disable(cfg, agent_name)
    ui.emit(
        {"plugin": state["plugin"], "agent": agent_name, "status": "disabled"},
        human=f"memory[{state['plugin']}] disabled for {agent_name!r}",
    )


def status(
    agent_name: str | None = None,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    cfg = cfg_mod.load(config_path)
    targets: list[str]
    if agent_name is not None:
        _require_hermes(cfg, agent_name)
        targets = [agent_name]
    else:
        doc = compose.read_compose_doc(cfg)
        targets = [
            s[len("hermes-") :] for s in (doc.get("services") or {}) if s.startswith("hermes-")
        ]

    rows: list[dict[str, Any]] = []
    for name in targets:
        state = read_state(cfg, name)
        if state is None:
            doc = hermes_config.read_doc(cfg, name)
            provider = (doc.get("memory") or {}).get("provider")
            rows.append(
                {
                    "agent": name,
                    "plugin": provider or "(none)",
                    "managed_by_toolkit": False,
                    "detail": "config-only" if provider else "no memory configured",
                }
            )
            continue
        plugin = _get_plugin(state["plugin"])
        info = plugin.status(cfg, name)
        rows.append(
            {
                "agent": name,
                "plugin": state["plugin"],
                "managed_by_toolkit": True,
                **info,
            }
        )

    ui.table(
        rows,
        ["agent", "plugin", "managed_by_toolkit", "detail"]
        if rows and "detail" in rows[0]
        else ["agent", "plugin", "managed_by_toolkit"],
    )
    return rows
