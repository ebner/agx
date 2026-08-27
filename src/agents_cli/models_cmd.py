"""``agents model``: pull / list / rm / use for local models.

Thin wrapper over Ollama's CLI inside the running ``ollama`` container,
plus a HuggingFace download path for llama.cpp GGUFs.

The interesting command is ``agents model use <agent> --engine X
--model Y``: it edits the Hermes agent's ``config.yaml`` to point at
the in-VM inference engine via ``provider: custom + base_url +
api_key``. That's the only part of this module that's not a passthrough.

Hermes-only for ``use`` — OpenClaw has its own provider wizard and
owns its config file.

Storage:

- Ollama: lives at ``<data-dir>/models/ollama/`` (Ollama's native blob
  store layout; manifests + blobs subdirs)
- llama.cpp: GGUF files at ``<data-dir>/models/llama-cpp/<name>.gguf``
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agents_cli import compose, hermes_config, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import _detect_type_from_compose
from agents_cli.compose import (
    LLAMACPP_DOWNLOAD_MOUNT,
    LLAMACPP_SERVICE_PREFIX,
    OLLAMA_SERVICE_NAME,
)
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError

SUPPORTED_ENGINES: tuple[str, ...] = ("ollama", "llama-cpp")
SUPPORTED_AUX: tuple[str, ...] = ("compression", "vision", "all")
# `model use` additionally accepts the EXTERNAL Ollama endpoint from the
# `[ollama]` config block (the toolkit does not run that instance — it's
# typically GPU inference on the host). pull/rm stay in-VM-only: the
# toolkit never mutates an external server's model store.
USE_ENGINES: tuple[str, ...] = (*SUPPORTED_ENGINES, "external-ollama")


def pull(
    engine: str,
    model: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Download a model into the engine's storage.

    Ollama accepts both registry refs (``llama3.2:3b``) and HF refs
    (``hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M``) natively —
    we just pass through to ``ollama pull``.

    llama-cpp goes via a Python ``huggingface_hub.hf_hub_download``
    inside the running ollama container (the easiest way to get a
    Python env with the hf_hub package without adding a new image).
    The expected form is ``<hf-repo>:<filename>``.
    """
    if engine not in SUPPORTED_ENGINES:
        raise UserError(f"unknown engine {engine!r}; supported: {', '.join(SUPPORTED_ENGINES)}")
    cfg = cfg_mod.load(config_path)
    if engine == "ollama":
        return _pull_ollama(cfg, model)
    return _pull_llamacpp_gguf(cfg, model)


