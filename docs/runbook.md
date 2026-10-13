# Runbook

## First-time setup

```bash
# Install the host CLI (PEP 668-aware; works on Homebrew / Debian / Ubuntu 24+).
pipx install -e .                 # macOS: brew install pipx first if missing
# or use a venv: python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# (Optional) Codex CLI on host — only needed for `agx codex login` (the Codex sandbox skill)
npm install -g @openai/codex

# Bring up the VM. Pick any path your duplicity (or other host backup) covers.
agx vm init --data-dir /data/agents

# (Optional) Authenticate the toolkit-scoped Codex account (only for the Codex sandbox skill;
# an agent's openai-codex LLM provider is authenticated in-container with `hermes auth add`)
agx codex login

# Create your first Hermes agent, run the wizard
agx create coder --type hermes --setup
```

## Day-to-day

```bash
agx list                      # what's running
agx doctor                    # health
agx logs coder -f             # follow logs
agx exec coder                # TTY shell in the container
agx exec coder hermes chat    # straight into Hermes
agx export coder              # archive into <data-dir>/backups/
```

## Upgrades

```bash
# Who's behind upstream? Read-only, no lock, safe any time.
agx outdated                        # every agent: current / outdated / unknown
agx outdated coder
agx outdated --include-prerelease   # compare against beta/rc tags too

# Bring one agent to the latest upstream release. Bare `upgrade`
# implicitly resolves the newest tag from GitHub Releases (same
# convention as `apt upgrade` / `brew upgrade`) and no-ops — no
# snapshots, no downtime — when the agent is already there.
agx upgrade coder

# Pin an explicit tag instead (skips the upstream check). Auto-rollback
# on doctor failure.
agx upgrade coder --tag v1.2.4

# Force-refresh at the current pin (no version change): full
# snapshot + recreate + doctor cycle. Picks up env-file edits,
# recovers a wedged container.
agx upgrade coder --recreate

# Upgrade everything; canary first
agx upgrade --all --canary coder

# Restore from the latest pre-upgrade snapshot if something went sideways
agx rollback coder --yes

# Or pick a specific snapshot id (filename, suffix-match, or pre-upgrade-*
# multipass snapshot name to restore only the VM):
agx rollback coder --snapshot coder-20260512T120000Z.tar.zst
agx rollback coder --snapshot pre-upgrade-coder-20260512T120000Z
```

Semantics worth knowing:

