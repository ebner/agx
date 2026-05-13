"""mem0 memory plugin (library mode, in-process).

mem0's OSS server mode (FastAPI + Postgres + dashboard) is deliberately
NOT supported — it duplicates state that already lives one mount away.
We wire library mode only: ``mem0ai`` runs inside the Hermes container,
writes to a local Qdrant file backend under ``$HERMES_HOME``, and uses
the toolkit's configured Ollama endpoint (or OpenAI directly) for
embeddings + fact extraction.

The toolkit's value-add:

- Flip ``memory.provider`` to ``mem0`` in the agent's config.yaml.
- Write the ``plugins.mem0`` config block: vector store at
  ``$HERMES_HOME/mem0_qdrant/``, embedder + LLM pointed at the configured
  Ollama URL (translated from ``[ollama]`` in config.toml), or refuse
  install if neither Ollama nor an ``OPENAI_API_KEY`` is available.
- Restart the agent's container.

Mem0 calls ``openai.OpenAI(api_key=..., base_url=...)`` directly. That
means **Codex auth alone won't work** — the user needs either an
``[ollama]`` block configured, or an ``OPENAI_API_KEY`` written to
``<data-dir>/.env``. We fail loud at enable time rather than silently
crash on first turn.
"""

from __future__ import annotations

from typing import Any

from agents_cli import compose_runner, hermes_config
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError
from agents_cli.memory import clear_state, read_state, write_state

NAME = "mem0"
TIER = "in-process"
SUMMARY = (
    "Library-mode mem0 inside the Hermes container with a local Qdrant "
    "file store. Needs Ollama (preferred) or an OpenAI API key for "
    "embeddings + extraction."
)

DEFAULT_VECTOR_PATH = "/opt/data/mem0_qdrant"
DEFAULT_COLLECTION = "mem0"


def enable(
    cfg: AgentsConfig,
    agent_name: str,
    *,
    embedder: str = "auto",
    llm: str = "auto",
    **_opts: Any,
) -> dict[str, Any]:
    """Wire mem0 in-process for the agent.

    ``embedder``/``llm`` accept:

    - ``"auto"`` (default): pick Ollama if configured, else OpenAI if
      ``OPENAI_API_KEY`` is set in ``.env``, else refuse.
    - ``"ollama"``: require ``[ollama]`` in config.toml; refuse otherwise.
    - ``"openai"``: require ``OPENAI_API_KEY`` in ``.env``; refuse
      otherwise.
    """
    embed_block, llm_block, provider_used = _resolve_provider_blocks(
        cfg, embedder=embedder, llm=llm
    )

    doc = hermes_config.read_doc(cfg, agent_name)

    memory = doc.setdefault("memory", {})
    memory["provider"] = NAME

    plugins = doc.setdefault("plugins", {})
    mem0_block = plugins.setdefault("mem0", {})
    mem0_block["vector_store"] = {
        "provider": "qdrant",
        "config": {
            "path": DEFAULT_VECTOR_PATH,
            "collection_name": DEFAULT_COLLECTION,
        },
    }
    mem0_block["embedder"] = embed_block
    mem0_block["llm"] = llm_block

    hermes_config.write_atomically(cfg, agent_name, doc)

    write_state(
        cfg,
        agent_name,
        {
            "plugin": NAME,
            "embedder_provider": provider_used["embedder"],
            "llm_provider": provider_used["llm"],
            "vector_path": DEFAULT_VECTOR_PATH,
        },
    )

    svc = _service_name_for(agent_name, "hermes")
    compose_runner.up(cfg, svc)

    return {
        "vector_path": DEFAULT_VECTOR_PATH,
        "embedder": provider_used["embedder"],
        "llm": provider_used["llm"],
    }


def disable(cfg: AgentsConfig, agent_name: str) -> None:
    doc = hermes_config.read_doc(cfg, agent_name)

    memory = doc.get("memory")
    if isinstance(memory, dict) and memory.get("provider") == NAME:
        memory.pop("provider", None)
        if not memory:
            doc.pop("memory", None)

    plugins = doc.get("plugins")
    if isinstance(plugins, dict):
        plugins.pop("mem0", None)
        if not plugins:
            doc.pop("plugins", None)

    hermes_config.write_atomically(cfg, agent_name, doc)
    clear_state(cfg, agent_name)

    svc = _service_name_for(agent_name, "hermes")
    compose_runner.up(cfg, svc)