def list_models(
    engine: str | None = None,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """List downloaded models. With ``engine=None``, lists both."""
    cfg = cfg_mod.load(config_path)
    payload: dict[str, Any] = {}
    if engine in (None, "ollama"):
        payload["ollama"] = _list_ollama(cfg)
    if engine in (None, "llama-cpp"):
        payload["llama_cpp"] = _list_llamacpp(cfg)
    _emit_list(payload)
    return payload


def rm(
    engine: str,
    model: str,
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    if engine not in SUPPORTED_ENGINES:
        raise UserError(f"unknown engine {engine!r}; supported: {', '.join(SUPPORTED_ENGINES)}")
    cfg = cfg_mod.load(config_path)
    if not yes and not ui.confirm(f"remove {engine} model {model!r}?", default=False):
        raise UserError("aborted by user")
    if engine == "ollama":
        return _rm_ollama(cfg, model)
    return _rm_llamacpp_gguf(cfg, model)


def use(
    agent_name: str,
    *,
    engine: str,
    model: str,
    aux: str | None = None,
    context_length: int | None = None,
    profile: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Wire a Hermes agent's ``model:`` block to an inference engine.

    Engines: the in-VM pair (``ollama`` / ``llama-cpp``) plus
    ``external-ollama``, which targets the ``[ollama]`` config block's
    URL (typically GPU inference on the host — the toolkit does not run
    that instance, so ``agents model pull/rm`` don't apply to it).

    Writes the structured ``model:`` form (``{default, provider,
    base_url, api_key}``) — same shape the ``hermes model`` wizard
    produces, so the doctor's model_set check stays happy.

    ``context_length`` writes BOTH ``model.context_length`` (startup
    resolution) and a per-model ``custom_providers`` entry keyed by the
    endpoint's base_url — the latter is what Hermes' ``/model`` switch
    and display paths consult, so mid-session model swaps keep the
    correct window too. REQUIRED for external-ollama: the server's
    ``OLLAMA_CONTEXT_LENGTH`` cap cannot be probed remotely, and
    Ollama's ``/api/show`` reports the model's NATIVE window — without
    the override Hermes compresses far too late while the server
    silently truncates the prompt.

    ``aux`` is optional: ``"compression"`` / ``"vision"`` / ``"all"``
    routes the named auxiliary tasks to the same engine. Useful when
    the user wants the local model to handle EVERYTHING (no fallback
    to a cloud API for context compression).

    For OpenClaw, refuses — its config schema differs and the toolkit
    stays out of its plugin lifecycle.
    """
    if engine not in USE_ENGINES:
        raise UserError(f"unknown engine {engine!r}; supported: {', '.join(USE_ENGINES)}")
    if aux is not None and aux not in SUPPORTED_AUX:
        raise UserError(f"--aux must be one of {', '.join(SUPPORTED_AUX)}")
    profile = hermes_config.normalize_profile(profile)
    cfg = cfg_mod.load(config_path)

    if engine == "external-ollama":
        if not cfg.ollama.url:
            raise UserError(
                "no external Ollama endpoint configured — run "
                "`agents ollama configure --url http://<host>:11434` first"
            )
        if context_length is None:
            raise UserError(
                "--context-length is required with --engine external-ollama: the server's "
                "OLLAMA_CONTEXT_LENGTH cap can't be probed remotely, and Ollama's /api/show "
                "reports the model's NATIVE window — Hermes would compress too late while "
                "the server silently truncates. Pass the server's configured cap "
                "(e.g. --context-length 131072)."
            )
        base_url = cfg.ollama.url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        # An authenticated endpoint stores its token in the PROJECT .env
        # under the name in [ollama].api_key_env (config.toml never holds
        # the secret). Writing the dummy key against such an endpoint
        # would 401 every request.
        api_key = _external_ollama_api_key(cfg)
    else:
        base_url, api_key = _url_for(engine, model), _api_key_for(engine)

    agent_type = _detect_type_from_compose(cfg, agent_name)
    if agent_type is None:
        raise UserError(f"agent {agent_name!r} not in compose")
    if agent_type != "hermes":
        raise UserError(
            f"`agents model use` is Hermes-only — {agent_name!r} is type {agent_type!r}. "
            f"For OpenClaw, run `openclaw configure` and set the provider's base_url "
            f"to {base_url} manually."
        )

    with compose.acquire_lock(cfg.paths.host_data_dir):
        doc = hermes_config.read_doc(cfg, agent_name, profile)
        # Structured `model:` form. Overwrites any prior value
        # (including the bare-string form Hermes also accepts).
        doc["model"] = {
            "default": model,
            "provider": "custom",
            "base_url": base_url,
            "api_key": api_key,
        }
        if context_length is not None:
            doc["model"]["context_length"] = context_length
            _upsert_provider_model_context(doc, engine, base_url, api_key, model, context_length)
        # Auxiliary routing — only the requested ones; never touch the
        # others. This preserves user-set vision/compression that the
        # user explicitly configured against a cloud endpoint.
        if aux:
            aux_targets = ("compression", "vision") if aux == "all" else (aux,)
            auxiliary = doc.setdefault("auxiliary", {})
            for task in aux_targets:
                target = auxiliary.setdefault(task, {})
                target["provider"] = "custom"
                target["base_url"] = base_url
                target["api_key"] = api_key
                target["model"] = model
        hermes_config.write_atomically(cfg, agent_name, doc, profile)

    aux_routed = list(("compression", "vision") if aux == "all" else ((aux,) if aux else ()))
    payload = {
        "agent": agent_name,
        "engine": engine,
        "model": model,
        "context_length": context_length,
        "base_url": base_url,
        "profile": profile,
        "aux_routed": aux_routed,
    }
    ui.emit(
        payload,
        human=(
            f"{agent_name}: model wired\n"
            f"  engine:  {engine}\n"
            f"  model:   {model}\n"
            f"  url:     {base_url}\n"
            + (
                f"  aux:     {', '.join(aux_routed)} routed to same endpoint\n"
                if aux_routed
                else ""
            )
            + f"  next: `agents restart {agent_name}` for in-flight sessions to pick this up"
        ),
    )
    return payload


def _upsert_provider_model_context(
    doc: Any,
    engine: str,
    base_url: str,
    api_key: str,
    model: str,
    context_length: int,
) -> None:
    """Record the per-model context window under ``custom_providers``.

    Hermes matches these entries by normalized base_url (provider name is
    cosmetic), and this is the lookup its ``/model`` switch + display
    paths use — ``model.context_length`` alone only covers the default
    model at startup. Upsert semantics: reuse an existing entry for the
    same endpoint (whatever the user named it), never drop its other
    models, only set/overwrite this model's context_length.
    """
    entries = doc.setdefault("custom_providers", [])
    entry = next(
        (
            e
            for e in entries
            if isinstance(e, dict)
            and str(e.get("base_url", "")).rstrip("/") == base_url.rstrip("/")
        ),
        None,
    )
    if entry is None:
        entry = {"name": engine, "base_url": base_url, "api_key": api_key}
        entries.append(entry)
    models_map = entry.setdefault("models", {})
    existing = models_map.get(model)
    if isinstance(existing, dict):
        existing["context_length"] = context_length
    else:
        models_map[model] = {"context_length": context_length}


# ---------- ollama internals ----------


def _pull_ollama(cfg: AgentsConfig, model: str) -> dict[str, Any]:
    _require_ollama_running(cfg)
    ui.progress(f"pulling {model} into ollama (multi-GB — may take a while)...")
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        OLLAMA_SERVICE_NAME,
        "ollama",
        "pull",
        model,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeFailure(f"`ollama pull {model}` failed: {result.stderr.strip()}")
    ui.progress(f"pull complete: {model}")
    ui.emit(
        {"engine": "ollama", "model": model, "pulled": True},
        human=f"ollama: pulled {model}",
    )
    return {"engine": "ollama", "model": model, "pulled": True}


def _rm_ollama(cfg: AgentsConfig, model: str) -> dict[str, Any]:
    _require_ollama_running(cfg)
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        OLLAMA_SERVICE_NAME,
        "ollama",
        "rm",
        model,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeFailure(f"`ollama rm {model}` failed: {result.stderr.strip()}")
    ui.emit(
        {"engine": "ollama", "model": model, "removed": True},
        human=f"ollama: removed {model}",
    )
    return {"engine": "ollama", "model": model, "removed": True}


def _list_ollama(cfg: AgentsConfig) -> list[dict[str, Any]]:
    if not _ollama_running(cfg):
        return []
    from agents_cli import inference_cmd

    return inference_cmd._ollama_list_models(cfg)


def _require_ollama_running(cfg: AgentsConfig) -> None:
    if not _ollama_running(cfg):
        raise UserError("ollama is not running — start it with `agents inference up ollama` first")


def _ollama_running(cfg: AgentsConfig) -> bool:
    from agents_cli import inference_cmd

    return inference_cmd._service_running(cfg, OLLAMA_SERVICE_NAME)


# ---------- llama.cpp internals ----------


def _pull_llamacpp_gguf(cfg: AgentsConfig, model: str) -> dict[str, Any]:
    """Download a GGUF from HuggingFace into ``<data-dir>/models/llama-cpp/``.

    ``model`` format: ``<hf-repo>:<filename>``.
    Example: ``bartowski/Llama-3.2-3B-Instruct-GGUF:Llama-3.2-3B-Instruct-Q4_K_M.gguf``.

    Uses ``huggingface_hub.hf_hub_download`` inside the running ollama
    container (which has Python available) so we don't grow another
    image just for downloads. The download target is the container's
    ``LLAMACPP_DOWNLOAD_MOUNT`` bind (see ``compose.ollama_service_fragment``),
    which is ``<data-dir>/models/llama-cpp/`` on the host — the same dir
    ``model list``/``model rm`` walk and the per-model ``llama-cpp-*``
    services mount at ``/models``. Before downloading we probe that the
    bind is actually present (an ollama container created from an older
    fragment lacks it — anything written there would land on the
    overlay fs and vanish on recreate).
    """
    if ":" not in model:
        raise UserError(
            "llama-cpp pull format is `<hf-repo>:<filename>` "
            "(e.g. `bartowski/Llama-3.2-3B-Instruct-GGUF:Llama-3.2-3B-Instruct-Q4_K_M.gguf`)"
        )
    repo, filename = model.split(":", 1)
    if not _ollama_running(cfg):
        raise UserError(
            "the toolkit uses the running ollama container as a download host "
            "for HF GGUF pulls. Start ollama first: `agents inference up ollama`."
        )

    _require_llamacpp_mount_visible(cfg)

    # Install hf_hub on first use (lives in container's overlay fs, lost
    # on recreate — but that's fine; we'll reinstall on next pull).
    # NOTE the braces: `A || B && C` parses as `(A || B) && C`, which
    # would run apt-get install on EVERY pull (and hard-fail offline).
    pip = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        OLLAMA_SERVICE_NAME,
        "sh",
        "-c",
        "command -v python3 >/dev/null 2>&1 || { apt-get -qq update && apt-get -qq install -y python3 python3-pip; }; "
        "python3 -m pip install --quiet --break-system-packages huggingface_hub",
        check=False,
    )
    if pip.returncode != 0:
        raise RuntimeFailure(
            f"could not install huggingface_hub in ollama container: {pip.stderr.strip()}"
        )

    # Download via hf_hub into the bind-mounted dir the host sees as
    # <data-dir>/models/llama-cpp/ (probed above).
    ui.progress(f"downloading {filename} from {repo} (multi-GB — may take a while)...")
    download = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        OLLAMA_SERVICE_NAME,
        "python3",
        "-c",
        f"from huggingface_hub import hf_hub_download; "
        f"p = hf_hub_download(repo_id={repo!r}, filename={filename!r}, "
        f"local_dir={LLAMACPP_DOWNLOAD_MOUNT!r}, "
        f"local_dir_use_symlinks=False); print(p)",
        check=False,
    )
    if download.returncode != 0:
        raise RuntimeFailure(f"hf_hub_download failed: {download.stderr.strip()}")
    ui.progress(f"download complete: {filename}")

    ui.emit(
        {"engine": "llama-cpp", "repo": repo, "filename": filename, "pulled": True},
        human=(
            f"llama-cpp: pulled {filename} from {repo}\n"
            f"  saved under <data-dir>/models/llama-cpp/\n"
            f"  next: `agents inference up-model llama-cpp <slug> --gguf {filename}`"
        ),
    )
    return {"engine": "llama-cpp", "repo": repo, "filename": filename, "pulled": True}


def _require_llamacpp_mount_visible(cfg: AgentsConfig) -> None:
    """Refuse the pull unless the ollama container's ``/llama-cpp`` bind
    actually reaches ``<data-dir>/models/llama-cpp/``.

    An ollama service created from a pre-bind compose fragment keeps its
    old volume set until the container is recreated — writes to
    ``/llama-cpp`` would then land on the container overlay: invisible
    to ``model list``, refused by ``inference load``, and wiped on the
    next recreate. Probe by touching a sentinel inside the container and
    checking it appears on the shared dir; fail loudly with the recreate
    hint if it doesn't. Runs BEFORE the download so a multi-GB pull
    isn't wasted on a doomed destination.
    """
    sentinel = ".toolkit-mount-probe"
    probe = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        OLLAMA_SERVICE_NAME,
        "sh",
        "-c",
        f"mkdir -p {LLAMACPP_DOWNLOAD_MOUNT} && touch {LLAMACPP_DOWNLOAD_MOUNT}/{sentinel}",
        check=False,
    )
    if probe.returncode != 0:
        raise RuntimeFailure(
            f"could not write to {LLAMACPP_DOWNLOAD_MOUNT} in the ollama container: "
            f"{probe.stderr.strip()}"
        )
    try:
        if cfg.paths.data_dir_in_vm:
            check = mp.exec_in_vm(
                cfg.vm.name,
                "test",
                "-e",
                f"{cfg.paths.vm_agents_root}/models/llama-cpp/{sentinel}",
                check=False,
            )
            visible = check.returncode == 0
        else:
            visible = (cfg.paths.host_data_dir / "models" / "llama-cpp" / sentinel).exists()
    finally:
        mp.exec_in_vm(
            cfg.vm.name,
            "docker",
            "exec",
            OLLAMA_SERVICE_NAME,
            "rm",
            "-f",
            f"{LLAMACPP_DOWNLOAD_MOUNT}/{sentinel}",
            check=False,
        )
    if not visible:
        raise UserError(
            f"the ollama container has no {LLAMACPP_DOWNLOAD_MOUNT} bind mount — it was "
            "created from an older compose fragment, so the download would land on the "
            "container's overlay fs (invisible to `agents model list`, wiped on recreate). "
            "Recreate it first: `agents inference down ollama && agents inference up ollama`, "
            "then retry the pull."
        )


def _list_llamacpp(cfg: AgentsConfig) -> list[dict[str, Any]]:
    """Walk ``<data-dir>/models/llama-cpp/`` for ``.gguf`` files."""
    if cfg.paths.data_dir_in_vm:
        # In-VM mode: shell into VM and ls the dir.
        result = mp.exec_in_vm(
            cfg.vm.name,
            "find",
            f"{cfg.paths.vm_agents_root}/models/llama-cpp",
            "-maxdepth",
            "2",
            "-name",
            "*.gguf",
            "-printf",
            "%P %s\n",
            check=False,
        )
        if result.returncode != 0:
            return []
        out = []
        for line in result.stdout.splitlines():
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                out.append({"filename": parts[0], "size_bytes": int(parts[1])})
        return out

    root = cfg.paths.host_data_dir / "models" / "llama-cpp"
    if not root.exists():
        return []
    return [
        {"filename": str(p.relative_to(root)), "size_bytes": p.stat().st_size}
        for p in root.rglob("*.gguf")
    ]


def _rm_llamacpp_gguf(cfg: AgentsConfig, model: str) -> dict[str, Any]:
    """Delete a GGUF file. ``model`` is the filename relative to
    ``<data-dir>/models/llama-cpp/``.

    Refuses if any ``llama-cpp-*`` service is currently bound to it
    (would orphan the service).
    """
    target = cfg.paths.host_data_dir / "models" / "llama-cpp" / model
    if cfg.paths.data_dir_in_vm:
        rm = mp.exec_in_vm(
            cfg.vm.name,
            "rm",
            "-f",
            f"{cfg.paths.vm_agents_root}/models/llama-cpp/{model}",
            check=False,
        )
        if rm.returncode != 0:
            raise RuntimeFailure(f"rm failed: {rm.stderr.strip()}")
    else:
        if not target.exists():
            raise UserError(f"no such GGUF: {target}")
        # Sanity: refuse if a compose service references it.
        doc = compose.read_compose_doc(cfg)
        for svc_name, svc in (doc.get("services") or {}).items():
            if not svc_name.startswith(LLAMACPP_SERVICE_PREFIX):
                continue
            cmd = svc.get("command") or []
            if any(model in (str(a)) for a in cmd):
                raise UserError(
                    f"GGUF {model} is loaded by service {svc_name!r}; "
                    f"unload it first with `agents inference down-model llama-cpp "
                    f"{svc_name[len(LLAMACPP_SERVICE_PREFIX) :]}`"
                )
        target.unlink()
    ui.emit(
        {"engine": "llama-cpp", "model": model, "removed": True},
        human=f"llama-cpp: removed {model}",
    )
    return {"engine": "llama-cpp", "model": model, "removed": True}


# ---------- URL/api-key resolution per engine ----------


def _url_for(engine: str, model: str) -> str:
    """The Docker-network DNS URL the Hermes container uses to reach
    the engine. For llama-cpp, the per-model service name encodes the
    model slug (since each model is its own service).
    """
    if engine == "ollama":
        return f"http://{OLLAMA_SERVICE_NAME}:11434/v1"
    slug = _slugify(model)
    return f"http://{LLAMACPP_SERVICE_PREFIX}{slug}:8080/v1"


def _external_ollama_api_key(cfg: AgentsConfig) -> str:
    env_name = (cfg.ollama.api_key_env or "").strip()
    if not env_name:
        # Unauthenticated endpoint: any non-empty key satisfies the
        # OpenAI client library; the server ignores it.
        return "ollama"
    env_path = cfg.paths.host_data_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == env_name and value.strip():
                return value.strip()
    import os

    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    raise UserError(
        f"[ollama].api_key_env is set to {env_name!r} but no value was found in the "
        f"project .env or the environment — re-run `agents ollama configure --api-key ...`"
    )


def _api_key_for(engine: str) -> str:
    # Both Ollama and llama-server accept (and require) an arbitrary
    # non-empty key when the OpenAI client library is talking to them
    # (the client refuses to send requests without one). The actual
    # value is ignored. Use the canonical convention.
    return "ollama" if engine == "ollama" else "sk-local"


def _slugify(name: str) -> str:
    """Sanitize a model name into a docker-safe slug. Lowercase, alnum
    + hyphens, max 39 chars (docker container name limit minus prefix).
    """
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return s[:39] or "model"


# ---------- rendering ----------


def _emit_list(payload: dict[str, Any]) -> None:
    lines: list[str] = []
    if "ollama" in payload:
        rows = payload["ollama"]
        if rows:
            lines.append(f"ollama: {len(rows)} model(s)")
            for r in rows[:20]:
                lines.append(f"  {r['name']:<32}  {r.get('size', '')}")
        else:
            lines.append("ollama: no models (or ollama not running)")
    if "llama_cpp" in payload:
        rows = payload["llama_cpp"]
        if rows:
            total = sum(r["size_bytes"] for r in rows)
            lines.append(f"llama-cpp: {len(rows)} GGUF file(s), {total / 1e9:.1f} GB total")
            for r in rows[:20]:
                size_gb = r["size_bytes"] / 1e9
                lines.append(f"  {r['filename']:<48}  {size_gb:>5.1f} GB")
        else:
            lines.append("llama-cpp: no GGUFs under <data-dir>/models/llama-cpp/")
    ui.emit(payload, human="\n".join(lines))
