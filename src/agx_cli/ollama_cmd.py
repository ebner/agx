"""Ollama / OpenAI-compatible endpoint configuration.

The toolkit does NOT manage an Ollama instance. The user runs Ollama
(or any OpenAI-compatible service) wherever they like, and this module
records the URL + optional credentials so memory plugins can wire it
into their per-backend configs.

Three valid endpoint shapes:

- **Host Ollama via the multipass NAT gateway**: ``http://10.<x>.<y>.1:11434``.
  Ollama must be bound to ``0.0.0.0:11434`` for the VM to reach it
  (``OLLAMA_HOST=0.0.0.0:11434``).
- **LAN / Tailscale-reachable host**: ``http://<ip>:11434`` or
  ``http://<tailscale-host>:11434``.
- **Public OpenAI-compatible cloud**: ``https://<vendor>/v1`` plus
  ``--api-key`` for the bearer token.

We refuse ``localhost`` / ``127.0.0.1`` / ``0.0.0.0`` at the config
schema layer (see :class:`agx_cli.config.OllamaConfig`) because those
hostnames resolve to the VM itself, not the host the user typed them
from. At ``configure`` time we additionally probe the URL from inside
the VM and (when at least one Hermes agent is running) from inside its
container, so reachability failures surface immediately instead of
becoming "memory backend silently broken" later.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agx_cli import compose, mp, sidecar, ui
from agx_cli import config as cfg_mod
from agx_cli.config import AgentsConfig, OllamaConfig
from agx_cli.errors import RuntimeFailure, UserError

ENV_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def configure(
    *,
    url: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    embed_model: str | None = None,
    llm_model: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Persist the Ollama endpoint to ``config.toml`` and probe reachability.

    ``api_key``, if provided, is stored in ``<data-dir>/.env`` under the
    name ``api_key_env`` (or ``OLLAMA_API_KEY`` by default). The TOML
    only stores the env-var name, never the secret. Pass an empty
    ``api_key`` (``--api-key ""``) to clear a previously-stored token.

    Reachability probes:

    1. From the host (best-effort — catches typo'd URLs before we even
       bother the VM).
    2. From inside the VM via ``multipass exec curl``.
    3. From inside any running Hermes containers via
       ``docker compose exec``, since the docker network is what every
       memory plugin will actually use.
    """
    cfg = cfg_mod.load(config_path)

    env_name = (api_key_env or "OLLAMA_API_KEY").strip()
    if env_name and not ENV_KEY_NAME_RE.match(env_name):
        raise UserError(f"invalid --api-key-env name {env_name!r}; must match ^[A-Z][A-Z0-9_]*$")

    new_block = OllamaConfig(
        url=url,
        api_key_env=env_name if api_key is not None or cfg.ollama.api_key_env else "",
        embed_model=embed_model or cfg.ollama.embed_model,
        llm_model=llm_model or cfg.ollama.llm_model,
    )
    # If the user explicitly cleared the api_key, drop the env name too.
    if api_key == "":
        new_block = new_block.model_copy(update={"api_key_env": ""})
    cfg.ollama = new_block

    cfg_mod.save(cfg, config_path)

    if api_key is not None:
        # Empty string clears the key; any other value sets it. The
        # project .env is shared with `firecrawl up` / `vm init` writers
        # — hold the mutation lock so a concurrent locked writer can't
        # lose this key (or vice versa). Rule #7.
        with compose.acquire_lock(cfg.paths.host_data_dir):
            _write_env_secret(cfg, env_name, api_key)

    # Probe reachability. Probes are best-effort but loud: we report each
    # outcome via ui.warn rather than raising, so a working setup behind a
    # transient hiccup doesn't lose its config.
    probes = _probe_reachability(cfg, agent_filter=None)

    ui.emit(
        {
            "url": new_block.url,
            "api_key_env": new_block.api_key_env or None,
            "embed_model": new_block.embed_model,
            "llm_model": new_block.llm_model,
            "probes": probes,
        },
        human=(
            f"ollama: configured\n"
            f"  url:         {new_block.url}\n"
            f"  api_key_env: {new_block.api_key_env or '(none)'}\n"
            f"  embed_model: {new_block.embed_model}\n"
            f"  llm_model:   {new_block.llm_model}\n"
            + "\n".join(f"  probe[{p['from']}]: {p['status']}" for p in probes)
        ),
    )
    for p in probes:
        if p["status"] not in ("ok", "skipped"):
            ui.warn(f"ollama probe from {p['from']}: {p['detail']}")
    return {"probes": probes}


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    if not cfg.ollama.is_configured():
        ui.emit(
            {"configured": False},
            human="ollama: not configured (run `agx ollama configure --url <url>`)",
        )
        return {"configured": False}
    probes = _probe_reachability(cfg, agent_filter=None)
    info = {
        "configured": True,
        "url": cfg.ollama.url,
        "api_key_env": cfg.ollama.api_key_env or None,
        "embed_model": cfg.ollama.embed_model,
        "llm_model": cfg.ollama.llm_model,
        "probes": probes,
    }
    ui.emit(
        info,
        human=(
            f"ollama: {cfg.ollama.url}\n"
            f"  api_key_env: {cfg.ollama.api_key_env or '(none)'}\n"
            f"  embed_model: {cfg.ollama.embed_model}\n"
            f"  llm_model:   {cfg.ollama.llm_model}\n"
            + "\n".join(f"  probe[{p['from']}]: {p['status']} ({p['detail']})" for p in probes)
        ),
    )
    return info


