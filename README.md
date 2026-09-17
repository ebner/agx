# `agents` — multi-agent VM toolkit

[![CI](https://github.com/ebner/agents/actions/workflows/ci.yml/badge.svg)](https://github.com/ebner/agents/actions/workflows/ci.yml)

Host-side CLI for managing AI agents (Hermes Agent from Nous Research, OpenClaw) as rootless-Docker containers behind a pluggable isolation backend:

- **`multipass`** (default, cross-platform) — a single Multipass VM; the host shells out only to `multipass`.
- **`libvirt`** (Linux-only, experimental) — a KVM/qemu VM driven via `virt-install`/`virsh` + ssh/scp; enables GPU passthrough.
- **`bare_metal`** (Linux-only, opt-in) — no VM at all; containers run on the host's rootless Docker. Forfeits the VM trust boundary — single-user dev workstations only.

Defense in depth (VM backends): outer isolation = VM, inner isolation = one rootless container per agent. Pick the backend at `agents vm init --backend …`; see [docs/threat-model.md](docs/threat-model.md) for what each trades away and [docs/runbook.md](docs/runbook.md) for the libvirt / bare-metal setup guides.

## Quickstart

```bash
# 1. Install the host-side CLI (requires Python 3.11+).
#    Recommended: pipx — installs into an isolated env, gets `agents` on $PATH,
#    works on Homebrew / Debian / Ubuntu 24+ where pip refuses (PEP 668).
pipx install -e .                 # macOS: brew install pipx first if missing
# or, if you prefer a manual venv:
#   python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# 2. (Optional) Install Codex CLI on the host if you want a toolkit-scoped Codex account
npm install -g @openai/codex

# 3. Bring up the VM. Pick a host path for all agent state — duplicity or whatever
#    host backup you run will back this up directly.
#
#    For SQLite-heavy use (Hermes' kanban/sessions/insights stores use SQLite
#    WAL, which doesn't work across multipass's 9p share), pass --data-dir-in-vm
#    so per-agent state lives on the VM's ext4 disk where WAL is native. The
#    host then sees agent state only via `agents export` archives (next step
#    below shows how to schedule those for multi-times-a-day backups).
agents vm init --data-dir /data/agents                      # SQLite WAL falls back to JSONL; files live on host
agents vm init --data-dir /data/agents --data-dir-in-vm     # SQLite WAL works; files visible only via archives

# 4. (Optional) Authenticate a toolkit-scoped Codex account, separate from your personal ~/.codex/
agents codex login

# 5. Create your first Hermes agent and run the setup wizard
agents create coder --type hermes --setup

# 6. Create an OpenClaw agent
agents create personal --type openclaw --setup

# 7. Schedule host-side backups. One crontab entry per time; replaces
#    any prior toolkit-managed entries on re-install.
agents export-cron install --time 02:30                     # once a day
agents export-cron install --time 02:30,14:30               # twice a day
agents export-cron install --time 02:30,08:30,14:30,20:30   # 6-hourly
agents export-cron status

# 8. Day-to-day
agents list
agents doctor
agents logs coder -f
agents exec coder -- hermes shell
```

For the full lifecycle — Hermes profiles vs. personalities vs. skills, connecting to Slack / WhatsApp / Mattermost / Matrix, and the OpenClaw flow — see [docs/tutorial.md](docs/tutorial.md). The [runbook](docs/runbook.md) is the cheat-sheet.

## Command overview

| Command | What it does | Docs |
|---|---|---|
| `agents vm init/start/stop/status/shell/resize/rebuild/snapshot/restore/destroy` | Runtime lifecycle for the selected backend (`--backend multipass\|libvirt\|bare_metal`) | [runbook](docs/runbook.md) |
| `agents create/list/remove/rename` | Agent CRUD (`remove --export-first` bags a final archive) | [tutorial](docs/tutorial.md) |
| `agents start/stop/restart/logs/exec/setup` | Day-2 container ops | [runbook](docs/runbook.md) |
| `agents doctor [name]` | Layered health checks: VM, mount, docker, per-agent probes, version pin-drift | [runbook](docs/runbook.md) |
| `agents outdated [name]` | Which agents are behind upstream (GitHub Releases) | [runbook](docs/runbook.md) |
| `agents upgrade [name]` / `rollback` / `pull` | Bare `upgrade` implicitly targets the latest upstream tag; `--tag` pins, `--recreate` force-refreshes; two-snapshot rollback safety | [runbook](docs/runbook.md) |
| `agents reseat <name>` | Rewrite an agent's compose block from the current template after toolkit upgrades | [runbook](docs/runbook.md) |
| `agents fix-perms <name>` | Remediate the recurring `/opt/data` permission-drift bug (Hermes) | [runbook](docs/runbook.md) |
| `agents export/import` / `export-cron` | Archives into `<data-dir>/backups/` + host-cron scheduling; auto-prunes to `keep_last` | [runbook](docs/runbook.md) |
| `agents secret set/unset/list/clear` | Per-agent API keys via compose `env_file:` | [runbook](docs/runbook.md) |
| `agents mount add/remove/list` | Share specific host folders with an agent (override-file volumes + ACLs) | [runbook](docs/runbook.md) |
| `agents edit <name> <path>` | Edit one of a Hermes agent's text files (`SOUL.md`, skills, `config.yaml`) in `$EDITOR` via the container — no sudo, no host access to the data dir | [runbook](docs/runbook.md) |
| `agents access grant/revoke/list` | Host-user ACLs on the agent's subuid-owned data dir | [runbook](docs/runbook.md) |
| `agents codex login/refresh/status/skill-enable/skill-disable` | Toolkit-scoped Codex auth + the Codex coding skill | [codex-integration](docs/codex-integration.md) |
| `agents memory list/enable/disable/status` | Managed memory backends for Hermes | [runbook](docs/runbook.md) |
| `agents ollama configure/status/clear` | Point the toolkit at an **external** Ollama / OpenAI-compatible endpoint | [runbook](docs/runbook.md) |
| `agents inference up/down/status/load/unload` | **In-VM** local inference (Ollama, llama.cpp) as sibling services | [runbook](docs/runbook.md) |
| `agents model pull/list/rm/use` | Local model lifecycle + wiring an agent to an engine | [runbook](docs/runbook.md) |
| `agents firecrawl up/down/status/logs/destroy` | Project-scoped self-hosted Firecrawl stack, digest-pinned | [runbook](docs/runbook.md) |
| `agents conversations status/export` | Session stats + Markdown transcript export (Hermes) | [migration](docs/migration.md) |
| `agents config` | Print the resolved toolkit config | — |

Every command supports `--json` (machine-readable output) and `--yes` (skip confirmations); exit codes are 0 success / 1 user error / 2 runtime failure / 3 health-check failure.

## What lives where

| Path | Role |
|---|---|
| `<data-dir>/docker-compose.yml` | Managed by the CLI — do not hand-edit. Use `docker-compose.override.yml` for overrides. |
| `<data-dir>/.env` | `HERMES_UID` / `HERMES_GID` so files written by containers round-trip cleanly to the host user. |
| `<data-dir>/<name>/.data/` | Each Hermes agent's `$HERMES_HOME` (auth, sessions, skills, memories, state.db). |
| `<data-dir>/<name>/.openclaw/` | Each OpenClaw agent's persistent dir. |
| `<data-dir>/<name>/.env` | Per-agent secrets (API keys for skills). Managed via `agents secret …`. Wired into the container via compose `env_file:`. |
| `<data-dir>/.firecrawl-state.json` | Captured image digests for the self-hosted Firecrawl stack (managed by `agents firecrawl …`). |
| `<data-dir>/firecrawl/postgres/` | Postgres data for self-hosted Firecrawl. Bind-mounted, duplicity-friendly. |
| `<data-dir>/<name>/.openviking/` | OpenViking sidecar's per-agent data (when `agents memory enable openviking <name>`). |
| `<data-dir>/.codex/` | Toolkit-scoped Codex auth (separate from your personal `~/.codex/`). |
| `<data-dir>/.memory-state/` | Per-agent record of which memory backend the toolkit enabled. |
| `<data-dir>/models/` | Local model weights for `agents inference` / `agents model` (Ollama blobs, llama.cpp GGUFs). |
| `<data-dir>/backups/` | Output of `agents export`. Host backup (e.g. duplicity) picks this up. Pruned to the newest `keep_last` archives per agent. |
| `~/.config/agents/config.toml` | CLI config — VM name, sizes, pinned image tags, captured VM runtime fields. |

## Codex paths

Two distinct ways Hermes can use your toolkit-scoped Codex account, both supported:

1. **As an LLM provider** (`model.provider: openai-codex`): pick this in the `hermes setup` wizard. The CLI mounts `<data-dir>/.codex` into the Hermes container so the setup wizard imports the auth into `<data-dir>/<name>/.data/.hermes/auth.json`. From then on the mount is just a fallback path — Hermes uses its own copy.
2. **As a coding skill** (the bundled `autonomous-ai-agents/codex` skill): opt in with `agents codex skill-enable <name>`. The CLI builds a sandbox image with Codex CLI baked in and points the agent's `terminal.docker_image` at it. The sandbox container — not the Hermes runtime — is where `codex` runs when Hermes delegates coding tasks.

OpenClaw bundles its own Codex plugin; the toolkit does **not** mount Codex into OpenClaw. Run `openclaw models auth login --provider openai-codex --device-code` inside the container after creation.

## Memory backends

Hermes agents start with built-in memory. The toolkit ships three managed alternatives:

| Plugin       | What it needs                                            |
|--------------|----------------------------------------------------------|
| `holographic`| Nothing — works with Codex auth alone, fully local       |
| `mem0`       | Ollama (preferred) or `OPENAI_API_KEY`                   |
| `openviking` | Ollama (sibling docker service from Volcengine)          |

```bash
agents memory list                            # plugins + tier + summary
agents memory enable holographic coder       # zero-dep, in-process
agents ollama configure --url http://10.20.30.1:11434  # point at an existing Ollama
agents memory enable openviking coder        # sibling service
agents memory status coder
```

The toolkit does NOT run Ollama for you — you point it at one you already run (host, LAN, Tailscale, or a hosted OpenAI-compatible service). `localhost` URLs are refused at config time because they'd resolve to the VM, not the host.

## Backups

The toolkit does not implement an internal backup engine. Pick a `<data-dir>` your existing host backup (duplicity, restic, borg, etc.) already covers, and you're done. `agents export <name>` produces a clean archive in `<data-dir>/backups/` when you want a crash-consistent point-in-time. Note: the default backups dir is a **rolling window** — each export prunes that agent's archives down to the newest `[export_cron].keep_last` (default 14; `0` disables). Long-term history is your host backup's job — check its retention settings. See the [runbook's retention section](docs/runbook.md#backup-retention).

## Threat model

See [docs/threat-model.md](docs/threat-model.md). Short version:

- Outer trust boundary: the VM (multipass or libvirt backends). Under `--backend bare_metal` there is no VM — the host is the boundary, which is why bare-metal is opt-in for single-user workstations only.
- Inner trust boundary: one Docker container per agent, rootless.
- Hermes mounts the rootless Docker socket so it can spawn its nested sandbox — bounded by the rootless `ubuntu` user, not root.
- Dashboards bind 127.0.0.1 only.

## Status

v0.1.0 — initial implementation. The locked architecture rules and their rationale live in [`CLAUDE.md`](CLAUDE.md) and [`docs/architecture.md`](docs/architecture.md).