- **Bare `agx upgrade <name>` needs network** (GitHub Releases API). It *refuses* — rather than silently no-oping — when the latest tag can't be resolved; pass `--tag <T>` to pin explicitly or `--recreate` to refresh at the current pin. `--latest` is the explicit spelling of the same default behavior.
- **No-op short-circuit**: when the target tag equals the current pin (and `--recreate` isn't set), the command reports "already at X — nothing to do" and exits without snapshots or downtime.
- **Successful upgrades re-baseline `defaults.<type>_image_tag`** in `~/.config/agx/config.toml`. A failed-and-rolled-back upgrade never re-baselines.
- **`agx create` also starts on the newest upstream release** (GitHub's "latest", never a pre-release) and re-baselines the default once the agent is up. Offline, it falls back to the configured default with a warning; `--pinned` uses the default without asking GitHub, `--image-tag <T>` names one. Tags are always pinned concretely in compose — never `:latest`.
- **Release tag ≠ image tag.** Hermes images carry the release tag verbatim (`v0.21.6`); OpenClaw images drop the `v` (release `v2026.10.1` → `openclaw/openclaw:2026.10.1`). The toolkit converts automatically, including tags you pass with `--image-tag` / `--tag`, and ignores non-version release tags such as OpenClaw's `linux-stable` desktop channel.
- **A release without an image is skipped.** Upstream sometimes publishes a GitHub release before (or without) an image under that version tag — Hermes v0.21.7 shipped only as `:stable` / `:latest`. `create` then stays on the configured default with a warning, and `upgrade` refuses before taking any snapshot; pin an existing tag with `--tag` if you need it.
- **Old upgrade artifacts are pruned automatically.** After every successful upgrade, `agx prune` keeps the newest `[upgrades].keep_generations` (default 2) generations of host archives, `pre-upgrade-*` VM snapshots and superseded image tags per agent. Run `agx prune [name] --dry-run` to see what it would delete; `keep_generations = 0` turns the automatic pass off.
- **Rollback never silently discards the present.** It overwrites the agent's data dir with the pre-upgrade snapshot, so everything the agent learned or changed since (skills, memories, sessions, config) would be lost. Before overwriting, the current state is exported to `<data-dir>/backups/pre-rollback/` (never auto-pruned; restore with `agx import`); the rollback aborts if that export fails. Under `--data-dir-in-vm` a VM-snapshot restore rewinds *every* agent's data, so every agent is exported first. The prompt shows the snapshot's age. Restored credentials may be stale — OAuth refresh tokens are single-use, so expect to re-authenticate providers that rotated since the snapshot.
- **Interrupted upgrades resume faithfully.** The journal records the original old→new tags at begin time; re-running `agx upgrade <name>` completes *that* upgrade (skipping done phases) instead of re-resolving latest — a release that appeared upstream mid-crash can't silently retarget the resume.

`agx upgrade` takes two snapshots before touching anything: a host-data `.tar.zst` at `<data-dir>/.upgrades/<name>-<utc>.tar.zst` and a multipass VM snapshot `pre-upgrade-<name>-<utc>`. Both are listed in the upgrade outcome JSON / human output. Legacy `.tar.gz` archives from pre-v0.1.1 versions still work for rollback.

### What survives restarts, recreates and upgrades

Agents modify themselves: they write skills, memories, config, cron jobs, plugins, and install tools. Whether such a change survives depends only on **where** it lands:

| Event | Bind-mounted state (below) | Container writable layer |
|---|---|---|
| `agx restart` / `stop` + `start`, VM or host reboot | kept | kept |
| any recreate — `upgrade`, `reseat`, `fix-perms`, `secret set`, `mount --restart`, `memory`, `codex skill-*`, `vm rebuild` | kept | **lost** |
| `agx rollback` | replaced by the snapshot — current state saved to `backups/pre-rollback/` first | lost |

What is bind-mounted:

- **Hermes:** all of `/opt/data` (`$HERMES_HOME`): `config.yaml`, `SOUL.md`, `skills/`, `memories/`, `plugins/`, `cron/`, `hooks/`, sessions, `auth.json`, `installs/` (dependencies Hermes installs on demand), `home/` (HOME of the agent's tool subprocesses — `git`, `gh`, `npm`, skill CLIs). The gateway always ran with HOME=`/opt/data`; `agx exec` sessions do too for agents created or `reseat`ed since 2026-10 (before that they got the ephemeral `/home/hermes`), so `agx exec <name> -- gh auth login` and friends persist.
- **OpenClaw:** `/home/node/.openclaw` (`openclaw.json`, SQLite state, agents, downloaded plugins, skills), `/home/node/.openclaw/workspace`, `/home/node/.config` (incl. the auth-profile key in `.config/openclaw`), `/home/node/.local` (`~/.local/bin` is on the image PATH; pnpm's global dir). The toolkit hands these dirs to the image's `node` user (uid 1000) at create and reseat — without that, OpenClaw cannot write its own state under rootless Docker.

Lost on recreate, by design: anything installed into the image (`apt`, `pip` into the image venv, edits under `/opt/hermes` — Hermes' `hermes update` refuses in Docker anyway; replace the image with `agx upgrade`), caches outside the mounts (OpenClaw `~/.cache`, Playwright browsers, `/tmp/openclaw` logs). For tools an agent needs permanently, have it use `npx`/`uvx` and remember that in its memory, or bake them into a derived image.

**Agents created before 2026-10:** run `agx reseat <name>` once to pick up the Hermes HOME change and OpenClaw's new mounts. OpenClaw's own config and skills edits are safe as-is; the toolkit's own `config.yaml` writes (`secret`, `model use`, `memory`, `firecrawl`) refuse instead of overwriting when the agent changed the file in the meantime — re-run the command.

`agx doctor` also reports each agent's version: the `<agent>.version` check shows the running tag and warns when the compose pin and the live container's image diverge (e.g. a hand-edited compose file without a restart) — the fix is `agx restart <name>`.

## Recovery from a crashed operation

The CLI writes a phase journal under `<data-dir>/.journal/<op>-<name>-<utc>.json` for every multi-phase op. If a command crashes mid-way, just re-run it — the journal causes already-completed phases to be skipped.

| op | phases (in order) |
|---|---|
| create | `data_dir_created` → `compose_updated` → `service_up` → `setup_done` (optional) |
| remove | `mounts_torn_down` → `container_stopped` → `compose_updated` → `data_dir_deleted` |
| rename | `dir_renamed` → `compose_updated` → `container_recreated` |
| upgrade | `host_data_snapshot` → `vm_snapshot` → `tag_pinned` → `pulled` → `recreated` → `config_migrated` → `doctor_passed` |

Upgrade journals additionally record the old and new tags at begin
time, so a resumed upgrade completes the original target instead of
re-resolving "latest" (see the Upgrades section above). The
`config_migrated` phase runs `hermes config migrate` inside the
container (silent no-op for OpenClaw).

`remove`'s `data_dir_deleted` phase deletes `<name>/` **as root inside the VM** (on bare-metal: via `rootlesskit`, falling back to a printed + confirmed `sudo rm -rf`), because the agent's state is owned by a rootless-Docker subuid the host user can't traverse. It refuses — leaving the journal resumable — if the data share isn't mounted in the VM (`agx vm start` fixes that) or if anything is still mounted beneath the agent's directory. While a `remove` journal is pending, `agx create <same-name>` is refused: finish the removal first (`agx remove <name>`, with `--keep-data` if the data should survive). `backups`, `firecrawl` and `models` are reserved and can't be used as agent names.

`agx doctor` warns about any journal older than 24 hours. To clear a stuck journal once you've manually reconciled state, simply delete the file from `<data-dir>/.journal/`.

## Reseating an agent after a toolkit upgrade

Compose service blocks are written once at `agx create` time and not
touched afterwards. If a toolkit version adds a new env var to the
service fragment (e.g. `HERMES_UID`), pre-existing agents keep their
stale block until you reseat them.

```bash
agx reseat coder
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
   (same chown/chmod dance as `agx fix-perms`).

When to run it: after upgrading the toolkit, if `agx doctor` flags
something or if `agx exec <name> …` starts misbehaving. Safe to
run any time — preserves all stateful agent data.

## Hermes ignores WhatsApp group messages after an upgrade

Symptom: the agent answers WhatsApp DMs normally and can still *send*
into group chats (cron notifications etc.), but never reacts to
anything written in a group. Nothing appears in any log at INFO —
`gateway.log` simply has no `inbound message ... @g.us` lines anymore.

Cause: Hermes v2026.7.1 shipped "fail-closed external-surface
defaults": every platform gained `dm_policy` / `group_policy` knobs
defaulting to `"pairing"`. DMs have a pairing handshake under that
default, and allowlisted senders keep working. Groups do **not** —
`_is_group_allowed` in `gateway/platforms/whatsapp_common.py` is a
hard `return False` under `pairing` (no group pairing flow exists).
A deployment with env-only WhatsApp config (no explicit
`group_policy`) silently loses all group ingress at the first upgrade
past v2026.7.1 — and because group traffic can be quiet for weeks,
the loss may go unnoticed for a long time.

Diagnosis, in dependency order (each step rules out a layer):

1. `grep 'g.us' <data>/logs/gateway.log` inside the container — if
   inbound group lines stop at some date, the gateway never sees them.
2. Bridge-level receipt: check mtimes of
   `/opt/data/whatsapp/session/sender-key-<groupjid>--*.json`. Fresh
   mtimes at the moment someone messaged the group prove Baileys
   receives AND decrypts group traffic — the drop is above the bridge.
3. Live policy: read the running bridge's env
   (`tr '\0' '\n' < /proc/<bridge-pid>/environ | grep WHATSAPP`).
   No `WHATSAPP_GROUP_POLICY` → the adapter is on the `pairing`
   default → groups are dropped in `_is_group_allowed`, silently.

Remediation: add BOTH lines to the agent's Hermes env file
(`/opt/data/.env` in-container, next to the other `WHATSAPP_*` keys),
then restart the gateway:

```
WHATSAPP_GROUP_POLICY=open
WHATSAPP_ALLOW_ALL_USERS=true
```

The second line is a **boot interlock opt-in**, not an access grant:
since the same hardening wave, the gateway refuses to start at all
("open policy without allow-all opt-in") when any policy is `open`
without it — setting only the first line takes the whole gateway down
(all platforms, crash-loop under s6). The flag is read only by that
startup guard and by the open-DM opt-in (irrelevant while `dm_policy`
stays `pairing`); the bridge never reads it. Real access control is
unchanged: the bridge's `WHATSAPP_ALLOWED_USERS` phone allowlist
(LID-aware via the session dir's `lid-mapping-*.json`) still gates
every sender, in groups and DMs alike.

Why not the tighter `group_policy: allowlist` with specific group
JIDs: the runtime reads `group_allow_from` only from the gateway
platform-config block (`config.extra`), which env-only deployments
don't have. The `WHATSAPP_GROUP_ALLOWED_USERS` env var is written by
the setup wizard but never read back by the runtime (upstream bug as
of v2026.7.x). If you need per-group restriction, that config block
has to exist first.

General lesson: upstream security waves can flip *defaults* under an
env-only config, and this class of change produces no error — the
feature just stops. After every `agx upgrade`, exercise each
connected platform's inbound path (a test message per platform,
groups included), not just "the agent responds somewhere".

## /opt/data permission drift (and how `agx fix-perms` resolves it)

Symptom: `agx exec coder hermes <anything>` crashes on import with
`PermissionError: '/opt/data/.env'` (or similar files under
`/opt/data`).

Cause: The Hermes image entrypoint *should* `usermod -u $HERMES_UID
hermes` on container start so the in-container `hermes` user matches
the toolkit's runtime uid (1000). When it doesn't (entrypoint skipped
on a stale running container, image-level changes, env-var
interpolation failure), `/opt/data` ends up owned by uid 10000 with
mode 700. `agx exec` runs as uid 1000 → falls to "other" perms on
a 700 dir → EACCES on every traversal.

Detection: `agx doctor` runs a per-Hermes-agent probe that stats
`/opt/data` as the runtime uid and flags failures or owner mismatches
with the precise remediation command.

Remediation: `agx fix-perms <agent>`.

```bash
agx fix-perms coder
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
6. `chmod 755 /opt/data` so the toolkit's `agx exec` at uid 1000
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

The known target:

- `/opt/hermes/scripts/` — where `hermes setup` installs per-bridge
  helpers (e.g. the WhatsApp bridge's `npm install` lands at
  `whatsapp-bridge/node_modules/`). Image-baked root-owned by
  default. Without a chown to uid 1000, `hermes setup` (or a
  re-run of one of its install branches) hits `EACCES: permission
  denied, mkdir '/opt/hermes/scripts/whatsapp-bridge/node_modules'`.
The toolkit chowns it after every container recreate (in
`fixperms_cmd.chown_hermes_install_paths`, called by `agx create`,
`agx fix-perms` and `agx reseat`) and again on every container boot
via the cont-init hook. The runtime user's HOME is `/opt/data`, which
is persisted, so caches and dotfiles survive recreates.

If you ever see an `EACCES` on a path inside `/opt/hermes/` that
*isn't* covered, add it to `fixperms_cmd.HERMES_WRITABLE_PATHS`
rather than chowning manually.

## Remote access with Tailscale

Dashboards and agent ports bind to `127.0.0.1` inside the VM. To reach them from your other devices without SSH tunnels, put the VM on your tailnet (VM backends only — on bare metal, install Tailscale on the host yourself):

```bash
agx tailscale up                        # prints a login URL; or --auth-key <key> for a non-interactive join
agx tailscale status                    # tailnet IPs + ports published in docker-compose.override.yml
agx tailscale down                      # leave the tailnet (tailscaled stays installed)
agx tailscale upgrade                   # upgrade the tailscale package in the VM
```

Tailscale runs as a system service in the VM, not as a container. Ports the toolkit publishes are bound to `127.0.0.1` and stay private; to expose one on the tailnet, publish it without that prefix in `docker-compose.override.yml` (for example `9119:9119` for the Hermes dashboard).

## Observability

### VM health — `agx doctor`

The doctor surface is binary (ok / warn / fail) — drop it in cron and
alert on warn+ exit codes. Currently checks:

- `vm_reachable`, `mount_round_trip`, `user_docker_service`,
  `port_conflicts`, `data_dir_exists`, `codex_auth`, journals,
  `override_mirror` (`--data-dir-in-vm`: the host override file has its in-VM mirror),
  per-agent container + docker-socket + hermes-doctor + `credential_pool`
  (every pooled credential of the primary provider `exhausted`/`dead` in
  Hermes' auth store — the "Provider authentication failed" class; detail
  carries the in-container re-auth one-liner) +
  data-dir-access + version (compose-pin vs running-image drift) +
  host_view (a registered `agx access mount` view that isn't mounted, or is stale).
- `firecrawl_running` — warns when firecrawl is defined in compose and
  agents are still wired to it but the containers aren't running (the
  state a VM rebuild/migration leaves behind). A stack that's stopped
  *and* unwired reads as a deliberate `agx firecrawl down` and
  stays quiet.
- `inference_ollama` / `inference_llamacpp` health probes and the
  `arm64_emulation` check fire when the respective stacks are
  installed.
- `disk_space` — warn < 15% free, fail < 5%.
- `vm_memory` — warn < 1 GiB available, fail < 256 MiB. Reads
  `MemAvailable` from `/proc/meminfo` (kernel's reclaim-aware
  accounting, not raw `free -used`).
- `vm_load` — warn at load1/cpus > 1.0, fail > 2.0.

Exit codes: 0 (all ok), 3 (any fail).

### Firecrawl observability — `agx firecrawl status`

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
agx firecrawl up                # first-time install + start
agx firecrawl up --upgrade      # re-resolve floating tags into fresh digests
agx firecrawl down              # stop; postgres data preserved
agx firecrawl status            # running? which agents are wired?
agx firecrawl logs              # tail api logs
agx firecrawl logs playwright -f
agx firecrawl destroy --yes     # nuke everything including postgres data
```

What `up` does:

1. On first run (or with `--upgrade`): pulls each upstream floating
   tag (linux-amd64, latest, alpine, 3-management) and captures the
   resolved digest via `docker image inspect`. The compose file only
   ever references images by `@sha256:…` so the toolkit's "no
   `:latest` outside canary" rule still holds. Subsequent `up`
   invocations reuse the digests already in
   `<data-dir>/.firecrawl-state.json` — no re-resolution. If the state
   file is missing but the compose blocks survive (post-VM-migration),
   the pins are reconstructed from the compose digests instead of
   re-resolved.
2. Populates `<data-dir>/.env` with `FIRECRAWL_API_KEY` (a generated
   `fc-self-hosted-...` placeholder — firecrawl-py insists on a
   non-empty key even when the server has auth disabled),
   `FIRECRAWL_API_URL=http://firecrawl-api:3002`, `BULL_AUTH_KEY`,
   `POSTGRES_PASSWORD`.
3. Generates the 5-service stack (`firecrawl-api`,
   `firecrawl-playwright`, `firecrawl-redis`, `firecrawl-rabbitmq`,
   `firecrawl-postgres`) and writes it into the project compose file
   via the atomic flock-protected path.
4. Persists the digest pins to `<data-dir>/.firecrawl-state.json`
   (with a `degraded` marker) *before* starting anything, so a failed
   start can't make the retry re-resolve floating tags — that would be
   an unintended upgrade.
5. `docker compose up -d` the five services (compose `depends_on`
   healthchecks gate the api on rabbitmq + postgres being ready),
   then probes `firecrawl-api` until it answers (bounded wait — the
   api itself carries no healthcheck and `up -d` wouldn't wait on one
   anyway).
6. Only once the api is observably serving: clears the `degraded`
   marker and adds `FIRECRAWL_API_KEY` / `FIRECRAWL_API_URL` to every
   existing Hermes agent's `terminal.env_passthrough` so the LLM's
   shell-based diagnostics also see the config.

If the stack never comes up healthy, `up` exits 2 with the digests
still pinned; re-running `agx firecrawl up` retries with the same
pins and no agents are wired to a dead stack in the meantime.

Agents created **after** `firecrawl up`: `agx create` checks
firecrawl state during the `service_up` phase and adds the
passthrough entries automatically. Zero extra steps.

Storage: `<data-dir>/firecrawl/postgres/` is the only durable volume
(captured by duplicity / `agx export`). Redis and RabbitMQ are
ephemeral — losing in-flight scrape jobs on restart is acceptable
for a single-user setup.

Resource caps in the generated compose: api 2 CPU / 3 GB, playwright
2 CPU / 2 GB (with `BLOCK_MEDIA=true`, `MAX_CONCURRENT_PAGES=2`),
others 512 MB each. Idle target ~1.5–2 GB total.

Networking: only `firecrawl-api` is reachable, and only over the
docker network as `http://firecrawl-api:3002`. No host port is
published; debug via `agx firecrawl logs` or `agx vm shell`.

## Editing an agent's files (`agx edit`)

An agent's state (`<data-dir>/<name>/.data/`) is owned by a rootless-Docker subuid under mode 700, so you can't just open `SOUL.md` from the host. `agx edit` goes through the running container instead — no sudo, no host-side access, and it works the same under `--data-dir-in-vm`:

```bash
agx edit coder SOUL.md
agx edit coder skills/my-skill/SKILL.md
agx edit coder config.yaml                 # validated like every toolkit config write
agx edit coder SOUL.md --profile work      # path relative to the profile's home
```

- Hermes only; the container must be running (`agx start <name>`). For a stopped agent or OpenClaw, use `agx exec` / `agx export`.
- `PATH` is relative to `/opt/data` (or the `--profile` home). It is resolved once inside the container and must stay inside that home; regular UTF-8 text files up to 1 MiB only — it will refuse `state.db` and friends.
- Anything that *resolves* to a `config.yaml` (default or profile, including through a symlink) is written by the same validated writer as the rest of the toolkit: a `0.0.0.0` dashboard bind is refused and `hermes config validate` runs. A YAML error re-opens the editor.
- The edit buffer lives in a private directory under `$XDG_RUNTIME_DIR` (tmpfs), never in the VM-shared data dir. If the write is refused — the file changed inside the agent meanwhile, validation failed, the editor crashed after saving — the buffer is **kept** and its path printed.
- The conflict check is optimistic: the file is re-read under the toolkit lock right before writing. That protects against other `agx` commands, not against Hermes rewriting the same file in that instant.
- `/opt/data/.env` is Hermes' own env file. Toolkit-managed per-agent secrets live in `<data-dir>/<name>/.env` — use `agx secret` for those.
- Needs an interactive terminal; uses `$VISUAL`, then `$EDITOR`.

## Reading agent files from the host (`agx access`)

The agent's runtime container writes its state into the bind-mounted data dir as a rootless-Docker subuid (typically host uid `100999`, mapped from container uid `1000`) under mode 700. `ls ~/agents/coder/.data` fails with permission denied, and that can't be fixed at the source: the container's uid map is one-to-one and your host uid is already taken by *container root*.

`agx access mount` gives you a second path to the same directory — an **idmapped bind mount** under `~/agents-view/` in which the owner ids are swapped, so through the view you simply *are* the owner. Mode bits, the entrypoint's `chmod`s and ACL masks stop mattering. Nothing in the data dir changes.

```bash
agx access mount coder          # read-only view at ~/agents-view/coder
agx access mount coder --rw     # read-write (files you create are owned by the agent)
agx access list                  # registered views + mounted / not-mounted / stale
agx access unmount coder
```

For a quick edit you don't need a view at all — see [`agx edit`](#editing-an-agents-files-agx-edit).

### What it does

- One `sudo mount --bind -o ro,nosuid,nodev,noexec,nosymfollow,X-mount.idmap=…` — printed in full and confirmed before it runs (`--yes` skips the confirmation). The uid/gid in the mapping are read from the directory itself, not guessed from `/etc/subuid`.
- Hermes: `<agent>/.data` → `~/agents-view/<agent>`. OpenClaw: `<agent>/.openclaw` → `~/agents-view/<agent>/openclaw`; `workspace` is usually already yours and is skipped.
- Read-only unless `--rw`. To change mode, `unmount` first.
- `unmount` is `sudo umount` plus a plain `rmdir` of the empty mountpoint. **Never `rm -rf` a view path yourself** — while mounted it *is* the agent's data.

### Surviving reboots

Mounts don't persist. `access mount` prints a ready-made `/etc/fstab` line (also in `agx --json access list` as `fstab_line`); add it yourself — the toolkit never edits `/etc`. It carries `nofail`, so a missing agent directory can't block boot. Until you add it, `agx doctor` warns after a reboot:

```
WARN  coder.host_view  host view /home/user/agents-view/coder is not-mounted — add its fstab line … or re-run `agx access mount coder`.
```

Doctor is silent if you never registered a view. Drop the fstab line again when you `unmount`.

### Caveats

- **The view is a second path to the same data, secrets included.** Exclude `~/agents-view` from host backups (`duplicity --exclude`) — otherwise it's archived twice — and remember that anything allowed to read your home can read it while mounted.
- **Don't open `*.db` files through the view while the agent runs.** The host and the VM share no locks or shared memory; a host-side SQLite client on a live WAL database is a corruption risk. Use `agx conversations export` or stop the agent first.
- `nosymfollow` is deliberate: the view carries agent-written content into your host namespace, and a planted `notes.md -> ~/.bashrc` must not resolve there. Symlinks inside the view show up but can't be followed.
- `agx remove`, `agx rename` and `agx vm migrate-data` refuse while a view is registered for the agent — `agx access unmount <agent>` first. A view whose directory was deleted underneath it shows as `stale`.
- View location: `[paths].host_view_dir` in `config.toml` (default `~/agents-view`). It must not be inside the data dir (it would be shared into the VM) nor contain it.

### Backend differences

| Backend | `agx access mount` |
|---|---|
| `multipass` / `libvirt`, host-mounted data dir | works |
| `bare_metal` | works — rootless Docker runs on the host, so agent files are subuid-owned there too |
| `--data-dir-in-vm` | refused — nothing on the host to view; use `agx edit`, `agx exec` or `agx export` |

Requirements: Linux ≥ 5.12 with a filesystem that supports idmapped mounts (ext4, xfs, btrfs, …) and util-linux ≥ 2.39. On hosts without it (macOS, old kernels) the `mount` error is shown as-is; `agx edit` works everywhere.

## Sharing host folders with an agent (`agx mount`)

Give an agent access to a specific folder on the host. The toolkit handles the two hops the folder needs to traverse before the agent container sees it: host → VM mount → docker bind volume. On `bare_metal` there is only one hop: the Docker daemon runs on the host and binds the folder directly.

```bash
# Default: ro, target both runtime + sandbox on Hermes
agx mount add coder --host ~/projects/x --container /workspace/x

# Read-write (will prompt; --yes to skip the warning)
agx mount add coder --host ~/data --container /workspace/data --rw

# Sandbox-only (visible to the LLM's shell tools, NOT the runtime container)
agx mount add coder --host ~/scripts --container /workspace/scripts --target sandbox

# OpenClaw — runtime only (sandbox concept is Hermes-specific)
agx mount add bob --host ~/projects --container /workspace/projects

# List, then remove
agx mount list coder
agx mount remove coder --container /workspace/x

# Apply immediately rather than reminding you to restart
agx mount add coder --host ~/projects/x --container /workspace/x --restart
```

### Defaults

- **Read-only** unless `--rw`. The AGENTS.md threat model is explicit: a prompt-injected agent can be persuaded into destructive ops on any writable mount.
- **`--target`** defaults per agent type: **Hermes → `both`** (runtime container + Hermes sandbox via `terminal.docker_volumes`), **OpenClaw → `runtime`** (sandbox is Hermes-only).
- **Per-agent isolation.** On the VM backends each agent gets its own VM-side mount target at `/home/ubuntu/host-extras/<agent>/<hash>-<basename>` — no cross-agent sharing. Two agents wanting the same host folder both run `agx mount add`. On `bare_metal` the bind source is the host folder itself.

### What lives where

| File | Owner | Purpose |
|---|---|---|
| `<data-dir>/<agent>/.mounts.json` | toolkit | Source of truth for that agent's extra mounts. |
| `<data-dir>/docker-compose.override.yml` | toolkit + user | Toolkit appends `volumes:` entries here; the main `docker-compose.yml` is left alone so `agx reseat` / `agx upgrade` don't clobber the additions. |
| `<data-dir>/<agent>/.data/config.yaml` (`terminal.docker_volumes`) | toolkit | Hermes sandbox mount list. |

### Backend differences

| Backend | Add | Remove |
|---|---|---|
| multipass | Stops the VM, runs `multipass mount --type native`, restarts (~30s downtime). The toolkit prompts; `--yes` skips. | Live `multipass umount` (no stop needed). |
| libvirt | Hot-attaches a virtiofs `<filesystem>` block via `virsh attach-device --live --config` + `mount -t virtiofs` + `_netdev` fstab entry inside the guest. No downtime. | Hot-detach via `virsh detach-device --live --config` + in-guest umount + fstab cleanup. |
| bare_metal | No VM step: the compose/sandbox volume entry binds the host folder directly (`<host-folder>:<container-path>`). ACLs are applied exactly as on the VM backends — the agent is a subuid on the host too. | No VM step. Also strips the `/home/ubuntu/host-extras/…` source that toolkit versions before this fix wrote on bare-metal (those mounts never worked). |

### Reserved container paths

The toolkit refuses container paths that would shadow its own bind mounts or the agent's install: `/`, `/opt`, `/opt/data`, `/opt/hermes`, `/home/hermes`, `/home/node`, `/home/node/.openclaw`, `/run`, `/proc`, `/sys`, `/dev`, `/etc`, `/usr`, `/var` — and anything beneath them (other than `/` itself). Pick something under `/workspace/`.

### Restart required

Both add and remove leave the running container with stale state until the next restart. The output reminds you:
```
agent restart required: agx restart coder
```
…or pass `--restart` to apply immediately.

### Export / import semantics

`agx export <agent>` includes the agent's `.mounts.json` in the tarball as metadata. `agx import <agent>` deliberately does NOT re-apply those mounts (host paths likely differ on the destination machine) — instead, it surfaces them as `agx mount add ...` commands you can copy-paste and edit.

### Safety knobs

- `--rw` prompts unless `--yes`/global `--yes`. Pick narrow read-only mounts wherever possible.
- The `target sandbox`/`both` choice on Hermes exposes the folder to the LLM's `terminal` and `execute_code` skills. If you only want it visible to your own scripts that you exec into the runtime, use `--target runtime`.

### Auto-ACL: how the user and the agent both see the folder

The agent (uid 1000 inside the container, which maps to host subuid 100999) and you (uid 1000 on the host) are *different uids* under rootless Docker. Without intervention, files would land owned by 100999 with the agent's umask — you wouldn't be able to read what the agent wrote, and the agent wouldn't be able to read your mode-600 files. `agx mount add` solves this automatically:

- **Read-only mount** (default): grants the agent's subuid `rX` on existing files + sets a default ACL so future files you add inherit the grant.
- **`--rw` mount**: grants the agent's subuid `rwX` for both directions, AND adds a default ACL granting your uid `rwx` so files the agent writes back are readable by you.

This applies on every backend, `bare_metal` included — rootless Docker maps the container uid to a subuid there as well. No sudo needed (you own the host folder). Probed first — if the filesystem doesn't support ACLs (FAT/exFAT/network mount), the mount still works but you'll see a warning about likely permission errors on mode-restrictive files.

`agx mount remove` strips the toolkit-added ACL entries; any unrelated entries you added by hand are left in place.

## Per-agent secrets

API keys for skills (Firecrawl, GitHub, etc.) go in a per-agent .env
file that the toolkit wires into the container via compose
`env_file:`. **Never** put them in `<data-dir>/.env` — that's a
project-level file shared across every container.

```bash
agx secret set coder FIRECRAWL_API_KEY            # hidden prompt
agx secret set coder GITHUB_TOKEN ghp_xxxx        # arg form
agx secret list coder                             # names only
agx secret unset coder FIRECRAWL_API_KEY
agx secret clear coder                            # nuke them all
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
agx memory list                          # show what's available
agx memory enable holographic coder     # zero-dep, works with Codex alone
agx memory enable mem0 coder            # in-process, needs Ollama or OPENAI_API_KEY
agx memory enable openviking coder      # sibling docker service, needs Ollama
agx memory disable coder                # back to built-in
agx memory status coder                 # what's wired
```

Backends that need an embeddings + LLM provider read it from the
toolkit's `[ollama]` config. The toolkit does NOT run Ollama for you —
point it at an existing one:

```bash
agx ollama configure --url http://10.20.30.1:11434
agx ollama configure --url https://hosted.example/v1 --api-key sk_…
agx ollama status                        # probe reachability from VM + containers
agx ollama clear
```

`localhost` / `127.0.0.1` / `0.0.0.0` URLs are refused — they resolve
to the VM itself, not to the host where Ollama actually runs. Use the
multipass NAT gateway (`multipass info <vm>` shows it; typically
`10.<x>.<y>.1`), a LAN/Tailscale IP, or a public hostname.

**Doctor checks worth knowing:**

- `holographic` + default host-mount: the SQLite WAL on 9p risk
  (matches the Hermes kanban/sessions trap). Switch to
  `agx vm init --data-dir-in-vm` if you're seeing `database is
  locked` errors.
- `holographic` requires NumPy inside the Hermes container — without
  it, the plugin silently degrades to FTS5-only (upstream issue
  #17350). The status command probes this.
- `mem0` and `openviking` need a reachable Ollama URL — `agx ollama
  status` is the fastest way to confirm.

## Codex skill (Hermes only)

```bash
agx codex skill-enable coder    # build sandbox image with Codex baked in
agx codex skill-disable coder   # restore original sandbox image
```

The first call slugifies the agent's current `terminal.docker_image` and builds `agents/hermes-sandbox-codex:<sha256[:12]>`. The state file at `<data-dir>/.codex-skill/<name>.json` remembers the original image so `skill-disable` can restore it exactly. The skill reads the toolkit-scoped `<data-dir>/.codex` (`agx codex login`); this is the ONLY thing that file is for.

## Codex as a Hermes agent's LLM provider — auth, expiry, recovery

Authenticate the `openai-codex` provider **inside the container**; the toolkit-scoped `.codex` is not involved:

```bash
agx exec coder -- hermes auth add openai-codex --type oauth --no-browser   # prints URL + code
agx exec coder -- hermes auth list openai-codex                            # shows pooled credentials
agx exec coder -- hermes auth remove openai-codex 1                        # drop an exhausted one
```

Symptom of a dead credential: the agent answers *"Provider authentication failed. Check the configured credentials; raw provider details are in the gateway logs."*, `agx logs coder` shows `HTTP 401 … token_expired` / `token_invalidated`, and `agx doctor` reports `coder.credential_pool: fail`. ChatGPT-subscription OAuth chains die every 1–3 weeks (upstream Hermes #65346, unresolved) — plan for it rather than around it:

- **Keep answering:** `fallback_providers` in the agent's `config.yaml` (a cheap OpenRouter model; Hermes fails over immediately on 401/403).
- **Hear about it within 15 min:** `agx doctor-cron install --mail you@example.org` (below).
- **Re-auth from anywhere:** the device-code URL works from a phone browser; with `agx tailscale up` + an SSH app you can run the `hermes auth add` line from the phone too.
- **Never** use `agx codex login` "for the agent": that login only feeds the Codex sandbox skill. The agent's provider credential is a separate login owned by Hermes (see [codex-integration](codex-integration.md)).

## doctor-cron

Host cron that runs `agx doctor` every N minutes and mails **on state change only** — one mail when a check turns red (with every failing check's detail and the fix one-liner), one when everything is green again; warnings are listed for context but never trigger a mail. Delivery is through the host's `sendmail` (postfix, msmtp, …): the toolkit carries no SMTP config, and `install` refuses when no `sendmail` is on PATH.

```bash
agx doctor-cron install --mail you@example.org [--every 15]
agx doctor-cron status      # installed?, last run, currently failing checks, last mail
agx doctor-cron run         # one poll by hand (what cron calls)
agx doctor-cron uninstall
```

State lives at `<data-dir>/.doctor-cron/state.json`; the cron log at `<data-dir>/.doctor-cron.log`. A red doctor never makes the cron line exit non-zero (that would trigger cron's own MAILTO spam) — only a failed run itself (config missing, sendmail rejected) does.

OpenClaw bundles its own Codex plugin — run `openclaw models auth login --provider openai --device-code` *inside* the OpenClaw container instead:

```bash
agx exec personal -- openclaw models auth login --provider openai --device-code
```

## Backups (host-side)

Whatever host backup you run over `<data-dir>` (duplicity, restic, borg, …) covers everything: per-agent state, compose, .env, Codex auth, journals, upgrade tarballs. There is no internal backup engine.

For crash-consistent snapshots, run `agx export <name>` first — that writes a clean tarball into `<data-dir>/backups/` that the host backup will pick up on its next run.

```bash
agx export coder              # one agent
agx export --all              # all agents
agx export --all --to ~/elsewhere/  # send archives somewhere other than <data-dir>/backups/

# Schedule one or more daily runs via host cron. Comma-separated times
# install one crontab line per time — re-running install replaces all
# prior toolkit-managed entries (clean idempotent).
agx export-cron install --time 02:30                       # once a day at 02:30
agx export-cron install --time 02:30,08:30,14:30,20:30     # four times a day
agx export-cron status
agx export-cron uninstall
```

### Picking a schedule for SQLite-heavy use

Hermes' kanban/sessions/insights databases use SQLite WAL. In `--data-dir-in-vm` mode they live on the VM's ext4 disk and work normally (WAL doesn't function over the host shared-FS share — see [architecture.md](architecture.md) for the why). The trade-off is that those files are not directly host-visible — your only host-side view of recent agent state is the most recent `agx export` archive.

A reasonable cadence:

| Use case | Schedule | Rationale |
|---|---|---|
| Single home/family bot, casual use | `02:30` (default) | One archive a day, ≤24 h of potential loss after a VM crash. |
| Active multi-bot, daily-driver host | `02:30,14:30` | ≤12 h. Two archives a day is enough headroom for most accidents. |
| Heavy SQLite write (kanban, sessions, insights) | `02:30,08:30,14:30,20:30` | ≤6 h. Each archive is a few MB; disk cost is trivial. |
| You really care | `*/2` (every two hours, hand-installed) | The toolkit's HH:MM cron syntax doesn't express step intervals; use `crontab -e` and add `0 */2 * * * /usr/local/bin/agents --yes export --all` with the `# agents toolkit — managed export-cron entry` marker omitted (the toolkit won't manage it, but it won't trip over it either). |

`agx export` runs `tar --zstd` over the agent's per-agent data dir **inside the VM** in both modes, then transfers the archive out to `<data-dir>/backups/`. For Hermes, this captures `state.db`, `kanban.db`, sessions, skills, SOUL.md, config.yaml. Under `--data-dir-in-vm` that's where the data lives anyway (ext4); in host-mount mode the tar runs VM-side as root because the agent's files are owned by a rootless-Docker subuid under mode 700 — a host-side walk can't read them and would silently produce archives containing only directory stubs. tar isn't aware of SQLite WAL mid-flight, so each individual run is *near-consistent* rather than guaranteed crash-consistent — if a write is in progress at the exact moment tar copies the file, the captured `.db` may be missing the corresponding WAL entries. SQLite's recovery on next open handles most such cases, and frequent backups give you many fallback points.

For strict crash consistency on a specific agent, stop it first: `agx stop coder && agx export coder && agx start coder` — but that's rarely necessary in practice given how cheap the multi-archive approach is.

### Backup retention

**`agx export` auto-prunes.** After every successful export to the
default `<data-dir>/backups/` directory, the toolkit deletes that
agent's oldest archives, keeping only the newest `keep_last`
(default **14**). Only files matching the toolkit's own naming pattern
(`<agent>-<utc>.tar.zst`, matched exactly) are pruning
candidates — user files, other agents' archives (including agents whose
name starts with the same prefix), and the
`.export-cron.log` are left alone. Archives written to a custom
`--to <path>` destination are **never** pruned; custom destinations
are user-managed.

Tune the window in `~/.config/agx/config.toml`:

```toml
[export_cron]
keep_last = 14    # ~3.5 days at the 4x/day schedule, 14 days at nightly
```

Set `keep_last = 0` to disable pruning entirely — unbounded history
accumulates and cleanup becomes your job.

**Duplicity interaction:** because the local window rolls, archives
pruned locally will also age out of the *remote* per duplicity's own
retention policy (`remove-older-than` and friends). The local
`keep_last` window is in-flight protection between off-host backup
runs; duplicity (or whatever host backup you run) is the long-history
layer. Verify your duplicity retention settings independently — do
not assume the toolkit keeps history for you.

## Rebuilding the VM (disk shrink, Ubuntu bump, wedged-VM recovery)

`agx vm resize` handles grow operations in place. The cases it can't:

- **Disk shrink** — multipass refuses; the only way to a smaller disk is destroy + re-init.
- **Bump the Ubuntu release** (e.g. 24.04 → 26.04 once Docker CE supports it).
- **The VM is wedged** — bootstrap stuck, kernel borked, network plumbing wrong — and you want a clean slate without losing agent state.

`agx vm rebuild` is the journaled, non-destructive flow for all three:

```bash
agx vm rebuild --disk 30                         # shrink (or grow) disk; keep other sizing
agx vm rebuild --ubuntu-release 26.04            # try a newer release
agx vm rebuild --cpus 4 --memory 4               # downsize the whole VM
agx vm rebuild                                   # same sizing as today (effectively a clean-room reboot)
```

Phases (each idempotent, journaled at `<data-dir>/.journal/rebuild-<vm>-<utc>.json`):

| phase | what it does |
|---|---|
| `staged_exports` | `agx export` every agent into `<data-dir>/.rebuild-staging/` |
| `vm_destroyed` | `multipass delete --purge` the old VM |
| `vm_initted` | fresh `agx vm init` with the (overridden or kept) sizing |
| `state_restored` | `agx import` each agent from its staging archive |
| `services_started` | `docker compose up -d` each service |
| `sidecars_started` | re-up the project-scoped sidecar stacks (firecrawl, ollama, llama-cpp model services) — their containers lived only in the old VM's engine. Best-effort: a sidecar that fails to come back warns with the manual command and never fails the rebuild |

If something crashes mid-rebuild, re-running `agx vm rebuild` picks up where the journal left off. The staging dir is left in place after success so you can inspect; safe to `rm -rf <data-dir>/.rebuild-staging/` once `agx doctor` is green.

## Local inference (Ollama, llama.cpp)

The toolkit can run local LLMs inside the VM as sibling Docker services. Hermes agents (and OpenClaw, with manual configuration) reach them over the project Docker network — no external API calls, no per-token billing.

### Phase 1: Ollama (simplest path)

```bash
# Start the Ollama service (auto-resolves and pins the image digest)
agx inference up ollama

# Pull a model — accepts both Ollama Hub refs and HuggingFace
agx model pull ollama llama3.2:3b
agx model pull ollama hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M

# Wire it into an agent (writes the model: block in config.yaml)
agx model use coder --engine ollama --model llama3.2:3b

# Or route compression + vision through it too (free, no cloud cost)
agx model use coder --engine ollama --model llama3.2:3b --aux all

# Restart so the gateway picks up the new config
agx restart coder
```

Status + introspection:

```bash
agx inference status            # what's running, models loaded
agx model list                  # disk inventory (both engines)
agx doctor coder               # includes inference_ollama check
```

### Phase 2: llama.cpp (HF GGUFs, per-model serving)

llama.cpp is one-model-per-process by design, so each loaded model becomes its own compose service:

```bash
# Pin the llama.cpp image (no service started yet)
agx inference up llama-cpp

# GGUF pulls use the running ollama container as the download host
# (it has Python for huggingface_hub) — start it first if it isn't up
agx inference up ollama

# Download a GGUF from HuggingFace into <data-dir>/models/llama-cpp/
agx model pull llama-cpp bartowski/Llama-3.2-3B-Instruct-GGUF:Llama-3.2-3B-Instruct-Q4_K_M.gguf

# Spawn a llama-server bound to that file
agx inference load llama-cpp llama-3-2-3b --gguf Llama-3.2-3B-Instruct-Q4_K_M.gguf --port 8080

# Wire an agent
agx model use coder --engine llama-cpp --model llama-3-2-3b

# Unload when done (frees RAM)
agx inference unload llama-cpp llama-3-2-3b
```

### vLLM

Not supported inside the toolkit's Multipass VM. vLLM is GPU-only in practice, and Multipass doesn't support GPU passthrough on stable releases (see [GH issue #2](https://github.com/ebner/agx/issues/2) for the libvirt-backend investigation). If you have an externally-hosted vLLM, point Hermes at it via:

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

Disk caveat: download multiple 7B variants and you'll burn 20+ GB fast. Default VM disk is 25 GB total. Run `agx vm resize --disk 80` (or higher) before going model-shopping.

### OpenClaw

The toolkit doesn't write OpenClaw's config. `agx model use` refuses on OpenClaw agents but prints the exact `base_url` to paste into `openclaw configure` manually.

### Two `[ollama]` blocks aren't a typo

- `[ollama]` (and `agx ollama configure/status`) — points the toolkit at an **external** Ollama (separate GPU box, hosted, etc.). Localhost is refused because it would resolve to the VM itself, not where you actually run Ollama.
- `[inference]` (managed by `agx inference up/down`) — **in-VM** Ollama. Lives inside the Multipass guest, served at the Docker DNS name `ollama:11434`.

Both can coexist — e.g. heavy LLM on the external GPU box, lightweight embedding model on the in-VM Ollama.

## Running on arm64 / Raspberry Pi 5

The toolkit works on arm64 Linux hosts (RPi5, Apple Silicon under Multipass, generic arm64 servers), but a few upstream images need awareness.

### What works natively on arm64

- `nousresearch/hermes-agent` — multi-arch manifest. Hermes itself is fully native.
- The Hermes sandbox image `nikolaik/python-nodejs:python3.11-nodejs20` — multi-arch.
- `redis:alpine`, `rabbitmq:3-management`, Firecrawl `playwright-service` — multi-arch.

### What runs under qemu-user emulation on arm64

- `ghcr.io/firecrawl/nuq-postgres:latest` — Firecrawl publishes amd64-only for this one. Postgres runs fine under emulation, just slower at init and on heavy queries. `agx doctor` flags this with an `arm64_emulation=warn` check when firecrawl is installed.

The toolkit auto-detects host arch at `agx firecrawl up` time and picks `ghcr.io/firecrawl/firecrawl:linux-arm64` (the api image ships per-arch tags, not a multi-arch manifest). No flag needed.

If `nuq-postgres` performance bites, override it via `docker-compose.override.yml` with a stock `postgres:16` and run Firecrawl's schema migrations against it. Out of scope for the toolkit to automate; document if you do this so the next maintainer knows why the override exists.

### Recommended VM sizing on RPi5

The toolkit defaults (4 CPU, 8 GiB RAM, 25 GiB disk) assume a workstation host. On RPi5 you'll want to scale down — the VM competes with everything else on the host.

| RPi5 board | Recommended `agx vm init` flags |
|---|---|
| RPi5 8 GiB | `--cpus 3 --memory 5 --disk 25` (leaves 3 GiB for host) |
| RPi5 4 GiB | Don't. The VM defaults eat too much of the board; run Hermes directly on the host instead. |

Resize an existing VM with `agx vm resize --cpus N --memory NG --disk NG` rather than reinstalling.

### `--data-dir-in-vm` is strongly recommended on arm64

The default mount layout puts the agent data dir on the host filesystem, shared into the VM via Multipass's 9p mount. This is fine on workstations with NVMe and fast 9p drivers, but on RPi5:

- microSD cards are slow at random I/O (the dominant pattern for SQLite WAL mode).
- 9p over QEMU on arm64 is measurably slower than on amd64.
- Hermes' `state.db` (sessions/messages), `kanban.db`, and any memory-backend SQLite use WAL mode, which interacts poorly with 9p.

The opt-out keeps agent state on the VM's ext4 filesystem and exposes it to host backups via the `agx export-cron` archive flow:

```bash
agx vm init --data-dir /data/agents --data-dir-in-vm
agx export-cron install --time 02:30,08:30,14:30,20:30
```

duplicity (or whatever you back up with) picks up the 6-hourly archives natively. You lose direct host-side inspection of the data dir but gain measurably better DB performance.

### What about NVMe-on-RPi5?

If your RPi5 has an NVMe HAT and you're booting from NVMe, microSD considerations don't apply — but the 9p mount perf is still slower on arm64. `--data-dir-in-vm` is still recommended; the gain from native ext4 access to SQLite is independent of the underlying physical disk.

### Limitations

- **OpenViking memory sidecar** — `volcengine/openviking` arm64 status not verified by the toolkit; manifest fetch failed during investigation. If you need OpenViking memory on arm64, check that the image actually ships your arch before enabling the backend. `holographic` and `mem0` memory backends are pure-Python and arch-agnostic.
- **OpenClaw image registry** — pull access denied at investigation time (the public registry path may differ from what the toolkit's allowlist expects). Verify OpenClaw publishes arm64 builds before relying on it on RPi5; Hermes-only deployments are unaffected.

## Using the libvirt backend (experimental — Linux + GPU passthrough)

The default `multipass` backend works everywhere and is the right choice for 95% of deployments. The `libvirt` backend exists for one reason: **GPU passthrough**, which Multipass doesn't support and which is required for vLLM (and helpful for accelerated Ollama/llama.cpp inference on workstations with discrete GPUs). See [GH #2](https://github.com/ebner/agx/issues/2) for the design rationale.

⚠️  **Experimental status**: lifecycle + mount + snapshot + restore validated on a real Linux + libvirt host (Ubuntu 24.04, kernel 6.17, AMD Strix Halo, 2026-05-16). GPU attach validated only via XML dry-run — actual hostdev attach has not been exercised yet because the test machine has no discrete GPU separable from the host display. Expect the GPU path may surface minor quirks on first use.

### Prerequisites

- **Linux host** (Ubuntu 22.04+ or equivalent). macOS and Windows hosts are explicitly refused — use Multipass there.
- **libvirt + KVM stack**:
  ```bash
  sudo apt install libvirt-clients libvirt-daemon-system virtinst \
                   genisoimage cloud-image-utils openssh-client \
                   qemu-utils qemu-system-x86 ovmf virtiofsd
  sudo usermod -aG libvirt,kvm $USER     # log out + back in for groups to take effect
  ```
- **For GPU passthrough**: IOMMU enabled in BIOS AND on the kernel cmdline (`amd_iommu=on iommu=pt` or `intel_iommu=on iommu=pt`), vfio-pci kernel module, and the target GPU bound to vfio-pci at boot. The Arch wiki's [PCI passthrough via OVMF](https://wiki.archlinux.org/title/PCI_passthrough_via_OVMF) is the canonical setup guide; the toolkit doesn't automate the kernel-cmdline / modprobe configuration because that's a host-level change with reboot consequences.
- **For arm64 (RPi5 etc.)**: KVM works on arm64 Linux; the cloud image arch is auto-detected from `platform.machine()`. No GPU passthrough on RPi5 (no discrete GPU).

### Two host-level gotchas worth fixing once

These don't block the toolkit from working but tripped up the first real-hardware run and will trip up other users on similar systems.

**(1) Snap-Multipass hijacks `/dev/kvm` group**

If both snap-Multipass and libvirt are installed on the same host, snap's udev rule (`/etc/udev/rules.d/70-snap.multipass.rules`) tags `/dev/kvm` with `snap_multipass_multipassd`, which triggers `snap-device-helper` to reassign the device's group to snapd's sandboxed namespace (often `systemd-resolve`). libvirt-qemu (in group `kvm`) then can't open the device and every libvirt VM start fails with `Could not access KVM kernel module: Permission denied`.

Symptom in the wild:

```
$ ls -la /dev/kvm
crw-rw----+ 1 root systemd-resolve 10, 232 May 16 10:29 /dev/kvm
                     ^^^^^^^^^^^^^^^ should be 'kvm'
```

Persistent fix — drop `/etc/udev/rules.d/99-fix-kvm-group.rules` that runs *after* snap's rule (99- sorts after 70-) and re-asserts the right group:

```bash
sudo tee /etc/udev/rules.d/99-fix-kvm-group.rules >/dev/null <<'EOF'
KERNEL=="kvm", RUN+="/bin/sh -c 'chgrp kvm /dev/kvm && chmod 0660 /dev/kvm'"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/kvm
```

Alternative: `sudo snap remove --purge multipass` if you've committed to libvirt as your only backend.

**(2) AMD systems often ship with `amd_iommu=off`**

Several AMD platforms (notably Strix Halo / Ryzen AI MAX, but also various Ryzen laptops) default-disable IOMMU because early-firmware BIOS revisions had stability issues with it. Check:

```bash
cat /proc/cmdline | tr ' ' '\n' | grep -i iommu
```

If you see `amd_iommu=off`, GPU passthrough is impossible regardless of vfio setup. To enable:

1. **Update BIOS first** — AMD AI-platform IOMMU stability fixes landed in mid-2025 firmware. Boards still on launch firmware may genuinely have IOMMU bugs.
2. Edit `/etc/default/grub` to change `amd_iommu=off` → `amd_iommu=on iommu=pt` in `GRUB_CMDLINE_LINUX_DEFAULT`.
3. `sudo update-grub && sudo reboot`.
4. Verify with `dmesg | grep -i AMD-Vi` (should show initialization, no errors).

`iommu=pt` (passthrough mode) is critical: with it, host-attached devices skip IOMMU translation entirely so bare-metal performance (LLM inference, NVMe I/O) is unaffected. Without it, you pay 1-5% on I/O-heavy workloads for nothing.

Memory capacity for LLMs is **not** affected by IOMMU — only your BIOS GPU-carveout setting (UMA Frame Buffer Size / Variable Graphics Memory) limits how much of system RAM the iGPU can use.

### Provisioning

```bash
# CPU-only (no GPU passthrough)
agx vm init --backend libvirt --data-dir /data/agents

# With autodetect (picks every vfio-pci-bound GPU it finds via lspci)
agx vm init --backend libvirt --gpu auto --data-dir /data/agents

# With explicit PCI ID(s); repeat --gpu to pass multiple devices
agx vm init --backend libvirt --gpu 0000:01:00.0 --gpu 0000:01:00.1 \
    --data-dir /data/agents
```

`vm init` with `--backend libvirt`:

1. Downloads the Ubuntu cloud image (~600 MB) into `<data-dir>/.images/<release>-<arch>.img` on first run; cached for subsequent rebuilds.
2. Generates an ed25519 SSH keypair at `<data-dir>/.ssh/id_ed25519`. The pubkey is injected via cloud-init user-data so the toolkit can `ssh ubuntu@<vm-ip>` for every subsequent exec.
3. Builds a NoCloud seed ISO via `cloud-localds` and feeds it to `virt-install --import` alongside a copy-on-write qcow2 backed by the cached image.
4. After the VM is reachable via SSH, attaches any configured GPUs (stops VM → `virsh attach-device <hostdev-xml>` per GPU → starts VM → re-waits).
5. Runs the same `vm-bootstrap.sh` script as the multipass path. The script knows nothing about the hypervisor.

### Verifying GPU passthrough

```bash
agx vm shell                              # ssh into the VM
nvidia-smi                                   # NVIDIA: should list the GPU
lspci | grep -E "VGA|3D"                     # AMD/Intel: card visible
```

If the card isn't there: the device isn't bound to vfio-pci. `lspci -k -d <vendor>:` on the **host** shows the driver — if it's `nvidia` / `amdgpu` / `i915`, you need to unbind the host driver and bind vfio-pci first (kernel cmdline `vfio-pci.ids=<vid>:<did>` is the usual approach).

### What's the same vs different vs Multipass

| Behavior | Multipass | libvirt |
|---|---|---|
| Cross-platform | Linux/macOS | Linux only |
| GPU passthrough | Not supported | Auto-detect NVIDIA/AMD/Intel |
| VM provisioning | `multipass launch` (one shot) | virt-install + cloud-init seed ISO + ssh wait |
| Exec into VM | `multipass exec` | `ssh ubuntu@<ip>` (key auth) |
| File transfer | `multipass transfer` (SFTP) | `scp` |
| Host-mount | `multipass mount --type native` | virtiofs via `<filesystem>` domain XML (needs `memoryBacking source.type='memfd' access.mode='shared'`, which the toolkit auto-emits) |
| Snapshots | `multipass snapshot` (running VM ok) | `virsh snapshot-create-as` — internal qcow2 snapshot; **requires VM stopped if a virtiofs mount is active** (vhost-user backend incompatibility). Toolkit auto-stops, prompts for confirmation, restarts afterwards |
| `vm resize` | `multipass set local.<vm>.<knob>` | `virsh setvcpus` / `setmaxmem` / `qemu-img resize` |
| Image catalog | `multipass find` | Cloud images at `cloud-images.ubuntu.com` (no catalog) |

### Snapshot semantics on libvirt + virtiofs

A virtiofs mount on a libvirt domain blocks `snapshot-create-as` on a running VM (vhost-user backend lacks `VHOST_USER_PROTOCOL_F_LOG_SHMFD`). The toolkit handles this transparently:

```
$ agx vm snapshot pre-upgrade
Snapshotting a libvirt VM with an active virtiofs mount requires
briefly stopping the VM (~30s downtime). Any running Docker
containers (Hermes agents, Firecrawl, Ollama, etc.) will be
cleanly stopped by libvirt's ACPI shutdown and restarted
afterwards.
Proceed? [y/N]:
```

Confirm → toolkit stops the VM via ACPI, snapshots, restarts, re-waits for SSH. Mount auto-remounts via the fstab `_netdev` entry. Same logic applies to `agx vm restore`. `-y` / global `--yes` skips the prompt (used by `agx upgrade`'s pre-upgrade snapshot path).

Why this over the alternatives:

- *Detach virtiofs, snapshot, reattach*: would let containers keep running, but the underlying `umount /home/ubuntu/agents` is either refused (`device is busy` due to Docker bind mounts) or, with `umount -l`, leaves running containers with stale fd handles to deleted mount points — high risk of SQLite-WAL corruption for Hermes' state.db / kanban.db.
- *External (`--disk-only`) snapshots*: work with virtiofs but **aren't revertible** via `snapshot-revert`. The earlier toolkit version used them and silently broke restore.

The ~30s downtime is the right trade-off: same lifecycle as a normal `agx vm stop`, no container-state risk.

### Migration between backends

Not automated. If you want to switch backends on an existing deployment:

1. `agx export --all` to capture every agent's data dir into `<data-dir>/backups/`.
2. `agx vm destroy --yes`.
3. `agx vm init --backend <new-backend> --data-dir <same-path>`.
4. `agx import <name> <archive>` for each archived agent.

The host data dir is backend-agnostic; only the VM provisioning changes.

**Sidecar stacks don't cross the migration.** Firecrawl (and in-VM
inference) containers live only in the *old* VM's Docker engine; the
compose blocks, project `.env`, postgres data, and per-agent wiring all
survive in the data dir, but the containers themselves don't — wired
agents' scrape calls fail connection-refused until you re-run
`agx firecrawl up` (and `agx inference up …`) in the new VM.
`agx doctor`'s `firecrawl_running` check flags exactly this state.
`up` reuses the digests pinned in `.firecrawl-state.json`; if the
state file was lost in the move, `up` reconstructs the pins from the
`@sha256:…` digests still in the compose `firecrawl-*` service blocks
rather than re-resolving upstream floating tags — a plain `up` never
turns into an unintended upgrade. (`agx vm rebuild` automates all
of this via its `sidecars_started` phase.)

### Known limitations

- **GPU attach not yet end-to-end validated.** XML emission verified via dry-run (matches libvirt's hostdev schema). Actual `virsh attach-device` + guest-driver visibility hasn't been exercised — needs a discrete GPU separable from the host display (typical workstation: integrated graphics for host, discrete card on vfio-pci).
- **9p mount fallback is documented, not coded.** If `virtiofsd` is unavailable, edit the domain XML manually via `virsh edit <vm>` to use 9p instead of virtiofs.
- **Internal snapshots accumulate inside the qcow2.** No automated pruning. Periodic `virsh snapshot-delete <vm> <name>` keeps the disk file lean on long-running hosts.
- **`agx vm list` schema isn't as rich as `multipass list`** — both return `{"list": [{"name": ...}]}` but multipass also surfaces IPs, mounts, release info that the libvirt path doesn't (yet) emit.
- **Doctor checks don't probe libvirt-daemon health.** Check `systemctl status libvirtd` manually if a VM seems wedged.

## Tearing down

```bash
agx export --all               # bag everything
agx vm destroy                 # prompts to run `export --all` first unless --yes
agx vm destroy --yes           # skip both the export offer and the destroy prompt
agx vm destroy --export-first  # always export before destroying
```

`vm destroy` does not touch `<data-dir>` or `~/.codex`. To reset completely, manually `rm -rf <data-dir>` afterward (irreversible).

## Using the bare_metal backend (no VM, Linux-only)

Bare-metal runs agent containers directly on the host with rootless Docker — no Multipass, no libvirt, no VM at all. It's the right answer when:

- You're on a **single-user dev workstation** where the VM trust boundary is overkill — the same human runs the editor, browser, and shell anyway.
- You want **native GPU access** for local LLMs (Strix Halo / Ryzen AI MAX, dedicated AMD/NVIDIA cards) without the libvirt vfio-pci passthrough dance.
- You want **fast init**: no cloud-image download, no cloud-init bring-up, no virtio overhead.

### What you trade away

- **The VM trust boundary.** A compromised agent that escapes a container is on your host, period. AGENTS.md's defense-in-depth rationale leans on the VM to absorb skill-marketplace malware; bare-metal forfeits that. Only use bare-metal where the host is *already* trusted.
- **Full-state rollback during upgrades.** `agx upgrade` still snapshots the host data-dir via tarball (so agent state can be reverted), but there's no VM snapshot of the rootless-Docker engine or OS state.
- **Snapshots in general.** `agx vm snapshot` / `restore` refuse on bare-metal — use `agx export` for data archives, and host-level tools (duplicity, restic, btrfs/zfs) for full-host snapshots.

### Prerequisites

The toolkit's bare-metal bootstrap is **verify-only**: it probes for rootless Docker and refuses if anything's missing rather than running `sudo apt` on your behalf. Install these yourself first:

```bash
# 1. Apt packages
sudo apt install uidmap dbus-user-session docker-compose-plugin

# 2. Enable lingering so systemd --user services survive logout
sudo loginctl enable-linger "$USER"

# 3. Install rootless Docker
curl -fsSL https://get.docker.com/rootless | sh

# 4. Container-log rotation. Rootless Docker never rotates container logs by
#    default; the VM bootstrap writes this file, bare-metal leaves it to you.
#    Do it BEFORE creating agents: log-opts bind at container create time.
mkdir -p ~/.config/docker
[ -e ~/.config/docker/daemon.json ] || cat > ~/.config/docker/daemon.json <<'JSON'
{"log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "3"}}
JSON

# 5. Start it + enable autostart (restart instead if it was already running)
systemctl --user enable --now docker.service

# 6. Sanity check
docker version
```

If you already have a `daemon.json`, merge the `log-driver` / `log-opts` keys into it by hand. `agx vm init` warns when rotation is missing, and `agx doctor`'s `container_logs` check keeps warning — it also names containers created before rotation was set up (recreate them with `agx reseat <name>`). Without it, one chatty container fills your **host** root fs (the 2026-08 outage class, see AGENTS.md).

If `/var/run/docker.sock` already exists (rootful Docker), the toolkit will refuse to run on top of it — uninstall rootful first or migrate via `dockerd-rootless-setuptool.sh install`. The toolkit's security model assumes the rootless user namespace; running on rootful would put compromised containers' processes as effective root on the host.

### Init

```bash
# Bare-metal defaults the data-dir to ~/.local/share/agents
agx vm init --backend bare_metal

# Or pick your own path
agx vm init --backend bare_metal --data-dir /data/agents

# init re-runs are safe — the verify loop is idempotent
```

The init step does not provision a VM. It:

1. Probes every rootless-Docker prerequisite, surfaces the exact missing-package commands if any fail
2. Captures `[vm_runtime]` (socket path, host uid/gid) into `~/.config/agx/config.toml`
3. Pins Hermes / OpenClaw image tags from GitHub releases
4. Seeds the data-dir with `docker-compose.yml` + `.env`

Output ends with a `bare_metal runtime ready (no VM)` line plus the standard trust-boundary warning.

### Lifecycle differences

| Command | Bare-metal behavior |
|---|---|
| `agx vm start` / `vm stop` | No-op (informational message). The host is always 'on'; stop individual agents via `agx stop <name>`. |
| `agx vm shell` | Spawns a subshell with `CWD=<data-dir>` and `DOCKER_HOST=unix://<rootless socket>` already exported — `docker compose ...` and `agx exec ...` work immediately. |
| `agx vm status` | Reports the host kernel and a synthesized "Running" state (consistent with VM-mode output for scripting). |
| `agx vm snapshot` / `restore` | Refuse. Use `agx export` archives + a host snapshot tool. |
| `agx vm resize` / `rebuild` | Refuse — host sizing is real hardware. |
| `agx vm destroy` | Stops and removes all toolkit-managed containers (`docker compose down --remove-orphans`). Host data-dir is preserved; `rm -rf <data-dir>` manually for a complete reset. |
| `agx create/start/stop/restart/logs/exec/setup/doctor/upgrade/rollback` | Work transparently — every host↔runtime call goes through the same backend dispatcher. Every `docker` call is pinned to the rootless socket recorded at init (`DOCKER_HOST` is forced; your shell's `DOCKER_HOST` / docker context is ignored), so cron jobs and shells pointing at another daemon can't redirect the toolkit. |
| `agx upgrade` | Rollback point is the data-dir tarball only (no VM snapshot). |
| `agx mount add/remove` | Binds the host folder directly; ACLs as on the VM backends. |
| `agx access mount` | Works (agent files are subuid-owned on the host too). |
| `agx tailscale ...` | Refuses — the toolkit never mutates the host OS. Install Tailscale on the host yourself. |
| `agx vm migrate-data` | Refuses — the data dir is already on the host fs. |

### Host reboot recovery

`restart: unless-stopped` on every agent container, plus the rootless `docker.service` running as a `systemctl --user` unit (with `enable-linger` set in the prereqs above), brings the whole stack back without manual intervention.

If `docker.service` isn't coming up on its own, check:

```bash
loginctl show-user "$USER" --property=Linger     # should print Linger=yes
systemctl --user status docker.service            # should be active
```

### Local inference on bare-metal

`agx inference up ollama` works the same on bare-metal — Ollama runs as a sibling Docker service. The win is GPU access: Ollama in a bare-metal container can use `--device /dev/kfd --device /dev/dri/renderD128` (ROCm/AMD) or `--gpus all` (NVIDIA Container Toolkit) to talk to the host GPU directly, while the host's X session keeps sharing the same GPU. No vfio-pci, no libvirt vGPU. The toolkit's `inference up` doesn't yet auto-wire those flags — add them via `docker-compose.override.yml` for now (tracked as a Phase 2 follow-up to issue #5).

### Running a VM deployment and bare-metal side by side

Not as one user. The toolkit reads exactly one config (`~/.config/agx/config.toml`, one `vm.backend`), and the `export-cron` / `doctor-cron` entries are per-user singletons in that user's crontab — a second install would replace the first.

Two Linux users on the same host can each run one deployment, because everything the toolkit owns is per-user: config, data dir, crontab, host view dir, and the rootless Docker daemon (each user has its own socket under `/run/user/<uid>/` and its own subuid range). Things to watch, none of which the toolkit coordinates across users:

- **Loopback ports.** Bare-metal agents publish dashboard/API ports on the host's `127.0.0.1`; the port allocator only knows its own compose file. Keep tunnels/forwards from the VM deployment off those ports.
- **Disk and RAM.** The VM's disk image, the bare-metal Docker image cache, and both sets of containers share the host. `agx doctor` in each deployment only sees its own share.
- **multipass VM name.** Only relevant if both users run a VM backend: `vm.name` (default `agx`) must differ.

This layout is not covered by the test suite.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `agx vm init` errors with `bootstrap failed: docker_host=fail` | Rootless Docker setup didn't finish (often a transient network issue mid-install) | Re-run `agx vm init` — it's idempotent. |
| `agx create` fails with `operation not supported` on container start | The host data-dir mount is a `classic` (fuse.sshfs) multipass mount; rootless Docker can't bind-mount from sshfs. Pre-v0.1.2 VMs are affected. | Stop the VM, drop the classic mount, let `agx vm start` re-create it as native: `agx vm stop && multipass umount agents:/home/ubuntu/agents && agx vm start`. |
| `agx doctor` reports `user_docker_service=fail` | systemd-user died (lingering disabled, OOM, …) | `agx vm shell` then `loginctl enable-linger ubuntu && systemctl --user start docker.service`. |
| `agx doctor` reports `<agent>.docker_socket=fail` | Hermes container can't reach the rootless socket | Container UID drift. Stop the agent, `cat <data-dir>/.env` to confirm `HERMES_UID/GID` match the VM's `ubuntu` user, recreate. |
| `agx codex skill-enable` fails on `docker build` | Network from VM blocked, base image missing, or `nodejs`/`npm` apt repo blocked | Run `agx doctor`; check `agx vm shell` then `docker info`. The Dockerfile lives at `<data-dir>/.codex-sandbox-*.Dockerfile` if you want to inspect it. |
| `agx create` refuses with "data directory already exists" | Previous run was interrupted, dir is on disk | `agx create coder --type hermes --reuse-data` if you intend to attach, or `rm -rf <data-dir>/coder` then re-create. |
| Mount on host has files owned by root or unknown UID | host UID/GID mapping wrong | `agx vm stop && agx vm start` — the start step verifies and remounts with `-u`/`-g` derived from the current host user. |
