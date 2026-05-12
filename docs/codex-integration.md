# Codex integration

OpenAI's Codex CLI (`codex`, package `@openai/codex`) authenticates against a ChatGPT/Codex subscription via device-code OAuth and stores tokens at `~/.codex/auth.json` by default. This toolkit wires Codex into Hermes and OpenClaw without commingling those credentials with the user's personal `~/.codex/`.

## Two distinct Hermes paths

### Path 1 — Codex as Hermes' LLM provider

Hermes ships an `openai-codex` provider. It uses Codex OAuth tokens directly, talks to OpenAI's Codex backend, and **does not need the Codex CLI binary anywhere**.

Wiring:

- Toolkit-scoped auth: `<data-dir>/.codex/auth.json` (populated by `agents codex login`).
- That dir is bind-mounted **read-only** into the Hermes runtime container at `/home/hermes/.codex` — exactly where `hermes setup`'s import looks.
- During `hermes setup` the user picks `openai-codex` as the model provider. Hermes imports the OAuth tokens into its own auth store at `/opt/data/.hermes/auth.json`. From then on, Hermes uses its own copy and the `/home/hermes/.codex` mount is just a fallback.

Files involved:
- `<data-dir>/.codex/auth.json` — toolkit-scoped Codex OAuth.
- `<data-dir>/<name>/.data/.hermes/auth.json` — Hermes-imported copy (persistent across container recreates because `/opt/data` is `$HERMES_HOME`).

No `agents codex skill-enable` needed for this path.

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
   /home/ubuntu/agents/.codex:/root/.codex:ro
   ```
   **Critical detail:** these paths are interpreted by the **VM Docker daemon**, not by paths inside the Hermes runtime container. So the host side is the VM-side mount path (`/home/ubuntu/agents/.codex`), not anything from inside the Hermes container.
6. Persists the original image at `<data-dir>/<name>/.data/.codex-skill.json` so `skill-disable` can reverse the change.
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

## Token expiry

Codex OAuth tokens typically expire after 30 days. `agents codex status` reports:

- Whether the auth file exists
- The expiry timestamp (parsed best-effort from several common OAuth JSON shapes)
- Days remaining
- A `⚠ less than 7 days` warning when refresh is needed soon
- Per-Hermes-agent skill-enable state and current sandbox image

`agents codex refresh` attempts a silent token refresh by invoking `codex auth status`. If interactive re-auth is needed (refresh token rotated out, etc.), it exits with code 1 and a hint to run `agents codex login`.

## Cheat sheet

| Goal | Command |
|---|---|
| Authenticate the toolkit account | `agents codex login` |
| Refresh quietly | `agents codex refresh` |
| Inspect state | `agents codex status` |
| Enable Codex skill on a Hermes agent | `agents codex skill-enable coder` |
| Disable Codex skill | `agents codex skill-disable coder` |
| Authenticate OpenClaw's bundled Codex | `agents exec personal -- openclaw models auth login --provider openai-codex --device-code` |