def status(cfg: AgentsConfig, agent_name: str) -> dict[str, Any]:
    state = read_state(cfg, agent_name) or {}
    return {
        "vector_path": state.get("vector_path", DEFAULT_VECTOR_PATH),
        "embedder_provider": state.get("embedder_provider", "?"),
        "llm_provider": state.get("llm_provider", "?"),
        "detail": (
            f"embedder={state.get('embedder_provider', '?')} llm={state.get('llm_provider', '?')}"
        ),
    }


# ---------- provider resolution ----------


def _resolve_provider_blocks(
    cfg: AgentsConfig, *, embedder: str, llm: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Build the ``embedder`` and ``llm`` config blocks for mem0.

    Returns ``(embed_block, llm_block, {"embedder": "ollama"|"openai",
    "llm": ...})``. Raises :class:`UserError` if a requested provider
    isn't available.
    """
    ollama_available = cfg.ollama.is_configured()
    openai_available = _openai_key_present(cfg)

    embed_choice = _pick(embedder, ollama_available, openai_available, what="embedder")
    llm_choice = _pick(llm, ollama_available, openai_available, what="llm")

    if embed_choice == "ollama":
        embed_block = {
            "provider": "openai",
            "config": {
                "model": cfg.ollama.embed_model,
                "openai_base_url": _vfix(cfg.ollama.url),
                "api_key": _api_key_env_value(cfg) or "ollama",
            },
        }
    else:
        embed_block = {
            "provider": "openai",
            "config": {
                "model": "text-embedding-3-small",
                "api_key": "${OPENAI_API_KEY}",
            },
        }

    if llm_choice == "ollama":
        llm_block = {
            "provider": "openai",
            "config": {
                "model": cfg.ollama.llm_model,
                "openai_base_url": _vfix(cfg.ollama.url),
                "api_key": _api_key_env_value(cfg) or "ollama",
            },
        }
    else:
        llm_block = {
            "provider": "openai",
            "config": {
                "model": "gpt-4o-mini",
                "api_key": "${OPENAI_API_KEY}",
            },
        }

    return embed_block, llm_block, {"embedder": embed_choice, "llm": llm_choice}


def _pick(requested: str, ollama_available: bool, openai_available: bool, *, what: str) -> str:
    if requested == "ollama":
        if not ollama_available:
            raise UserError(
                f"--{what} ollama requested but no [ollama] block in config.toml; "
                "run `agents ollama configure --url <url>` first"
            )
        return "ollama"
    if requested == "openai":
        if not openai_available:
            raise UserError(
                f"--{what} openai requested but OPENAI_API_KEY is not set in "
                f"<data-dir>/.env; add it or pass --{what} ollama"
            )
        return "openai"
    if requested != "auto":
        raise UserError(f"unknown --{what} {requested!r}; expected one of: auto, ollama, openai")
    if ollama_available:
        return "ollama"
    if openai_available:
        return "openai"
    raise UserError(
        f"mem0 needs an {what} provider but neither [ollama] nor OPENAI_API_KEY "
        "is configured. Either: (a) `agents ollama configure --url <url>` to point "
        "at an existing Ollama / OpenAI-compatible endpoint, or (b) add "
        "OPENAI_API_KEY=... to <data-dir>/.env."
    )


def _vfix(url: str) -> str:
    """mem0 expects an OpenAI-compatible base URL. Ollama natively serves
    its OpenAI-compatible API under ``/v1`` — append it if the user
    didn't already.
    """
    return url.rstrip("/") + ("" if url.rstrip("/").endswith("/v1") else "/v1")


def _openai_key_present(cfg: AgentsConfig) -> bool:
    env = _read_env(cfg)
    return bool(env.get("OPENAI_API_KEY"))


def _api_key_env_value(cfg: AgentsConfig) -> str | None:
    """If the user configured an ``api_key_env`` on the Ollama block,
    return the literal ``${NAME}`` interpolation token mem0 should use.
    Returns ``None`` when no auth is configured (vanilla Ollama).
    """
    name = cfg.ollama.api_key_env.strip()
    if not name:
        return None
    return "${" + name + "}"


def _read_env(cfg: AgentsConfig) -> dict[str, str]:
    env_path = cfg.paths.host_data_dir / ".env"
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out
