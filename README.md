# `agents` — multi-agent VM toolkit

Host-side CLI for managing AI agents (Hermes Agent from Nous Research, OpenClaw) inside a single Multipass VM with rootless Docker.

Defense in depth: outer isolation = VM, inner isolation = one container per agent, host calls only `multipass`.

## Quickstart

```bash
# 1. Install the host-side CLI (requires Python 3.11+)
pip install -e .

# 2. (Optional) Install Codex CLI on the host if you want a toolkit-scoped Codex account
npm install -g @openai/codex

# 3. Bring up the VM. Pick a host path for all agent state — duplicity or whatever
#    host backup you run will back this up directly.
agents vm init --data-dir /data/agents

# 4. (Optional) Authenticate a toolkit-scoped Codex account, separate from your personal ~/.codex/
agents codex login

# 5. Create your first Hermes agent and run the setup wizard
agents create coder --type hermes --setup

# 6. Create an OpenClaw agent
agents create personal --type openclaw --setup

# 7. Day-to-day
agents list
agents doctor
agents logs coder -f
agents exec coder -- hermes shell
```

## What lives where

| Path | Role |
|---|---|
| `<data-dir>/docker-compose.yml` | Managed by the CLI — do not hand-edit. Use `docker-compose.override.yml` for overrides. |
| `<data-dir>/.env` | `HERMES_UID` / `HERMES_GID` so files written by containers round-trip cleanly to the host user. |
| `<data-dir>/<name>/.data/` | Each Hermes agent's `$HERMES_HOME` (auth, sessions, skills, memories, state.db). |
| `<data-dir>/<name>/.openclaw/` | Each OpenClaw agent's persistent dir. |
| `<data-dir>/.codex/` | Toolkit-scoped Codex auth (separate from your personal `~/.codex/`). |
| `<data-dir>/backups/` | Output of `agents export`. Host backup (e.g. duplicity) picks this up. |
| `~/.config/agents/config.toml` | CLI config — VM name, sizes, pinned image tags, captured VM runtime fields. |

## Codex paths

Two distinct ways Hermes can use your toolkit-scoped Codex account, both supported:

1. **As an LLM provider** (`model.provider: openai-codex`): pick this in the `hermes setup` wizard. The CLI mounts `<data-dir>/.codex` into the Hermes container so the setup wizard imports the auth into `<data-dir>/<name>/.data/.hermes/auth.json`. From then on the mount is just a fallback path — Hermes uses its own copy.
2. **As a coding skill** (the bundled `autonomous-ai-agents/codex` skill): opt in with `agents codex skill-enable <name>`. The CLI builds a sandbox image with Codex CLI baked in and points the agent's `terminal.docker_image` at it. The sandbox container — not the Hermes runtime — is where `codex` runs when Hermes delegates coding tasks.

OpenClaw bundles its own Codex plugin; the toolkit does **not** mount Codex into OpenClaw. Run `openclaw models auth login --provider openai-codex --device-code` inside the container after creation.

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