def clear(*, yes: bool = False, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    """Drop the ``[ollama]`` block from config and remove the stored API key.

    Destructive (wipes the stored token — the user has to dig it out of
    their provider again), so it confirms like the sibling wipe commands
    (``secret clear``, ``firecrawl destroy``) unless ``yes=True``.
    """
    cfg = cfg_mod.load(config_path)
    if not yes and not ui.confirm(
        "clear the [ollama] configuration and remove the stored API key?",
        default=False,
    ):
        raise UserError("aborted by user")
    prior_env = cfg.ollama.api_key_env
    cfg.ollama = OllamaConfig()
    cfg_mod.save(cfg, config_path)
    if prior_env:
        # Same locked read-modify-write as configure — see rule #7.
        with compose.acquire_lock(cfg.paths.host_data_dir):
            _write_env_secret(cfg, prior_env, "")
    ui.emit({"cleared": True}, human="ollama: configuration cleared")


# ---------- internals ----------


def _write_env_secret(cfg: AgentsConfig, key: str, value: str) -> None:
    """Set or remove ``key=value`` in the project ``.env`` without
    disturbing other entries. Empty ``value`` removes the line.

    Mode-aware via :func:`sidecar.write_project_env` — in
    ``--data-dir-in-vm`` mode the file the containers actually read
    lives at ``<vm_agents_root>/.env`` inside the VM; a host-only write
    there would leave probes passing with auth the containers never
    see. Same dict-based whole-file render as ``firecrawl up`` uses, so
    comments in the project ``.env`` are not preserved. Callers hold
    ``compose.acquire_lock``.
    """
    env = sidecar.read_project_env(cfg)
    if value:
        env[key] = value
    else:
        env.pop(key, None)
    sidecar.write_project_env(cfg, env)


def _probe_reachability(cfg: AgentsConfig, *, agent_filter: str | None) -> list[dict[str, str]]:
    """Probe the configured URL from the VM. Returns a list of probe
    results, one per "location" (host, VM, each running Hermes
    container).
    """
    probes: list[dict[str, str]] = []
    if not cfg.ollama.is_configured():
        return probes

    probes.append(_probe_from_vm(cfg))

    if cfg.vm_runtime.is_populated():
        for entry in _hermes_services(cfg, agent_filter):
            probes.append(_probe_from_container(cfg, entry))
    return probes


def _probe_from_vm(cfg: AgentsConfig) -> dict[str, str]:
    if not cfg.vm_runtime.is_populated():
        return {"from": "vm", "status": "skipped", "detail": "vm not initialized yet"}
    auth_args = _curl_auth_args(cfg)
    result = mp.exec_in_vm(
        cfg.vm.name,
        "curl",
        "-sf",
        "--max-time",
        "5",
        *auth_args,
        f"{cfg.ollama.url.rstrip('/')}/api/tags",
        check=False,
    )
    if result.returncode == 0:
        return {"from": "vm", "status": "ok", "detail": "/api/tags reachable"}
    return {
        "from": "vm",
        "status": "fail",
        "detail": (result.stderr or "").strip() or f"curl rc={result.returncode}",
    }


def _probe_from_container(cfg: AgentsConfig, hermes_svc: str) -> dict[str, str]:
    from agx_cli.ops import _compose_exec_in_vm_argv

    auth_args = _curl_auth_args(cfg)
    argv = _compose_exec_in_vm_argv(
        cfg,
        hermes_svc,
        "curl",
        "-sf",
        "--max-time",
        "5",
        *auth_args,
        f"{cfg.ollama.url.rstrip('/')}/api/tags",
        no_tty=True,
    )
    result = mp.exec_in_vm(cfg.vm.name, *argv, check=False)
    if result.returncode == 0:
        return {
            "from": f"container[{hermes_svc}]",
            "status": "ok",
            "detail": "/api/tags reachable",
        }
    err = (result.stderr or "").lower()
    if "not running" in err or "no such" in err:
        return {
            "from": f"container[{hermes_svc}]",
            "status": "skipped",
            "detail": "container down",
        }
    return {
        "from": f"container[{hermes_svc}]",
        "status": "fail",
        "detail": (result.stderr or "").strip() or f"curl rc={result.returncode}",
    }


def _curl_auth_args(cfg: AgentsConfig) -> list[str]:
    if not cfg.ollama.api_key_env:
        return []
    env = _read_env(cfg)
    token = env.get(cfg.ollama.api_key_env, "")
    if not token:
        return []
    return ["-H", f"Authorization: Bearer {token}"]


def _read_env(cfg: AgentsConfig) -> dict[str, str]:
    """Mode-aware project-.env read (values stripped for header use)."""
    return {k: v.strip() for k, v in sidecar.read_project_env(cfg).items()}


def _hermes_services(cfg: AgentsConfig, agent_filter: str | None) -> list[str]:
    """Service names of every Hermes agent in compose (optionally filtered)."""
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return []
    out: list[str] = []
    for svc in doc.get("services") or {}:
        if not svc.startswith("hermes-"):
            continue
        if agent_filter is not None and svc != f"hermes-{agent_filter}":
            continue
        out.append(svc)
    return out
