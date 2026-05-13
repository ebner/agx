# Tutorial — agents end-to-end

This walks the full lifecycle for one Hermes agent and one OpenClaw agent: create → personality → skills → messaging-platform connection → daily use → upgrade → tear-down. If you just want the cheat sheet, see [runbook.md](runbook.md).

## 1. The model

One toolkit-managed *agent* = one Docker container = one Hermes (or OpenClaw) installation with its own `$HERMES_HOME` bind-mounted at `<data-dir>/<name>/.data/`. The host CLI never talks to the in-VM Docker daemon directly — it shells out to `incus`.

```
host                       Multipass VM                     container
─────────────────────────  ──────────────────────────────   ──────────────────────────
<data-dir>/coder/.data/ ↔  /home/ubuntu/agents/coder/.data ↔ /opt/data   ($HERMES_HOME)
```

## 2. Three concepts that are easy to confuse

Hermes uses three terms that all sound related but aren't. Get this straight up front and the rest is easy.

| Concept | What it is | Where it lives | How you change it |
|---|---|---|---|
| **Profile** | An isolated Hermes instance — its own `$HERMES_HOME` with its own auth, sessions, skills, config | `$HERMES_HOME/profiles/<name>/` inside the container | `hermes profile {list,create --clone,use,delete,rename,export,import}` |
| **Personality** | The agent's persistent voice / identity — slot #1 of the system prompt | `$HERMES_HOME/SOUL.md` | Edit `SOUL.md` directly, or `hermes setup --section agent` |
| **Skill** | A folder of procedural knowledge an agent loads on demand (Anthropic's open Agent-Skills format) | `$HERMES_HOME/skills/<skill-id>/SKILL.md` | `hermes skills {browse,install,update,uninstall}` |

Also: `/personality <name>` inside an interactive Hermes session is a **temporary** overlay (preset like `concise`, `technical`, `kawaii`, … or a custom one defined in `config.yaml`). It doesn't modify `SOUL.md`.

## 3. How many agents do you actually need?

**Rule of thumb: one container = one agent = one Hermes installation = one bot identity.** Profiles are a within-container tool for *throwaway* second `$HERMES_HOME`s (sandbox, A/B, scratch) — they don't unlock more bots, more dashboards, or auto-routing, and any of those needs another container.

Your instinct will be "one agent per topic" — *finance*, *family*, *health*, etc. Resist it. Start small and earn the splits.

### The default: one container, one persona

For most home/family deployments, the right starting point is **one toolkit agent**:

```bash
agents create home --type hermes --setup
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

…you want a *cheap, disposable* second `$HERMES_HOME` inside one container. Real use cases:

1. **Scratch persona.** `hermes profile create scratch --clone default` → experiment → `hermes profile delete scratch`. Faster than `agents create`/`agents remove`.
2. **Sandbox a risky change.** Clone to `experiment`, test a new `SOUL.md` or a flaky skill, then promote (`hermes profile use experiment`) or discard.
3. **A/B test personalities** without container churn.
4. **Shared VM with another human.** Each person gets their own profile so sessions, skills, and auth don't cross-pollute. (Rare in personal use.)

### What profiles are NOT for

**Profiles are not a way to multiplex one Mattermost (or Slack/Discord) bot across personas.** Each profile that runs a gateway runs its own gateway process bound to its own bot token; two profiles cannot share a bot. The Hermes docs are explicit:

> Each profile runs its own gateway as a separate process with its own bot token. If two profiles accidentally share a token, the second gateway will be blocked with a clear error naming the conflicting profile.

So "one container, three profiles, one Mattermost bot routing per channel" — that pattern does not exist natively. If you want channel-aware persona switching, you have three options:

- **One bot, one voice, topical skills** (the default above; the bot adapts to context via skills).
- **One bot, one voice, `/personality <name>` runtime overlay** (manual: the bot still defaults to the agent's `SOUL.md`, but interactive users can switch the voice mid-session).
- **Separate bot users + separate toolkit agents** — one container per persona, each with its own Mattermost bot user. Use this when the personas really are different agents with different skills, different LLM, or different data-blast-radius needs.

### Worked examples for the finance/family/health scenario

- **A. Cheap default.** One `home` container, one Mattermost bot, skills added on demand. Start here unless you have a reason not to.
- **B. Privacy split.** Two containers: `home` (finance + family on the family Mattermost) and `health` (separate container, possibly a different LLM, possibly not even on Mattermost — answers via the local dashboard or a private bot). Earned by criteria 1 and 5.
- **C. Profile as sandbox.** One `home` container in production. When you want to try a "stricter financial advisor" voice: `hermes profile create stricter-fin --clone default`, iterate, decide, then either `profile use` it or delete. Profiles shine here precisely because they're disposable.
- **D. Three containers.** Only if each meaningfully differs. The cost is three Mattermost bot users (Hermes won't let two share one), three ports, three sets of upgrade decisions. Rare for personal use.

The rest of this tutorial assumes the default — one toolkit agent named `coder` — but the commands apply identically whether you have one agent or several.

## 4. Walk-through: create, customize, connect, use

### 4.1 Create the agent

```bash
agents create coder --type hermes --setup
```

`--setup` chains `hermes setup`, which is the interactive wizard for model provider, terminal backend, gateway, etc. Pick `openai-codex` as the model provider if you ran `agents codex login` first — Hermes imports the toolkit-scoped auth automatically.

### 4.2 Customize the personality

Edit `SOUL.md` directly. The file is on your host filesystem:

```bash
$EDITOR <data-dir>/coder/.data/SOUL.md
```

Or run the wizard:

```bash
agents exec coder hermes setup --section agent
```

For a session-only experiment, drop into the agent and use the built-in presets:

```bash
agents exec coder hermes chat
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
agents exec coder hermes skills browse
agents exec coder hermes skills search kubernetes
```

Install from any of the supported sources:

```bash
# bundled / official
agents exec coder hermes skills install official/security/1password

# GitHub path
agents exec coder hermes skills install openai/skills/k8s

# direct URL
agents exec coder hermes skills install https://example.com/path/SKILL.md
```

Skills land in `<data-dir>/coder/.data/skills/<skill-id>/` on the host, so duplicity backs them up alongside the rest of the agent. List, update, uninstall:

```bash
agents exec coder hermes skills list
agents exec coder hermes skills check        # any upstream changes?
agents exec coder hermes skills update
agents exec coder hermes skills uninstall <skill-id>
```

Skills are **profile-scoped** inside Hermes. With Strategy A (one container per agent) you don't have to think about this — each toolkit agent has its own skill set. With Strategy B you'd run `hermes -p <profile> skills install ...`.

### 4.4 Connect to a messaging platform

Hermes ships a *messaging gateway* (`hermes gateway`) plus a few platform-specific subcommands. Coverage and the exact flow depend on the platform.

**Slack** (officially supported):

```bash
# generate a Slack app manifest you paste into api.slack.com → Create an app
agents exec coder hermes slack

# configure the gateway with the bot token from your new Slack app
agents exec coder hermes setup --section gateway

# start the gateway as a background service inside the container
agents exec coder hermes gateway start
agents exec coder hermes gateway          # status / live runtime
```

**WhatsApp** (officially supported):

```bash
agents exec coder hermes whatsapp         # pairing code flow
```

**Mattermost / Matrix / Discord / IRC / Rocket.Chat / Teams** — *not first-class as of Hermes v1.x*. Two viable paths:

1. Check the skill registry — community skills may exist:
   ```bash
   agents exec coder hermes skills search mattermost
   agents exec coder hermes skills search matrix
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
agents exec coder hermes chat                           # interactive
agents exec coder hermes chat -q "summarize today's PRs"  # one-shot, no TTY
agents exec coder hermes dashboard --no-open            # open the web UI URL
```

Dashboards are reachable from the host at `http://127.0.0.1:<port>/` — the port is what `agents list` prints for that agent. The dashboard is only ever bound to loopback (toolkit enforces this).

### 4.6 Monitor, debug

```bash
agents logs coder -f                  # follow container stdout/stderr
agents doctor coder                   # health: VM, mount, socket, Hermes itself
agents exec coder hermes status       # Hermes' own view of auth, gateway, model
agents exec coder hermes doctor       # Hermes' deeper diagnostics
agents exec coder hermes sessions     # list / export / delete past chats
agents exec coder hermes insights     # token/cost/activity report
```

### 4.7 Back up

If you trust your host backup (duplicity, restic, …) over `<data-dir>`, you're already covered. For an explicit crash-consistent point-in-time:

```bash
agents export coder                    # → <data-dir>/backups/coder-<utc>.tar.zst
agents export --all
agents export-cron install --time 02:30
```

### 4.8 Upgrade

```bash
agents upgrade coder --tag v1.2.4
```

This takes both an Incus VM snapshot and a host-side `.tar.zst` of the agent's state before touching anything, then pulls, recreates, and runs `agents doctor`. If doctor fails, the toolkit auto-rolls-back. To roll back manually later:

```bash
agents rollback coder --yes
agents rollback coder --snapshot coder-20260512T120000Z.tar.zst
```

### 4.9 Rename, remove, tear down

```bash
agents rename coder reviewer           # container + data-dir move atomically
agents remove coder                    # prompts; --export-first to bag it first
agents vm destroy --yes                # whole VM gone; <data-dir> on host untouched
```

`vm destroy` is intentionally non-destructive of host state — `<data-dir>` and your personal `~/.codex/` survive. If you want to wipe everything, `rm -rf <data-dir>` after destroying the VM.

## 5. OpenClaw — same lifecycle, different commands

OpenClaw uses its own bundled Codex plugin and onboarding flow:

```bash
agents create personal --type openclaw --setup
agents exec personal openclaw models auth login --provider openai-codex --device-code
agents exec personal openclaw chat
agents logs personal -f
agents remove personal
```

The toolkit does **not** mount Codex auth into OpenClaw — OpenClaw's plugin manages it itself. Don't try to "share" your toolkit-scoped Codex with OpenClaw via mounts; you'll end up with two managers fighting over the same `auth.json`.

## 6. Working with Hermes profiles inside one container

Profiles are most useful as **disposable second `$HERMES_HOME`s** for sandboxing, A/B-testing personalities, or cheap experimentation — see §3 for when this earns its keep and when it doesn't.

```bash
# create a profile that clones the default's auth + skills, then diverges
agents exec coder hermes profile create scratch --clone default

# use it
agents exec coder hermes -p scratch chat
agents exec coder hermes -p scratch skills install official/codereview/standards
agents exec coder hermes -p scratch setup --section agent      # different SOUL.md

# day-to-day
agents exec coder hermes profile list
agents exec coder hermes profile rename scratch reviewer
agents exec coder hermes profile export reviewer -o /opt/data/reviewer.zip
agents exec coder hermes profile delete reviewer
```

Profile data lives at `<data-dir>/coder/.data/profiles/<profile-name>/` on the host, so `agents export coder` and your host backup cover all profiles inside that container.

### Profile + gateway: one bot token per profile

The gateway model is strict: each profile that runs a gateway runs its own gateway process with its own bot token. From the Hermes docs:

> Each profile runs its own gateway as a separate process with its own bot token. Each profile gets its own service name. They run independently. If two profiles accidentally share a token, the second gateway will be blocked with a clear error naming the conflicting profile.

Practical consequences:

- You start each profile's gateway independently: `agents exec coder hermes -p reviewer gateway start`.
- Each profile needs its own Slack/Mattermost/Discord bot account (or bot token within an account that supports multiple). You can't have profile A and profile B both reply as the same bot on the same workspace.
- At that point ask yourself: if I'm paying for a second bot user anyway, is a profile actually cheaper than a second toolkit agent? Often the answer is no — separate toolkit agents give you separate upgrade cadence, separate logs, separate resource limits, and isolation between domains, at the cost of a bit more disk.
- Profiles win when the second persona is **not gateway-exposed at all** — purely interactive via `hermes -p <profile> chat` or via the dashboard. Those are the use cases in §3.

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
After the first `hermes setup`, persistent auth is at `<data-dir>/coder/.data/.hermes/auth.json` *inside* the agent's data dir. The toolkit-scoped `<data-dir>/.codex/` is only a one-time import bridge — bind-mounted read-only at `/home/hermes/.codex` in the container so Hermes can pick it up.

**Q: My agent needs an inbound webhook for Slack/Discord/Mattermost.**
Prefer the platform's outbound mode (Slack Socket Mode, Discord gateway, Mattermost websocket) — no inbound port required. If you truly need a webhook, terminate it on the host with a tunnel (Cloudflare Tunnel, tailscale-funnel) that forwards to `127.0.0.1:<vm-forwarded-port>`. Avoid binding `0.0.0.0` directly on the VM.

**Q: I broke `SOUL.md` and want the default back.**
`rm <data-dir>/coder/.data/SOUL.md` — Hermes auto-creates a fresh one on next start. Existing files are never overwritten.

**Q: How do I migrate from a stand-alone Hermes install into a toolkit agent?**
Inside the agent: `hermes import <path-to-backup.zip>`. Put the backup somewhere visible to the container — e.g. drop it in `<data-dir>/coder/.data/` on the host, then `agents exec coder hermes import /opt/data/backup.zip`.
