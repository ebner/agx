# `agx` — multi-agent VM toolkit

[![CI](https://github.com/ebner/agx/actions/workflows/ci.yml/badge.svg)](https://github.com/ebner/agx/actions/workflows/ci.yml)

`agx` runs a few long-lived AI agents — [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research and [OpenClaw](https://github.com/openclaw/openclaw) — on hardware you own. Each agent gets its own rootless Docker container, all inside one VM, with pinned images, upgrades that can roll back, health checks with mail alerts, and scheduled exports of each agent's state to a directory your regular backup covers.

The host-side CLI drives one of three isolation backends:

- **`libvirt`** (default on Linux) — a KVM/qemu VM driven via `virt-install`/`virsh` + ssh/scp; the data dir is shared over virtiofs, and GPUs can be passed through.
- **`multipass`** (default on macOS) — a single Multipass VM; the host shells out only to `multipass`.
- **`bare_metal`** (Linux-only, opt-in) — no VM at all; containers run on the host's rootless Docker. Forfeits the VM trust boundary — single-user dev workstations only.

Defense in depth (VM backends): outer isolation = VM, inner isolation = one rootless container per agent. `agx vm init` picks libvirt on Linux and multipass on macOS unless you pass `--backend …`; see [docs/threat-model.md](docs/threat-model.md) for what each trades away and [docs/runbook.md](docs/runbook.md) for the libvirt / bare-metal setup guides.

## Requirements

- A Linux or macOS host with Python 3.11+.
- **Linux** (default backend `libvirt`): the KVM/libvirt stack, and your user in the `libvirt` group (log in again afterwards). `agx vm init` checks all of this and names whatever is missing.
  ```bash
  sudo apt install libvirt-clients libvirt-daemon-system virtinst genisoimage \
                   cloud-image-utils openssh-client qemu-utils qemu-system-x86 ovmf virtiofsd
  sudo usermod -aG libvirt $USER
  ```
- **macOS** (default backend `multipass`): [Multipass](https://canonical.com/multipass) (`brew install --cask multipass`).
- The VM defaults to 4 vCPUs, 8 GB RAM and a 25 GB disk (`agx vm init --cpus/--memory/--disk`). For `bare_metal`, see the setup guide linked under [Choosing a backend](#choosing-a-backend).

## Quickstart

```bash
# 1. Install the host-side CLI. pipx puts `agx` on your PATH in an isolated
#    environment (macOS: `brew install pipx` first if missing).
pipx install git+https://github.com/ebner/agx.git
# For development: git clone https://github.com/ebner/agx.git && cd agx && pipx install -e .

# 2. (Optional) Install Codex CLI on the host if you want a toolkit-scoped Codex account
npm install -g @openai/codex

# 3. Bring up the VM. Pick a host path for all agent state — duplicity or whatever
#    host backup you run will back this up directly.
#
#    Without --backend, Linux gets libvirt and macOS gets multipass.
#
#    For SQLite-heavy use (Hermes' kanban/sessions/insights stores use SQLite
#    WAL, which doesn't work reliably across the VM's shared folder — virtiofs
#    or 9p), pass --data-dir-in-vm so per-agent state lives on the VM's ext4
#    disk where WAL is native. The host then sees agent state only via
#    `agx export` archives (next step below shows how to schedule those for
#    multi-times-a-day backups).
agx vm init --data-dir /data/agents                      # files live on the host
agx vm init --data-dir /data/agents --data-dir-in-vm     # SQLite WAL works; files visible only via archives
agx vm init --backend bare_metal --data-dir /data/agents # Linux only: no VM — see "Choosing a backend"

# 4. (Optional) Authenticate a toolkit-scoped Codex account for the Codex *sandbox skill*, separate from your personal ~/.codex/
#    (a Hermes agent's openai-codex LLM provider is authenticated inside the container instead — see "Codex paths")
agx codex login

# 5. Create your first Hermes agent and run the setup wizard. `create` starts on
#    the newest upstream release (offline: the configured default; --pinned /
#    --image-tag to choose) and pins that exact tag.
agx create coder --type hermes --setup

# 6. Create an OpenClaw agent
agx create personal --type openclaw --setup

# 7. Schedule host-side backups. One crontab entry per time; replaces
#    any prior toolkit-managed entries on re-install.
agx export-cron install --time 02:30                     # once a day
agx export-cron install --time 02:30,14:30               # twice a day
agx export-cron install --time 02:30,08:30,14:30,20:30   # 6-hourly
agx export-cron status

# 8. Day-to-day
agx list
agx doctor
agx logs coder -f
agx exec coder -- hermes chat
```

For the full lifecycle — Hermes profiles vs. personalities vs. skills, connecting to Slack / WhatsApp / Mattermost / Matrix, and the OpenClaw flow — see [docs/tutorial.md](docs/tutorial.md). The [runbook](docs/runbook.md) is the cheat-sheet.

## Choosing a backend

All three backends run the same compose project through the same commands — `create`, `start`/`stop`, `logs`, `exec`, `setup`, `doctor`, `upgrade`/`rollback`, `export`/`import`, `reseat`, `secret`, `firecrawl`, `inference`, `model`, `memory`, `profile`, `edit` behave identically. What differs:

| | `multipass` | `libvirt` | `bare_metal` |
|---|---|---|---|
| Host OS | macOS (default), Linux | Linux (default) | Linux |
| Outer trust boundary | VM | VM | **none — the host** |
| Bootstrap | installs rootless Docker in the VM | same | **verify-only**: you install the prerequisites, the toolkit checks them |
| Container-log rotation | written by the bootstrap | same | **you** write `~/.config/docker/daemon.json` (`vm init` and `doctor` warn until you do) |
| GPU | none | vfio-pci passthrough | native (device flags via `docker-compose.override.yml`) |
| `--data-dir-in-vm` | yes | yes | n/a — state is always on the host fs |
| `vm stop`/`resize`/`rebuild` | yes | yes | no-op / refused |
| `vm snapshot`/`restore` | yes | yes (stop-cycle while the data dir is host-mounted) | refused — `agx export` + a host snapshot tool |
| Upgrade rollback point | VM snapshot + data tarball | same | data tarball only |
| `vm migrate-data` | yes | yes | refused |
| `tailscale` | yes (in the VM) | yes | refused — install Tailscale on the host yourself |
| `mount add` | VM share (multipass: ~30 s VM restart) + bind | virtiofs hot-attach + bind | direct bind of the host folder |
| `access mount` | yes (not with `--data-dir-in-vm`) | same | yes |

Pick `bare_metal` only where the host is already trusted (single-user workstation, dedicated LLM box) — see [docs/threat-model.md](docs/threat-model.md). Setup guides: [libvirt](docs/runbook.md#using-the-libvirt-backend-linux-default), [bare-metal](docs/runbook.md#using-the-bare_metal-backend-no-vm-linux-only).

**One backend per host user.** `~/.config/agx/config.toml` selects exactly one backend, and the cron entries (`export-cron`, `doctor-cron`) are per-user singletons, so one user cannot run a VM deployment and a bare-metal deployment side by side. Two separate Linux users (each with its own config, data dir, crontab and rootless Docker) can — see [the runbook](docs/runbook.md#running-a-vm-deployment-and-bare-metal-side-by-side).

## How agx compares

agx is for running a few **long-lived personal agents around the clock on hardware you own**, with their state on your own disk. Other ways to run Hermes or OpenClaw solve different problems:

| | Isolation | Where agent state lives | Built for |
|---|---|---|---|
| **agx** | One VM (or none, on bare metal) shared by all agents + one rootless container per agent | Host directory you back up yourself | 24/7 messaging agents; pinned versions, upgrade with rollback, health alerts, scheduled exports |
| **Upstream Docker images** ([Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/docker), [OpenClaw](https://docs.openclaw.ai/install/docker)) | One container on your regular Docker daemon | Host bind mount | The quickest start; upgrades, backups and monitoring are up to you |
| **Agent-side tool sandboxing** (Hermes [terminal backends](https://hermes-agent.nousresearch.com/docs/user-guide/configuration), OpenClaw [sandboxing](https://docs.openclaw.ai/gateway/sandboxing)) | Only the commands the agent's tools run; the agent process itself, with its credentials and memory, is not isolated | Unchanged | Complementary — usable inside an agx agent too |
| **[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)** (`sbx`, with community [Hermes](https://hub.docker.com/r/sbx/hermes-agent-kit) and [OpenClaw](https://hub.docker.com/r/sbx/openclaw) kits) | One microVM per agent, outbound traffic limited to an allow-list, API keys injected by a host proxy | Inside each sandbox (removed with it) | Interactive coding-agent sessions on a developer machine |
| **[NanoClaw](https://nanoclaw.dev/)** | Agents in containers (Docker / Apple Container) | Per-group mounted directories | A different assistant (Claude Agent SDK) designed around container isolation |
| **Hosted templates** (e.g. [Railway](https://railway.com/deploy/hermes-agent-official)) | The provider's | The provider's servers | Not running hardware yourself |

Where agx is weaker today: all agents share one VM (sbx gives each its own), agents can reach any host on the internet (sbx enforces an allow-list), and API keys live in the agent's container environment (sbx and NanoClaw keep them in a host-side proxy or vault).

## Command overview

| Command | What it does | Docs |
|---|---|---|
| `agx vm init/start/stop/status/shell/resize/rebuild/snapshot/snapshots/restore/migrate-data/destroy` | Runtime lifecycle for the selected backend (`--backend multipass\|libvirt\|bare_metal`) | [runbook](docs/runbook.md) |
| `agx create/list/remove/rename` | Agent CRUD (`remove --export-first` bags a final archive) | [tutorial](docs/tutorial.md) |
| `agx start/stop/restart/logs/exec/setup` | Day-2 container ops | [runbook](docs/runbook.md) |
| `agx doctor [name]` | Layered health checks: VM, mount, docker, per-agent probes, version pin-drift | [runbook](docs/runbook.md) |
| `agx outdated [name]` | Which agents are behind upstream (GitHub Releases) | [runbook](docs/runbook.md) |
| `agx upgrade [name]` / `rollback` / `pull` | Bare `upgrade` implicitly targets the latest upstream tag; `--tag` pins, `--recreate` force-refreshes; two-snapshot rollback safety | [runbook](docs/runbook.md) |
| `agx prune [name]` | Delete old upgrade artifacts (archives, VM snapshots, superseded images); also runs after every successful upgrade | [runbook](docs/runbook.md#upgrades) |
| `agx reseat <name>` | Rewrite an agent's compose block from the current template after toolkit upgrades | [runbook](docs/runbook.md) |
| `agx fix-perms <name>` | Remediate the recurring `/opt/data` permission-drift bug (Hermes) | [runbook](docs/runbook.md) |
| `agx export/import` / `export-cron` | Archives into `<data-dir>/backups/` + host-cron scheduling; auto-prunes to `keep_last` | [runbook](docs/runbook.md) |
| `agx secret set/unset/list/clear` | Per-agent API keys via compose `env_file:` | [runbook](docs/runbook.md) |
| `agx mount add/remove/list` | Share specific host folders with an agent (override-file volumes + ACLs) | [runbook](docs/runbook.md) |
| `agx edit <name> <path>` | Edit one of a Hermes agent's text files (`SOUL.md`, skills, `config.yaml`) in `$EDITOR` via the container — no sudo, no host access to the data dir | [runbook](docs/runbook.md) |
| `agx access mount/unmount/list` | Host view of an agent's subuid-owned state dir (idmapped bind mount under `~/agents-view/`; read-only by default) | [runbook](docs/runbook.md) |
| `agx codex login/status/skill-enable/skill-disable` | Toolkit-scoped Codex auth for the Codex coding *skill* (sandbox). Provider auth is `hermes auth add openai-codex` in-container | [codex-integration](docs/codex-integration.md) |
| `agx doctor-cron install/uninstall/status/run` | Host cron: run doctor every N min, mail on state change via the host's `sendmail` (catches expired Codex credentials within one interval); also mails once per new upstream release, with condensed release notes | [runbook](docs/runbook.md#doctor-cron) |
| `agx memory list/enable/disable/status` | Managed memory backends for Hermes | [runbook](docs/runbook.md) |
| `agx ollama configure/status/clear` | Point the toolkit at an **external** Ollama / OpenAI-compatible endpoint | [runbook](docs/runbook.md) |
| `agx inference up/down/status/load/unload` | **In-VM** local inference (Ollama, llama.cpp) as sibling services | [runbook](docs/runbook.md) |
| `agx model pull/list/rm/use` | Local model lifecycle + wiring an agent to an engine | [runbook](docs/runbook.md) |
| `agx firecrawl up/down/status/logs/destroy` | Project-scoped self-hosted Firecrawl stack, digest-pinned | [runbook](docs/runbook.md) |
| `agx profile list <name>` | List a Hermes agent's profiles; most Hermes commands take `--profile` | [tutorial](docs/tutorial.md) |
| `agx tailscale up/down/status/upgrade` | Put the VM on your tailnet, e.g. to reach a dashboard from your phone (VM backends only) | [runbook](docs/runbook.md#remote-access-with-tailscale) |
| `agx conversations status/export` | Session stats + Markdown transcript export (Hermes) | [migration](docs/migration.md) |
| `agx config` | Print the resolved toolkit config | — |

Every command supports `--json` (machine-readable output) and `--yes` (skip confirmations); exit codes are 0 success / 1 user error / 2 runtime failure / 3 health-check failure.

## What lives where

| Path | Role |
|---|---|
| `<data-dir>/docker-compose.yml` | Managed by the CLI — do not hand-edit. Use `docker-compose.override.yml` for overrides. |
| `<data-dir>/.env` | Project-level values for compose `${VAR}` interpolation (`HERMES_UID` / `HERMES_GID`, `OLLAMA_API_KEY`, Firecrawl). Per-agent secrets go in `<name>/.env`. |
| `<data-dir>/<name>/.data/` | Each Hermes agent's `$HERMES_HOME` (auth, sessions, skills, memories, state.db). |
| `<data-dir>/<name>/.openclaw/` | Each OpenClaw agent's persistent dir. |
| `<data-dir>/<name>/.env` | Per-agent secrets (API keys for skills). Managed via `agx secret …`. Wired into the container via compose `env_file:`. |
| `<data-dir>/.firecrawl-state.json` | Captured image digests for the self-hosted Firecrawl stack (managed by `agx firecrawl …`). |
| `<data-dir>/firecrawl/postgres/` | Postgres data for self-hosted Firecrawl. Bind-mounted, duplicity-friendly. |
| `<data-dir>/<name>/.openviking/` | OpenViking sidecar's per-agent data (when `agx memory enable openviking <name>`). |
| `<data-dir>/.codex/` | Toolkit-scoped Codex auth (separate from your personal `~/.codex/`). |
| `<data-dir>/.memory-state/` | Per-agent record of which memory backend the toolkit enabled. |
| `<data-dir>/models/` | Local model weights for `agx inference` / `agx model` (Ollama blobs, llama.cpp GGUFs). |
| `<data-dir>/backups/` | Output of `agx export`. Host backup (e.g. duplicity) picks this up. Pruned to the newest `keep_last` archives per agent. |
| `~/.config/agx/config.toml` | CLI config — VM name, sizes, pinned image tags, captured VM runtime fields. |

## Codex paths

Two distinct ways Hermes can use Codex, both supported — but authenticated differently:

1. **As an LLM provider** (`model.provider: openai-codex`): authenticate **inside the container** — `agx exec <name> -- hermes auth add openai-codex --type oauth --no-browser` (device-code flow; open the URL on any browser logged into your ChatGPT account). Hermes owns that credential and its refresh cycle in the agent's `/opt/data/auth.json`. The toolkit no longer mounts `<data-dir>/.codex` into the runtime container: the old "import bridge" shared one single-use OAuth refresh chain between the host file and the agent, so refreshing either invalidated the other. Expect the subscription chain to die every few weeks anyway (upstream bug) — set `fallback_providers` and `agx doctor-cron install --mail …`.
2. **As a coding skill** (the bundled `autonomous-ai-agents/codex` skill): opt in with `agx codex skill-enable <name>`. The CLI builds a sandbox image with Codex CLI baked in and points the agent's `terminal.docker_image` at it. The sandbox container — not the Hermes runtime — is where `codex` runs when Hermes delegates coding tasks.

OpenClaw bundles its own Codex plugin; the toolkit does **not** mount Codex into OpenClaw. Run `openclaw models auth login --provider openai --device-code` inside the container after creation.

## Memory backends

Hermes agents start with built-in memory. The toolkit ships three managed alternatives:

| Plugin       | What it needs                                            |
|--------------|----------------------------------------------------------|
| `holographic`| Nothing — works with Codex auth alone, fully local       |
| `mem0`       | Ollama (preferred) or `OPENAI_API_KEY`                   |
| `openviking` | Ollama (sibling docker service from Volcengine)          |

```bash
agx memory list                            # plugins + tier + summary
agx memory enable holographic coder        # zero-dep, in-process
agx ollama configure --url http://192.168.1.50:11434  # point at an existing Ollama (not localhost)
agx memory enable openviking coder         # sibling service
agx memory status coder
```

The toolkit does NOT run Ollama for you — you point it at one you already run (host, LAN, Tailscale, or a hosted OpenAI-compatible service). `localhost` URLs are refused at config time because they'd resolve to the VM, not the host.

## Backups

The toolkit does not implement an internal backup engine. Pick a `<data-dir>` your existing host backup (duplicity, restic, borg, etc.) already covers, and you're done. `agx export <name>` produces a clean archive in `<data-dir>/backups/` when you want a crash-consistent point-in-time. Note: the default backups dir is a **rolling window** — each export prunes that agent's archives down to the newest `[export_cron].keep_last` (default 14; `0` disables). Long-term history is your host backup's job — check its retention settings. See the [runbook's retention section](docs/runbook.md#backup-retention).

## Threat model

See [docs/threat-model.md](docs/threat-model.md). Short version:

- Outer trust boundary: the VM (multipass or libvirt backends). Under `--backend bare_metal` there is no VM — the host is the boundary, which is why bare-metal is opt-in for single-user workstations only.
- Inner trust boundary: one Docker container per agent, rootless.
- Hermes mounts the rootless Docker socket so it can spawn its nested sandbox — bounded by the rootless `ubuntu` user, not root.
- Dashboards bind 127.0.0.1 only.

## Status

A personal, single-maintainer project that the author runs daily. The `libvirt` backend (the Linux default) is the most exercised path; `multipass` (the macOS default) has seen less testing, GPU passthrough is still experimental, and `bare_metal` is opt-in. Before 1.0, expect breaking changes — run `agx reseat <name>` after upgrading the toolkit. Issues and pull requests are welcome and handled on a best-effort basis.

The architecture rules and their rationale live in [`docs/architecture.md`](docs/architecture.md) and [`AGENTS.md`](AGENTS.md) (the engineering guide, also used by AI coding assistants).

## Contributing, security, license

- Contributing: bug reports and feature requests are welcome as [issues](https://github.com/ebner/agx/issues); pull requests are limited to collaborators. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).
- Security issues: please report privately, see [SECURITY.md](SECURITY.md).
- License: [MIT](LICENSE).

agx is an independent project. It is not affiliated with or endorsed by Nous Research, the OpenClaw project, OpenAI, Anthropic, Docker Inc. or Canonical; product names are trademarks of their respective owners.
