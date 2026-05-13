# Architecture

```
┌──────────────────────────── host (Ubuntu) ────────────────────────────┐
│                                                                       │
│   $ agents …                                                          │
│        │                                                              │
│        ▼                                                              │
│   ┌────────────┐    ┌────────────┐                                    │
│   │ agents CLI │───►│ incus      │                                    │
│   └────────────┘    └────────────┘                                    │
│        │                  │                                           │
│        ▼                  │   ┌──────────────── VM ("agents") ───┐    │
│   <data-dir>/             │   │                                  │    │
│     docker-compose.yml ◄──┼──►│  /home/ubuntu/agents/ (virtiofs) │    │
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
| Incus VM | Semi-trusted | Outer sandbox. Break-out from a container ends here. |
| Rootless Docker daemon | User-scoped | Runs as VM's `ubuntu`, never root. |
| Per-agent container | Untrusted | Executes LLM-generated commands. |
| Hermes nested sandbox | Most untrusted | Spawned ad-hoc by Hermes for tool calls. |

## Data flow

1. **`agents vm init`** launches the Incus VM with a single-shot YAML config piped via stdin — sizing (`limits.cpu`, `limits.memory`, root disk size), `raw.idmap` for host↔VM UID/GID mapping, and the host data-dir disk device (virtiofs-backed) all set at launch time so the VM comes up with the mount already attached. After the VM agent reports ready, the bootstrap script (`templates.VM_BOOTSTRAP_SCRIPT`) is piped via `incus exec ... -- bash -s`. The script emits a final `BOOTSTRAP_OK ...` line; `mp.parse_bootstrap_ok` reads it. Captured fields (rootless docker socket path, ubuntu UID/GID, etc.) land in `~/.config/agents/config.toml` under `[vm_runtime]`. Seed files (`docker-compose.yml`, `.env`, `.codex/`, `backups/`, `.upgrades/`) are written host-side in host-mount mode; in `--data-dir-in-vm` mode they're written VM-side via `exec_bash` and only `.journal/` lives on the host.
2. **`agents create`** allocates a free port (inside the flock), ensures the pinned Hermes image is pulled, writes a service fragment into `docker-compose.yml` via the staging-and-transfer mutation pattern (host temp under `<data-dir>/.staging/` → `incus file push` → validate via `docker compose config -q` → VM-side atomic `mv`), then `docker compose up -d <service>` inside the VM. Per-agent paths are bind-mounted from the host so backups round-trip cleanly. `--reuse-data` is allowed only if no live container currently maps the dir.
3. **`agents upgrade`** takes two snapshots (host-data `.tar.zst` + Incus VM snapshot), pins the new tag via compose mutation, pulls, verifies via `docker image inspect`, recreates, then runs `doctor`. Auto-reverts the compose tag on any failure; both snapshot ids are reported so the user can `agents rollback <name>` (or `--snapshot <id>` to pick a specific one).
4. **`agents doctor`** runs a layered health probe (VM reachable, mount round-trip, rootless docker service active, container state, `docker ps` inside the Hermes container as the runtime user, `hermes doctor` itself, disk space, port conflicts, dangling journals, Codex auth + token expiry, per-Hermes-agent in-container Codex mount).

## Why a single mount

The Incus VM's own disk is small (Ubuntu + Docker engine + image cache). Every byte of agent state — config, sessions, skills, memories, the Codex auth — lives under `<data-dir>` on the host filesystem (in host-mount mode) or inside the VM at `<vm_agents_root>` (in `--data-dir-in-vm` mode). Three knock-on benefits in host-mount mode:

- **Backups are free.** Whatever the user already runs over `<data-dir>` (duplicity, restic, borg, …) automatically backs up agent state.
- **VM is disposable.** `agents vm destroy` removes the VM but leaves `<data-dir>` and the toolkit-scoped Codex auth intact, so re-running `vm init` brings the agents back.
- **Mutations are uniform.** Compose and per-agent config edits go through `incus file push` to a staged file in the VM project directory, validate via `docker compose ... config -q`, then atomic VM-side `mv`. Same code path applies in `--data-dir-in-vm` mode; only the read side differs (`incus file pull` vs direct host read).

The mount is virtiofs (provided by `virtiofsd` on the host); it's a real Linux filesystem from the kernel's perspective, so SQLite, fcntl byte-range locks, and Docker bind-mounts into containers all work normally. This was a hard problem on multipass (the predecessor backend — fuse.sshfs broke Docker bind-mounts, 9p broke SQLite); see [issue #1](https://github.com/ebner/agents/issues/1) for the migration history.

## Module map

## Backend prerequisites

The host needs `incus`, `qemu-system-x86`, `ovmf`, and `virtiofsd` installed. `virtiofsd` is non-negotiable — without it Incus silently falls back to 9p for the host data-dir share, and SQLite in agent containers breaks with `disk I/O error` (was the JSONL-fallback symptom we saw on multipass).

ufw + Docker on the host need three rules to coexist with `incusbr0`:

```bash
sudo ufw route allow in on incusbr0    # FORWARD: VM ↔ internet
sudo ufw route allow out on incusbr0
sudo ufw allow in on incusbr0          # INPUT: DHCP DISCOVER → dnsmasq
sudo ufw reload
```

Docker enables `net.bridge.bridge-nf-call-iptables=1` globally, which routes bridged broadcast packets through iptables. Without the INPUT rule, ufw drops DHCP DISCOVER before dnsmasq sees it — the VM silently never gets an IPv4 lease and the bootstrap dies at the first apt-get update.

## Module map

| Module | Role |
|---|---|
| `cli.py` | click root + every subcommand. Exit-code translator in `_run`. |
| `errors.py` | `CliError` hierarchy: 1 user / 2 runtime / 3 health. |
| `ui.py` | `--json` / `--yes` plumbing, table renderer, confirmation prompts. |
| `config.py` | pydantic v2 schema + tomlkit round-trip. |
| `mp.py` | Incus primitives: launch (single-shot YAML config) / exec (as ubuntu/uid 1000) / file push+pull / mount (raw.idmap + disk device) / snapshot / restore / find / list. BOOTSTRAP_OK parser. Module name retained for minimum churn. |
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
