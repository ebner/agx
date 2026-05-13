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

agents export-cron install --time 02:30   # daily `export --all` at 02:30 host time
agents export-cron status
agents export-cron uninstall
```

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
