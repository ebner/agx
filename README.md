# `agents` — multi-agent VM toolkit

Host-side CLI for managing AI agents (Hermes Agent from Nous Research, OpenClaw) inside a single Multipass VM with rootless Docker.

Defense in depth: outer isolation = VM, inner isolation = one container per agent, host calls only `multipass`.

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
| `<data-dir>/backups/` | Output of `agents export`. Host backup (e.g. duplicity) picks this up. |
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

The toolkit does not implement an internal backup engine. Pick a `<data-dir>` your existing host backup (duplicity, restic, borg, etc.) already covers, and you're done. `agents export <name>` produces a clean `hermes backup` archive in `<data-dir>/backups/` when you want a crash-consistent point-in-time.

## Threat model

See [docs/threat-model.md](docs/threat-model.md). Short version:

- Outer trust boundary: the Multipass VM.
- Inner trust boundary: one Docker container per agent, rootless.
- Hermes mounts the rootless Docker socket so it can spawn its nested sandbox — bounded by the rootless `ubuntu` user, not root.
- Dashboards bind 127.0.0.1 only.

## Status

v0.1.0 — initial implementation. See [`the design plan`](the design plan) for the locked design.
