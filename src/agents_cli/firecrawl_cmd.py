"""Self-hosted Firecrawl stack, orchestrated as a project-level sibling.

One Firecrawl deployment per VM, shared across every Hermes agent the
toolkit manages. ``agents firecrawl up`` adds five docker compose
services (api / playwright / redis / rabbitmq / postgres), generates
the auth bits into ``<data-dir>/.env``, and auto-wires every Hermes
agent's ``FIRECRAWL_API_KEY`` + ``FIRECRAWL_API_URL`` into
``terminal.env_passthrough`` so both the Hermes Python tools AND the
LLM's shell-based diagnostics see the configuration.

Pinning policy
--------------

Upstream ships only floating tags (``latest``, ``linux-amd64``). The
toolkit's locked "no ``:latest``" rule means we have to digest-pin —
but we can't bake digests as constants because they drift weekly.
Resolution: on first ``firecrawl up``, the toolkit pulls each
upstream floating tag, captures the resulting ``RepoDigest`` via
``docker image inspect``, writes the digests to
``<data-dir>/.firecrawl-state.json``, and the generated compose entry
references only the digest-pinned form (``image@sha256:…``). Subsequent
``up`` invocations reuse the captured digests. ``firecrawl up
--upgrade`` re-resolves them.

State file: ``<data-dir>/.firecrawl-state.json``
------------------------------------------------

::

    {
      "images": {
        "api":        "ghcr.io/firecrawl/firecrawl@sha256:…",
        "playwright": "ghcr.io/firecrawl/playwright-service@sha256:…",
        "postgres":   "ghcr.io/firecrawl/nuq-postgres@sha256:…",
        "redis":      "redis@sha256:…",
        "rabbitmq":   "rabbitmq@sha256:…"
      },
      "api_url":    "http://firecrawl-api:3002"
    }

The other auth secrets (``FIRECRAWL_API_KEY``, ``BULL_AUTH_KEY``,
``POSTGRES_PASSWORD``) live in ``<data-dir>/.env`` — same place as
``HERMES_UID`` / ``HERMES_GID`` — so compose ``${VAR}`` interpolation
resolves them naturally.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from agents_cli import compose, compose_runner, hermes_config, mp, sidecar, ui
from agents_cli import config as cfg_mod
from agents_cli.compose import (
    FIRECRAWL_API_IMAGE_BASE,
    FIRECRAWL_PLAYWRIGHT_IMAGE_BASE,
    FIRECRAWL_POSTGRES_IMAGE_BASE,
    FIRECRAWL_RABBITMQ_IMAGE_BASE,
    FIRECRAWL_REDIS_IMAGE_BASE,
    FIRECRAWL_SERVICE_PREFIX,
)
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError


def _docker_arch() -> str:
    """Thin alias for :func:`sidecar.docker_arch` — kept because ops.py's
    doctor checks (and their tests) reference it by this name.
    """
    return sidecar.docker_arch()


# Upstream-floating references the toolkit uses ONCE to resolve digests.
# These get written to the compose file only as ``<repo>@sha256:<digest>``,
# never as ``<repo>:<tag>`` — see module docstring on pinning policy.
#
# Firecrawl's ``api`` image ships per-arch tags (``:linux-amd64`` and
# ``:linux-arm64`` are separate single-arch images, NOT a multi-arch
# manifest), so the tag must match the host's arch at digest-resolve
# time. ``nuq-postgres`` has no arm64 tag — on arm64 hosts it runs
# under qemu-user emulation (see ``_check_arm64_emulation`` in ``ops``).
# The other three (playwright / redis / rabbitmq) ship multi-arch
# manifests, so a single tag suffices.
FLOATING_REFS: dict[str, str] = {
    "api": f"{FIRECRAWL_API_IMAGE_BASE}:linux-{_docker_arch()}",
    "playwright": f"{FIRECRAWL_PLAYWRIGHT_IMAGE_BASE}:latest",
    "postgres": f"{FIRECRAWL_POSTGRES_IMAGE_BASE}:latest",
    "redis": f"{FIRECRAWL_REDIS_IMAGE_BASE}:alpine",
    "rabbitmq": f"{FIRECRAWL_RABBITMQ_IMAGE_BASE}:3-management",
}

# Images that are amd64-only at upstream and will run under emulation
# on arm64 hosts. Surfaced by ``_check_arm64_emulation`` in doctor.
ARM64_EMULATED_IMAGES: tuple[str, ...] = (
    f"{FIRECRAWL_POSTGRES_IMAGE_BASE} (Firecrawl publishes no arm64 tag)",
)

# Env-var names the project-level .env carries when firecrawl is up.
# Hermes agents see API_KEY + API_URL via compose ``${VAR:-}``
# interpolation in their service fragment; passthrough is a separate
# Hermes-internal allowlist for the sandboxed terminal/execute_code
# subprocesses (see hermes_config.add_env_passthrough).
PROJECT_ENV_KEYS: tuple[str, ...] = (
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "FIRECRAWL_BULL_AUTH_KEY",
    "FIRECRAWL_POSTGRES_PASSWORD",
)

# Subset that's also propagated into Hermes' env_passthrough — only the
# pair the agent's tools actually use, not the internal-only
# BULL/POSTGRES secrets that are for the Firecrawl stack itself.
AGENT_PASSTHROUGH_KEYS: tuple[str, ...] = (
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
)

# Env vars OpenClaw's bundled Firecrawl plugin reads from its container.
# Different from Hermes' pair because OpenClaw spells the base-URL as
# ``FIRECRAWL_BASE_URL`` (Hermes uses ``FIRECRAWL_API_URL``). The
# OpenClaw compose fragment bridges the asymmetry via interpolation —
# both ultimately resolve to the same project ``.env`` value.
# No ``terminal.env_passthrough`` analogue: OpenClaw has no sandbox env
# filter, its shell tools inherit container env directly.
OPENCLAW_ENV_KEYS: tuple[str, ...] = (
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_BASE_URL",
)

# `agents firecrawl status` returns these payload keys so JSON callers
# can rely on them.
STATUS_KEYS: tuple[str, ...] = ("running", "api_url", "wired_agents", "images")

# The api's address on the project docker network — what lands in the
# state file, the project .env, and every agent's container env.
API_URL_IN_VM = "http://firecrawl-api:3002"


def _state_path(cfg: AgentsConfig) -> Path:
    return cfg.paths.host_data_dir / ".firecrawl-state.json"


def read_state(cfg: AgentsConfig) -> dict[str, Any]:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(cfg: AgentsConfig, payload: dict[str, Any]) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def clear_state(cfg: AgentsConfig) -> None:
    _state_path(cfg).unlink(missing_ok=True)


def is_installed(cfg: AgentsConfig) -> bool:
    """True iff the project compose has firecrawl services AND the state
    file says we own them. Used by ``agents create`` to decide whether
    to auto-wire a new agent's passthrough.
    """
    state = read_state(cfg)
    if "images" not in state:
        return False
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return False
    services = doc.get("services") or {}
    return any(s.startswith(FIRECRAWL_SERVICE_PREFIX) for s in services)


# ---------- CLI entry points ----------


def up(
    *,
    upgrade: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
    emit: bool = True,
) -> dict[str, Any]:
    """Install (or upgrade) the firecrawl stack and wire every Hermes agent.

    First-run flow: pull floating tags, resolve digests, generate
    compose entries, populate project ``.env`` secrets, ``compose up``,
    probe ``firecrawl-api`` until it answers (bounded wait — the api has
    no healthcheck and ``up -d`` wouldn't wait on one anyway), and only
    then add the FIRECRAWL_API_KEY / FIRECRAWL_API_URL pair to each
    Hermes agent's ``terminal.env_passthrough`` so the wiring is visible
    to sandboxed tools too. A failed up / probe raises
    :class:`RuntimeFailure` (exit 2 — the documented exit-code contract)
    after persisting the degraded state; nothing new is wired.

    ``upgrade=True`` re-resolves digests against the upstream floating
    tags, even if a state file already exists.

    When the state file is missing (or lacks ``images``) but the compose
    doc still carries the firecrawl service blocks — the post-VM-
    migration stranding — the digest pins are reconstructed from compose
    instead of re-resolved, so a plain ``up`` never turns into an
    unintended upgrade. See :func:`_reconstruct_pins_from_compose`.

    ``emit=False`` skips the final :func:`ui.emit` so composite callers
    (``vm rebuild``) that own the command's single stdout object can
    embed this flow; the payload is still returned.
    """
    cfg = cfg_mod.load(config_path)
    if not cfg.vm_runtime.is_populated():
        raise UserError("vm_runtime not initialised — run `agents vm init` first")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        state = read_state(cfg)
        if upgrade:
            images = _resolve_digests(cfg)
            state = {"images": images, "api_url": API_URL_IN_VM}
        elif "images" in state:
            images = state["images"]
        else:
            reconstructed = _reconstruct_pins_from_compose(cfg)
            if reconstructed is not None:
                ui.progress(
                    "reconstructing digest pins from compose — a VM migration "
                    "likely lost .firecrawl-state.json; reusing the exact "
                    "pinned digests instead of re-resolving upstream tags..."
                )
                images = reconstructed
            else:
                images = _resolve_digests(cfg)
            state = {"images": images, "api_url": API_URL_IN_VM}

        _ensure_project_env(cfg)
        _ensure_postgres_dir(cfg)

        doc = compose.read_compose_doc(cfg)
        services = doc.setdefault("services", {})
        fragments = compose.firecrawl_service_fragments(images=images)
        for svc_name, fragment in fragments.items():
            services[svc_name] = fragment
        compose.write_compose_atomically(cfg, doc)

        # Persist the digest pins BEFORE the (long, lock-free) compose
        # up: a failed up must NOT make the retry re-resolve floating
        # tags — that would be an unintended upgrade (see module
        # docstring). ``degraded`` marks a stack whose api never came
        # up healthy; it's cleared once the probe below passes, and it
        # keeps ``ensure_agent_wired`` (the `agents create` hook) from
        # advertising a dead stack to new agents in the meantime.
        state["degraded"] = True
        write_state(cfg, state)

    # Bring the stack up, then PROBE. `compose up -d` returning 0 means
    # "containers created/started", not "api serving" — the api fragment
    # carries no healthcheck and `up -d` doesn't wait on health anyway.
    # Only a passing probe earns `running: True` and the agent wiring.
    failure_reason: str | None = None
    ui.progress(f"starting firecrawl stack ({len(fragments)} services)...")
    try:
        compose_runner.up(cfg, *fragments.keys())
    except RuntimeFailure as e:
        failure_reason = f"compose up failed: {e}"
    if failure_reason is None:
        ui.progress(
            f"waiting for firecrawl-api to become healthy (up to {int(_HEALTH_TIMEOUT_S)}s)..."
        )
        if not _wait_api_healthy(cfg):
            failure_reason = (
                f"firecrawl-api did not become healthy within {int(_HEALTH_TIMEOUT_S)}s"
            )

    if failure_reason is not None:
        # The degraded state (digest pins + flag) is already on disk from
        # the locked window above. Raise — not emit-and-return — so
        # `cli._run` exits 2 per the exit-code contract; shell scripts
        # must not sail past a stack that never came up. ui's error
        # handler emits the single error object, keeping the
        # one-JSON-object stdout contract (emitting a payload here AND
        # raising would print two).
        raise RuntimeFailure(
            f"firecrawl did not start: {failure_reason}. "
            "Digests stay pinned in .firecrawl-state.json; re-run "
            "`agents firecrawl up` to retry without re-resolving. "
            "No new agents were wired."
        )

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Revalidate before committing success: the up/probe window ran
        # lock-free, so another mutating command may have interleaved.
        # Committing the stale local `state` blindly would resurrect a
        # destroyed stack's state file and wire agents to nothing.
        current = read_state(cfg)
        if "images" not in current or not _firecrawl_services_in_compose(cfg):
            # `agents firecrawl destroy` (or equivalent teardown) won the
            # race: state file and/or compose services are gone. Abort
            # the success commit — no state rewrite, no wiring.
            raise RuntimeFailure(
                "firecrawl came up healthy, but a concurrent command tore "
                "the stack down during the start window (state file or "
                "compose services are gone — `agents firecrawl destroy`?). "
                "Nothing was rewritten and no agents were wired; re-run "
                "`agents firecrawl up` if the stack is still wanted."
            )
        if current["images"] == images:
            # No interleaving re-pin — commit our success by clearing the
            # degraded marker written before the up.
            current.pop("degraded", None)
            write_state(cfg, current)
        else:
            # A concurrent `firecrawl up --upgrade` re-resolved the pins
            # during our window. Its state file is newer than our
            # in-memory copy — prefer the CURRENT pins, skip rewriting
            # ours (its own success commit will clear its flag).
            ui.warn(
                "a concurrent `agents firecrawl up` re-pinned the image "
                "digests during the start window; keeping its newer pins."
            )
        images = current["images"]
        api_url = str(current.get("api_url") or state["api_url"])

        # Auto-wire every existing Hermes agent's passthrough — only now
        # that the stack is observably serving. OpenClaw agents are wired
        # purely via compose-fragment env interpolation (no passthrough
        # analogue) — see ``compose.openclaw_service_fragment``.
        wired_hermes = _ensure_all_agents_wired(cfg)

    wired_openclaw = _openclaw_agents_with_env(cfg)
    wired = wired_hermes + wired_openclaw
    stale = _agents_with_stale_fragment(cfg)
    payload: dict[str, Any] = {
        "running": True,
        "api_url": api_url,
        "wired_agents": wired,
        "stale_agents": stale,
        "images": images,
    }
    human_lines = [
        f"firecrawl: up at {api_url}",
        f"  wired agents: {', '.join(wired) or '(none)'}",
    ]
    if wired_openclaw:
        human_lines.append(
            "  note: OpenClaw agents have the env wiring in place, but the "
            "bundled Firecrawl plugin must still be enabled in-container "
            "via `openclaw configure` (or picked during onboarding)."
        )
    human_lines.append("  images:")
    human_lines.extend(f"    {k}: {v}" for k, v in images.items())
    if emit:
        ui.emit(payload, human="\n".join(human_lines))
    if stale:
        ui.warn(
            "Agents created before the firecrawl interpolation entries "
            "landed in the compose fragment don't have firecrawl env in "
            "their container yet. Hermes: `agents reseat <name>`. OpenClaw "
            "has no reseat path — recreate via remove + create, or hand-edit "
            "the compose service block.\nAffected:\n  " + "\n  ".join(stale)
        )
    return payload


def down(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> None:
    """Stop the firecrawl stack — keeps the postgres data, removes the
    agent passthrough wiring so the LLM's diagnostics correctly report
    "not configured" again.
    """
    cfg = cfg_mod.load(config_path)
    if not is_installed(cfg):
        raise UserError("firecrawl is not installed")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        services = list(_firecrawl_services_in_compose(cfg))
        if services:
            compose_runner.stop(cfg, *services)
            compose_runner.rm(cfg, *services)
        _drop_all_agents_wired(cfg)

    ui.emit({"running": False}, human="firecrawl: stopped (postgres data preserved)")


def destroy(
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """Tear down completely: stop, drop compose entries, wipe state file,
    drop project ``.env`` keys, drop agent passthrough wiring, delete
    the postgres data dir.
    """
    cfg = cfg_mod.load(config_path)
    if not yes and not ui.confirm(
        "destroy the firecrawl stack? This deletes the postgres data dir too.",
        default=False,
    ):
        raise UserError("aborted by user")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        services = list(_firecrawl_services_in_compose(cfg))
        if services:
            compose_runner.stop(cfg, *services)
            compose_runner.rm(cfg, *services)

        doc = compose.read_compose_doc(cfg)
        compose_services = doc.get("services") or {}
        for svc_name in list(compose_services):
            if svc_name.startswith(FIRECRAWL_SERVICE_PREFIX):
                del compose_services[svc_name]
        compose.write_compose_atomically(cfg, doc)

        _drop_all_agents_wired(cfg)
        _drop_project_env_keys(cfg)
        clear_state(cfg)
        _remove_postgres_dir(cfg)

    ui.emit({"running": False, "destroyed": True}, human="firecrawl: destroyed")


def status(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Snapshot the firecrawl deployment: per-container resource use,
    api health probe, redis-side rate-limit counters, image digests,
    wired agents.

    No side effects — safe to run in tight loops / from cron.
    """
    cfg = cfg_mod.load(config_path)
    state = read_state(cfg)
    services = list(_firecrawl_services_in_compose(cfg))
    running = bool(services) and _service_running(cfg, "firecrawl-api")
    wired = _list_wired_agents(cfg)
    payload: dict[str, Any] = {
        "running": running,
        "installed": bool(services),
        "api_url": state.get("api_url"),
        "wired_agents": wired,
        "images": state.get("images", {}),
    }
    if not services:
        ui.emit(payload, human="firecrawl: not installed (run `agents firecrawl up`)")
        return payload

    payload["containers"] = _container_stats(cfg, services)
    payload["api_health"] = _api_health(cfg)
    payload["rate_limits"] = _rate_limit_snapshot(cfg) if running else {}
    payload["queues"] = _queue_stats(cfg) if running else {"rabbitmq": [], "bull": []}
    payload["requests"] = _request_counts(cfg) if running else {}
    payload["saturation"] = _saturation(payload["containers"]) if running else {}

    # One name-column width shared across containers + both queue
    # blocks. Makes "running", "pending=", "active=" all start at the
    # same horizontal position — the three sections read as one
    # logical table.
    queues = payload["queues"]
    rmq_qs = queues.get("rabbitmq") or []
    bull_qs = queues.get("bull") or []
    name_w = max(
        (
            *(len(s) for s in payload["containers"]),
            *(len(q["name"]) for q in rmq_qs + bull_qs),
            12,  # floor for short-name-only runs
        ),
    )

    human_lines = [
        f"firecrawl: {'running' if running else 'stopped'} at {state.get('api_url', '?')}",
        f"  wired agents: {', '.join(wired) or '(none)'}",
        f"  api health:   {payload['api_health']}",
    ]
    req = payload.get("requests", {})
    if req:
        human_lines.append(
            f"  requests served: {req.get('lifetime', '?')} lifetime, "
            f"{req.get('last_hour', '?')} in last hour"
        )
    sat = payload.get("saturation", {})
    if sat:
        human_lines.append(f"  saturation:   {sat['message']}")

    human_lines.append("  containers:")
    for svc, stats in payload["containers"].items():
        human_lines.append(
            f"    {svc:<{name_w}}  {stats['state']:<8}  "
            f"cpu={stats.get('cpu_pct', '?'):<7} "
            f"mem={stats.get('mem_usage', '?')}"
        )

    if rmq_qs or bull_qs:
        if rmq_qs:
            human_lines.append("  queues (RabbitMQ — current depth):")
            for q in rmq_qs:
                human_lines.append(
                    f"    {q['name']:<{name_w}}  "
                    f"pending={q['pending']:<3}  "
                    f"in-flight={q['in_flight']:<3}  "
                    f"consumers={q['consumers']}"
                )
        if bull_qs:
            bull_any = any(
                any(q.get(k, 0) for k in ("active", "completed", "failed", "wait")) for q in bull_qs
            )
            human_lines.append(
                "  queues (Bull / redis — lifetime totals):"
                if bull_any
                else "  queues (Bull / redis — lifetime totals, all idle):"
            )
            for q in bull_qs:
                human_lines.append(
                    f"    {q['name']:<{name_w}}  "
                    f"active={q.get('active', 0):<3}  "
                    f"completed={q.get('completed', 0):<4}  "
                    f"failed={q.get('failed', 0):<3}  "
                    f"waiting={q.get('wait', 0)}"
                )

    rl = payload["rate_limits"]
    if rl:
        human_lines.append("  rate-limit usage (current window):")
        for mode, n in sorted(rl.items()):
            human_lines.append(f"    {mode:<22} {n} req/min")
    else:
        # `USE_DB_AUTHENTICATION=false` (the toolkit default) doesn't
        # just skip the redis counter STORAGE — it short-circuits the
        # auth middleware before any `consume()` call, so the in-source
        # caps (scrape=100/min etc) never fire either. Effectively
        # unbounded. Verified empirically: 30 parallel scrapes return
        # 30x 200s with zero 429s.
        human_lines.append(
            "  rate-limit counters: not tracked — auth disabled, "
            "in-source caps are not enforced either."
        )
    ui.emit(payload, human="\n".join(human_lines))
    return payload


def logs(
    *,
    service: str | None = None,
    follow: bool = False,
    since: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    cfg = cfg_mod.load(config_path)
    target = service or "firecrawl-api"
    if not target.startswith(FIRECRAWL_SERVICE_PREFIX):
        target = f"{FIRECRAWL_SERVICE_PREFIX}{target}"
    if not _service_in_compose(cfg, target):
        raise UserError(f"unknown firecrawl service {target!r}")
    result = compose_runner.logs(cfg, target, follow=follow, since=since)
    ui.emit({"service": target, "logs": result.stdout}, human=result.stdout.rstrip())


# ---------- helpers exposed to agents_cmd.create ----------


def ensure_agent_wired(cfg: AgentsConfig, agent_name: str) -> None:
    """Add the FIRECRAWL_API_KEY / FIRECRAWL_API_URL pair to a single
    agent's ``terminal.env_passthrough`` — called from ``agents create``
    so a new Hermes agent auto-picks-up firecrawl when it's already up.

    No-op when firecrawl isn't installed. Also a no-op while the state
    file carries the ``degraded`` flag (last ``up`` never saw the api
    healthy) — don't advertise a dead stack to a brand-new agent.
    Idempotent on the agent side (``hermes_config.add_env_passthrough``
    deduplicates).
    """
    if not is_installed(cfg):
        return
    if read_state(cfg).get("degraded"):
        return
    ui.progress(f"firecrawl detected — wiring FIRECRAWL_* env passthrough for {agent_name!r}...")
    _wire_agent(cfg, agent_name)


# ---------- internals ----------


# Bounded wait for the api to start serving after `compose up`.
# Host-side module — plain time.sleep is fine.
_HEALTH_TIMEOUT_S = 60.0
_HEALTH_INTERVAL_S = 2.0


def _wait_api_healthy(
    cfg: AgentsConfig,
    *,
    timeout_s: float = _HEALTH_TIMEOUT_S,
    interval_s: float = _HEALTH_INTERVAL_S,
) -> bool:
    """Poll :func:`_api_health` until it reports ``healthy`` or the
    window closes. The api typically needs a few seconds after container
    start before its TCP port answers.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _api_health(cfg) == "healthy":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def _resolve_digests(cfg: AgentsConfig) -> dict[str, str]:
    """Pull each upstream floating tag, harvest its repo-digest, return
    a map of role → ``<repo>@sha256:…``. The compose file only ever
    references the digest form so the toolkit's "no ``:latest``" rule
    holds. Allowlist validation happens inside
    :func:`sidecar.resolve_digest`, before any ref is returned.

    Narrated per image via :func:`ui.progress` — five pulls back to
    back run for minutes on a first install, and a silent terminal
    reads as a hang.
    """
    images: dict[str, str] = {}
    total = len(FLOATING_REFS)
    for i, (role, floating) in enumerate(FLOATING_REFS.items(), 1):
        ui.progress(f"[{i}/{total}] pulling {floating} and resolving its digest...")
        images[role] = sidecar.resolve_digest(cfg, floating)
    return images


def _reconstruct_pins_from_compose(cfg: AgentsConfig) -> dict[str, str] | None:
    """Self-heal a lost ``.firecrawl-state.json`` from the compose doc.

    ``compose.firecrawl_service_fragments`` writes ``images[role]``
    verbatim into the ``firecrawl-<role>`` service's ``image`` field, so
    the mapping round-trips: each service block still carries the exact
    digest pin the lost state file recorded. The real-world trigger is a
    VM migration (multipass → libvirt) or rebuild — the host data-dir
    (compose included) survives wholesale while the state file sometimes
    doesn't, and the naive no-state ``up`` would re-resolve upstream
    floating tags = an unintended upgrade (2026-07 incident).

    Compose-read failures PROPAGATE instead of returning ``None`` —
    ``None`` is reserved for a *successfully read* compose doc with no
    usable pins, because the caller treats ``None`` as "first install,
    resolve upstream". A transient multipass transfer error (the normal
    read path in ``--data-dir-in-vm`` mode) must surface as the exit-2
    failure it is, never silently trigger an unintended upgrade.

    Returns ``None`` when reconstruction isn't possible, leaving the
    caller to resolve upstream for ALL roles:

    - no firecrawl services in compose at all → genuine first install,
      resolving is the intended path (no warning);
    - only SOME role services present, an ``image`` that isn't a
      well-formed digest pin, or a digest from the WRONG repository for
      its role (e.g. ``firecrawl-api`` pointing at ``redis@sha256:…``)
      → mixed/misassigned pins would be incoherent, so warn and
      re-resolve everything.

    Role-matching each pin against its :data:`FLOATING_REFS` repository
    implies allowlist membership (rule #5): only refs whose base equals
    the toolkit's own upstream repo for that role are accepted, so a
    doctored compose can't launder an off-allowlist image back through
    the reconstructed state file — it falls into the warn-and-re-resolve
    path like any other unusable pin.
    """
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    images: dict[str, str] = {}
    unusable: list[str] = []
    for role, floating in FLOATING_REFS.items():
        svc_name = f"{FIRECRAWL_SERVICE_PREFIX}{role}"
        svc = services.get(svc_name)
        image = svc.get("image") if isinstance(svc, dict) else None
        if isinstance(image, str) and _is_valid_pin_for_role(image, floating):
            images[role] = image
        else:
            unusable.append(svc_name)
    if not images:
        return None
    if unusable:
        ui.warn(
            "firecrawl services found in compose, but a complete digest-pin "
            "set could not be reconstructed from them ("
            + ", ".join(unusable)
            + " missing, not digest-pinned, or pinned to the wrong repository "
            "for their role). Mixed pins would be incoherent, so ALL roles "
            "will be re-resolved against upstream floating tags — "
            "effectively an upgrade."
        )
        return None
    return images


def _is_valid_pin_for_role(image: str, floating_ref: str) -> bool:
    """True iff ``image`` is a well-formed digest pin
    (``<repo>@sha256:<64 hex>``) whose repository matches the role's
    upstream repository (``floating_ref`` with its tag stripped).
    """
    expected_repo = floating_ref.rsplit(":", 1)[0]
    repo, sep, digest = image.partition("@sha256:")
    return bool(sep) and repo == expected_repo and re.fullmatch(r"[0-9a-f]{64}", digest) is not None


def _ensure_project_env(cfg: AgentsConfig) -> None:
    """Write the four project-level env vars (api key, url, bull auth,
    postgres password) into the project ``.env`` if not already set.
    Existing values are preserved across re-ups.

    Picks the right ``.env`` for the current mode: host file in
    host-mount mode (visible to VM via the multipass mount), VM-side
    file in ``--data-dir-in-vm`` mode (where the host has no copy) —
    both via :func:`sidecar.read_project_env` /
    :func:`sidecar.write_project_env`.
    """
    existing = sidecar.read_project_env(cfg)
    if not existing.get("FIRECRAWL_API_KEY"):
        existing["FIRECRAWL_API_KEY"] = f"fc-self-hosted-{secrets.token_hex(8)}"
    if not existing.get("FIRECRAWL_API_URL"):
        existing["FIRECRAWL_API_URL"] = API_URL_IN_VM
    if not existing.get("FIRECRAWL_BULL_AUTH_KEY"):
        existing["FIRECRAWL_BULL_AUTH_KEY"] = secrets.token_hex(32)
    if not existing.get("FIRECRAWL_POSTGRES_PASSWORD"):
        existing["FIRECRAWL_POSTGRES_PASSWORD"] = secrets.token_hex(16)
    sidecar.write_project_env(cfg, existing)


def _drop_project_env_keys(cfg: AgentsConfig) -> None:
    existing = sidecar.read_project_env(cfg)
    if not existing:
        return
    for k in PROJECT_ENV_KEYS:
        existing.pop(k, None)
    sidecar.write_project_env(cfg, existing)


def _ensure_postgres_dir(cfg: AgentsConfig) -> None:
    """Make sure ``<data-dir>/firecrawl/postgres/`` exists. Postgres
    initialises an empty dir on first start. In ``--data-dir-in-vm``
    mode this happens in the VM; otherwise on the host mount.
    """
    if cfg.paths.data_dir_in_vm:
        mp.exec_in_vm(
            cfg.vm.name,
            "mkdir",
            "-p",
            f"{cfg.paths.vm_agents_root}/firecrawl/postgres",
            check=False,
        )
        return
    (cfg.paths.host_data_dir / "firecrawl" / "postgres").mkdir(parents=True, exist_ok=True)


def _remove_postgres_dir(cfg: AgentsConfig) -> None:
    if cfg.paths.data_dir_in_vm:
        mp.exec_in_vm(
            cfg.vm.name,
            "rm",
            "-rf",
            f"{cfg.paths.vm_agents_root}/firecrawl",
            check=False,
        )
        return
    import shutil

    path = cfg.paths.host_data_dir / "firecrawl"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _agents_with_stale_fragment(cfg: AgentsConfig) -> list[str]:
    """Return agents whose compose service ``environment`` block doesn't
    yet have the firecrawl interpolation entries.

    Hermes needs ``FIRECRAWL_API_KEY`` + ``FIRECRAWL_API_URL`` (see
    ``AGENT_PASSTHROUGH_KEYS``); OpenClaw needs ``FIRECRAWL_API_KEY``
    + ``FIRECRAWL_BASE_URL`` (see ``OPENCLAW_ENV_KEYS``). Both classes
    were originally created before the firecrawl wiring landed in
    their respective service fragment templates.

    Hermes agents are remediated with ``agents reseat <name>`` (which
    rewrites the service block from the current template, preserving
    port/tag/env_file/depends_on additions); OpenClaw doesn't have a
    reseat path yet, so its stale agents need ``agents remove`` +
    ``agents create`` or a manual compose edit.

    Auto-reseating from within ``firecrawl up`` would force-recreate
    the agent's container and interrupt its gateway — too disruptive
    as a silent side-effect, so we surface a hint instead.
    """
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return []
    out: list[str] = []
    services = doc.get("services") or {}
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment") or {}
        if svc_name.startswith("hermes-"):
            required = AGENT_PASSTHROUGH_KEYS
            prefix = "hermes-"
        elif svc_name.startswith("openclaw-"):
            required = OPENCLAW_ENV_KEYS
            prefix = "openclaw-"
        else:
            continue
        if not all(k in env for k in required):
            out.append(svc_name[len(prefix) :])
    return out


def _hermes_agents(cfg: AgentsConfig) -> list[str]:
    doc = compose.read_compose_doc(cfg)
    return [s[len("hermes-") :] for s in (doc.get("services") or {}) if s.startswith("hermes-")]


def _openclaw_agents_with_env(cfg: AgentsConfig) -> list[str]:
    """OpenClaw agents whose compose fragment has the firecrawl env
    interpolation entries. Used as the OpenClaw side of "wired
    agents" — there's no ``terminal.env_passthrough`` analogue to
    check, so fragment presence IS the wiring on the toolkit side.
    Whether OpenClaw's bundled Firecrawl *plugin* is also enabled
    is a question for ``openclaw configure`` — out of the toolkit's
    scope.
    """
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return []
    out: list[str] = []
    for svc_name, svc in (doc.get("services") or {}).items():
        if not svc_name.startswith("openclaw-") or not isinstance(svc, dict):
            continue
        env = svc.get("environment") or {}
        if all(k in env for k in OPENCLAW_ENV_KEYS):
            out.append(svc_name[len("openclaw-") :])
    return out


def _ensure_all_agents_wired(cfg: AgentsConfig) -> list[str]:
    wired: list[str] = []
    for agent in _hermes_agents(cfg):
        _wire_agent(cfg, agent)
        wired.append(agent)
    return wired


def _drop_all_agents_wired(cfg: AgentsConfig) -> None:
    for agent in _hermes_agents(cfg):
        _unwire_agent(cfg, agent)


def _wire_agent(cfg: AgentsConfig, agent_name: str) -> None:
    doc = hermes_config.read_doc(cfg, agent_name)
    for key in AGENT_PASSTHROUGH_KEYS:
        hermes_config.add_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc)


def _unwire_agent(cfg: AgentsConfig, agent_name: str) -> None:
    doc = hermes_config.read_doc(cfg, agent_name)
    for key in AGENT_PASSTHROUGH_KEYS:
        hermes_config.remove_env_passthrough(doc, key)
    hermes_config.write_atomically(cfg, agent_name, doc)


def _list_wired_agents(cfg: AgentsConfig) -> list[str]:
    """Agents whose toolkit-side firecrawl wiring is in place.

    Hermes: ``terminal.env_passthrough`` contains the API key/URL
    pair (the most restrictive Hermes-side check — passthrough is
    added only after the compose env interpolation is present, so
    this also implies the container env is wired).

    OpenClaw: compose service fragment has the env interpolation
    entries. There's no passthrough analogue to check. Note that
    "wired" here means the toolkit has done its part; the OpenClaw
    bundled Firecrawl plugin still needs ``plugins.entries.firecrawl
    .enabled: true`` flipped via ``openclaw configure``.
    """
    out: list[str] = []
    for agent in _hermes_agents(cfg):
        try:
            doc = hermes_config.read_doc(cfg, agent)
        except (RuntimeFailure, UserError):
            continue
        passthrough = (doc.get("terminal") or {}).get("env_passthrough") or []
        if all(k in passthrough for k in AGENT_PASSTHROUGH_KEYS):
            out.append(agent)
    out.extend(_openclaw_agents_with_env(cfg))
    return out


def _container_stats(cfg: AgentsConfig, services: list[str]) -> dict[str, dict[str, str]]:
    """Per-container resource snapshot via ``docker stats --no-stream``.

    Returns a dict keyed by service name, each entry containing
    ``state`` (running/stopped), ``cpu_pct`` (e.g. ``"0.5%"``),
    ``mem_usage`` (e.g. ``"380MiB / 3GiB"``), ``mem_pct``.
    """
    out: dict[str, dict[str, str]] = {svc: {"state": "absent"} for svc in services}

    ps = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "ps",
        "-a",
        "--filter",
        f"name=^{FIRECRAWL_SERVICE_PREFIX}",
        "--format",
        "{{.Names}}\t{{.State}}",
        check=False,
    )
    if ps.returncode == 0:
        for line in ps.stdout.splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) != 2:
                continue
            name, state = parts
            if name in out:
                out[name]["state"] = state

    stats = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
        *services,
        check=False,
    )
    if stats.returncode == 0:
        for line in stats.stdout.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 4:
                continue
            name, cpu, mem, memp = parts[:4]
            if name in out:
                out[name].update(cpu_pct=cpu, mem_usage=mem, mem_pct=memp)
    return out


def _api_health(cfg: AgentsConfig) -> str:
    """Probe ``/v1/health`` on the firecrawl-api over the docker network
    from a sibling container. Returns ``"healthy"`` / ``"unhealthy"`` /
    ``"unknown"``.

    Why a sibling? The api image is built ``FROM`` a minimal node base
    that has no curl or wget — ``docker exec firecrawl-api ...`` to
    probe localhost won't work. Redis' image has the ``timeout`` +
    raw-socket utilities; we just connect TCP to firecrawl-api:3002
    via ``nc`` (busybox netcat in the redis:alpine image).
    """
    from agents_cli.ops import _compose_exec_in_vm_argv

    # Use redis-cli's host as a netcat-style probe: redis:alpine ships
    # busybox netcat. `nc -z` is the standard "is the port open" check.
    probe = _compose_exec_in_vm_argv(
        cfg,
        "firecrawl-redis",
        "nc",
        "-z",
        "-w",
        "3",
        "firecrawl-api",
        "3002",
        no_tty=True,
        as_root=True,
    )
    result = mp.exec_in_vm(cfg.vm.name, *probe, check=False)
    if result.returncode == 0:
        return "healthy"
    err = (result.stderr or "").lower()
    if "is not running" in err or "no such" in err:
        return "unknown"
    return "unhealthy"


def _rate_limit_snapshot(cfg: AgentsConfig) -> dict[str, int]:
    """Read the rate-limit counters firecrawl maintains in redis.

    ``rate-limiter-flexible`` stores one key per (mode, token) pair as
    ``<keyPrefix>:<token>`` with the current window's consumed-points
    count as the value. We aggregate across tokens since self-host
    typically has a single shared bearer (``USE_DB_AUTHENTICATION=false``).

    Returns ``{mode: total_consumed_in_current_window}``. Empty dict
    on any redis error (status command must not fail noisily).
    """
    from agents_cli.ops import _compose_exec_in_vm_argv

    # KEYS is fine on a self-host single-user redis with a small key
    # count; the rate-limiter creates O(modes * tokens) keys, at most
    # a few hundred even in heavy use.
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg,
            "firecrawl-redis",
            "redis-cli",
            "--no-raw",
            "KEYS",
            "*",
            no_tty=True,
            as_root=True,
        ),
        check=False,
    )
    if result.returncode != 0:
        return {}

    # rate-limiter-flexible keys look like ``scrape:fc-self-hosted-…``
    # — mode prefix followed by ``:`` then the bearer token. Skip
    # everything else (firecrawl also writes its own session keys
    # which we don't care about for rate-limit snapshots).
    known_modes = {
        "scrape",
        "crawl",
        "search",
        "map",
        "extract",
        "preview",
        "crawlStatus",
        "extractStatus",
        "browser",
        "browserExecute",
        "account",
    }
    counters: dict[str, int] = {}
    for raw in result.stdout.splitlines():
        key = raw.strip().strip('"')
        if not key or ":" not in key:
            continue
        mode = key.split(":", 1)[0]
        if mode not in known_modes:
            continue
        val = mp.exec_in_vm(
            cfg.vm.name,
            *_compose_exec_in_vm_argv(
                cfg,
                "firecrawl-redis",
                "redis-cli",
                "--no-raw",
                "GET",
                key,
                no_tty=True,
                as_root=True,
            ),
            check=False,
        )
        if val.returncode != 0:
            continue
        # `redis-cli --no-raw GET` returns either a quoted integer string
        # ("42") or `(nil)`. Parse defensively.
        token = val.stdout.strip().strip('"')
        if not token.lstrip("-").isdigit():
            continue
        counters[mode] = counters.get(mode, 0) + int(token)
    return counters


def _saturation(containers: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Turn per-container memory % into a single ok/warn/critical signal.

    Memory is the right axis: rate limits aren't enforced in
    auth-disabled mode (so 429s never fire), and CPU spikes during a
    scrape are normal and harmless. Sustained memory pressure → OOM
    kill → broken stack. Playwright is the usual offender (chromium
    + page contexts).

    Thresholds (against each container's compose ``mem_limit``):

    - **critical** ≥ 95% — OOM imminent, raise the limit NOW.
    - **warn**     ≥ 80% — headroom is thin; bump the limit or
      reduce concurrency.
    - **ok**       < 80% — fine.

    Reports the WORST container, since one bad actor is enough to
    take the stack down. Returns ``{level, worst_service,
    worst_pct, message}`` — JSON consumers get the structured form,
    humans get the message.
    """
    worst_pct = 0.0
    worst_svc = ""
    for svc, stats in containers.items():
        mem_pct_str = stats.get("mem_pct", "")
        if not mem_pct_str:
            continue
        try:
            pct = float(str(mem_pct_str).rstrip("%"))
        except ValueError:
            continue
        if pct > worst_pct:
            worst_pct = pct
            worst_svc = svc

    short = worst_svc.removeprefix("firecrawl-") or worst_svc
    if worst_pct >= 95.0:
        level, msg = (
            "critical",
            f"CRITICAL — {short} at {worst_pct:.0f}% mem; raise its mem_limit now (OOM imminent)",
        )
    elif worst_pct >= 80.0:
        level, msg = (
            "warn",
            f"WARN — {short} at {worst_pct:.0f}% mem; thin headroom, consider raising mem_limit",
        )
    elif worst_svc:
        level, msg = (
            "ok",
            f"OK (worst: {short} at {worst_pct:.0f}% mem)",
        )
    else:
        level, msg = "unknown", "unknown (no docker stats data)"

    return {
        "level": level,
        "worst_service": worst_svc,
        "worst_mem_pct": worst_pct,
        "message": msg,
    }


def _request_counts(cfg: AgentsConfig) -> dict[str, int]:
    """Count completed api requests from firecrawl-api container logs.

    Why log-grep and not a metrics endpoint? The synchronous
    ``v1/scrape`` / ``v1/extract`` paths bypass the queue
    infrastructure entirely — controller handles them in-process,
    calls playwright, returns. They never touch the rabbitmq or
    redis queues we report depth for, so queue stats always show
    zero even under load. Firecrawl's api emits a
    ``"request completed"`` log line once per finished request,
    which is the most reliable signal that "real traffic flowed
    through this stack" for the synchronous paths.

    Brittle to upstream log-format changes — the markers we grep for
    (``"request completed"``) might rename across versions. When that
    happens the counts go to zero; queue stats and container CPU%
    are the unaffected fallbacks.

    Returns ``{lifetime, last_hour}`` — both are counts of
    request-completion lines since the api container started (lifetime)
    and in the last 60 minutes (last_hour).
    """
    out: dict[str, int] = {"lifetime": 0, "last_hour": 0}

    full = mp.exec_in_vm(cfg.vm.name, "docker", "logs", "firecrawl-api", check=False)
    if full.returncode == 0:
        haystack = (full.stdout or "") + (full.stderr or "")
        out["lifetime"] = haystack.count("request completed")

    recent = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "logs",
        "--since",
        "1h",
        "firecrawl-api",
        check=False,
    )
    if recent.returncode == 0:
        haystack = (recent.stdout or "") + (recent.stderr or "")
        out["last_hour"] = haystack.count("request completed")

    return out


def _queue_stats(cfg: AgentsConfig) -> dict[str, list[dict[str, Any]]]:
    """Queue-depth + consumer + lifetime-totals snapshot.

    Two sources because firecrawl is mid-migration between queue
    backends:

    - **RabbitMQ** carries the modern NUQ-based paths
      (``extract.jobs``, ``extract.dlq``, plus ``nuq.queue_*.prefetch``
      internal buffers). Returns *current depth* (pending /
      in-flight / consumers) via ``rabbitmqctl list_queues``.
      Historical totals aren't reliably populated via mgmt-plugin
      ``message_stats`` for these queues (NUQ's framing bypasses
      standard rabbitmq accounting), so we report only what's
      trustworthy.

    - **Bull (redis)** carries the legacy / less-trafficked paths
      (``precrawlQueue``, ``billingQueue``, ``generateLlmsTxtQueue``,
      ``deepResearchQueue``). For these, redis stores per-queue
      ``:wait`` / ``:active`` / ``:completed`` / ``:failed`` lists
      and sets — Bull doesn't auto-trim ``:completed`` by default, so
      LLEN gives a usable "how many jobs have completed since this
      redis came up" counter.

    Internal NUQ prefetch queues are filtered out — they're transient
    framing buffers, not user-meaningful work.
    """
    out: dict[str, list[dict[str, Any]]] = {"rabbitmq": [], "bull": []}

    rmq = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "exec",
        "firecrawl-rabbitmq",
        "rabbitmqctl",
        "list_queues",
        "--quiet",
        "name",
        "messages",
        "messages_ready",
        "messages_unacknowledged",
        "consumers",
        check=False,
    )
    if rmq.returncode == 0:
        for raw_line in rmq.stdout.splitlines():
            parts = raw_line.split("\t")
            if len(parts) < 5 or parts[0] == "name":
                continue
            name = parts[0]
            # `nuq.queue_*.prefetch` are NUQ-internal in-flight buffers
            # — always 0 with 0 consumers. Skip for human output but
            # keep them in JSON so anyone debugging can still see them.
            try:
                total, ready, unacked, consumers = (int(x or 0) for x in parts[1:5])
            except ValueError:
                continue
            if name.startswith("nuq.") and ".prefetch" in name:
                continue
            out["rabbitmq"].append(
                {
                    "name": name,
                    "total": total,
                    "pending": ready,
                    "in_flight": unacked,
                    "consumers": consumers,
                }
            )

    bull_queues = (
        "precrawlQueue",
        "billingQueue",
        "generateLlmsTxtQueue",
        "deepResearchQueue",
    )
    for queue in bull_queues:
        stats: dict[str, Any] = {"name": queue}
        for state in ("wait", "active", "completed", "failed", "delayed"):
            # Bull uses lists for some states (wait/completed/failed)
            # and sets for others (active/delayed) — LLEN returns 0 on
            # a key that's a set, so try ZCARD as a fallback.
            r = mp.exec_in_vm(
                cfg.vm.name,
                "docker",
                "exec",
                "firecrawl-redis",
                "redis-cli",
                "LLEN",
                f"bull:{{{queue}}}:{state}",
                check=False,
            )
            n = 0
            if r.returncode == 0 and r.stdout.strip().lstrip("-").isdigit():
                n = int(r.stdout.strip())
            if n == 0:
                z = mp.exec_in_vm(
                    cfg.vm.name,
                    "docker",
                    "exec",
                    "firecrawl-redis",
                    "redis-cli",
                    "ZCARD",
                    f"bull:{{{queue}}}:{state}",
                    check=False,
                )
                if z.returncode == 0 and z.stdout.strip().lstrip("-").isdigit():
                    n = max(n, int(z.stdout.strip()))
            stats[state] = n
        out["bull"].append(stats)

    return out


def _firecrawl_services_in_compose(cfg: AgentsConfig) -> list[str]:
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return []
    services = doc.get("services") or {}
    return [s for s in services if s.startswith(FIRECRAWL_SERVICE_PREFIX)]


def _service_in_compose(cfg: AgentsConfig, name: str) -> bool:
    try:
        doc = compose.read_compose_doc(cfg)
    except (RuntimeFailure, UserError):
        return False
    return name in (doc.get("services") or {})


def _service_running(cfg: AgentsConfig, svc: str) -> bool:
    """Thin alias for :func:`sidecar.service_running` — kept because
    ops.py's doctor checks (and their tests) reference it by this name.
    """
    return sidecar.service_running(cfg, svc)
