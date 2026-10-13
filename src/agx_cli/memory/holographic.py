"""Holographic memory plugin (Nous Research, in-tree).

Local-only, in-process inside the Hermes container. No sibling docker
service, no external embedder. Storage = one SQLite file at
``$HERMES_HOME/memory_store.db``, retrieval = FTS5 keyword + HRR algebra
over random-projection vectors (NumPy).

Toolkit value-add is small but real: config flip + a couple of doctor
warnings that matter on this stack:

- **SQLite WAL on 9p**. Under default host-mount mode the data dir is
  shared via 9p, and SQLite WAL is fragile across that share. We warn
  loudly (matches the Hermes kanban/sessions trap documented in
  ``AGENTS.md``).
- **NumPy missing in image**. Hermes upstream issue #17350: without
  NumPy the plugin silently degrades to FTS5-only and lies about it. We
  probe ``python -c "import numpy"`` in the running container.
"""

from __future__ import annotations

from typing import Any

from agx_cli import compose_runner, hermes_config, mp
from agx_cli.agents_cmd import service_name as _service_name_for
from agx_cli.config import AgentsConfig
from agx_cli.memory import clear_state, read_state, write_state
from agx_cli.ops import _compose_exec_in_vm_argv

NAME = "holographic"
TIER = "in-process"
SUMMARY = (
    "Local SQLite + FTS5 + HRR vectors. No embeddings, no LLM, no sibling "
    "service. Works with Codex auth alone."
)

DEFAULT_DB_PATH = "/opt/data/memory_store.db"
DEFAULT_HRR_DIM = 1024


def enable(
    cfg: AgentsConfig, agent_name: str, profile: str | None = None, **_opts: Any
) -> dict[str, Any]:
    """Flip ``memory.provider`` to ``holographic`` in the agent's config.yaml
    and write the matching ``plugins.hermes-memory-store`` block.

    Restarts the container so the new provider takes effect: ``up`` first
    to guarantee the container exists (``compose restart`` cannot create
    an absent one — e.g. after a VM rebuild or ``docker compose rm``),
    then a real ``restart``, because ``up`` alone is a NO-OP for an
    already-running container when only config.yaml changed — the
    gateway would silently keep the old provider.
    """
    doc = hermes_config.read_doc(cfg, agent_name, profile)

    # Each profile is its own HERMES_HOME — the memory store MUST live
    # inside it, or two profiles would share one DB (the cross-write
    # memory-drift hazard upstream explicitly warns about).
    db_path = f"{hermes_config.profile_home(profile)}/memory_store.db"

    memory = doc.setdefault("memory", {})
    memory["provider"] = NAME

    plugins = doc.setdefault("plugins", {})
    block = plugins.setdefault("hermes-memory-store", {})
    if profile is not None:
        # A profile created with `--clone default` inherits the DEFAULT
        # profile's db_path — setdefault would keep it and silently share
        # one memory DB across profiles. Named profiles always get their
        # own store, unconditionally.
        block["db_path"] = db_path
    else:
        block.setdefault("db_path", db_path)
    block.setdefault("auto_extract", False)
    block.setdefault("default_trust", 0.5)
    block.setdefault("hrr_dim", DEFAULT_HRR_DIM)

    hermes_config.write_atomically(cfg, agent_name, doc, profile)

    write_state(
        cfg,
        agent_name,
        {"plugin": NAME, "db_path": db_path, "hrr_dim": DEFAULT_HRR_DIM},
        profile,
    )

    svc = _service_name_for(agent_name, "hermes")
    # `up -d` only creates the container when it's missing — the memory
    # config lives in config.yaml, not compose, so up alone is a NO-OP
    # for an already-running container and the gateway would keep the
    # old memory provider while the CLI claims holographic is enabled.
    # Restart forces the gateway to re-read config.yaml.
    compose_runner.up(cfg, svc)
    compose_runner.restart(cfg, svc)

    return {
        "db_path": db_path,
        "hrr_dim": DEFAULT_HRR_DIM,
        "warnings": _warn_if_sqlite_on_9p(cfg),
    }


def disable(cfg: AgentsConfig, agent_name: str, profile: str | None = None) -> None:
    """Restore the agent to built-in memory and clear our state pointer.

    Leaves ``$HERMES_HOME/memory_store.db`` in place — the user may want
    to re-enable later or export the memories separately.

    Same up-then-restart rationale as :func:`enable`: ``up`` creates the
    container if absent; ``restart`` forces the config.yaml re-read that
    a no-op ``up`` wouldn't trigger.
    """
    doc = hermes_config.read_doc(cfg, agent_name, profile)

    memory = doc.get("memory")
    if isinstance(memory, dict) and memory.get("provider") == NAME:
        memory.pop("provider", None)
        if not memory:
            doc.pop("memory", None)

    plugins = doc.get("plugins")
    if isinstance(plugins, dict):
        plugins.pop("hermes-memory-store", None)
        if not plugins:
            doc.pop("plugins", None)

    hermes_config.write_atomically(cfg, agent_name, doc, profile)
    clear_state(cfg, agent_name, profile)

    svc = _service_name_for(agent_name, "hermes")
    # Same as enable: compose config didn't change, so restart (not a
    # no-op `up`) is what actually drops the provider from the gateway;
    # the `up` guarantees the container exists to be restarted.
    compose_runner.up(cfg, svc)
    compose_runner.restart(cfg, svc)


def status(cfg: AgentsConfig, agent_name: str, profile: str | None = None) -> dict[str, Any]:
    state = read_state(cfg, agent_name, profile)
    out: dict[str, Any] = {
        "db_path": (state or {}).get("db_path", DEFAULT_DB_PATH),
        "hrr_dim": (state or {}).get("hrr_dim", DEFAULT_HRR_DIM),
        "numpy_ok": _probe_numpy(cfg, agent_name),
        "sqlite_on_9p": _sqlite_on_9p_risk(cfg),
    }
    risks = []
    if not out["numpy_ok"]:
        risks.append("numpy missing in container (FTS5-only fallback, see hermes-agent #17350)")
    if out["sqlite_on_9p"]:
        risks.append(
            "data dir is host-mounted; SQLite WAL may misbehave (consider --data-dir-in-vm)"
        )
    out["detail"] = "; ".join(risks) if risks else "ok"
    return out


# ---------- internal probes / warnings ----------


def _sqlite_on_9p_risk(cfg: AgentsConfig) -> bool:
    """True when the agent's data dir lives on a multipass host mount.

    SQLite WAL doesn't play well with 9p; the toolkit's
    ``--data-dir-in-vm`` mode is the recommended workaround.
    """
    return not cfg.paths.data_dir_in_vm


def _warn_if_sqlite_on_9p(cfg: AgentsConfig) -> list[str]:
    if _sqlite_on_9p_risk(cfg):
        return [
            "data dir is host-mounted (9p); SQLite WAL is fragile across that share. "
            "If you see 'database is locked' or corruption, re-init with "
            "`agx vm init --data-dir-in-vm` and re-import the agent."
        ]
    return []


def _probe_numpy(cfg: AgentsConfig, agent_name: str) -> bool:
    """Return True iff ``python -c 'import numpy'`` succeeds inside the
    running Hermes container. Conservative on errors (returns False).
    """
    if not cfg.vm_runtime.is_populated():
        return False
    svc = _service_name_for(agent_name, "hermes")
    argv = _compose_exec_in_vm_argv(cfg, svc, "python", "-c", "import numpy", no_tty=True)
    result = mp.exec_in_vm(cfg.vm.name, *argv, check=False)
    return result.returncode == 0
