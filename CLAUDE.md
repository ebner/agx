# Project context for Claude Code

## What this is

`agents` is a host-side Python CLI that manages AI agents (Hermes Agent from Nous Research, OpenClaw) inside a single Multipass VM with rootless Docker. The host invokes only `multipass` — it never talks to the in-VM Docker daemon directly. Outer trust boundary = VM. Inner trust boundary = one Docker container per agent. The defense-in-depth motivation is the 2026 wave of agent-marketplace malware and skill-execution CVEs.

## Locked architecture rules (do not relitigate)

1. **Rootless Docker inside the VM.** Daemon runs as `ubuntu`, socket at `<docker_socket_path>` (read from `[vm_runtime]` in config — set by BOOTSTRAP_OK). Never hardcode the path or the UID.
2. **One mount, one source of truth.** Host `<data-dir>` is mounted at VM `/home/ubuntu/agents` with explicit `multipass mount -u <host-uid>:1000 -g <host-gid>:1000`. All per-agent data, the compose file, the `.env`, exports, and the toolkit-scoped Codex auth live under this path. The VM disk only holds OS + Docker engine + image cache. Opt-out is `vm init --data-dir-in-vm`.
3. **Toolkit-scoped Codex auth** at `<data-dir>/.codex/` — NEVER the user's personal `~/.codex/`. `agents codex login` sets `CODEX_HOME=<data-dir>/.codex`.
4. **The CLI owns the compose file.** Users get `docker-compose.override.yml` for hand edits. Never heredoc through `multipass exec` — always: flock → ruamel.yaml round-trip → write host temp at `/tmp/agents-compose-<pid>-<uuid>.yml` → `multipass transfer` to `<vm_agents_root>/.compose.<pid>.<uuid>.new` → `docker compose --project-directory <vm_agents_root> -f ... --env-file <vm_agents_root>/.env config -q` validation → VM-side `mv` to `docker-compose.yml` (atomic same-fs rename) → cleanup. `compose.cleanup_stale_temps(cfg)` is invoked by `vm.start` and at the head of every mutation. Same pattern applies to per-agent `config.yaml` writes via `hermes_config.write_atomically`, which validates via `hermes config validate` exec'd inside the running container (best-effort — silently skipped if the container isn't up yet).
5. **Image-source allowlist** is a parsed `(registry, repo)` tuple match — `nousresearch/hermes-agent` and `openclaw/openclaw` only. Reject `hermes-agent.org` and any registry-prefixed variant.
6. **Dashboard binding** is `127.0.0.1`/`localhost` only. The CLI refuses any Hermes config change to `0.0.0.0`.
7. **Every mutation holds `<data-dir>/.agents.lock`** (`flock`). Read-only commands skip the lock. Two parallel `agents create` calls must serialize.
8. **Every multi-phase op writes a phase journal** at `<data-dir>/.journal/<op>-<name>-<utc>.json`. Re-running the command resumes from the last completed phase. `doctor` flags journals older than 24 h.

## Critical cross-layer pitfalls (already fixed; don't reintroduce)

- **`terminal.docker_volumes` paths are interpreted by the VM Docker daemon**, not by paths inside the Hermes container. Sandbox volume entries use VM-side host paths like `/home/ubuntu/agents/.codex:/<sandbox-user>/.codex:ro`.
- **Hermes runtime container needs `DOCKER_HOST` env** plus the rootless socket mount, otherwise its CLI can't reach the daemon to spawn the sandbox.
- **Multipass snapshots do NOT cover the host-mounted data-dir.** Upgrade flow takes BOTH a VM snapshot AND a host-data tarball (in `<data-dir>/.upgrades/`).
- **Multipass mount does not always survive stop/start.** `agents vm start` verifies and remounts, then runs a write/read/unlink probe.
- **OpenClaw service paths are per-agent** (`./<name>/.openclaw`, `./<name>/workspace`) so multiple named OpenClaw agents don't collide.
- **Codex sandbox image tag** must be slugified (`agents/hermes-sandbox-codex:<sha256(base)[:12]>`) — base refs contain `/` and `:`.

## Codex paths — how Hermes and OpenClaw differ

| Agent | Codex as LLM | Codex as tool |
|---|---|---|
| Hermes | `model.provider: openai-codex`. Mounted Codex auth at `/home/hermes/.codex:ro` is an import bridge; once `hermes setup` runs, persistent auth lives at `/opt/data/.hermes/auth.json` on the bind-mounted data dir. Container USER+HOME are verified via `docker inspect` at `agents create` time — refuses if upstream Hermes ever changes the entrypoint user. | Opt in via `agents codex skill-enable`. CLI builds a derived sandbox image with Codex CLI baked in and `ENV CODEX_HOME=/codex`; volume entry mounts the toolkit Codex auth at `/codex:ro`. User-independent — works for sandbox images running as root, pn, node, etc. The whole operation holds `<data-dir>/.agents.lock`. |
| OpenClaw | OpenClaw bundles its own Codex plugin. Run `openclaw models auth login --provider openai-codex` inside the container. **Never** mount Codex into OpenClaw — would conflict. | Same plugin path. |

