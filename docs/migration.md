# Migrating an agent between Hermes and OpenClaw

The two agent runtimes are intentionally different (different LLM-glue
architectures, different skill/plugin systems, different session
schemas), so there is **no seamless one-button migration**. Most of an
agent's state isn't actually portable. This document is the runbook for
the realistic path: **side-by-side cutover with archival**.

> If you're looking for in-place version upgrades (Hermes → newer
> Hermes, OpenClaw → newer OpenClaw), use `agents upgrade <name>
> --latest` and `agents outdated` instead — those are seamless.

## What carries over and what doesn't

| Field | Portable? | How |
|---|---|---|
| Personality / `SOUL.md` | Yes (plain text) | Hand-port into the new agent's system-prompt slot |
| LLM provider (Codex auth) | Yes | `openclaw models auth login --provider openai-codex` (or `hermes auth add openai-codex` going the other way) |
| Other LLM API keys (OpenRouter, etc.) | Yes (bare strings) | Re-enter in the destination agent's wizard |
| Conversation history | **Lossy** — text only | `agents conversations export <name> --to <dir>` produces Markdown transcripts |
| Bot tokens (Slack, Mattermost, email) | Yes if same bot | The tokens themselves are bare strings; bot apps don't care which client connects |
| Channel routing | Partial | Channel IDs are stable; map structures differ between runtimes |
| WhatsApp pairing | **No** | Bridge state is runtime-specific; re-pair on the destination side |
| Cron jobs | **No** | Hermes-specific scheduler; redefine in OpenClaw's job system |
| Skills / plugins | **No** | Conceptually different abstractions; reinstall the closest equivalents |
| Memory (holographic / mem0 / openviking) | **No** | Backend-specific binary state; OpenClaw has its own memory architecture |
| Kanban data | **No** | Hermes-specific feature |
| Sessions, plans, sandboxes, workspace | Archival only | Tarball via `agents export <name>`; not re-importable |

## The runbook (Hermes → OpenClaw)

The same steps run in reverse for OpenClaw → Hermes — substitute the
respective CLI invocations.

### 0. Pre-flight

```bash
# Confirm both runtimes are reachable + current
agents outdated
agents doctor

# Capture the current Hermes data dir for posterity
agents export coder              # → <data-dir>/backups/coder-<utc>.tar
```

### 1. Archive transcripts for context-seeding

```bash
# One Markdown file per session, suitable for grep/diff/hand-seeding
agents conversations export coder --to ~/migrations/coder-transcripts/
```

The transcripts give you a searchable history of what the agent was
working on, which platforms it spoke to, and what its tone was. Keep
this directory checked into version control if you care about audit.

### 2. Stand up the new agent side-by-side

Pick a new name so the two run in parallel during cutover:

```bash
agents create coder2 --type openclaw
agents setup coder2                  # runs `openclaw onboard`
```

During `openclaw onboard`:

- Pick the same LLM provider as the source (most likely
  `openai-codex` for ChatGPT-subscription users).
- Enable the same external services. If you're running self-hosted
  Firecrawl, the env wiring is already in place (`FIRECRAWL_API_KEY` +
  `FIRECRAWL_BASE_URL` interpolated from project `.env`) — just flip
  `plugins.entries.firecrawl.enabled: true` in the wizard.
- Skip skill / plugin installation for now; you'll pick the new
  agent's equivalents in step 5.

### 3. Port the agent's identity

```bash
# Personality
docker compose exec hermes-coder cat /opt/data/SOUL.md \
  | agents exec --root coder2 tee /home/node/.openclaw/PERSONA.md

# (Or copy via the export archive; the path inside OpenClaw will
# depend on its current config schema.)
```

Then open `agents setup coder2` again and paste the personality into
OpenClaw's system-prompt UI. OpenClaw doesn't have a one-file `SOUL.md`
analogue; it ships its own personality store via `openclaw configure`.

### 4. Move the platform credentials

The bot apps themselves (the Slack app registration, the Mattermost
bot user, the inbound mail address) don't care which client connects —
the tokens are reusable across runtimes.

```bash
# Hermes side: read the tokens you need to carry
agents exec --root coder sh -c 'ls /opt/data/platforms/'
agents exec --root coder sh -c 'cat /opt/data/platforms/slack.yaml'   # example

# OpenClaw side: enter them in `openclaw configure` under the
# matching platform plugin.
agents exec coder2 -- openclaw configure
```

**WhatsApp is the exception**: the bridge state (session keys, paired
device) is Hermes-specific. Re-pair via OpenClaw's WhatsApp plugin.
You'll briefly see the QR code prompt again.

### 5. Reinstall skills as plugins (best-effort mapping)

Skills are not portable. List what Hermes has installed:

```bash
agents exec coder -- hermes skills list
```

For each one, look for an OpenClaw plugin with similar capabilities
(`openclaw plugins search <topic>`). Some categories have direct
analogues (web search, calendar, time); others require accepting that
the new agent simply won't have that capability or that you'll write a
plugin.

### 6. Cron jobs

```bash
agents exec coder -- hermes cron list
```

Re-create each job through OpenClaw's job system. The cron expressions
themselves transfer literally; the action body (which tool to call,
which prompt to issue) usually needs a rewrite for OpenClaw's plugin
surface.

### 7. Memory backends

Hermes' memory backends (holographic / mem0 / openviking) have no
OpenClaw equivalents in the toolkit. If you depended on long-term
memory:

- Export memory dumps from Hermes if the backend supports it (e.g.
  `agents memory disable coder` cleanly tears the sidecar down and
  the data dir at `<data-dir>/coder/.openviking/` is preserved).
- Manually seed the OpenClaw agent with a brief summary as a
  system-prompt addition or first message.

### 8. Parallel-test for a few days

Keep both agents running. Compare their behavior on the same prompts.
This is also when you'll notice anything you forgot to carry over
(forgotten skills, channel-routing differences, personality drift).

### 9. Cut over and decommission

When you're confident:

```bash
# Stop the old agent first so it can't catch new messages
agents stop coder

# Re-point platform integrations (bot user → coder2's webhook /
# socket-mode endpoint). For Slack Socket Mode this is automatic
# once coder2's app token is the only active subscriber.

# After a week of soak, remove the old agent's container + data dir
agents remove coder              # confirms; offers --export-first
```

The export archive from step 0 and the transcripts from step 1 are
your durable record of everything the old agent ever knew.

## What this toolkit does NOT do

- **No automated identity-export manifest.** A `agents identity export`
  command would automate steps 1-3, but the value-per-LOC is low
  because so little is automatable — most of the carry-over work
  (picking equivalent plugins, re-pairing WhatsApp, deciding which
  skills to drop) is judgment, not mechanical translation.
- **No two-way sync** between Hermes and OpenClaw agents.
- **No state-file translation** between session schemas. The
  transcript export is one-way; you can't re-import transcripts as
  live session state.

If your migration is mostly mechanical and you find yourself doing the
same steps repeatedly, raise an issue describing the actual pattern —
that's the time to invest in the manifest format.
