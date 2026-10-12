# Tutorial — agents end-to-end

This walks the full lifecycle for one Hermes agent and one OpenClaw agent: create → personality → skills → messaging-platform connection → daily use → upgrade → tear-down. If you just want the cheat sheet, see [runbook.md](runbook.md).

## 1. The model

One toolkit-managed *agent* = one Docker container = one Hermes (or OpenClaw) installation with its own `$HERMES_HOME` bind-mounted at `<data-dir>/<name>/.data/`. The host CLI never talks to the in-VM Docker daemon directly — it goes through the configured VM backend (`multipass` by default; `libvirt` drives the VM via `virsh` + ssh/scp; `bare_metal` skips the VM entirely — see the [runbook](runbook.md)). This tutorial assumes the multipass default.

```
host                       Multipass VM                     container
─────────────────────────  ──────────────────────────────   ──────────────────────────
<data-dir>/coder/.data/ ↔  /home/ubuntu/agents/coder/.data ↔ /opt/data   ($HERMES_HOME)
```

## 2. Three concepts that are easy to confuse

Hermes uses three terms that all sound related but aren't. Get this straight up front and the rest is easy.

| Concept | What it is | Where it lives | How you change it |
|---|---|---|---|
| **Profile** | A state-isolated Hermes instance — its own `$HERMES_HOME` with its own auth, sessions, skills, config, memories, cron jobs. Isolates *state*, not *privileges* — see §6 | `$HERMES_HOME/profiles/<name>/` inside the container | `hermes profile {list,create --clone,use,delete,rename,export,import,install}` |
| **Personality** | The agent's persistent voice / identity — slot #1 of the system prompt | `$HERMES_HOME/SOUL.md` | Edit `SOUL.md` directly, or `hermes setup --section agent` |
| **Skill** | A folder of procedural knowledge an agent loads on demand (Anthropic's open Agent-Skills format) | `$HERMES_HOME/skills/<skill-id>/SKILL.md` | `hermes skills {browse,install,update,uninstall}` |

Also: `/personality <name>` inside an interactive Hermes session is a **temporary** overlay (preset like `concise`, `technical`, `kawaii`, … or a custom one defined in `config.yaml`). It doesn't modify `SOUL.md`.

## 3. How many agents do you actually need?

**Rule of thumb: one container = one agent = one Hermes installation = one bot identity.** Upstream Hermes positions profiles as full production agents, and its own default Docker topology is one container hosting many profile gateways. This toolkit deliberately doesn't follow that default: the container is the isolation boundary profiles don't provide (upstream is explicit that profiles do not sandbox the agent), and the toolkit's management surface targets one Hermes home per container. Here, profiles are a within-container tool for *interactive* second `$HERMES_HOME`s (scratch personas, A/B tests, state-contained experiments — see §6) — in this toolkit they don't (yet) unlock managed extra bots or dashboards (GH #6), auto-routing doesn't exist natively, and any of those wants another container for now.

Your instinct will be "one agent per topic" — *finance*, *family*, *health*, etc. Resist it. Start small and earn the splits.

### The default: one container, one persona

For most home/family deployments, the right starting point is **one toolkit agent**:

```bash
agx create home --type hermes --setup
```

One Mattermost bot. One `SOUL.md`. One neutral voice that replies in family chat. Cheaper to run, easier to back up, and easier to reason about than anything more.

Add skills later as needs surface — `hermes skills browse` and `hermes skills search <keyword>` discover what's in the registry, `hermes skills install <id>` adds one. Start with zero and grow; skills are cheap to add and remove. See §4.3.

### Earn a separate toolkit agent (container) when…

…at least one of these is genuinely true:

1. **Different LLM provider per agent.** *Health* runs against a local Llama for privacy; *finance* uses Codex.
2. **Different messaging connectivity.** *Family* on Mattermost, *work* on Slack — isolated so a Slack adapter bug can't kill the family bot. (Each toolkit agent runs its own gateway against its own bot token — see §4.4.)
3. **Different upgrade cadence.** You want to canary a new Hermes version on the low-stakes agent first.
4. **Hard resource isolation.** Different CPU/mem caps, different restart policy.
5. **Strong blast-radius isolation between domains.** A compromised skill in *finance* must not even *see* the *health* agent's auth or data.

If none of those apply, you probably don't need a second container.

### Earn a Hermes-internal profile when…

…you want a *cheap, state-isolated* second `$HERMES_HOME` inside one container, used **interactively** (not gateway-exposed — see §6). Real use cases:

1. **Scratch persona.** `hermes profile create scratch --clone default` → experiment → `hermes profile delete scratch`. Faster than `agx create`/`agx remove`.
2. **Contain an experiment's state.** Clone to `experiment`, test a new `SOUL.md`, a rewritten config, or an unfamiliar skill without polluting your main profile's config, sessions, or memories — then promote (`hermes profile use experiment`) or discard. This contains *state*, not *privileges*: the experiment still runs with the same filesystem access as the default profile.
3. **A/B test personalities** without container churn.
4. **Try a profile distribution.** `hermes profile install <git-url>` installs a packaged agent (personality + skills + cron jobs + config, distributed as a git repo) as a new profile — evaluate it in isolation before adopting its pieces into your main profile.
5. **Shared VM with another human.** Each person gets their own profile so sessions, skills, and auth don't cross-pollute. (Rare in personal use.)

### What profiles are NOT for

**Profiles are not a security boundary.** The Hermes docs are explicit: "Profiles do not sandbox the agent." Every profile in a container has the same filesystem access, and tool subprocesses share the real `HOME` across profiles by default (opt into stricter separation per profile with `terminal.home_mode: profile` in that profile's `config.yaml`). If a persona handles data that must survive another persona's compromised skill, that's a second container (criterion 5 above), full stop.

**Profiles are not (yet) this toolkit's way to run more bots.** Upstream supports it — in the official image each profile's gateway is a first-class s6-supervised service, and upstream's default topology is one container running all profile gateways side by side. The mechanics work in this toolkit's containers (spike-verified, with tier-1 guardrails landed: boot-time log-dir pre-create + a doctor watch — see §6), but secondary profiles' *configuration* stays outside the toolkit, and each gateway needs its own bot token anyway — at which point a second toolkit agent buys real isolation for the same token cost. Profile-aware management (tier 2) is tracked in GH #6.

**Profiles are not a way to multiplex one Mattermost (or Slack/Discord) bot across personas.** Each profile that runs a gateway binds its own bot token; two profiles cannot share a bot. The Hermes docs are explicit:

> Each profile runs its own gateway as a separate process with its own bot token. If two profiles accidentally share a token, the second gateway will be blocked with a clear error naming the conflicting profile.

So "one container, three profiles, one Mattermost bot routing per channel" — that pattern does not exist natively. If you want channel-aware persona switching, you have three options:

- **One bot, one voice, topical skills** (the default above; the bot adapts to context via skills).
- **One bot, one voice, `/personality <name>` runtime overlay** (manual: the bot still defaults to the agent's `SOUL.md`, but interactive users can switch the voice mid-session).
- **Separate bot users + separate toolkit agents** — one container per persona, each with its own Mattermost bot user. Use this when the personas really are different agents with different skills, different LLM, or different data-blast-radius needs.

### Worked examples for the finance/family/health scenario

- **A. Cheap default.** One `home` container, one Mattermost bot, skills added on demand. Start here unless you have a reason not to.
- **B. Privacy split.** Two containers: `home` (finance + family on the family Mattermost) and `health` (separate container, possibly a different LLM, possibly not even on Mattermost — answers via the local dashboard or a private bot). Earned by criteria 1 and 5.
- **C. Profile as scratch experiment.** One `home` container in production. When you want to try a "stricter financial advisor" voice: `hermes profile create stricter-fin --clone default`, iterate interactively, decide, then either `profile use` it or delete. Profiles shine here precisely because they're disposable.
- **D. Three containers.** Only if each meaningfully differs. The cost is three Mattermost bot users (Hermes won't let two share one), three ports, three sets of upgrade decisions. Rare for personal use.

The rest of this tutorial assumes the default — one toolkit agent named `coder` — but the commands apply identically whether you have one agent or several.

## 4. Walk-through: create, customize, connect, use

### 4.1 Create the agent

```bash
agx create coder --type hermes --setup
```

`--setup` chains `hermes setup`, which is the interactive wizard for model provider, terminal backend, gateway, etc. If you pick `openai-codex` as the model provider, authenticate it inside the container afterwards: `agx exec coder -- hermes auth add openai-codex --type oauth --no-browser` (device-code URL + code, any browser logged into your ChatGPT account). `agx codex login` is unrelated — it only feeds the optional Codex *sandbox skill*.

### 4.2 Customize the personality

Edit `SOUL.md` directly. The file is on your host filesystem:

```bash
$EDITOR <data-dir>/coder/.data/SOUL.md
```

Or run the wizard:

```bash
agx exec coder hermes setup --section agent
```

For a session-only experiment, drop into the agent and use the built-in presets:

```bash
agx exec coder hermes chat
> /personality concise
> /personality technical
```

To define a custom named personality that survives sessions, add it to `<data-dir>/coder/.data/config.yaml`:

```yaml
agent:
  personalities:
    codereviewer: |
      You are a meticulous code reviewer. Be specific, cite line numbers, prefer
      small reversible suggestions over rewrites.
```

Then `/personality codereviewer` inside the session.

### 4.3 Install skills

Browse or search the registry from inside the agent:

```bash
agx exec coder hermes skills browse
agx exec coder hermes skills search kubernetes
```

Install from any of the supported sources:

```bash
# bundled / official
agx exec coder hermes skills install official/security/1password

# GitHub path
agx exec coder hermes skills install openai/skills/k8s

# direct URL
agx exec coder hermes skills install https://example.com/path/SKILL.md
```

Skills land in `<data-dir>/coder/.data/skills/<skill-id>/` on the host, so duplicity backs them up alongside the rest of the agent. List, update, uninstall:

```bash
agx exec coder hermes skills list
agx exec coder hermes skills check        # any upstream changes?
agx exec coder hermes skills update
agx exec coder hermes skills uninstall <skill-id>
```

Skills are **profile-scoped** inside Hermes. With Strategy A (one container per agent) you don't have to think about this — each toolkit agent has its own skill set. With Strategy B you'd run `hermes -p <profile> skills install ...`.

Most skills that talk to a third-party service need an API key. Wire one in per-agent — never in the project-level `.env`. (Exception: Firecrawl can be self-hosted via `agx firecrawl up` — see `docs/runbook.md`. No per-agent setup needed, every agent picks it up automatically.)

```bash
# prompts for the value (hidden, never printed back)
agx secret set coder FIRECRAWL_API_KEY

# or pass it on the command line (visible in shell history)
agx secret set coder FIRECRAWL_API_KEY fc-xxxxx

agx secret list coder         # names only
agx secret unset coder FIRECRAWL_API_KEY
agx secret clear coder        # wipe all
```

Secrets land in `<data-dir>/coder/.env` and become container env vars on the next restart (which `secret set` handles automatically). The Firecrawl skill — like every skill following the standard convention — reads `os.environ['FIRECRAWL_API_KEY']`, no further configuration needed.

### 4.3a (Optional) Pick a memory backend

By default Hermes uses built-in memory (`MEMORY.md` / `USER.md`). For
longer-lived semantic recall the toolkit ships three managed backends:

```bash
agx memory list
```

| Plugin       | Tier             | What it needs                                                |
|--------------|------------------|--------------------------------------------------------------|
| `holographic`| in-process       | nothing — works with Codex auth alone, fully local           |
| `mem0`       | in-process       | Ollama (recommended) or an `OPENAI_API_KEY`                  |
| `openviking` | sibling-service  | Ollama (or any OpenAI-compatible endpoint)                   |

`holographic` is the easiest starting point: no extra services, no extra
credentials, no network calls.

```bash
agx memory enable holographic coder
```

`mem0` and `openviking` need an embeddings + LLM provider. The toolkit
talks to whichever **existing** Ollama instance (or OpenAI-compatible
service) you already run — it does not start one for you:

```bash
# point the toolkit at an Ollama you already run (host, LAN, Tailscale,
# or a hosted vendor — but not `localhost`, which the VM can't reach)
agx ollama configure --url http://10.20.30.1:11434

# now use it
agx memory enable mem0 coder           # in-process inside Hermes
agx memory enable openviking coder     # sibling docker service
```

For a hosted OpenAI-compatible endpoint with bearer-token auth, pass
`--api-key` — it lands in `<data-dir>/.env`, never in `config.toml`:

```bash
agx ollama configure --url https://api.groq.example/v1 --api-key gsk_…
```

Switch backends with `agx memory disable coder` followed by another
`enable`. `agx memory status` shows what's wired and (for
holographic) flags the SQLite-on-9p / missing-NumPy risks described in
`CLAUDE.md`.

### 4.4 Connect to a messaging platform

Hermes ships a *messaging gateway* (`hermes gateway`) plus a few platform-specific subcommands. Coverage and the exact flow depend on the platform.

**Slack** (officially supported):

```bash
# generate a Slack app manifest you paste into api.slack.com → Create an app
agx exec coder hermes slack

# configure the gateway with the bot token from your new Slack app
agx exec coder hermes setup --section gateway

# start the gateway as a background service inside the container
agx exec coder hermes gateway start
agx exec coder hermes gateway          # status / live runtime
```

**WhatsApp** (officially supported):

```bash
agx exec coder hermes whatsapp         # pairing code flow
```

**Mattermost / Matrix / Discord / IRC / Rocket.Chat / Teams** — *not first-class as of Hermes v1.x*. Two viable paths:

1. Check the skill registry — community skills may exist:
   ```bash
   agx exec coder hermes skills search mattermost
   agx exec coder hermes skills search matrix
   ```
2. Or write/find a custom gateway adapter and drop it under `$HERMES_HOME/gateway/adapters/`. See the Hermes Developer Guide section *Extending the gateway*.

If you discover a working community skill or adapter, please open a PR adding it to this section.

#### Networking considerations

By design the toolkit refuses to bind any Hermes dashboard or service to `0.0.0.0`. That's fine for:

- Bot integrations using outbound websockets / long-polling (Slack Socket Mode, Discord gateway, Mattermost websocket, Matrix federation client) — no inbound port needed.
- Cloud-hosted webhooks that POST to a public URL — needs a tunnel (Cloudflare Tunnel, ngrok, tailscale-funnel) which terminates on the host and forwards into the VM.

If a connector strictly needs an inbound port on a public interface, expose it via `<data-dir>/docker-compose.override.yml`:

```yaml
services:
  agent-coder:
    ports:
      - "127.0.0.1:8642:8642"   # dashboard — keep on loopback
      - "8765:8765"             # gateway webhook — only if you understand the exposure
```

### 4.5 Use the agent

```bash
agx exec coder hermes chat                           # interactive
agx exec coder hermes chat -q "summarize today's PRs"  # one-shot, no TTY
agx exec coder hermes dashboard --no-open            # open the web UI URL
```

Dashboards are reachable from the host at `http://127.0.0.1:<port>/` — the port is what `agx list` prints for that agent. The dashboard is only ever bound to loopback (toolkit enforces this).

### 4.6 Monitor, debug

```bash
agx logs coder -f                  # follow container stdout/stderr
agx doctor coder                   # health: VM, mount, socket, Hermes itself
agx exec coder hermes status       # Hermes' own view of auth, gateway, model
agx exec coder hermes doctor       # Hermes' deeper diagnostics
agx exec coder hermes sessions     # list / export / delete past chats
agx exec coder hermes insights     # token/cost/activity report
```

### 4.7 Back up

If you trust your host backup (duplicity, restic, …) over `<data-dir>`, you're already covered. For an explicit crash-consistent point-in-time:

```bash
agx export coder                    # → <data-dir>/backups/coder-<utc>.tar.zst
agx export --all
agx export-cron install --time 02:30
```

Heads-up on retention: the default backups dir is a rolling window — each export prunes the agent's archives down to the newest `[export_cron].keep_last` (default 14). Long-term history is your host backup's job; see the [runbook's retention section](runbook.md#backup-retention).

### 4.8 Upgrade

```bash
agx outdated                  # who's behind upstream? (read-only)
agx upgrade coder             # bring coder to the latest upstream release
agx upgrade coder --tag v1.2.4   # or pin an explicit tag
agx upgrade coder --recreate  # force-refresh at the current pin (no version change)
```

Bare `agx upgrade <name>` implicitly resolves the latest upstream tag from GitHub Releases (like `apt upgrade`) and no-ops — no snapshots, no downtime — when the agent is already there. When there is work to do, it takes both a VM snapshot and a host-side `.tar.zst` of the agent's state before touching anything, then pulls, recreates, runs `hermes config migrate`, and runs `agx doctor`. If doctor fails, the toolkit auto-rolls-back. An interrupted upgrade resumes on re-run from its journal, targeting the tags recorded when it began. To roll back manually later:

```bash
agx rollback coder --yes
agx rollback coder --snapshot coder-20260512T120000Z.tar.zst
```

### 4.9 Rename, remove, tear down

```bash
agx rename coder reviewer           # container + data-dir move atomically
agx remove coder                    # prompts; --export-first to bag it first
agx vm destroy --yes                # whole VM gone; <data-dir> on host untouched
```

`vm destroy` is intentionally non-destructive of host state — `<data-dir>` and your personal `~/.codex/` survive. If you want to wipe everything, `rm -rf <data-dir>` after destroying the VM.

## 5. OpenClaw — same lifecycle, different commands

OpenClaw uses its own bundled Codex plugin and onboarding flow:

```bash
agx create personal --type openclaw --setup
agx exec personal openclaw models auth login --provider openai --device-code
agx exec personal openclaw chat
agx logs personal -f
agx remove personal
```

The toolkit does **not** mount Codex auth into OpenClaw — OpenClaw's plugin manages it itself. Don't try to "share" your toolkit-scoped Codex with OpenClaw via mounts; you'll end up with two managers fighting over the same `auth.json`.

## 6. Working with Hermes profiles inside one container

Upstream Hermes treats profiles as full production agents — each one a complete `$HERMES_HOME` with its own `config.yaml`, `.env`, `SOUL.md`, sessions, memories, skills, and cron jobs. Within this toolkit, use them freely for **interactive** work: scratch personas, A/B tests, state-contained experiments, trying profile distributions (see §3 for when each earns its keep). Keep gateways on the default profile only — the last subsection below explains why.

```bash
# create a profile that clones the default's config, .env, SOUL.md and skills, then diverges
agx exec coder hermes profile create scratch --clone default

# use it
agx exec coder hermes -p scratch chat
agx exec coder hermes -p scratch skills install official/codereview/standards
agx exec coder hermes -p scratch setup --section agent      # different SOUL.md

# day-to-day
agx exec coder hermes profile list
agx exec coder hermes profile rename scratch reviewer
agx exec coder hermes profile export reviewer -o /opt/data/reviewer.zip
agx exec coder hermes profile delete reviewer
```

Worth knowing before you lean on them:

- **Backups cover profiles automatically.** Profile data lives at `<data-dir>/coder/.data/profiles/<profile-name>/` on the host, so `agx export coder` and your host backup cover all profiles inside that container.
- **Two clone flavors.** `--clone` copies config, `.env`, `SOUL.md`, and skills. `--clone-all` additionally copies memories, cron jobs, and plugins — but never per-profile history (`state.db`, `backups/`, `checkpoints/`), which can reach tens of GB. Because `--clone` copies `.env`, a cloned profile inherits the default's bot token — harmless, since Hermes refuses to start a second gateway on a token already in use.
- **The toolkit manages the default profile only.** `agx secret set` (the `terminal.env_passthrough` half), `agx model use`, `agx ollama configure`, `agx conversations`, `agx memory`, the dashboard `0.0.0.0` refusal, and doctor's per-agent Hermes probes all read/write the *default* profile's `config.yaml` and `state.db`. Secondary profiles are yours to manage by hand via `agx exec <agent> hermes -p <profile> …` — the toolkit neither sees nor validates them.
- **No privilege isolation between profiles.** Same container, same uid, same filesystem access; tool subprocesses share the real `HOME` across profiles by default (`terminal.home_mode: profile` opts a profile's tools into `$HERMES_HOME/home` instead). The container is the security boundary, not the profile.
- **`hermes update` upgrades all profiles at once.** Profiles share the installation, so there's no per-profile version pinning or canarying — that's a per-container property (§3 criterion 3).

### Profile + gateway: works, with tier-1 toolkit guardrails

Upstream supports concurrent per-profile gateways — in the official image each profile's gateway is a dynamically registered, s6-supervised service (each bound to its own bot token, enforced), and upstream's default topology is one container running all of them.

A 2026-07 spike (Hermes v2026.7.7.2, libvirt backend, virtiofs-mounted data dir) verified the mechanics work in this toolkit's containers too: `hermes profile create` registers the s6 slot live and pre-creates the gateway log dir (`/opt/data/logs/gateways/<profile>/`) as the `hermes` user — defusing the rootless root-`mkdir`-on-shared-mount class that bit the default gateway — the profile gateway and its cron scheduler run supervised, and the running set auto-resumes after container restarts and recreates via each profile's `gateway_state.json`.

The toolkit backs this with two guardrails (GH #6 tier 1):

- The cont-init hook pre-creates **every existing profile's** gateway log dir on each container boot, not just the default's. This covers restored or imported data dirs, which arrive without the log tree and would otherwise hot-loop the log service. Restart the agent after restoring an archive (or after creating a profile you plan to gateway-expose) so the hook runs.
- `agx doctor` warns when a profile whose `gateway_state.json` wants `running` has its s6 service down or its log service flapping, with the remediation in the message.

Still, a gateway-exposed secondary profile remains **mostly unmanaged** (GH #6 tier 2):

1. **One bot token per gateway, enforced.** Each profile needs its own Slack/Mattermost/Discord bot account. If you're provisioning a second bot anyway, a profile saves you almost nothing over a second toolkit agent.
2. **Config stays outside the toolkit.** No `agx conversations`, no secrets/`env_passthrough` wiring, no config validation (including the `0.0.0.0` dashboard refusal) for secondary profiles — you manage them by hand via `agx exec <agent> hermes -p <profile> …`.
3. **The dashboard isn't reachable.** The compose fragment publishes only the default profile's dashboard port to the host loopback; a secondary profile's dashboard binds a port nothing forwards.

If a second gateway-exposed persona is worth a bot token, it's usually worth a container: `agx create <name> --type hermes` buys full doctor coverage, independent upgrade/rollback, resource caps, and a real isolation boundary. The sweet spot for profile gateways is a **pod**: several same-trust-domain personas sharing one container, with containers still separating trust domains.

## 7. Common questions

**Q: I want a different LLM provider per persona — separate agent or profile?**
Separate toolkit agent. The provider is per-`$HERMES_HOME` and lives in `config.yaml`, so profiles technically support it, but providers usually come with different cost/privacy/upgrade profiles you'll want to track separately anyway.

**Q: Can one Mattermost bot route incoming messages to different personas based on channel or sender?**
Not natively in Hermes — each profile that runs a gateway needs its own bot token, so you can't have one bot replying as different personas. Three options:
1. **One bot, topical skills** (simplest): the bot has one neutral voice and loads finance/family/health skills as the conversation calls for them.
2. **One bot, `/personality` overlay**: interactive users can switch the bot's voice mid-session with `/personality <name>` — but it doesn't auto-switch by channel.
3. **One bot per persona, one toolkit agent per bot**: real separation, real cost (multiple bot users).
If you want true auto-routing by channel, you'd need a custom gateway adapter that selects an LLM + skill set per message — not a profile-switching trick.

**Q: Where does Codex auth live for a Hermes agent that uses Codex as its LLM?**
In Hermes' own pooled auth store at `<data-dir>/coder/.data/auth.json` (`/opt/data/auth.json` in the container), written by `agx exec coder -- hermes auth add openai-codex --type oauth --no-browser`. The toolkit-scoped `<data-dir>/.codex/` is NOT involved any more — it used to be bind-mounted as an "import bridge", which tied the agent's credential to the host file's single-use OAuth refresh chain and got agents' tokens invalidated whenever the host copy refreshed. The subscription chain still dies every few weeks on its own (upstream Hermes #65346): set `fallback_providers` in the agent's `config.yaml` so it keeps answering, and `agx doctor-cron install --mail you@…` so you hear about it within 15 minutes; re-auth is the same one-liner, from any device with a browser (phone + `agx tailscale` + SSH works).

**Q: My agent needs an inbound webhook for Slack/Discord/Mattermost.**
Prefer the platform's outbound mode (Slack Socket Mode, Discord gateway, Mattermost websocket) — no inbound port required. If you truly need a webhook, terminate it on the host with a tunnel (Cloudflare Tunnel, tailscale-funnel) that forwards to `127.0.0.1:<vm-forwarded-port>`. Avoid binding `0.0.0.0` directly on the VM.

**Q: I broke `SOUL.md` and want the default back.**
`rm <data-dir>/coder/.data/SOUL.md` — Hermes auto-creates a fresh one on next start. Existing files are never overwritten.

**Q: How do I migrate from a stand-alone Hermes install into a toolkit agent?**
Inside the agent: `hermes import <path-to-backup.zip>`. Put the backup somewhere visible to the container — e.g. drop it in `<data-dir>/coder/.data/` on the host, then `agx exec coder hermes import /opt/data/backup.zip`.
