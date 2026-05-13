# Tutorial — agents end-to-end

This walks the full lifecycle for one Hermes agent and one OpenClaw agent: create → personality → skills → messaging-platform connection → daily use → upgrade → tear-down. If you just want the cheat sheet, see [runbook.md](runbook.md).

## 1. The model

One toolkit-managed *agent* = one Docker container = one Hermes (or OpenClaw) installation with its own `$HERMES_HOME` bind-mounted at `<data-dir>/<name>/.data/`. The host CLI never talks to the in-VM Docker daemon directly — it shells out to `multipass`.

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

## 3. Profiles: pick a strategy before you create

You have two ways to run multiple "personas":

**Strategy A — one toolkit agent per persona (recommended for most users).**
```bash
agents create coder    --type hermes --setup
agents create reviewer --type hermes --setup
agents create writer   --type hermes --setup
```
Each lives in its own container with its own port, logs, auth, and skill set. Upgrades, backups, and removal are per-agent. This is what the toolkit is built around.

**Strategy B — one toolkit agent, multiple Hermes profiles inside it.**
```bash
agents create work --type hermes --setup
agents exec work hermes profile create reviewer --clone default
agents exec work hermes -p reviewer chat
```
Same LLM credentials, same skill base image, smaller footprint. Use this when the personas only differ in `SOUL.md` and a few skills, and you don't need separate ports.

Mixing is fine, but Strategy A is what the rest of this tutorial assumes.

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

This takes both a multipass VM snapshot and a host-side `.tar.zst` of the agent's state before touching anything, then pulls, recreates, and runs `agents doctor`. If doctor fails, the toolkit auto-rolls-back. To roll back manually later:

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

## 6. Multi-profile inside one Hermes container (Strategy B)

If you decided Strategy B is right for your case:

```bash
# create a Hermes profile that clones the default's auth + skills, then diverges
agents exec coder hermes profile create reviewer --clone default

# use it
agents exec coder hermes -p reviewer chat
agents exec coder hermes -p reviewer skills install official/codereview/standards
agents exec coder hermes -p reviewer setup --section agent      # different SOUL.md

# day-to-day
agents exec coder hermes profile list
agents exec coder hermes profile rename reviewer pr-reviewer
agents exec coder hermes profile export reviewer -o /opt/data/reviewer.zip
agents exec coder hermes profile delete reviewer
```

Profile data lives at `<data-dir>/coder/.data/profiles/<profile-name>/` on the host, so `agents export coder` and your host backup cover all profiles inside that container.

## 7. Common questions

**Q: I want a different LLM provider per persona — Strategy A or B?**
Strategy A. The provider is per-`$HERMES_HOME` and lives in `config.yaml`. Using two profiles inside one container would technically work, but you'd be sharing the same image and skill base layer, which is rarely what you want when providers differ.

**Q: Where does Codex auth live for a Hermes agent that uses Codex as its LLM?**
After the first `hermes setup`, persistent auth is at `<data-dir>/coder/.data/.hermes/auth.json` *inside* the agent's data dir. The toolkit-scoped `<data-dir>/.codex/` is only a one-time import bridge — bind-mounted read-only at `/home/hermes/.codex` in the container so Hermes can pick it up.

**Q: My agent needs an inbound webhook for Slack/Discord/Mattermost.**
Prefer the platform's outbound mode (Slack Socket Mode, Discord gateway, Mattermost websocket) — no inbound port required. If you truly need a webhook, terminate it on the host with a tunnel (Cloudflare Tunnel, tailscale-funnel) that forwards to `127.0.0.1:<vm-forwarded-port>`. Avoid binding `0.0.0.0` directly on the VM.

**Q: I broke `SOUL.md` and want the default back.**
`rm <data-dir>/coder/.data/SOUL.md` — Hermes auto-creates a fresh one on next start. Existing files are never overwritten.

**Q: How do I migrate from a stand-alone Hermes install into a toolkit agent?**
Inside the agent: `hermes import <path-to-backup.zip>`. Put the backup somewhere visible to the container — e.g. drop it in `<data-dir>/coder/.data/` on the host, then `agents exec coder hermes import /opt/data/backup.zip`.
