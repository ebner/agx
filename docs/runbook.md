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
