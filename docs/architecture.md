# Architecture

```
┌──────────────────────────────── host ─────────────────────────────────┐
│                                                                       │
│   $ agents …                                                          │
│        │                                                              │
│        ▼                                                              │
│   ┌────────────┐    ┌───────────────────────────────┐                 │
│   │ agents CLI │───►│ mp.py — VM-backend dispatcher │                 │
│   └────────────┘    └───────────────┬───────────────┘                 │
│        │            ┌───────────────┼────────────────┐                │
│        │            ▼               ▼                ▼                │
│        │      backends/       backends/        backends/              │
│        │      multipass.py    libvirt.py       bare_metal.py          │
│        │      (`multipass`)   (virsh + ssh/scp) (host subprocess —    │
│        │            │               │            no VM at all)        │
│        │            └───────┬───────┘                                 │
│        ▼                    │   ┌──────────────── VM ("agents") ───┐  │
│   <data-dir>/               │   │                                  │  │
│     docker-compose.yml ◄────┼──►│  /home/ubuntu/agents/            │  │
│     .env                    │   │   (9p or virtiofs mount)         │  │
│     <coder>/.data/  ◄───────┼──►│   <coder>/.data/                 │  │
│     <personal>/.openclaw/ ◄─┼──►│   <personal>/.openclaw/          │  │
│     .codex/auth.json ◄──────┼──►│   .codex/auth.json               │  │
│     backups/                │   │                                  │  │
│     .upgrades/              │   │  rootless dockerd (user ubuntu)  │  │
│     .journal/               │   │  ┌──────────────┐ ┌─────────────┐│  │
│                             │   │  │ hermes-coder │ │openclaw-…   ││  │
│                             │   │  │ (container)  │ │(container)  ││  │
│                             │   │  │              │ │             ││  │
│                             │   │  │  spawns ↓    │ │             ││  │
│                             │   │  │ ┌──────────┐ │ │             ││  │
│                             │   │  │ │ sandbox  │ │ │             ││  │
│                             │   │  │ │ container│ │ │             ││  │
│                             │   │  │ └──────────┘ │ │             ││  │
│                             │   │  └──────────────┘ └─────────────┘│  │
│                             │   └──────────────────────────────────┘  │
│        ▼                                                              │
│   duplicity (or any host backup) over <data-dir>                      │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

Under `--backend bare_metal` the VM box disappears: the rootless Docker
daemon runs directly on the host, `vm_agents_root` *is* the host
data-dir, and the same container topology (agents + nested sandboxes)
applies unchanged.

## The pluggable-backend layer

`cfg.vm.backend` selects `multipass` (cross-platform default),
`libvirt` (Linux-only, experimental — enables GPU passthrough), or
`bare_metal` (Linux-only, no VM). `mp.py` is a **dispatcher**: every
host↔VM primitive (`exec_in_vm`, `transfer_to_vm`, `mount`,
`snapshot`, …) delegates to whichever backend module is active.
Backend modules are duck-typed — free functions matching the
`VmBackend` Protocol in `backends/__init__.py`. `cli.main` calls
`mp.set_backend(get_backend_module(cfg.vm.backend))` once at startup;
everything downstream uses `mp.foo(...)` and stays backend-agnostic.
New VM-host primitives must be added to **all three** backend modules
plus the dispatcher, never to `mp.py` alone.

## Trust boundaries

| Layer | Trust | Notes |
|---|---|---|
| Host | Trusted | Runs the CLI, owns the data-dir, runs duplicity. |
| VM (multipass / libvirt backends) | Semi-trusted | Outer sandbox. Break-out from a container ends here. **Absent on `bare_metal`** — the host becomes the boundary (see [threat-model.md](threat-model.md)). |
| Rootless Docker daemon | User-scoped | Runs as VM's `ubuntu` (the host user on bare-metal), never root. |
| Per-agent container | Untrusted | Executes LLM-generated commands. |
| Hermes nested sandbox | Most untrusted | Spawned ad-hoc by Hermes for tool calls. |

## Data flow

1. **`agents vm init`** provisions the runtime for the selected `--backend`: `multipass launch` on the default backend, cloud-image download + seed ISO + `virt-install` + SSH-wait on libvirt, and *no VM at all* on bare-metal (a verify-only probe of the host's rootless-Docker prerequisites). VM backends mount the host `<data-dir>` at `/home/ubuntu/agents` with explicit UID/GID mapping (host-mount mode) or skip the mount (`--data-dir-in-vm`), then run the bootstrap script (`templates.VM_BOOTSTRAP_SCRIPT`). The script emits a final `BOOTSTRAP_OK ...` line; `mp.parse_bootstrap_ok` reads it. Captured fields (rootless docker socket path, ubuntu UID/GID, etc.) land in `~/.config/agents/config.toml` under `[vm_runtime]`. Seed files (`docker-compose.yml`, `.env`, `.codex/`, `backups/`, `.upgrades/`) are written host-side in host-mount mode; in `--data-dir-in-vm` mode they're written VM-side via `exec_bash` and only `.journal/` lives on the host. On bare-metal, `vm_agents_root` is set to the host data-dir so compose paths need no translation.
2. **`agents create`** allocates a free port (inside the flock), ensures the pinned image is pulled (the compose fragment pins the runtime `user:` and `HOME` explicitly, so image USER/HOME drift between releases doesn't matter), sets a default ACL on the fresh data dir so container-written files stay host-readable, writes a service fragment into `docker-compose.yml` via the staged-transfer mutation pattern, then `docker compose up -d <service>` inside the VM. Per-agent paths are bind-mounted from the host so backups round-trip cleanly. `--reuse-data` is allowed only if no live container currently maps the dir.
3. **`agents upgrade`** resolves the target tag first — bare `upgrade <name>` implicitly fetches the latest upstream tag from GitHub Releases (`--tag` pins explicitly, `--recreate` force-refreshes at the current pin) and no-ops without downtime when the agent is already at the target. Otherwise it takes two snapshots (host-data `.tar.zst` + VM snapshot), pins the new tag via compose mutation, pulls, verifies via `docker image inspect`, recreates, runs `hermes config migrate` (Hermes only), then runs `doctor`. Auto-reverts the compose tag on any failure; both snapshot ids are reported so the user can `agents rollback <name>` (or `--snapshot <id>` to pick a specific one). Interrupted upgrades resume from a journal that recorded the original old→new tags.
4. **`agents doctor`** runs a layered health probe (VM reachable, mount round-trip, rootless docker service active, disk/memory/load, port conflicts, dangling journals, Codex auth + token expiry; per-agent: container state, docker socket, `hermes doctor`, `/opt/data` access, Codex mount, version pin-vs-running drift, host-access ACLs; project-level: firecrawl and inference stack health when installed).

## Why a single mount

The VM's own disk is small (Ubuntu + Docker engine + image cache). Every byte of agent state — config, sessions, skills, memories, the Codex auth — lives under `<data-dir>` on the host filesystem (in host-mount mode) or inside the VM at `<vm_agents_root>` (in `--data-dir-in-vm` mode). Three knock-on benefits in host-mount mode:

- **Backups are free.** Whatever the user already runs over `<data-dir>` (duplicity, restic, borg, …) automatically backs up agent state.
- **VM is disposable.** `agents vm destroy` removes the VM but leaves `<data-dir>` and the toolkit-scoped Codex auth intact, so re-running `vm init` brings the agents back.
- **Mutations are uniform.** Compose and per-agent config edits go through the backend's transfer primitive (`multipass transfer` / `scp` / host-side copy) to a staged file in the VM project directory, validate via `docker compose ... config -q`, then atomic VM-side `mv`. Same code path applies in `--data-dir-in-vm` mode; only the read side differs (transfer-from-VM vs direct host read).

The cost is 9p/virtiofs mount overhead for SQLite-heavy workloads. The opt-out is `vm init --data-dir-in-vm`, which keeps data on the VM disk and routes every read/write through the backend's transfer primitive + `tar --zstd` for exports.

## Module map

| Module | Role |
|---|---|
| `cli.py` | click root + every subcommand. Exit-code translator in `_run`. Wires the backend dispatcher at startup. |
| `errors.py` | `CliError` hierarchy: 1 user / 2 runtime / 3 health. |
| `ui.py` | `--json` / `--yes` plumbing, table renderer, confirmation prompts, `progress` narration on stderr. |
| `config.py` | pydantic v2 schema + tomlkit round-trip. |
| `mp.py` | **VM-backend dispatcher** (historically the Multipass primitives module). Thin wrappers over the active backend; re-exports `MpResult` / `BootstrapStatus` / `parse_bootstrap_ok` for backward compat. |
| `backends/__init__.py` | `VmBackend` Protocol, value types, `parse_bootstrap_ok`, `get_backend_module`. |
| `backends/multipass.py` | Multipass implementation (cross-platform default): launch / exec / transfer / mount with -u/-g / snapshot / restore / find / list. |
| `backends/libvirt.py` | libvirt/KVM implementation, Linux-only, experimental. Cloud-init seed ISO + virt-install + ssh/scp; GPU passthrough autodetect + IOMMU validation + hostdev XML attach. |
| `backends/bare_metal.py` | No-VM implementation, Linux-only. exec/transfer collapse to host subprocess + file copy; mount/launch/snapshot are no-ops or explicit refusals; verify-only bootstrap. |
| `templates.py` | Bootstrap script, sandbox Dockerfile, cont-init fix-perms hook, fallback image tags. |
| `vm.py` | VM lifecycle: init / start / stop / shell / status / resize / rebuild / snapshot / restore / destroy. |
| `compose.py` / `compose_runner.py` | Compose mutation (flock + atomic rename), service fragments, port allocator, image allowlist + docker compose verbs. |
| `hermes_config.py` | Round-trip edits to per-agent config.yaml. Dashboard refusal lives here. |
| `journal.py` | Phase journals for multi-phase mutations (create/remove/rename/upgrade/rebuild). |
| `agents_cmd.py` | create / list / remove / rename / reseat. |
| `ops.py` | start / stop / restart / logs / exec / setup / doctor. |
| `upgrades.py` | Implicit-latest resolution + two-snapshot state machine with rollback and journal resume. |
| `outdated_cmd.py` | `agents outdated`: classify each agent against the latest GitHub Releases tag (current / outdated / unknown). |
| `export_cmd.py` | export / import / host-cron integration. Auto-prunes the default backups dir to `[export_cron].keep_last`. |
| `archive.py` | tar.zst helpers shared by export and upgrade snapshots. |
| `codex.py` | Codex login / refresh / status / skill-enable / skill-disable. |
| `ollama_cmd.py` | configure / status / clear for an **external** Ollama / OpenAI-compatible endpoint. Stores URL + model names in `[ollama]`; stores bearer token (if any) in `<data-dir>/.env`, never in TOML. Refuses `localhost` URLs. |
| `inference_cmd.py` | `agents inference {up,down,status,load,unload}` — **in-VM** Ollama + per-model llama.cpp sibling services, digest-pinned on first up. |
| `models_cmd.py` | `agents model {pull,list,rm,use}` — model lifecycle (Ollama Hub / HF refs; HF GGUF downloads for llama.cpp) + wiring an agent's `model:` block. |
| `sidecar.py` | Shared leaf helpers for project-scoped sidecars: arch mapping, digest resolution (allowlist-validated), service liveness, project-env I/O. |
| `memory/__init__.py` | Registry + dispatch for memory backend plugins (Hermes only). State at `<data-dir>/.memory-state/<agent>.json`. |
| `memory/holographic.py` | In-process Hermes plugin. Zero external deps. |
| `memory/mem0.py` | In-process library mode. Wires Ollama or `OPENAI_API_KEY`. |
| `memory/openviking.py` | Per-agent sibling docker service. Needs Ollama. |
| `secrets_cmd.py` | Per-agent API keys at `<data-dir>/<name>/.env`. Wired into the container via compose `env_file:`. Backfills the directive on existing agents. Mutations restart the container. |
| `mounts_cmd.py` | `agents mount {add,remove,list}` — share specific host folders with an agent; writes runtime volumes to `docker-compose.override.yml` (never the main compose) + sandbox volumes to Hermes `terminal.docker_volumes`; auto-applies bidirectional ACLs. |
| `access_cmd.py` | `agents access {grant,revoke,list}` — POSIX-ACL grants between the host user and the agent's subuid-owned data dir; `auto_grant_on_create` hook. |
| `permissions.py` | POSIX-ACL helpers: subuid math, setfacl argv builders, ACL-support probe, host-access drift assessment for doctor. |
| `fixperms_cmd.py` | `agents fix-perms <agent>`: stops, force-removes, recreates the container so the entrypoint runs fresh, then chowns `/opt/data` to the resulting hermes uid and chmods it 755 so the toolkit's `agents exec` (uid 1000) can traverse. Remediation for the recurring "files at uid 10000, EACCES" bug. Also owns `chown_hermes_install_paths` for image-baked paths. |
| `firecrawl_cmd.py` | Self-hosted Firecrawl stack (5 containers, project-level). `up/down/status/destroy/logs`. Resolves upstream floating tags to `@sha256:…` digests on first run, writes to `<data-dir>/.firecrawl-state.json`, health-probes the api before wiring agents. Auto-wires every Hermes agent's `terminal.env_passthrough` with `FIRECRAWL_API_KEY`/`FIRECRAWL_API_URL`. `agents create` checks `is_installed(cfg)` and auto-wires fresh Hermes agents. |
| `conversations_cmd.py` | `agents conversations {status,export}` — read-only session stats from Hermes' `/opt/data/state.db` + Markdown transcript export (see [migration.md](migration.md)). |
