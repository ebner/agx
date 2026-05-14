# Project context for Claude Code

## What this is

`agents` is a host-side Python CLI that manages AI agents (Hermes Agent from Nous Research, OpenClaw) inside a single Multipass VM with rootless Docker. The host invokes only `multipass` — it never talks to the in-VM Docker daemon directly. Outer trust boundary = VM. Inner trust boundary = one Docker container per agent. The defense-in-depth motivation is the 2026 wave of agent-marketplace malware and skill-execution CVEs.

## Locked architecture rules (do not relitigate)

1. **Rootless Docker inside the VM.** Daemon runs as `ubuntu`, socket at `<docker_socket_path>` (read from `[vm_runtime]` in config — set by BOOTSTRAP_OK). Never hardcode the path or the UID.
2. **One mount, one source of truth.** Host `<data-dir>` is mounted at VM `/home/ubuntu/agents` with explicit `multipass mount -u <host-uid>:1000 -g <host-gid>:1000`. All per-agent data, the compose file, the `.env`, exports, and the toolkit-scoped Codex auth live under this path. The VM disk only holds OS + Docker engine + image cache. Opt-out is `vm init --data-dir-in-vm` — **recommended for SQLite-heavy workloads** (Hermes' kanban/sessions/insights stores use WAL, which doesn't work over the 9p share; in-VM ext4 fixes that). Under `--data-dir-in-vm` agent state is host-visible only via `agents export` archives; `agents export-cron install --time 02:30,08:30,14:30,20:30` gives you 6-hourly archives that duplicity picks up natively.
3. **Toolkit-scoped Codex auth** at `<data-dir>/.codex/` — NEVER the user's personal `~/.codex/`. `agents codex login` sets `CODEX_HOME=<data-dir>/.codex`.
4. **The CLI owns the compose file.** Users get `docker-compose.override.yml` for hand edits. Never heredoc through `multipass exec` — always: flock → ruamel.yaml round-trip → write host temp via `compose.host_staging_dir(cfg)` (= `<host_data_dir>/.staging/`) → `multipass transfer` to `<vm_agents_root>/.compose.<pid>.<uuid>.new` → `docker compose --project-directory <vm_agents_root> -f ... --env-file <vm_agents_root>/.env config -q` validation → VM-side `mv` to `docker-compose.yml` (atomic same-fs rename) → cleanup. Host staging path is NOT under `/tmp/` (snap-confined `multipass` — the default Ubuntu install — cannot SFTP from `/tmp`: `cannot access`) and NOT under `$HOME/.cache/` (snap's home interface excludes dotfile paths: `Permission denied`). Staging under the host data dir works because that dir is already `multipass mount`ed into the VM, so multipass has explicit access — guaranteed on both snap and deb installs. Every module that `multipass transfer`s a host file into the VM (`compose`, `hermes_config`, `codex` for Dockerfile + auth-tar sync) uses `compose.host_staging_dir(cfg)` — do NOT introduce new `/tmp` or `~/.cache` paths. `compose.cleanup_stale_temps(cfg)` is invoked by `vm.start` and at the head of every mutation. Same pattern applies to per-agent `config.yaml` writes via `hermes_config.write_atomically`, which validates via `hermes config validate` exec'd inside the running container (best-effort — silently skipped if the container isn't up yet).
5. **Image-source allowlist** is a parsed `(registry, repo)` tuple match — `nousresearch/hermes-agent`, `openclaw/openclaw`, `volcengine/openviking` (memory sidecar), `ghcr.io/firecrawl/firecrawl` + `ghcr.io/firecrawl/playwright-service` + `ghcr.io/firecrawl/nuq-postgres` (firecrawl stack), and `redis` + `rabbitmq` (firecrawl dependencies). Reject `hermes-agent.org` and any registry-prefixed variant. `validate_image_ref` handles `repo`, `repo:tag`, AND `repo@sha256:…` (digest pin) forms — strips `@sha256:…` BEFORE partitioning on `:`. New sibling-service backends require explicit allowlist additions in `compose.py`.
6. **Dashboard binding** is `127.0.0.1`/`localhost` only. The CLI refuses any Hermes config change to `0.0.0.0`.
7. **Every mutation holds `<data-dir>/.agents.lock`** (`flock`). Read-only commands skip the lock. Two parallel `agents create` calls must serialize.
8. **Every multi-phase op writes a phase journal** at `<data-dir>/.journal/<op>-<name>-<utc>.json`. Re-running the command resumes from the last completed phase. `doctor` flags journals older than 24 h.
9. **Compose service blocks are written once and never auto-regenerated.** When the service-fragment template evolves (new env var, changed volume layout), existing agents keep their stale block forever. `agents reseat <name>` is the explicit "rewrite this agent's compose service entry from the current fragment, preserving port + image tag + env_file additions + depends_on additions, then force-recreate" command. Run after toolkit upgrades.

## Critical cross-layer pitfalls (already fixed; don't reintroduce)

- **`terminal.docker_volumes` paths are interpreted by the VM Docker daemon**, not by paths inside the Hermes container. Sandbox volume entries use VM-side host paths like `/home/ubuntu/agents/.codex:/<sandbox-user>/.codex:ro`.
- **Hermes runtime container needs `DOCKER_HOST` env** plus the rootless socket mount, otherwise its CLI can't reach the daemon to spawn the sandbox.
- **Multipass snapshots do NOT cover the host-mounted data-dir.** Upgrade flow takes BOTH a VM snapshot AND a host-data tarball (in `<data-dir>/.upgrades/`).
- **Multipass mount does not always survive stop/start.** `agents vm start` verifies and remounts, then runs a write/read/unlink probe.
- **Multipass mount MUST be `--type native`, not the default `classic` / fuse.sshfs.** Rootless Docker cannot bind-mount paths from a fuse.sshfs filesystem into a container — kernel returns `operation not supported` and every agent fails on first start. Native uses 9p (Linux/QEMU) or virtiofs (macOS/HyperKit), which Docker handles fine. Native mounts require the VM stopped to add, so `vm._ensure_mount` does a stop→mount→start dance when needed; mounts persist across reboots so it's a one-time cost.
- **No `user:` override on Hermes container under rootless Docker.** Container UID 0 maps to the rootless host user (`ubuntu`/1000) which owns the multipass-mounted data dir. Setting `user: 1000:1000` would push the process into the rootless subuid range (e.g. host uid 100999) and every write into `/opt/data` would be permission-denied. The compose fragment pins HOME via env instead so the Codex import-bridge path is stable regardless of image USER/HOME drift.
- **Compose env must pin `PATH=/opt/hermes/.venv/bin:...`** — the Hermes image's entrypoint prepends this internally, but `docker exec` (used by `agents doctor` to run `hermes doctor`) bypasses the entrypoint and would otherwise hit `executable file not found`.
- **Rootless Docker install does NOT ship the compose plugin.** `get.docker.com/rootless` installs only daemon + CLI. The bootstrap adds Docker Inc's apt source and installs `docker-compose-plugin` system-wide (lands at `/usr/libexec/docker/cli-plugins/`, picked up by the rootless CLI via plugin auto-discovery).
- **`/home/ubuntu/bin` MUST be on the system PATH** (`/etc/environment`). The rootless install puts `docker` there; `multipass exec` doesn't source `.profile` so without this the toolkit hits `bash: docker: command not found`. The bootstrap prepends it idempotently.
- **OpenClaw service paths are per-agent** (`./<name>/.openclaw`, `./<name>/workspace`) so multiple named OpenClaw agents don't collide.
- **Codex sandbox image tag** must be slugified (`agents/hermes-sandbox-codex:<sha256(base)[:12]>`) — base refs contain `/` and `:`.
- **Image-baked Hermes paths need runtime-user write — they're lost on every container recreate.** Symptom: `hermes setup` (or any other in-container install) hits `EACCES: permission denied, mkdir '/opt/hermes/scripts/whatsapp-bridge/node_modules'`. Cause: `/opt/hermes/scripts/` and `/home/hermes/` are baked into the image owned by root (or whatever USER the image was built as). The runtime user is `hermes`/1000 after the entrypoint's `usermod`, but `usermod` doesn't migrate the home dir, and image-baked subdirs of `/opt/hermes` stay root-owned. Every recreate (`docker rm -f` + `compose up`) wipes the container's overlay fs, so any chown is ephemeral. Remediation: `fixperms_cmd.chown_hermes_install_paths(cfg, svc, huid)` runs after every recreate — called from `agents create` (service_up phase), `agents fix-perms`, and `agents reseat`. Targets list lives at `fixperms_cmd.HERMES_WRITABLE_PATHS`; each entry pairs a target with a tuple of paths to `-prune` so the recursive chown doesn't follow read-only bind-mounts (notably the Codex import bridge at `/home/hermes/.codex:ro`). **Any new code path that force-recreates a Hermes container must call this helper after start** or `hermes setup`'s install branches will silently regress.
- **`/opt/data` permission drift is a recurring class of bug.** Symptom: `agents exec coder hermes <anything>` hits `PermissionError: '/opt/data/.env'` (or similar). Cause: the Hermes image entrypoint *should* `usermod -u $HERMES_UID hermes` on container start, but sometimes doesn't — `docker compose up -d` skips entrypoint when it thinks there's no drift, and stale containers from before HERMES_UID was wired in keep running with hermes=10000. `/opt/data` ends up owned by uid 10000 mode 700; `agents exec` runs at uid 1000; EACCES. Detection: `_check_hermes_data_dir_accessible` in `ops.py` probes `stat /opt/data` as the runtime uid every doctor run. Remediation: `agents fix-perms <agent>` — force-remove the container, recreate fresh (entrypoint runs), then `chown -R <huid>:<huid> /opt/data` + `chmod 755 /opt/data`. The chmod 755 is a band-aid for when the entrypoint *still* doesn't honour HERMES_UID; mode-600 files (auth.json etc.) stay private.
- **`localhost` / `127.0.0.1` / `0.0.0.0` are refused in `[ollama]` URLs** (`OllamaConfig._validate_url`). Inside the VM those resolve to the VM itself, not to the host where the user is actually running Ollama. The toolkit suggests the multipass NAT gateway (`multipass info <vm>` → IPv4) when surfacing the error.
- **Memory backends needing embeddings/LLM read from `[ollama]` only.** The toolkit does NOT manage an Ollama instance. mem0 and OpenViking refuse to install when `[ollama]` is empty (mem0 falls back to `OPENAI_API_KEY` in `.env` if present; OpenViking does not, since its fact-extraction loop would melt a personal ChatGPT subscription).
- **Codex auth ≠ OpenAI API key.** Hermes' `openai-codex` provider knows how to drive the ChatGPT-subscription OAuth flow; nothing else does. Of the supported memory backends, only `holographic` works with Codex auth alone — it doesn't call out for embeddings or LLM. Don't introduce a "Codex proxy" path for the others; it would violate OpenAI ToS and break on every flow rotation.
- **Per-agent secrets live in `<data-dir>/<name>/.env`**, NOT the project-level `<data-dir>/.env`. The project file is for compose `${VAR}` interpolation only (HERMES_UID/GID, OLLAMA_API_KEY). The per-agent file is wired into each container via compose `env_file: ./<name>/.env` so its KEY=VALUE pairs become container env. Managed by `agents secret {set,unset,list,clear}` — values are never printed back, key names must match `^[A-Z][A-Z0-9_]*$`. Both fragments (Hermes and OpenClaw) include the env_file clause; the file is created (empty) by `agents create` so compose validation passes before any secrets are set.
- **Hermes filters env vars before spawning sandboxed subprocesses** (`terminal.env_passthrough` in `config.yaml`, defaults to `[]`). A secret in the container env is visible to Hermes' built-in Python tools (which read `os.environ` directly) but NOT to anything the `terminal` or `execute_code` tools spawn — meaning the LLM's own shell-based "is this configured?" diagnostics return false negatives. `agents secret set` therefore *also* adds the key to `terminal.env_passthrough` (Hermes agents only; OpenClaw doesn't have this concept). `agents secret unset` removes it. Idempotent on both sides. Per Hermes' docs: "For env vars not declared by any skill, add them to terminal.env_passthrough." Same wiring applies to `agents firecrawl up/down` for the FIRECRAWL_API_KEY/URL pair.
- **Firecrawl self-host is project-scoped, not per-agent**, and pinned by digest. Upstream only publishes floating tags (`latest`, `linux-amd64`); the toolkit resolves each to a `@sha256:…` digest on first `agents firecrawl up` via `docker pull` + `docker image inspect`, writes the digests to `<data-dir>/.firecrawl-state.json`, and only ever references them by digest in compose. Subsequent `up` invocations reuse the captured digests; `agents firecrawl up --upgrade` re-resolves. Postgres data lives at `<data-dir>/firecrawl/postgres/` (bind mount, duplicity-friendly). `agents create` checks `firecrawl_cmd.is_installed(cfg)` after compose-up and auto-adds FIRECRAWL_API_KEY/URL to the new Hermes agent's `terminal.env_passthrough` — so a fresh agent created while firecrawl is up needs zero further wiring.

## Codex paths — how Hermes and OpenClaw differ

| Agent | Codex as LLM | Codex as tool |
|---|---|---|
| Hermes | `model.provider: openai-codex`. Mounted Codex auth at `/home/hermes/.codex:ro` is an import bridge; once `hermes setup` runs, persistent auth lives at `/opt/data/.hermes/auth.json` on the bind-mounted data dir. The compose fragment pins BOTH the runtime user (`user: ${HERMES_UID}:${HERMES_GID}`) AND HOME (`HOME=/home/hermes` in env), so the import target is a function of compose settings — NOT of the image's USER/HOME, which have drifted between Hermes releases (newer images run as `root` with no explicit HOME). `agents create` only ensures the image is pulled; it no longer refuses on USER/HOME drift. | Opt in via `agents codex skill-enable`. CLI builds a derived sandbox image with Codex CLI baked in and `ENV CODEX_HOME=/codex`; volume entry mounts the toolkit Codex auth at `/codex:ro`. User-independent — works for sandbox images running as root, pn, node, etc. The whole operation holds `<data-dir>/.agents.lock`. |
| OpenClaw | OpenClaw bundles its own Codex plugin. Run `openclaw models auth login --provider openai-codex` inside the container. **Never** mount Codex into OpenClaw — would conflict. | Same plugin path. |

## Hermes terminology — don't conflate

Three distinct concepts that all sound related. Future doc work and any tooling we add must keep them separate:

| Term | What it is | Where it lives | CLI |
|---|---|---|---|
| **Profile** | An isolated Hermes instance — separate `$HERMES_HOME` with its own auth/sessions/skills/config | `$HERMES_HOME/profiles/<name>/` | `hermes profile {list,create --clone,use,delete,rename,export,import}` |
| **Personality** (SOUL.md + `/personality`) | The agent's persistent voice/identity (file) plus session-level overlays (preset or custom in `agent.personalities` in `config.yaml`) | `$HERMES_HOME/SOUL.md` | edit file directly; `hermes setup --section agent` |
| **Skill** | Folder of procedural knowledge in the open Agent-Skills format; loaded on demand | `$HERMES_HOME/skills/<id>/SKILL.md` | `hermes skills {browse,search,install,update,uninstall}` |

The toolkit's model is **one container per toolkit "agent"** = one `$HERMES_HOME`. The default recommendation is **one container + topical skills**, not one container per persona. Earn a second container only when LLM provider, connectivity, upgrade cadence, resource limits, or blast-radius isolation actually differ. See `docs/tutorial.md` §3 for the full decision tree.

**Hermes profiles are NOT a way to multiplex one chat-bot across personas.** From the Hermes docs: each profile that runs a gateway runs its own gateway process with its own bot token; two profiles cannot share a bot. So profiles are for *disposable* second `$HERMES_HOME`s — sandboxing, A/B testing personalities, scratch experiments — not for production multi-persona deployments. If a user wants channel-aware persona routing, the answer is either (a) one bot + topical skills, (b) one bot + interactive `/personality` overlay, or (c) separate toolkit agents with separate bot users. There is no native gateway-side per-channel dispatcher.

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
  ollama_cmd.py   # configure/status/clear — points the toolkit at an external Ollama / OpenAI-compatible endpoint (the toolkit does NOT run Ollama)
  secrets_cmd.py  # per-agent .env management (API keys for skills like firecrawl); never the project .env
  fixperms_cmd.py # `agents fix-perms <agent>` — force-recreates container, chowns /opt/data, chmods 755 for the recurring permission-drift bug
  firecrawl_cmd.py # project-level self-hosted Firecrawl (api + playwright + redis + rabbitmq + postgres). `agents firecrawl up` self-pins images to digests on first run, auto-wires every Hermes agent's terminal.env_passthrough.
  memory/         # memory backend plugins (Hermes only)
    __init__.py   # registry + dispatch + state file at <data-dir>/.memory-state/<agent>.json
    holographic.py # in-process; SQLite+FTS5+HRR; doctor checks for SQLite-on-9p + NumPy
    mem0.py       # in-process library mode; reads [ollama] or OPENAI_API_KEY
    openviking.py # sibling docker service per agent; needs [ollama]
  templates.py    # bootstrap script body, sandbox Dockerfile, fallback image tags
scripts/vm-bootstrap.sh   # idempotent rootless Docker + Node-less / Codex-less setup; emits BOOTSTRAP_OK line
```

## Backups

The user runs **duplicity on the host** as the only backup layer. The toolkit does NOT implement an internal backup engine. Don't add restic, borg, or off-site replication. `agents export` produces a clean archive in `<data-dir>/backups/` — duplicity picks it up.

## Testing

`pytest` (unit, no network), `mypy --strict`, `ruff check + ruff format --check`. Integration tests live behind `@pytest.mark.multipass`; opt in via `pytest -m multipass`. Never in CI.

The unit suite covers: compose round-trip and golden files, port allocator with override files, mp argv quoting, BOOTSTRAP_OK parser failure modes, upgrade state machine with injected runner, codex skill-enable / skill-disable, Hermes config round-trip, journal resume, flock serialization, sandbox-image-tag slugification, memory plugin enable/disable round-trip per plugin (holographic, mem0, openviking), Ollama URL schema validation (localhost refusal).

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
