# Threat model

## What we're defending against

2026 has been rough for this class of software:

- Multiple skill-marketplace malware incidents (~12% malicious-skill rate documented across two large agent ecosystems).
- A wave of CVEs in agent runtimes that execute LLM-generated shell commands.
- Supply-chain attacks via typosquatted image registries.

The toolkit assumes:

- Every agent **will** at some point execute a malicious command (skill, prompt-injection, etc).
- The host is fully trusted.
- The user is the only legitimate principal.
- Network egress from the agent is needed and not blocked.

## Trust boundaries (outer to inner)

| Boundary | What's outside | What's inside |
|---|---|---|
| **Host process** | Anything else on the user's host (browser, email, IDE). | The host CLI itself. |
| **Multipass VM** | The host. | A whole Ubuntu instance, isolated by KVM/qemu. |
| **Rootless Docker** | VM root. | The `ubuntu` user's daemon. A compromised container cannot escalate to VM root. |
| **Per-agent container** | The rootless daemon. | A single agent runtime. |
| **Nested sandbox** | The Hermes runtime. | One ephemeral container spawned per Hermes session for tool calls. |

## Known exposure surfaces

### 1. Rootless Docker socket mounted into Hermes

For Hermes' nested-sandbox feature to work, the rootless Docker socket (`/run/user/<uid>/docker.sock`) is bind-mounted into the Hermes container and `DOCKER_HOST` is set. A compromised Hermes can spawn arbitrary containers as the `ubuntu` user.

**Bounded by:**

- Rootless Docker. No path to root on host.
- VM is the outer trust boundary; container break-out ends inside the VM.
- A user who doesn't want nested sandboxing can set `terminal.backend: local` in Hermes config. The toolkit's `hermes_config` round-trip preserves that choice across upgrades.

### 2. Host data-dir mounted into VM

`<data-dir>` is mounted at `/home/ubuntu/agents`. A compromised VM (or container that escapes its container, but stays inside the VM) can read/write everything there.

**Mitigations:**

- Pick a `<data-dir>` on a filesystem you're comfortable trusting agents with. Don't point it at your homedir if you keep non-agent secrets there.
- The toolkit refuses `<data-dir>` paths under `/home/*/snap` or `/var/snap` — those subtrees have unusual permission and mount semantics that backends struggle with, and they're exactly where unrelated secrets often live.

### 3. Codex auth mount

`<data-dir>/.codex/auth.json` is mounted read-only into the Hermes container at `/home/hermes/.codex` (for setup-time import) and, when `agents codex skill-enable` is invoked, into the Hermes nested sandbox at `/root/.codex:ro`.

**Mitigations:**

- The toolkit-scoped Codex auth is **separate** from the user's personal `~/.codex/`. Use a dedicated Codex account for the agents; compromise of that account doesn't leak the user's personal Codex.
- The auth is bind-mounted, never baked into a Docker image, so a token rotation propagates immediately.
- `agents codex status` warns when the OAuth token expires within 7 days. Token expiry is a routine event, not a security event, but stale tokens block work and should be refreshed.

### 4. Image source

The CLI refuses any image whose `(registry, repo)` doesn't match `nousresearch/hermes-agent` or `openclaw/openclaw`. Registry-prefixed lookalikes (e.g. `ghcr.io/evil/nousresearch/hermes-agent`) are rejected by the parsed-tuple match. `hermes-agent.org` is rejected anywhere.

### 5. Dashboard binding

Hermes' web dashboard always binds `127.0.0.1` only. The CLI refuses any `dashboard.address` change to `0.0.0.0` (or anything else than `127.0.0.1` / `localhost` / `::1`). Remote access goes through SSH port-forwarding.

### 6. Compose-file mutation

The compose file is the CLI's, not the user's. Any external write would be overwritten on the next `agents create`/`remove`/`upgrade`. Users with legitimate customizations use `docker-compose.override.yml`, which the CLI leaves untouched and which `docker compose` merges automatically.

The atomic-write contract (flock + stage in project dir + `docker compose config -q` validate + same-filesystem `os.replace`) ensures partial writes never appear in the live file even on a host crash mid-mutation. `cleanup_stale_temps` sweeps temp files left by prior crashes on startup.

### 7. Phase journals

Every multi-phase op (`create`, `remove`, `rename`, `upgrade`) writes a journal so a crash mid-op can resume cleanly. The journal contains only operation metadata (op name, agent name, completed phases) — no secrets. `doctor` warns when a journal is older than 24 hours so dangling state is visible.

## Out of scope

- Sophisticated lateral movement once a container is compromised. The VM is the boundary; a VM compromise is a VM compromise.
- Network-level egress filtering. The user's host firewall is the right layer.
- Skill-marketplace curation or static scanning. Skills land in `<data-dir>/<name>/.data/skills/` and are subject to the trust model above.
- Off-site backup encryption. The user's host backup (duplicity, restic, borg, …) owns that.
