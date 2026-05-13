# Architecture

```
┌──────────────────────────── host (Ubuntu) ────────────────────────────┐
│                                                                       │
│   $ agents …                                                          │
│        │                                                              │
│        ▼                                                              │
│   ┌────────────┐    ┌────────────┐                                    │
│   │ agents CLI │───►│ multipass  │                                    │
│   └────────────┘    └────────────┘                                    │
│        │                  │                                           │
│        ▼                  │   ┌──────────────── VM ("agents") ───┐    │
│   <data-dir>/             │   │                                  │    │
│     docker-compose.yml ◄──┼──►│  /home/ubuntu/agents/  (9p mount)│    │
│     .env                  │   │   docker-compose.yml             │    │
│     <coder>/.data/  ◄─────┼──►│   <coder>/.data/                 │    │
│     <personal>/.openclaw/◄┼──►│   <personal>/.openclaw/          │    │
│     .codex/auth.json ◄────┼──►│   .codex/auth.json               │    │
│     backups/              │   │                                  │    │
│     .upgrades/            │   │  rootless dockerd (user ubuntu)  │    │
│     .journal/             │   │  ┌──────────────┐ ┌─────────────┐│    │
│                           │   │  │ hermes-coder │ │openclaw-…   ││    │
│                           │   │  │ (container)  │ │(container)  ││    │
│                           │   │  │              │ │             ││    │
│                           │   │  │  spawns ↓    │ │             ││    │
│                           │   │  │ ┌──────────┐ │ │             ││    │
│                           │   │  │ │ sandbox  │ │ │             ││    │
│                           │   │  │ │ container│ │ │             ││    │
│                           │   │  │ └──────────┘ │ │             ││    │
│                           │   │  └──────────────┘ └─────────────┘│    │
│                           │   └──────────────────────────────────┘    │
│        ▼                                                              │
│   duplicity (or any host backup) over <data-dir>                      │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

## Trust boundaries

| Layer | Trust | Notes |
|---|---|---|
| Host | Trusted | Runs the CLI, owns the data-dir, runs duplicity. |
| Multipass VM | Semi-trusted | Outer sandbox. Break-out from a container ends here. |
| Rootless Docker daemon | User-scoped | Runs as VM's `ubuntu`, never root. |
| Per-agent container | Untrusted | Executes LLM-generated commands. |
| Hermes nested sandbox | Most untrusted | Spawned ad-hoc by Hermes for tool calls. |

## Data flow

1. **`agents vm init`** launches the Multipass VM, mounts the host `<data-dir>` at `/home/ubuntu/agents` with explicit UID/GID mapping (host-mount mode) or skips the mount (`--data-dir-in-vm`), installs rootless Docker, and runs the bootstrap script (`templates.VM_BOOTSTRAP_SCRIPT`). The script emits a final `BOOTSTRAP_OK ...` line; `mp.parse_bootstrap_ok` reads it. Captured fields (rootless docker socket path, ubuntu UID/GID, etc.) land in `~/.config/agents/config.toml` under `[vm_runtime]`. Seed files (`docker-compose.yml`, `.env`, `.codex/`, `backups/`, `.upgrades/`) are written host-side in host-mount mode; in `--data-dir-in-vm` mode they're written VM-side via `exec_bash` and only `.journal/` lives on the host.
2. **`agents create`** allocates a free port (inside the flock), inspects the pinned Hermes image to verify USER/HOME, writes a service fragment into `docker-compose.yml` via the multipass-transfer mutation pattern, then `docker compose up -d <service>` inside the VM. Per-agent paths are bind-mounted from the host so backups round-trip cleanly. `--reuse-data` is allowed only if no live container currently maps the dir.
3. **`agents upgrade`** takes two snapshots (host-data `.tar.zst` + multipass VM snapshot), pins the new tag via compose mutation, pulls, verifies via `docker image inspect`, recreates, then runs `doctor`. Auto-reverts the compose tag on any failure; both snapshot ids are reported so the user can `agents rollback <name>` (or `--snapshot <id>` to pick a specific one).
4. **`agents doctor`** runs a layered health probe (VM reachable, mount round-trip, rootless docker service active, container state, `docker ps` inside the Hermes container as the runtime user, `hermes doctor` itself, disk space, port conflicts, dangling journals, Codex auth + token expiry, per-Hermes-agent in-container Codex mount).

## Why a single mount

The Multipass VM's own disk is small (Ubuntu + Docker engine + image cache). Every byte of agent state — config, sessions, skills, memories, the Codex auth — lives under `<data-dir>` on the host filesystem (in host-mount mode) or inside the VM at `<vm_agents_root>` (in `--data-dir-in-vm` mode). Three knock-on benefits in host-mount mode:

- **Backups are free.** Whatever the user already runs over `<data-dir>` (duplicity, restic, borg, …) automatically backs up agent state.
- **VM is disposable.** `agents vm destroy` removes the VM but leaves `<data-dir>` and the toolkit-scoped Codex auth intact, so re-running `vm init` brings the agents back.
- **Mutations are uniform.** Compose and per-agent config edits go through `multipass transfer` to a staged file in the VM project directory, validate via `docker compose ... config -q`, then atomic VM-side `mv`. Same code path applies in `--data-dir-in-vm` mode; only the read side differs (transfer-from-VM vs direct host read).

The cost is 9p mount overhead for SQLite-heavy workloads. The opt-out is `vm init --data-dir-in-vm`, which keeps data on the VM disk and routes every read/write through `multipass transfer` + `tar --zstd` for exports.

## Module map

| Module | Role |
|---|---|
| `cli.py` | click root + every subcommand. Exit-code translator in `_run`. |
| `errors.py` | `CliError` hierarchy: 1 user / 2 runtime / 3 health. |
| `ui.py` | `--json` / `--yes` plumbing, table renderer, confirmation prompts. |
| `config.py` | pydantic v2 schema + tomlkit round-trip. |
| `mp.py` | Multipass primitives: launch / exec / transfer / mount with -u/-g / snapshot / restore / find / list. BOOTSTRAP_OK parser. |
| `templates.py` | Bootstrap script, sandbox Dockerfile, fallback image tags. |
| `vm.py` | VM lifecycle. |
| `compose.py` / `compose_runner.py` | Compose mutation (flock + atomic rename) + docker compose verbs. |
| `hermes_config.py` | Round-trip edits to per-agent config.yaml. Dashboard refusal lives here. |
| `journal.py` | Phase journals for multi-phase mutations. |
| `agents_cmd.py` | create / list / remove / rename. |
| `ops.py` | start / stop / restart / logs / exec / setup / doctor. |
| `upgrades.py` | Two-snapshot state machine with rollback. |
| `export_cmd.py` | export / import / host-cron integration. |
| `codex.py` | Codex login / refresh / status / skill-enable / skill-disable. |
| `ollama_cmd.py` | configure / status / clear for an external Ollama / OpenAI-compatible endpoint. Stores URL + model names in `[ollama]`; stores bearer token (if any) in `<data-dir>/.env`, never in TOML. Refuses `localhost` URLs. |
| `memory/__init__.py` | Registry + dispatch for memory backend plugins (Hermes only). State at `<data-dir>/.memory-state/<agent>.json`. |
| `memory/holographic.py` | In-process Hermes plugin. Zero external deps. |
| `memory/mem0.py` | In-process library mode. Wires Ollama or `OPENAI_API_KEY`. |
| `memory/openviking.py` | Per-agent sibling docker service. Needs Ollama. |
