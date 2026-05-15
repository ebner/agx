# Runbook

## First-time setup

```bash
# Install the host CLI (PEP 668-aware; works on Homebrew / Debian / Ubuntu 24+).
pipx install -e .                 # macOS: brew install pipx first if missing
# or use a venv: python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# (Optional) Codex CLI on host so Hermes can use Codex as LLM provider
npm install -g @openai/codex

# Bring up the VM. Pick any path your duplicity (or other host backup) covers.
agents vm init --data-dir /data/agents

# (Optional) Authenticate the toolkit-scoped Codex account
agents codex login

# Create your first Hermes agent, run the wizard
agents create coder --type hermes --setup
```

## Day-to-day

```bash
agents list                      # what's running
agents doctor                    # health
agents logs coder -f             # follow logs
agents exec coder                # TTY shell in the container
agents exec coder hermes shell   # straight into Hermes
agents export coder              # archive into <data-dir>/backups/
```

## Upgrades

```bash
# Upgrade one agent. Auto-rollback on doctor failure.
agents upgrade coder --tag v1.2.4

# Upgrade everything; canary first
agents upgrade --all --canary coder

# Restore from the latest pre-upgrade snapshot if something went sideways
agents rollback coder --yes

# Or pick a specific snapshot id (filename, suffix-match, or pre-upgrade-*
# multipass snapshot name to restore only the VM):
agents rollback coder --snapshot coder-20260512T120000Z.tar.zst
agents rollback coder --snapshot pre-upgrade-coder-20260512T120000Z
```

`agents upgrade` takes two snapshots before touching anything: a host-data `.tar.zst` at `<data-dir>/.upgrades/<name>-<utc>.tar.zst` and a multipass VM snapshot `pre-upgrade-<name>-<utc>`. Both are listed in the upgrade outcome JSON / human output. Legacy `.tar.gz` archives from pre-v0.1.1 versions still work for rollback.

## Recovery from a crashed operation

The CLI writes a phase journal under `<data-dir>/.journal/<op>-<name>-<utc>.json` for every multi-phase op. If a command crashes mid-way, just re-run it — the journal causes already-completed phases to be skipped.

| op | phases (in order) |
|---|---|
| create | `data_dir_created` → `compose_updated` → `service_up` → `setup_done` (optional) |
| remove | `container_stopped` → `compose_updated` → `data_dir_deleted` |
| rename | `compose_updated` → `dir_renamed` → `container_recreated` |
| upgrade | `host_data_snapshot` → `vm_snapshot` → `tag_pinned` → `pulled` → `recreated` → `doctor_passed` |

`agents doctor` warns about any journal older than 24 hours. To clear a stuck journal once you've manually reconciled state, simply delete the file from `<data-dir>/.journal/`.

## Reseating an agent after a toolkit upgrade

Compose service blocks are written once at `agents create` time and not
touched afterwards. If a toolkit version adds a new env var to the
service fragment (e.g. `HERMES_UID`), pre-existing agents keep their
stale block until you reseat them.

```bash
agents reseat coder
```

What it does:

1. Reads the agent's existing compose service entry.
2. Extracts the port, image tag, `env_file` additions (per-agent
   secrets), and `depends_on` additions (sidecars like OpenViking) so
   they survive the regeneration.
3. Re-renders the service block from the current toolkit fragment —
   picks up any new env var, volume, or other template changes.
