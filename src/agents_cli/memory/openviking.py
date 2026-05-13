"""OpenViking memory plugin (sibling docker service).

OpenViking (volcengine/openviking) is a context/memory database that
runs as its own HTTP service on port 1933. The toolkit deploys it as a
sibling compose service per Hermes agent, on the project's default
docker network, reachable from the Hermes container by service name.

Per-agent isolation: each Hermes agent gets its own ``openviking-<agent>``
service with its own data volume under ``./<agent>/.openviking/``. This
keeps memories scoped per agent without inventing a cross-agent
namespace.

OpenViking needs an LLM provider AND an embeddings provider (both
OpenAI-API-compatible). We wire them from the toolkit's ``[ollama]``
block; if it's empty, refuse install with a clear remediation hint —
unlike mem0 we cannot fall back to a stock-OpenAI default sanely
because OpenViking's heavy fact-extraction loop would melt a personal
ChatGPT subscription.
"""

from __future__ import annotations

from typing import Any

from agents_cli import compose, compose_runner, hermes_config
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.compose import OPENVIKING_IMAGE_BASE
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError
from agents_cli.memory import clear_state, read_state, write_state

NAME = "openviking"
TIER = "sibling-service"
SUMMARY = (
    "Sibling docker service from Volcengine. Filesystem-paradigm memory "
    "(viking:// URIs) with tiered context loading. Needs an Ollama / "
    "OpenAI-compatible embeddings + LLM endpoint."
)

# Pinned by upstream tag — `agents codex skill-enable` style pinning by
# digest is overkill for a sibling service the user is already opting
# into; tags are documented and stable enough.
DEFAULT_IMAGE_TAG = "0.3.16"


def enable(
    cfg: AgentsConfig,
    agent_name: str,
    *,
    image_tag: str = DEFAULT_IMAGE_TAG,
    **_opts: Any,
) -> dict[str, Any]:
    if not cfg.ollama.is_configured():
        raise UserError(
            "openviking needs an Ollama / OpenAI-compatible endpoint for "
            "embeddings + LLM. Run `agents ollama configure --url <url>` "
            "first, then re-run this command."
        )

    sidecar_svc = f"openviking-{agent_name}"
    sidecar_url = f"http://{sidecar_svc}:1933"

    fragment = compose.openviking_sidecar_fragment(
        agent_name=agent_name,
        image_tag=image_tag,
        api_key_env=cfg.ollama.api_key_env or None,
        embed_provider_url=_vfix(cfg.ollama.url),
        embed_model=cfg.ollama.embed_model,
        llm_provider_url=_vfix(cfg.ollama.url),
        llm_model=cfg.ollama.llm_model,
    )

    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    if sidecar_svc not in services:
        compose.add_service(doc, sidecar_svc, fragment)
    else:
        services[sidecar_svc] = fragment

    hermes_svc = _service_name_for(agent_name, "hermes")
    hermes_service = (doc.get("services") or {}).get(hermes_svc)
    if hermes_service is not None:
        depends = hermes_service.setdefault("depends_on", [])
        if isinstance(depends, list) and sidecar_svc not in depends:
            depends.append(sidecar_svc)

    compose.write_compose_atomically(cfg, doc)

    hdoc = hermes_config.read_doc(cfg, agent_name)
    memory = hdoc.setdefault("memory", {})
    memory["provider"] = NAME
    plugins = hdoc.setdefault("plugins", {})
    block = plugins.setdefault("openviking", {})
    block["endpoint"] = sidecar_url
    if cfg.ollama.api_key_env:
        block["api_key_env"] = cfg.ollama.api_key_env
    hermes_config.write_atomically(cfg, agent_name, hdoc)

    write_state(
        cfg,
        agent_name,
        {
            "plugin": NAME,
            "sidecar_service": sidecar_svc,
            "endpoint": sidecar_url,
            "image_tag": image_tag,
        },
    )

    # Bring up the sidecar AND the Hermes service so the new depends_on
    # + memory.provider edit takes effect.
    compose_runner.up(cfg, sidecar_svc, hermes_svc)

    return {
        "sidecar_service": sidecar_svc,
        "endpoint": sidecar_url,
        "image_tag": image_tag,
    }


def disable(cfg: AgentsConfig, agent_name: str) -> None:
    state = read_state(cfg, agent_name) or {}
    sidecar_svc = state.get("sidecar_service", f"openviking-{agent_name}")

    # Stop+remove the sidecar container before pulling its service out
    # of compose, so the user-visible state is clean even if a later
    # phase fails.
    compose_runner.stop(cfg, sidecar_svc)
    compose_runner.rm(cfg, sidecar_svc)

    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    if sidecar_svc in services:
        compose.remove_service(doc, sidecar_svc)
    hermes_svc = _service_name_for(agent_name, "hermes")
    hermes_service = services.get(hermes_svc)
    if isinstance(hermes_service, dict):
        deps = hermes_service.get("depends_on")
        if isinstance(deps, list) and sidecar_svc in deps:
            deps.remove(sidecar_svc)
            if not deps:
                hermes_service.pop("depends_on", None)
    compose.write_compose_atomically(cfg, doc)

    hdoc = hermes_config.read_doc(cfg, agent_name)
    memory = hdoc.get("memory")
    if isinstance(memory, dict) and memory.get("provider") == NAME:
        memory.pop("provider", None)
        if not memory:
            hdoc.pop("memory", None)
    plugins = hdoc.get("plugins")
    if isinstance(plugins, dict):
        plugins.pop("openviking", None)
        if not plugins:
            hdoc.pop("plugins", None)
    hermes_config.write_atomically(cfg, agent_name, hdoc)

    clear_state(cfg, agent_name)

    compose_runner.up(cfg, hermes_svc)


def status(cfg: AgentsConfig, agent_name: str) -> dict[str, Any]:
    state = read_state(cfg, agent_name) or {}
    return {
        "sidecar_service": state.get("sidecar_service", f"openviking-{agent_name}"),
        "endpoint": state.get("endpoint", "?"),
        "image": f"{OPENVIKING_IMAGE_BASE}:{state.get('image_tag', DEFAULT_IMAGE_TAG)}",
        "detail": f"sidecar={state.get('sidecar_service', '?')} endpoint={state.get('endpoint', '?')}",
    }


def _vfix(url: str) -> str:
    return url.rstrip("/") + ("" if url.rstrip("/").endswith("/v1") else "/v1")
