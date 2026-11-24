# Changelog

All notable user-visible changes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/) (before 1.0, minor versions may break things).

Entries marked **reseat** need `agx reseat <name>` on existing agents to take effect.

## [Unreleased]

## [0.3.0] - 2026-11-24

### Added
- `agx doctor-cron` also mails when upstream publishes a new release for an agent: at most every `[doctor_cron].update_check_hours` (default 24, `0` turns it off; `install --update-check-hours`), once per release, with the releases between the pinned and the latest tag and their condensed release notes.
- Rebuildable directories in the data dir (`.disks/` with the libvirt VM disk, `.images/`, `.seeds/`, `.staging/`, `.rebuild-staging/`, `.upgrades/`, `models/`) get a `CACHEDIR.TAG`, so `borg`/`restic`/`tar --exclude-caches` (duplicity: `--exclude-if-present CACHEDIR.TAG`) skip them. Written at `vm init` and `vm start`.
- `agx doctor` checks Hermes' web UI (`<name>.web_access`): every gateway Webapp or dashboard listener outside loopback must require sign-in (`fail` otherwise); it also warns about a configured listener that doesn't answer, and about the dashboard slot and a Webapp claiming the same port. The runbook describes turning on the Webapp (Hermes v0.23.4+) and moving from the dashboard slot to it.

### Changed
- `agx export` and the pre-upgrade data snapshot leave out package-manager caches (npm, uv, XDG) inside a Hermes agent's data dir; on a long-lived agent that is more than half of the archive. The same caches in the live data dir get a `CACHEDIR.TAG` for host backups. **reseat** (or the next `agx upgrade`)
- `doctor-cron` mails are sectioned (failing, recovered, warnings) and wrapped, and the `hermes_doctor` check lists the issues Hermes itself counts instead of the first warning lines.
- `agx vm init` uses the `libvirt` backend on Linux and `multipass` on macOS when `--backend` is omitted (before: `multipass` everywhere). Existing deployments keep their configured backend. On Linux, `agx vm init` first checks the libvirt stack (commands, `virtiofsd`, `/dev/kvm`, access to `qemu:///system`, the `default` network) and lists everything that is missing.

### Fixed
- `agx model use --context-length` writes the context window to Hermes' `providers:` map instead of the retired `custom_providers` list, which `hermes doctor` flagged; an existing legacy entry for the same endpoint is moved over.
- On Hermes v0.23.4+ images (no PyYAML), the doctor's web UI check reported nothing and `agx conversations` ignored the agent's freshness window; both now read `config.yaml` with ruamel.yaml as well.

### Removed
- The refusal of `dashboard.address` values other than loopback in toolkit config writes and `agx edit`: Hermes takes the dashboard address only from `--host`, so the setting it guarded had no effect. The doctor's `web_access` check replaces it.

## [0.2.0] - 2026-10-13

### Changed
- The CLI is now called `agx` (package `agx-cli`, config in `~/.config/agx/`). Live state keeps its names: the VM, the data dir and the crontab entries still say `agents`.
- `agx create` starts new agents on the newest upstream release instead of the configured default, and skips releases that have no published image.
- Hermes containers use `/opt/data` as `HOME`, so files written by `agx exec` sessions survive container recreates. **reseat**
- `agx rollback` exports the current state to `backups/pre-rollback/` before overwriting it.
- Toolkit writes to an agent's `config.yaml` refuse to overwrite changes the agent made in the meantime.

### Fixed
- OpenClaw 2.0: agents can write their state (the image runs as non-root `node`), image tags without the `v` prefix, and the new `.config` / `.local` state directories are persisted. **reseat**
- libvirt backend: `agx mount add` / `remove` work (virtiofs tag length, share detach).
- Host-side writes are only acted on once the VM sees them, which fixes `mount remove --restart` occasionally keeping the old mount.
- bare-metal backend: `agx mount` binds the host folder, every `docker` call uses the rootless socket, and `agx doctor` checks container-log rotation.
- Export pruning for an agent no longer counts or deletes archives of another agent whose name starts with the same prefix (`foo` vs `foo-bar`).

### Removed
- Retired commands `agx codex refresh` and `agx access grant/revoke` (replaced by `agx access mount`).
- Compatibility code for pre-release installs: the `.tar.gz` archive format, the `/home/hermes` runtime home, the old Codex bridge mount and the leftover-ACL hints.

## [0.1.1] and earlier

Initial development; see the git history up to tag `v0.1.1` (2026-05-12).

[Unreleased]: https://github.com/ebner/agx/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ebner/agx/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/ebner/agx/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/ebner/agx/releases/tag/v0.1.1
