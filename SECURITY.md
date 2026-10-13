# Security Policy

This toolkit exists because running LLM agents means running code you did
not write and cannot fully predict. Its security model is documented in
[docs/threat-model.md](docs/threat-model.md) — read that first; it defines
the trust boundaries per backend (Multipass/libvirt VM boundary,
bare-metal's deliberate forfeiture of it) and what the toolkit does and
does not defend against.

## Reporting a vulnerability

Please use **GitHub's private vulnerability reporting** (the *Security*
tab → *Report a vulnerability*) rather than a public issue. Reports that
demonstrate a crossing of a documented trust boundary — a containerized
agent reaching host data it shouldn't, image-allowlist bypasses,
compose/exec injection through agent-controlled values — are exactly the
kind we want.

Please include: the backend in use (`multipass` / `libvirt` /
`bare_metal`), the toolkit version or commit, and a reproduction path.

You can expect an acknowledgement within a week. Please allow a fix to
land before public disclosure.

## Supported versions

Rolling: the latest state of `master` and the most recent tagged release. Older pins receive no backported fixes — upgrade via
`agx upgrade`, which snapshots before touching anything.

## Out of scope

- Vulnerabilities in the agents themselves (Hermes Agent, OpenClaw) or in
  the images the toolkit deploys — report those upstream. The toolkit's
  job is containment, not fixing the payload.
- Attacks requiring host root or a compromised host user — the host is
  trusted in every backend's model.
- Denial of service by an agent against its own container/VM resources
  (bounded by the compose limits; that's the sandbox working).
