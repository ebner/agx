# Threat model

## What we're defending against

2026 has been rough for this class of software:

- Multiple reported waves of malicious skills in agent skill marketplaces.
- A wave of CVEs in agent runtimes that execute LLM-generated shell commands.
- Supply-chain attacks via typosquatted image registries.

The toolkit assumes:

- Every agent **will** at some point execute a malicious command (skill, prompt-injection, etc).
- The host is fully trusted.
- The user is the only legitimate principal.
- Network egress from the agent is needed and not blocked.

## The outer boundary depends on the backend

`agx vm init --backend …` picks how much isolation sits between a
compromised agent and the host. The inner layers (rootless Docker →
per-agent container → nested sandbox) are identical across backends;
only the outer boundary changes:

| Backend | Outer trust boundary | Notes |
|---|---|---|
| `multipass` (default) | **The VM.** A whole Ubuntu instance isolated by the platform hypervisor (KVM/qemu on Linux; Multipass's native driver on macOS). | Cross-platform default. Container break-out ends inside the VM. |
| `libvirt` | **The VM.** Same guarantee as multipass — Ubuntu guest under KVM/qemu. | Linux-only, experimental. Exists for GPU passthrough (vfio-pci hostdev attach). |
| `bare_metal` | **The host itself — there is no VM.** A compromised agent that escapes its container reaches the host directly. | Linux-only, opt-in. `vm init` warns explicitly: *"the VM trust boundary is forfeited … Only use this on single-user dev workstations where the host is already trusted."* Rootless Docker (never rootful — the toolkit refuses a rootful socket) is the only remaining isolation layer. |

The defense-in-depth rationale in the rest of this document leans on
the VM to absorb skill-marketplace malware and runtime CVEs; bare-metal
forfeits that deliberately. Choose it only where the host would be in
the blast radius anyway (single-user dev workstation, dedicated LLM
box).

## Trust boundaries (outer to inner, VM backends)

| Boundary | What's outside | What's inside |
|---|---|---|
| **Host process** | Anything else on the user's host (browser, email, IDE). | The host CLI itself. |
| **VM (multipass or libvirt)** | The host. | A whole Ubuntu instance, isolated by KVM/qemu (or the platform hypervisor). Absent on `bare_metal`. |
| **Rootless Docker** | VM root (host root on `bare_metal`). | The unprivileged user's daemon. A compromised container cannot escalate to root. |
| **Per-agent container** | The rootless daemon. | A single agent runtime. |
| **Nested sandbox** | The Hermes runtime. | One ephemeral container spawned per Hermes session for tool calls. |

## Known exposure surfaces

### 1. Rootless Docker socket mounted into Hermes

For Hermes' nested-sandbox feature to work, the rootless Docker socket (`/run/user/<uid>/docker.sock`) is bind-mounted into the Hermes container and `DOCKER_HOST` is set. A compromised Hermes can spawn arbitrary containers as the `ubuntu` user.

**Bounded by:**

- Rootless Docker. No path to root on host.
- On the VM backends, the VM is the outer trust boundary; container break-out ends inside the VM. On `bare_metal`, rootless Docker is the last line — see the backend table above.
- A user who doesn't want nested sandboxing can set `terminal.backend: local` in Hermes config. The toolkit's `hermes_config` round-trip preserves that choice across upgrades.

### 2. Host data-dir mounted into VM

`<data-dir>` is mounted at `/home/ubuntu/agents`. A compromised VM (or container that escapes its container, but stays inside the VM) can read/write everything there.

**Mitigations:**

- Pick a `<data-dir>` on a filesystem you're comfortable trusting agents with. Don't point it at your homedir if you keep non-agent secrets there.
- The toolkit refuses `<data-dir>` paths under `/home/*/snap` or `/var/snap` (multipass-snap can't read those, but also: that's exactly where unrelated secrets often live).

### 3. Codex auth mount

`<data-dir>/.codex/auth.json` is mounted, only when `agx codex skill-enable` is invoked, into the Hermes nested sandbox at `/codex:ro`. It is **not** mounted into the Hermes runtime container any more (the `/home/hermes/.codex` "import bridge" was retired 2026-09); a Hermes agent's `openai-codex` LLM-provider credential is created in-container via `hermes auth add` and lives in the agent's own `/opt/data/auth.json`, so a compromised agent holds only its own chain. The derived sandbox image bakes in `ENV CODEX_HOME=/codex`, so the Codex CLI reads auth from there regardless of which user the sandbox image runs as (root, `pn`, `node`, …) — see [codex-integration.md](codex-integration.md).

**Mitigations:**

- The toolkit-scoped Codex auth is **separate** from the user's personal `~/.codex/`. Use a dedicated Codex account for the agents; compromise of that account doesn't leak the user's personal Codex.
- The auth is bind-mounted, never baked into a Docker image, so a token rotation propagates immediately.
- `agx codex status` warns when the skill's OAuth token expires within 7 days; `agx doctor`'s `credential_pool` check (and `agx doctor-cron`'s mail) flags an agent whose provider credential died. Token expiry is a routine event, not a security event, but stale tokens block work and should be re-authenticated.

### 4. Image source

The CLI refuses any image whose `(registry, repo)` isn't on the explicit allowlist in `compose.py`: the agent runtimes (`nousresearch/hermes-agent`, `openclaw/openclaw`), the OpenViking memory sidecar (`volcengine/openviking`), the self-hosted Firecrawl stack (`ghcr.io/firecrawl/firecrawl`, `ghcr.io/firecrawl/playwright-service`, `ghcr.io/firecrawl/nuq-postgres`, plus `redis` and `rabbitmq`), and the in-VM inference engines (`ollama/ollama`, `ghcr.io/ggml-org/llama.cpp`). Registry-prefixed lookalikes (e.g. `ghcr.io/evil/nousresearch/hermes-agent`) are rejected by the parsed-tuple match; `repo:tag` and `repo@sha256:…` digest-pin forms are both validated. `hermes-agent.org` is rejected anywhere. New sibling services require explicit allowlist additions.

### 5. Dashboard binding

Hermes' web dashboard always binds `127.0.0.1` only. The CLI refuses any `dashboard.address` change to `0.0.0.0` (or anything else than `127.0.0.1` / `localhost` / `::1`). Remote access goes through SSH port-forwarding or the VM's Tailscale node (`agx tailscale up`).

### 6. Compose-file mutation

The compose file is the CLI's, not the user's. Any external write would be overwritten on the next `agx create`/`remove`/`upgrade`. Users with legitimate customizations use `docker-compose.override.yml`, which the CLI only ever appends to (via `agx mount`), never rewrites, and which `docker compose` merges automatically.

The atomic-write contract (flock + stage in project dir + `docker compose config -q` validate + same-filesystem `os.replace`) ensures partial writes never appear in the live file even on a host crash mid-mutation. `cleanup_stale_temps` sweeps temp files left by prior crashes on startup.

### 7. Phase journals

Every multi-phase op (`create`, `remove`, `rename`, `upgrade`) writes a journal so a crash mid-op can resume cleanly. The journal contains only operation metadata (op name, agent name, completed phases) — no secrets. `doctor` warns when a journal is older than 24 hours so dangling state is visible.

## Out of scope

- Sophisticated lateral movement once a container is compromised. The VM is the boundary; a VM compromise is a VM compromise.
- Network-level egress filtering. The user's host firewall is the right layer.
- Skill-marketplace curation or static scanning. Skills land in `<data-dir>/<name>/.data/skills/` and are subject to the trust model above.
- Off-site backup encryption. The host backup (e.g. duplicity or restic) owns that.