## Hermes terminology — don't conflate

Three distinct concepts that all sound related. Future doc work and any tooling we add must keep them separate:

| Term | What it is | Where it lives | CLI |
|---|---|---|---|
| **Profile** | An isolated Hermes instance — separate `$HERMES_HOME` with its own auth/sessions/skills/config | `$HERMES_HOME/profiles/<name>/` | `hermes profile {list,create --clone,use,delete,rename,export,import}` |
| **Personality** (SOUL.md + `/personality`) | The agent's persistent voice/identity (file) plus session-level overlays (preset or custom in `agent.personalities` in `config.yaml`) | `$HERMES_HOME/SOUL.md` | edit file directly; `hermes setup --section agent` |
| **Skill** | Folder of procedural knowledge in the open Agent-Skills format; loaded on demand | `$HERMES_HOME/skills/<id>/SKILL.md` | `hermes skills {browse,search,install,update,uninstall}` |

The toolkit's model is **one container per toolkit "agent"** = one `$HERMES_HOME`. Hermes-internal profiles are an *advanced* path for users who want multiple personas in one container; documented in `docs/tutorial.md` as Strategy B. Default Strategy A = one toolkit agent per persona.

**Messaging-platform connectivity** is via `hermes gateway` (long-running messaging gateway in-container) plus platform-specific subcommands: `hermes slack` (manifest generator), `hermes whatsapp` (pairing). Mattermost / Matrix / Discord / Teams / IRC / Rocket.Chat are **not** built-in as of Hermes v1.x — users go via community skills (`hermes skills search <platform>`) or a custom gateway adapter. The toolkit's `0.0.0.0` refusal applies to dashboards specifically; if a connector strictly needs an inbound webhook port, document users to add it via `docker-compose.override.yml` (and prefer outbound modes like Slack Socket Mode where possible).

## How the code is organized

```
src/agents_cli/
  cli.py          # click root, --json / --yes / --verbose; routes to subcommands
  config.py       # pydantic v2 schema + tomlkit round-trip of ~/.config/agents/config.toml
  ui.py           # human/JSON output, confirmation prompts, --yes bypass
  errors.py       # exit-code mapping (0 success, 1 user error, 2 runtime, 3 health)
  mp.py           # multipass primitives: launch/exec/transfer/mount/snapshot/list/info; BOOTSTRAP_OK parser
  compose_runner.py  # docker compose verbs via multipass exec
  compose.py      # ruamel round-trip, Hermes/OpenClaw service fragments, port allocator
  hermes_config.py   # round-trip edit of each agent's config.yaml; refuses 0.0.0.0
  journal.py      # generic phase journal (create/remove/rename/upgrade)
  vm.py           # init/start/stop/shell/status/snapshot/restore/destroy
  agents_cmd.py   # create/list/remove/rename
  ops.py          # start/stop/restart/logs/exec/setup/doctor
  upgrades.py     # state machine: host-data-snapshot → vm-snapshot → pull → recreate → doctor → rollback
  export_cmd.py   # export/import/export-cron (host-cron, idempotent)
  codex.py        # login/refresh/status/skill-enable/skill-disable
  templates.py    # bootstrap script body, sandbox Dockerfile, fallback image tags
scripts/vm-bootstrap.sh   # idempotent rootless Docker + Node-less / Codex-less setup; emits BOOTSTRAP_OK line
```

## Backups

The user runs **duplicity on the host** as the only backup layer. The toolkit does NOT implement an internal backup engine. Don't add restic, borg, or off-site replication. `agents export` produces a clean archive in `<data-dir>/backups/` — duplicity picks it up.

## Testing

`pytest` (unit, no network), `mypy --strict`, `ruff check + ruff format --check`. Integration tests live behind `@pytest.mark.multipass`; opt in via `pytest -m multipass`. Never in CI.

The unit suite covers: compose round-trip and golden files, port allocator with override files, mp argv quoting, BOOTSTRAP_OK parser failure modes, upgrade state machine with injected runner, codex skill-enable / skill-disable, Hermes config round-trip, journal resume, flock serialization, sandbox-image-tag slugification.

## Conventions

- Pinned image tags — no `:latest` outside an explicit canary path.
- Every destructive op behind `--yes`.
- Every command emits machine-readable output under `--json`.
- Exit codes are load-bearing: 0 / 1 / 2 / 3.
- No `print()` — everything goes through `ui.py` so `--json` works.
- Subprocess invocations use list-form argv (never `shell=True`).
- New cross-layer mistakes get added to the "Critical cross-layer pitfalls" section above.

## Reference docs

- Hermes Agent: https://hermes-agent.nousresearch.com/docs (configuration, Docker terminal backend, Codex provider, Codex skill)
- OpenClaw: https://docs.openclaw.ai/providers/openai (bundled Codex plugin)
- Multipass: https://multipass.run/docs (mount, snapshot, find)
- OpenAI Codex CLI: `CODEX_HOME` env var for auth-dir relocation
