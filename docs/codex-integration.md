# Codex integration

OpenAI's Codex CLI (`codex`, package `@openai/codex`) authenticates against a ChatGPT/Codex subscription via device-code OAuth and stores tokens at `~/.codex/auth.json` by default. This toolkit wires Codex into Hermes and OpenClaw without commingling those credentials with the user's personal `~/.codex/`.

## Two distinct Hermes paths

### Path 1 — Codex as Hermes' LLM provider (Hermes-owned, NOT the toolkit's `.codex`)

Hermes ships an `openai-codex` provider. It uses Codex OAuth tokens directly, talks to OpenAI's Codex backend, and **does not need the Codex CLI binary anywhere** — and, since 2026-09, it does not use the toolkit-scoped `<data-dir>/.codex` either.

Authenticate **inside the container**, where Hermes owns the credential and its refresh cycle:

```bash
agents exec coder -- hermes auth add openai-codex --type oauth --no-browser
```

Device-code flow: it prints a URL + code; open the URL on **any** browser that is logged into the ChatGPT account (phone included — combined with `agents tailscale up` + an SSH app this is the "re-auth from anywhere" path). The credential lands in the agent's pooled auth store at `/opt/data/auth.json` (persistent: `/opt/data` is `$HERMES_HOME` on the bind-mounted data dir), and `agents exec coder -- hermes auth list openai-codex` shows it.

**Why not the old "import bridge"?** The toolkit used to bind-mount `<data-dir>/.codex` read-only at `/home/hermes/.codex` so `hermes setup` could seed a credential from it. ChatGPT-subscription OAuth uses a **single-use refresh-token chain**: the seeded credential and the host file were two copies of one chain, so any refresh of the host copy (`agents codex refresh`, or the Codex CLI itself) rotated the token under the running agent — *"Your authentication token has been invalidated"* — and the two could never both stay valid. The bridge is gone from the compose fragment; `agents reseat <name>` removes it from agents created before. Hermes also suppresses re-seeding once you `hermes auth remove` a bridge-seeded credential ("Suppressed openai-codex device_code source").

**Expiry is not solved by any of this.** Upstream Hermes has an unresolved class of "Codex OAuth dies every 1–3 weeks although the token is not expired; the internal refresh stalls" ([NousResearch/hermes-agent#65346](https://github.com/NousResearch/hermes-agent/issues/65346)). Mitigations that work:

- `fallback_providers` in the agent's `config.yaml` (Hermes fails over immediately on 401/403) — a cheap OpenRouter model keeps the agent answering until you re-auth at leisure.
- `agents doctor-cron install --mail you@example.org` — polls doctor every 15 min and mails within one interval when `<agent>.credential_pool` turns red (Hermes marks the credential `exhausted`/`dead` the moment a request fails). There is no reliable *pre*-warning: the access token is refreshed continuously and says nothing about when the chain will die.

### Path 2 — Codex CLI as a Hermes skill

Hermes has a bundled `autonomous-ai-agents/codex` skill that shells out to the `codex` CLI from inside Hermes' nested sandbox container. The Hermes sandbox is one long-lived container per Hermes process, image set by `terminal.docker_image`. For the skill to work, the sandbox image must contain the `codex` binary and the sandbox must have access to the OAuth tokens.

The toolkit handles both via `agents codex skill-enable <hermes-agent>`:

1. Reads the agent's current `terminal.docker_image` from `<data-dir>/<name>/.data/config.yaml` (defaulting to the Hermes default sandbox image).
2. Renders a small Dockerfile (in `templates.py`):
   ```dockerfile
   FROM {base_image}
   RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm curl ca-certificates \
    && npm install -g @openai/codex \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
   ```
3. Builds the derived image inside the VM, tagged `agents/hermes-sandbox-codex:<sha256(base)[:12]>`. The hash slug is required because bases like `nikolaik/python-nodejs:python3.11-nodejs20` contain `/` and `:`, which aren't valid Docker tag characters.
4. Updates the agent's `terminal.docker_image` to the derived image.
5. Adds a sandbox volume entry to `terminal.docker_volumes`:
   ```
   /home/ubuntu/agents/.codex:/codex:ro
   ```
   The mount target is the fixed path `/codex` because the derived Dockerfile sets `ENV CODEX_HOME=/codex`. That makes the integration work for sandbox base images that run as root, `pn`, `node`, or anything else — Codex CLI honors `CODEX_HOME` everywhere.

   **Critical detail:** the host side of the mount (`/home/ubuntu/agents/.codex`) is a VM-side host path, interpreted by the **VM Docker daemon**. NOT a path inside the Hermes runtime container.
6. Persists the original image at `<data-dir>/.codex-skill/<name>.json` so `skill-disable` can reverse the change.
7. Restarts the agent's service.

To reverse: `agents codex skill-disable <hermes-agent>`. The original `docker_image` is restored, the volume entry removed, the state file deleted, and the container restarted.

## OpenClaw

OpenClaw bundles its own Codex plugin. The toolkit does **not** mount Codex into OpenClaw — that would conflict with OpenClaw's plugin management. Run the OpenClaw-native flow inside the container:

```bash
agents exec personal -- openclaw models auth login --provider openai-codex --device-code
```

## `CODEX_HOME` lifecycle

The Codex CLI honors a `CODEX_HOME` env var to relocate its auth directory. The toolkit sets `CODEX_HOME=<data-dir>/.codex` for every `agents codex …` invocation. This keeps the toolkit account isolated from the user's personal `~/.codex/`.

**Caveat:** if a future Codex CLI release stops honoring `CODEX_HOME` for the refresh path (token rotations writing back to `~/.codex/` regardless), the toolkit will silently leak refreshes into the user's personal auth. There's an integration test under `pytest -m multipass` that exercises the full lifecycle (login → wait past TTL → re-invoke). If it ever fails, the fallback is to launch `codex` with `HOME=<data-dir>` instead, isolating the entire HOME for that subprocess. Switch is one-line in `codex.py`.

## Token expiry (sandbox skill auth)

`agents codex status` reports, for the toolkit-scoped `<data-dir>/.codex/auth.json` that only the sandbox skill uses:

- Whether the auth file exists
- The expiry timestamp (parsed best-effort from several common OAuth JSON shapes)
- Days remaining
- A `⚠ less than 7 days` warning when re-login is needed soon
- Per-Hermes-agent skill-enable state and current sandbox image

Re-login with `agents codex login` when it expires. **`agents codex refresh` is retired** (hidden stub, exit 1): a silent refresh rotates the single-use refresh chain, which is exactly what invalidated agents' provider credentials under the old import bridge. The sandbox mounts the file `:ro`, so the Codex CLI inside the sandbox cannot persist its own refreshes either — for heavy skill use, expect to `agents codex login` periodically.

## Cheat sheet

| Goal | Command |
|---|---|
| Authenticate a Hermes agent's `openai-codex` **LLM provider** | `agents exec <name> -- hermes auth add openai-codex --type oauth --no-browser` |
| See / prune an agent's pooled credentials | `agents exec <name> -- hermes auth list openai-codex` / `hermes auth remove openai-codex <n>` |
| Authenticate the toolkit account (sandbox **skill** only) | `agents codex login` |
| Inspect skill-auth state | `agents codex status` |
| Enable Codex skill on a Hermes agent | `agents codex skill-enable coder` |
| Disable Codex skill | `agents codex skill-disable coder` |
| Authenticate OpenClaw's bundled Codex | `agents exec personal -- openclaw models auth login --provider openai-codex --device-code` |