4. Force-recreates the container so the new block actually takes
   effect (a plain `docker compose up -d` often decides "no drift, keep
   the running container" and skips the entrypoint).
5. For Hermes agents, additionally re-aligns `/opt/data` ownership
   (same chown/chmod dance as `agents fix-perms`).

When to run it: after upgrading the toolkit, if `agents doctor` flags
something or if `agents exec <name> …` starts misbehaving. Safe to
run any time — preserves all stateful agent data.

## /opt/data permission drift (and how `agents fix-perms` resolves it)

Symptom: `agents exec coder hermes <anything>` crashes on import with
`PermissionError: '/opt/data/.env'` (or similar files under
`/opt/data`).

Cause: The Hermes image entrypoint *should* `usermod -u $HERMES_UID
hermes` on container start so the in-container `hermes` user matches
the toolkit's runtime uid (1000). When it doesn't (entrypoint skipped
on a stale running container, image-level changes, env-var
interpolation failure), `/opt/data` ends up owned by uid 10000 with
mode 700. `agents exec` runs as uid 1000 → falls to "other" perms on
a 700 dir → EACCES on every traversal.

Detection: `agents doctor` runs a per-Hermes-agent probe that stats
`/opt/data` as the runtime uid and flags failures or owner mismatches
with the precise remediation command.

Remediation: `agents fix-perms <agent>`.

```bash
agents fix-perms coder
```

This is the one-shot fix:

1. Stops the container.
2. Force-removes it (so `compose up` can't decide "no drift, keep
   running" and skip the entrypoint).
3. Brings it back up — entrypoint runs from scratch.
4. Queries the *actual* hermes uid that resulted (whether the
   entrypoint did or didn't honour HERMES_UID).
5. `chown -R <huid>:<huid> /opt/data` so files line up with the live
   runtime user.
6. `chmod 755 /opt/data` so the toolkit's `agents exec` at uid 1000
   can traverse the dir even when the entrypoint failed to align
   hermes (band-aid for the worst case). Per-file modes are untouched
   — secrets at mode 600 (`auth.json`, `auth.lock`) stay private.
7. `chown` of image-baked paths the runtime user writes into — see
   below.

When the entrypoint succeeded, output is `ok` and hermes uid = 1000.
When it didn't, output is `PARTIAL`: traversal works, but the
underlying image issue persists and you should investigate the
entrypoint of your pinned Hermes image tag.

### Image-baked paths the runtime user writes into

`/opt/data` is mounted from the host (so its perms persist), but
Hermes also writes into image-baked paths *inside* the container's
overlay filesystem — and those get wiped on every recreate.

The two known targets:

- `/opt/hermes/scripts/` — where `hermes setup` installs per-bridge
  helpers (e.g. the WhatsApp bridge's `npm install` lands at
  `whatsapp-bridge/node_modules/`). Image-baked root-owned by
  default. Without a chown to uid 1000, `hermes setup` (or a
  re-run of one of its install branches) hits `EACCES: permission
  denied, mkdir '/opt/hermes/scripts/whatsapp-bridge/node_modules'`.
- `/home/hermes/` — the runtime user's home. npm cache, pip cache,
  browser cache, dotfiles. The image's `usermod -u 1000 hermes`
  doesn't migrate the existing home dir, so it stays at the
  image-baked ownership.

The toolkit auto-chowns both after every container recreate (in
`fixperms_cmd.chown_hermes_install_paths`) from three code paths:
`agents create` (service_up phase), `agents fix-perms`, and
`agents reseat`. The recursive chown prunes the Codex import
bridge at `/home/hermes/.codex` (which is `:ro` and would otherwise
emit "Read-only file system" on every entry).

If you ever see an `EACCES` on a path inside `/opt/hermes/` or
`/home/hermes/` that *isn't* in this list, add it to
`fixperms_cmd.HERMES_WRITABLE_PATHS` (with prune entries for any
ro bind-mounts under it) rather than chowning manually.

## Observability

### VM health — `agents doctor`

The doctor surface is binary (ok / warn / fail) — drop it in cron and
alert on warn+ exit codes. Currently checks:

- `vm_reachable`, `mount_round_trip`, `user_docker_service`,
  `port_conflicts`, `data_dir_exists`, `codex_auth`, journals,
  per-agent container + docker-socket + hermes-doctor + codex-mount +
  data-dir-access.
- `disk_space` — warn < 15% free, fail < 5%.
- `vm_memory` — warn < 1 GiB available, fail < 256 MiB. Reads
  `MemAvailable` from `/proc/meminfo` (kernel's reclaim-aware
  accounting, not raw `free -used`).
- `vm_load` — warn at load1/cpus > 1.0, fail > 2.0.

Exit codes: 0 (all ok), 3 (any fail).

### Firecrawl observability — `agents firecrawl status`

Snapshot of the running stack: per-container state + cpu% + memory
use, API health probe (via netcat from a sibling container), redis
rate-limit counters when present, image digests, list of agents
wired in. No side effects, safe to poll.

Rate-limit counters only exist when Firecrawl runs with DB auth
enabled. Self-host defaults to `USE_DB_AUTHENTICATION=false`, which
makes the auth middleware short-circuit before any rate-limiter call
fires — so the in-source caps (scrape=100/min etc) aren't enforced
either. Verified empirically: 30 parallel scrapes returned 30 × 200,
zero 429s. **Real saturation manifests as container resource
pressure**, not as 429s. Watch CPU% and memory in the `containers`
section — playwright OOM is the practical ceiling.

The `queues` section gives both **current depth** (RabbitMQ — modern
NUQ scrape/crawl path) and **lifetime totals** (Bull / redis — legacy
queues for precrawl, billing, llms-txt, deepresearch). RabbitMQ's
`pending` + `in-flight` + `consumers` tell you whether work is
backlogged or actively flowing; Bull's `completed` + `failed` track
lifetime job counts since the redis instance came up (Bull doesn't
auto-trim, so these accumulate). NUQ-internal `prefetch` queues are
filtered out of the human view since they're transient framing
buffers — included in the `--json` payload for debugging.

## Self-hosted Firecrawl

One Firecrawl deployment per VM, shared across every Hermes agent the
toolkit manages — no per-agent setup needed.

```bash
agents firecrawl up                # first-time install + start
agents firecrawl up --upgrade      # re-resolve floating tags into fresh digests
agents firecrawl down              # stop; postgres data preserved
agents firecrawl status            # running? which agents are wired?
agents firecrawl logs              # tail api logs
agents firecrawl logs playwright -f
agents firecrawl destroy --yes     # nuke everything including postgres data
```

What `up` does:

1. Pulls each upstream floating tag (linux-amd64, latest, alpine,
   3-management).
2. Captures the resolved digest via `docker image inspect` and writes
   it to `<data-dir>/.firecrawl-state.json`. The compose file only
   ever references images by `@sha256:…` so the toolkit's "no
   `:latest` outside canary" rule still holds.
3. Generates the 5-service stack (`firecrawl-api`,
   `firecrawl-playwright`, `firecrawl-redis`, `firecrawl-rabbitmq`,
   `firecrawl-postgres`) and writes it into the project compose file
   via the atomic flock-protected path.
4. Populates `<data-dir>/.env` with `FIRECRAWL_API_KEY` (a generated
   `fc-self-hosted-...` placeholder — firecrawl-py insists on a
   non-empty key even when the server has auth disabled),
   `FIRECRAWL_API_URL=http://firecrawl-api:3002`, `BULL_AUTH_KEY`,
   `POSTGRES_PASSWORD`.
5. Adds `FIRECRAWL_API_KEY` and `FIRECRAWL_API_URL` to every existing
   Hermes agent's `terminal.env_passthrough` so the LLM's
   shell-based diagnostics also see the config.
6. `docker compose up -d` the five services. Healthchecks gate the
   api on rabbitmq + postgres being ready.

Agents created **after** `firecrawl up`: `agents create` checks
firecrawl state during the `service_up` phase and adds the
passthrough entries automatically. Zero extra steps.

Storage: `<data-dir>/firecrawl/postgres/` is the only durable volume
(captured by duplicity / `agents export`). Redis and RabbitMQ are
ephemeral — losing in-flight scrape jobs on restart is acceptable
for a single-user setup.

Resource caps in the generated compose: api 2 CPU / 3 GB, playwright
2 CPU / 2 GB (with `BLOCK_MEDIA=true`, `MAX_CONCURRENT_PAGES=2`),
others 512 MB each. Idle target ~1.5–2 GB total.

Networking: only `firecrawl-api` is reachable, and only over the
docker network as `http://firecrawl-api:3002`. No host port is
published; debug via `agents firecrawl logs` or `agents vm shell`.

## Per-agent secrets

API keys for skills (Firecrawl, GitHub, etc.) go in a per-agent .env
file that the toolkit wires into the container via compose
`env_file:`. **Never** put them in `<data-dir>/.env` — that's a
project-level file shared across every container.

```bash
agents secret set coder FIRECRAWL_API_KEY            # hidden prompt
agents secret set coder GITHUB_TOKEN ghp_xxxx        # arg form
agents secret list coder                             # names only
agents secret unset coder FIRECRAWL_API_KEY
agents secret clear coder                            # nuke them all
```

Storage: `<data-dir>/<name>/.env`. Duplicity already covers it because
it sits under the host data-dir. The toolkit restarts the agent's
container automatically on each mutation so the new env takes effect
(compose only re-reads `env_file:` on container recreate).

Key names must match `^[A-Z][A-Z0-9_]*$`. Values may not contain
newlines. Secret values are never printed back — `list` returns names
only, JSON output never includes values.

Backfill: agents created before the secret command existed get their
compose `env_file:` clause added on first `secret set`. Idempotent.

## Memory backends

The toolkit manages three memory plugins for Hermes agents. Pick based
on what credentials / infrastructure you already have:

```bash
agents memory list                          # show what's available
agents memory enable holographic coder     # zero-dep, works with Codex alone
agents memory enable mem0 coder            # in-process, needs Ollama or OPENAI_API_KEY
agents memory enable openviking coder      # sibling docker service, needs Ollama
agents memory disable coder                # back to built-in
agents memory status coder                 # what's wired
```

Backends that need an embeddings + LLM provider read it from the
toolkit's `[ollama]` config. The toolkit does NOT run Ollama for you —
point it at an existing one:

```bash
agents ollama configure --url http://10.20.30.1:11434
agents ollama configure --url https://hosted.example/v1 --api-key sk_…
agents ollama status                        # probe reachability from VM + containers
agents ollama clear
```

`localhost` / `127.0.0.1` / `0.0.0.0` URLs are refused — they resolve
to the VM itself, not to the host where Ollama actually runs. Use the
multipass NAT gateway (`multipass info <vm>` shows it; typically
`10.<x>.<y>.1`), a LAN/Tailscale IP, or a public hostname.

**Doctor checks worth knowing:**

- `holographic` + default host-mount: the SQLite WAL on 9p risk
  (matches the Hermes kanban/sessions trap). Switch to
  `agents vm init --data-dir-in-vm` if you're seeing `database is
  locked` errors.
- `holographic` requires NumPy inside the Hermes container — without
  it, the plugin silently degrades to FTS5-only (upstream issue
  #17350). The status command probes this.
- `mem0` and `openviking` need a reachable Ollama URL — `agents ollama
  status` is the fastest way to confirm.

## Codex skill (Hermes only)

```bash
agents codex skill-enable coder    # build sandbox image with Codex baked in
agents codex skill-disable coder   # restore original sandbox image
```

The first call slugifies the agent's current `terminal.docker_image` and builds `agents/hermes-sandbox-codex:<sha256[:12]>`. The state file at `<data-dir>/<name>/.data/.codex-skill.json` remembers the original image so `skill-disable` can restore it exactly.

OpenClaw bundles its own Codex plugin — run `openclaw models auth login --provider openai-codex --device-code` *inside* the OpenClaw container instead:

```bash
agents exec personal -- openclaw models auth login --provider openai-codex --device-code
```

## Backups (host-side)

Whatever host backup you run over `<data-dir>` (duplicity, restic, borg, …) covers everything: per-agent state, compose, .env, Codex auth, journals, upgrade tarballs. There is no internal backup engine.

For crash-consistent snapshots, run `agents export <name>` first — that writes a clean tarball into `<data-dir>/backups/` that the host backup will pick up on its next run.

```bash
agents export coder              # one agent
agents export --all              # all agents
agents export --all --to ~/elsewhere/  # send archives somewhere other than <data-dir>/backups/

# Schedule one or more daily runs via host cron. Comma-separated times
# install one crontab line per time — re-running install replaces all
# prior toolkit-managed entries (clean idempotent).
agents export-cron install --time 02:30                       # once a day at 02:30
agents export-cron install --time 02:30,08:30,14:30,20:30     # four times a day
agents export-cron status
agents export-cron uninstall
```

### Picking a schedule for SQLite-heavy use

Hermes' kanban/sessions/insights databases use SQLite WAL. In `--data-dir-in-vm` mode they live on the VM's ext4 disk and work normally (WAL doesn't function over the host shared-FS share — see [architecture.md](architecture.md) for the why). The trade-off is that those files are not directly host-visible — your only host-side view of recent agent state is the most recent `agents export` archive.

A reasonable cadence:

| Use case | Schedule | Rationale |
|---|---|---|
| Single home/family bot, casual use | `02:30` (default) | One archive a day, ≤24 h of potential loss after a VM crash. |
| Active multi-bot, daily-driver host | `02:30,14:30` | ≤12 h. Two archives a day is enough headroom for most accidents. |
| Heavy SQLite write (kanban, sessions, insights) | `02:30,08:30,14:30,20:30` | ≤6 h. Each archive is a few MB; disk cost is trivial. |
| You really care | `*/2` (every two hours, hand-installed) | The toolkit's HH:MM cron syntax doesn't express step intervals; use `crontab -e` and add `0 */2 * * * /usr/local/bin/agents --yes export --all` with the `# agents toolkit — managed export-cron entry` marker omitted (the toolkit won't manage it, but it won't trip over it either). |

`agents export` shells out to `tar --zstd` for the agent's per-agent data dir. For Hermes, this captures `state.db`, `kanban.db`, sessions, skills, SOUL.md, config.yaml. Under `--data-dir-in-vm` the tar runs inside the VM (where the data lives on ext4), then the archive is `multipass transfer`-ed out to the host. tar isn't aware of SQLite WAL mid-flight, so each individual run is *near-consistent* rather than guaranteed crash-consistent — if a write is in progress at the exact moment tar copies the file, the captured `.db` may be missing the corresponding WAL entries. SQLite's recovery on next open handles most such cases, and frequent backups give you many fallback points.

For strict crash consistency on a specific agent, stop it first: `agents stop coder && agents export coder && agents start coder` — but that's rarely necessary in practice given how cheap the multi-archive approach is.

### Backup retention

`agents export` only writes new archives — it never deletes old ones. Either:

- Let your host backup tool (duplicity etc.) prune `<data-dir>/backups/` per its own retention policy.
- Or add a separate cron entry like `find <data-dir>/backups -name '*.tar.zst' -mtime +14 -delete` to drop archives older than 14 days. The toolkit deliberately doesn't manage retention — that's the user's policy call.

## Rebuilding the VM (disk shrink, Ubuntu bump, wedged-VM recovery)

`agents vm resize` handles grow operations in place. The cases it can't:

- **Disk shrink** — multipass refuses; the only way to a smaller disk is destroy + re-init.
- **Bump the Ubuntu release** (e.g. 24.04 → 26.04 once Docker CE supports it).
- **The VM is wedged** — bootstrap stuck, kernel borked, network plumbing wrong — and you want a clean slate without losing agent state.

`agents vm rebuild` is the journaled, non-destructive flow for all three:

```bash
agents vm rebuild --disk 30                         # shrink (or grow) disk; keep other sizing
agents vm rebuild --ubuntu-release 26.04            # try a newer release
agents vm rebuild --cpus 4 --memory 4               # downsize the whole VM
agents vm rebuild                                   # same sizing as today (effectively a clean-room reboot)
```

Phases (each idempotent, journaled at `<data-dir>/.journal/rebuild-<vm>-<utc>.json`):

| phase | what it does |
|---|---|
| `staged_exports` | `agents export` every agent into `<data-dir>/.rebuild-staging/` |
| `vm_destroyed` | `multipass delete --purge` the old VM |
| `vm_initted` | fresh `agents vm init` with the (overridden or kept) sizing |
| `state_restored` | `agents import` each agent from its staging archive |
| `services_started` | `docker compose up -d` each service |

If something crashes mid-rebuild, re-running `agents vm rebuild` picks up where the journal left off. The staging dir is left in place after success so you can inspect; safe to `rm -rf <data-dir>/.rebuild-staging/` once `agents doctor` is green.

## Local inference (Ollama, llama.cpp)

The toolkit can run local LLMs inside the VM as sibling Docker services. Hermes agents (and OpenClaw, with manual configuration) reach them over the project Docker network — no external API calls, no per-token billing.

### Phase 1: Ollama (simplest path)

```bash
# Start the Ollama service (auto-resolves and pins the image digest)
agents inference up ollama

# Pull a model — accepts both Ollama Hub refs and HuggingFace
agents model pull ollama llama3.2:3b
agents model pull ollama hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M

# Wire it into an agent (writes the model: block in config.yaml)
agents model use coder --engine ollama --model llama3.2:3b

# Or route compression + vision through it too (free, no cloud cost)
agents model use coder --engine ollama --model llama3.2:3b --aux all

# Restart so the gateway picks up the new config
agents restart coder
```

Status + introspection:

```bash
agents inference status            # what's running, models loaded
agents model list                  # disk inventory (both engines)
agents doctor coder               # includes inference_ollama check
```

### Phase 2: llama.cpp (HF GGUFs, per-model serving)

llama.cpp is one-model-per-process by design, so each loaded model becomes its own compose service:

```bash
# Pin the llama.cpp image (no service started yet)
agents inference up llama-cpp

# Download a GGUF from HuggingFace into <data-dir>/models/llama-cpp/
agents model pull llama-cpp bartowski/Llama-3.2-3B-Instruct-GGUF:Llama-3.2-3B-Instruct-Q4_K_M.gguf

# Spawn a llama-server bound to that file
agents inference load llama-cpp llama-3-2-3b --gguf Llama-3.2-3B-Instruct-Q4_K_M.gguf --port 8080

# Wire an agent
agents model use coder --engine llama-cpp --model llama-3-2-3b

# Unload when done (frees RAM)
agents inference unload llama-cpp llama-3-2-3b
```

### vLLM

Not supported inside the toolkit's Multipass VM. vLLM is GPU-only in practice, and Multipass doesn't support GPU passthrough on stable releases (see [GH issue #2](https://github.com/ebner/agents/issues/2) for the libvirt-backend investigation). If you have an externally-hosted vLLM, point Hermes at it via:

```yaml
model:
  default: meta-llama/Llama-3-8B-Instruct
  provider: custom
  base_url: http://your-vllm-host:8000/v1
  api_key: <your-token>
```

This works today with no toolkit changes — the `custom` provider in Hermes bypasses its provider registry.

### Storage and sizing

Models live under `<data-dir>/models/{ollama,llama-cpp}/`. Rough RAM budget per quantized model:

| Model | Disk | RAM | Notes |
|---|---|---|---|
| 1.5B Q4 | ~1 GB | ~2 GB | Fine on RPi5 8 GiB |
| 3B Q4 | ~2 GB | ~3-4 GB | Tight on RPi5 8 GiB |
| 7B Q4_K_M | ~4 GB | ~6 GB | Workstation only |
| 13B Q4_K_M | ~8 GB | ~10 GB | Workstation only |

Disk caveat: download multiple 7B variants and you'll burn 20+ GB fast. Default VM disk is 25 GB total. Run `agents vm resize --disk 80` (or higher) before going model-shopping.

### OpenClaw

The toolkit doesn't write OpenClaw's config. `agents model use` refuses on OpenClaw agents but prints the exact `base_url` to paste into `openclaw configure` manually.

### Two `[ollama]` blocks aren't a typo

- `[ollama]` (and `agents ollama configure/status`) — points the toolkit at an **external** Ollama (separate GPU box, hosted, etc.). Localhost is refused because it would resolve to the VM itself, not where you actually run Ollama.
- `[inference]` (managed by `agents inference up/down`) — **in-VM** Ollama. Lives inside the Multipass guest, served at the Docker DNS name `ollama:11434`.

Both can coexist — e.g. heavy LLM on the external GPU box, lightweight embedding model on the in-VM Ollama.

## Running on arm64 / Raspberry Pi 5

The toolkit works on arm64 Linux hosts (RPi5, Apple Silicon under Multipass, generic arm64 servers), but a few upstream images need awareness.

### What works natively on arm64

- `nousresearch/hermes-agent` — multi-arch manifest. Hermes itself is fully native.
- The Hermes sandbox image `nikolaik/python-nodejs:python3.11-nodejs20` — multi-arch.
- `redis:alpine`, `rabbitmq:3-management`, Firecrawl `playwright-service` — multi-arch.

### What runs under qemu-user emulation on arm64

- `ghcr.io/firecrawl/nuq-postgres:latest` — Firecrawl publishes amd64-only for this one. Postgres runs fine under emulation, just slower at init and on heavy queries. `agents doctor` flags this with an `arm64_emulation=warn` check when firecrawl is installed.

The toolkit auto-detects host arch at `agents firecrawl up` time and picks `ghcr.io/firecrawl/firecrawl:linux-arm64` (the api image ships per-arch tags, not a multi-arch manifest). No flag needed.

If `nuq-postgres` performance bites, override it via `docker-compose.override.yml` with a stock `postgres:16` and run Firecrawl's schema migrations against it. Out of scope for the toolkit to automate; document if you do this so the next maintainer knows why the override exists.

### Recommended VM sizing on RPi5

The toolkit defaults (4 CPU, 8 GiB RAM, 25 GiB disk) assume a workstation host. On RPi5 you'll want to scale down — the VM competes with everything else on the host.

| RPi5 board | Recommended `agents vm init` flags |
|---|---|
| RPi5 8 GiB | `--cpus 3 --memory 5 --disk 25` (leaves 3 GiB for host) |
| RPi5 4 GiB | Don't. The VM defaults eat too much of the board; run Hermes directly on the host instead. |

Resize an existing VM with `agents vm resize --cpus N --memory NG --disk NG` rather than reinstalling.

### `--data-dir-in-vm` is strongly recommended on arm64

The default mount layout puts the agent data dir on the host filesystem, shared into the VM via Multipass's 9p mount. This is fine on workstations with NVMe and fast 9p drivers, but on RPi5:

- microSD cards are slow at random I/O (the dominant pattern for SQLite WAL mode).
- 9p over QEMU on arm64 is measurably slower than on amd64.
- Hermes' `state.db` (sessions/messages), `kanban.db`, and any memory-backend SQLite use WAL mode, which interacts poorly with 9p.

The opt-out keeps agent state on the VM's ext4 filesystem and exposes it to host backups via the `agents export-cron` archive flow:

```bash
agents vm init --data-dir /data/agents --data-dir-in-vm
agents export-cron install --time 02:30,08:30,14:30,20:30
```

duplicity (or whatever you back up with) picks up the 6-hourly archives natively. You lose direct host-side inspection of the data dir but gain measurably better DB performance.

### What about NVMe-on-RPi5?

If your RPi5 has an NVMe HAT and you're booting from NVMe, microSD considerations don't apply — but the 9p mount perf is still slower on arm64. `--data-dir-in-vm` is still recommended; the gain from native ext4 access to SQLite is independent of the underlying physical disk.

### Limitations

- **OpenViking memory sidecar** — `volcengine/openviking` arm64 status not verified by the toolkit; manifest fetch failed during investigation. If you need OpenViking memory on arm64, check that the image actually ships your arch before enabling the backend. `holographic` and `mem0` memory backends are pure-Python and arch-agnostic.
- **OpenClaw image registry** — pull access denied at investigation time (the public registry path may differ from what the toolkit's allowlist expects). Verify OpenClaw publishes arm64 builds before relying on it on RPi5; Hermes-only deployments are unaffected.

## Tearing down

```bash
agents export --all               # bag everything
agents vm destroy                 # prompts to run `export --all` first unless --yes
agents vm destroy --yes           # skip both the export offer and the destroy prompt
agents vm destroy --export-first  # always export before destroying
```

`vm destroy` does not touch `<data-dir>` or `~/.codex`. To reset completely, manually `rm -rf <data-dir>` afterward (irreversible).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `agents vm init` errors with `bootstrap failed: docker_host=fail` | Rootless Docker setup didn't finish (often a transient network issue mid-install) | Re-run `agents vm init` — it's idempotent. |
| `agents create` fails with `operation not supported` on container start | The host data-dir mount is a `classic` (fuse.sshfs) multipass mount; rootless Docker can't bind-mount from sshfs. Pre-v0.1.2 VMs are affected. | Stop the VM, drop the classic mount, let `agents vm start` re-create it as native: `agents vm stop && multipass umount agents:/home/ubuntu/agents && agents vm start`. |
| `agents doctor` reports `user_docker_service=fail` | systemd-user died (lingering disabled, OOM, …) | `agents vm shell` then `loginctl enable-linger ubuntu && systemctl --user start docker.service`. |
| `agents doctor` reports `<agent>.docker_socket=fail` | Hermes container can't reach the rootless socket | Container UID drift. Stop the agent, `cat <data-dir>/.env` to confirm `HERMES_UID/GID` match the VM's `ubuntu` user, recreate. |
| `agents codex skill-enable` fails on `docker build` | Network from VM blocked, base image missing, or `nodejs`/`npm` apt repo blocked | Run `agents doctor`; check `agents vm shell` then `docker info`. The Dockerfile lives at `<data-dir>/.codex-sandbox-*.Dockerfile` if you want to inspect it. |
| `agents create` refuses with "data directory already exists" | Previous run was interrupted, dir is on disk | `agents create coder --type hermes --reuse-data` if you intend to attach, or `rm -rf <data-dir>/coder` then re-create. |
| Mount on host has files owned by root or unknown UID | host UID/GID mapping wrong | `agents vm stop && agents vm start` — the start step verifies and remounts with `-u`/`-g` derived from the current host user. |
