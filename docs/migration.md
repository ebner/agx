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
| LLM provider (Codex auth) | Yes | `openclaw models auth login --provider openai` (or `hermes auth add openai-codex` going the other way) |
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
agents export coder              # → <data-dir>/backups/coder-<utc>.tar.zst
```

Retention caveat: the default `backups/` dir is a rolling window —
exports prune each agent's archives down to the newest
`[export_cron].keep_last` (default 14). If `export-cron` keeps running
during a multi-day soak, the step-0 archive can age out before cutover
finishes. Export with `--to <dir>` (custom destinations are never
pruned) or copy the archive somewhere durable.

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
agents remove coder --export-first   # bags a final archive, confirms, removes
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

---

# Migrating the data dir into the VM (`agents vm migrate-data`)

A different kind of migration: same agents, same runtime, but the agent
state moves from the host-mounted data dir onto the VM's own disk —
converting an existing deployment to what `agents vm init
--data-dir-in-vm` would have set up from scratch.

## When to use it

The host-mount layout has two structural problems that `agents fix-perms`
can't permanently fix:

- **Subuid-owned state on the host.** The agent's files land on the host
  as a rootless-Docker subuid under mode 700. The toolkit itself no
  longer needs to read them from the host (it works through the VM), and
  `agents access mount` / `agents edit` cover human access — but it stays
  a second moving part that `--data-dir-in-vm` simply doesn't have.
- **The 9p/virtiofs root asymmetry.** Container-root cannot write to
  the mounted share (container uid 0 maps to an unprivileged VM user,
  and 9p enforces DAC at the VM level) — the class of bug behind
  "agent silently stops replying" (`s6-log: fatal: unable to mkdir`).

On top of that, SQLite WAL doesn't work over the 9p share, which is why
`--data-dir-in-vm` is already the recommendation for Hermes'
SQLite-heavy stores (kanban/sessions/insights). If you're hitting any of
these on an existing deployment, run:

```bash
agents vm migrate-data          # add --yes for non-interactive
```

## What it does

Phased and journaled (op `migrate-data`) — a crashed or failed run
resumes from the last completed phase by re-running the command:

1. Preflight: refuses on bare_metal (no VM), when `data_dir_in_vm` is
   already enabled, when the VM isn't running, and when the VM disk
   lacks headroom for the copy (data size + 20% margin, with the
   numbers printed — grow via `agents vm resize --disk N` first).
2. Safety exports: one archive per agent into `<data-dir>/backups/` —
   your restore path (`agents import <name> <archive>`), and the last
   host-visible state your backup tool picks up before the move.
3. Confirms the plan (downtime: every agent and sidecar stops).
4. Stops all services, then copies the entire data dir VM-side onto the
   VM disk (a root `tar | tar` pipeline — compose file, `.env`, agent
   dirs, firecrawl postgres, model weights, everything), so sidecars
   survive with zero special-casing. Exclusions apply *before
   traversal* (`tar --exclude`), never as a post-copy prune: host-only
   entries stay host-side (`backups/`, `.upgrades/`, `.journal/`,
   `.staging/`, `.rebuild-staging/`, `.agents.lock`), and on the
   libvirt backend the VM's own state under the data dir (`.disks/` —
   the running VM's qcow2! — plus `.images/`, `.seeds/`, `.ssh/`) is
   never copied into itself. The disk-headroom preflight uses the same
   exclusion list, so the qcow2 doesn't count against the estimate.
5. Detaches the host mount and swaps the copy into
   `<vm_agents_root>` — the same path `vm init --data-dir-in-vm` uses,
   so absolute paths recorded elsewhere (e.g. the Codex-skill sandbox
   volume `<vm_agents_root>/.codex`) stay valid.
6. Flips `[paths] data_dir_in_vm = true` in `config.toml`, restarts all
   services, and requires a green `agents doctor` (run render-suppressed
   — its per-check summary is folded into the command's single JSON
   payload). A red doctor exits 3, keeps the journal, and prints the
   exact flip-back steps.

If you roll back per those printed steps and later re-run the command,
the leftover journal is detected as stale (its completed phases
contradict the observed config/mount state), discarded with a warning,
and the migration starts over from scratch — safe, because the host
source is untouched and every phase is idempotent.

## The verify-before-delete contract

**The command never deletes, overwrites, or moves anything under the
host data dir.** It only *reads* it (through the VM mount) and adds
metadata (export archives, the journal). The success summary lists the
host paths that are now superseded by the VM copy — each agent's
bulk-state subdirs (`<agent>/.data` for Hermes, `<agent>/.openclaw` +
`<agent>/workspace` for OpenClaw), `docker-compose.yml`, `firecrawl/`,
`models/`. Archive or delete them **manually**, and only after the
migrated deployment has run cleanly for a few days. Until then they are
your instant rollback:

```toml
# ~/.config/agents/config.toml
[paths]
data_dir_in_vm = false
```

Rollback order matters — the container lifecycle must sandwich the
remount:

```bash
agents stop <name>                  # 1. each agent — nothing may keep
                                    #    writing the VM-local copy
# 2. edit config.toml as above
agents vm start                     # 3. re-adds + verifies the host mount
agents upgrade <name> --recreate    # 4. each agent — force-recreate so the
                                    #    bind mounts point at host data again
```

A plain `agents start` is **not** enough for step 4: on libvirt the
remount is live (no VM reboot), and an existing container's bind mounts
were materialized against the pre-rollback filesystem — it can keep
writing the VM-local copy hidden beneath the reattached share.

Keep (still live on the host after migration, never listed as
deletable): `docker-compose.override.yml` — compose only merges the
override while the **host** copy exists, so deleting it silently drops
every `agents mount` volume from future compose calls;
`<agent>/.mounts.json` — `agents mount` reads its manifest host-side in
both modes (which is why agent dirs are listed via their bulk-state
subdirs, never wholesale); `.codex/` (the toolkit-scoped Codex auth
home — `agents codex login` writes there and syncs into the VM),
`.env`, `.firecrawl-state.json`, `.memory-state/`, `backups/`,
`.upgrades/`, `.journal/`, `.agents.lock`.

## Afterwards

- Live agent state is **no longer host-visible**, so your host backup
  (duplicity) can't see it directly anymore. Install the export cron —
  the archives it drops in `<data-dir>/backups/` are what duplicity
  picks up:

  ```bash
  agents export-cron install --time 02:30,08:30,14:30,20:30
  ```

- `agents vm snapshot` now covers agent state too (the VM disk holds
  it), and on libvirt snapshots no longer force a VM stop (the
  virtiofs limitation only applied to host-mount mode).
- If doctor flags `/opt/data` access for an agent after the migration,
  `agents fix-perms <name>` realigns ownership — one-time, not the
  host-mount treadmill.
